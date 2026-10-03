"""Fail the obvious standard-library leaks. This is not a sandbox.

`datetime.datetime` is a C type, so `now` cannot be assigned on it. The module
attribute is replaced with a subclass. A name bound before `install_guards()`
still points at the original type.

`socket.create_connection` looks up the address before it connects. The guard
fails that lookup. Waiting for `socket.connect` would already have touched the network.

The audit hook carries the filesystem policy. `open` fires for the builtin and
for `os.open`; the mode argument says whether the call writes. `os.stat` raises
no audit event at all, so it is patched directly. `ctypes.dlopen` and
`ctypes.dlsym` do fire, which is what closes the libc clock: without them a
handler could read real nanoseconds, and the run digest would then differ on
every replay while the run still reported `passed`.

Reads and writes are refused unless the path is allowlisted, so a run cannot
quietly depend on the host filesystem. Reads a handler did make are recorded in
the artifact under `fs_reads`. That list is deliberately outside the run digest:
it is provenance for a human, not an input to the run.

Seam's own file I/O runs inside `trusted()`, so writing the artifact is not a
policy violation.
"""

import contextlib
import datetime as _datetime
import os
import random
import secrets
import socket
import sys
import sysconfig
import tempfile
import threading
import time
import uuid

from seam.errors import Fault

_INSTALLED = False
_SOCKET_EVENTS = {
    "socket.connect": "socket.connect",
    "socket.bind": "socket.bind",
    "socket.getaddrinfo": "socket.getaddrinfo",
    "socket.__new__": "socket.socket",
}
_WRITE_EVENTS = {
    "os.mkdir": "os.mkdir",
    "os.rmdir": "os.rmdir",
    "os.rename": "os.rename",
    "os.remove": "os.remove",
    "os.unlink": "os.unlink",
    "os.truncate": "os.truncate",
    "os.chmod": "os.chmod",
    "os.chown": "os.chown",
    "os.symlink": "os.symlink",
    "os.link": "os.link",
    "os.utime": "os.utime",
}
# `os.fork` and `exec` fire audit events but were not inspected, which left the
# widest hole in the guard: a forked child is a fresh process with none of these
# guards installed, so `os.fork()` plus `os.execv('/bin/cat', ...)` read the host
# and reported `passed`. The audit hook is now the only thing standing between a
# handler and a real shell, so this list is deliberately conservative.
#: Events that mean a brand-new process, which carries none of these guards.
#:
#: Only the names CPython actually raises are listed. The `os.exec*` family is
#: absent on purpose and not by oversight: those calls raise the bare `os.exec`
#: event, and CPython also raises `os.exec` for an ordinary `exec()` of Python
#: objects, so blocking it would break any code that evaluates -- unittest
#: included. `os.posix_spawnp` raises `os.posix_spawn` rather than its own name.
#: `os.spawnv` and friends raise `os.fork` on this platform, which is already
#: here. Listing names that never fire would make this set look like it was
#: covering more than it does; the CI `guard` job asserts the events that matter
#: do fire on the runner.
_FORK_EVENTS = frozenset(
    {
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.system",
    }
)
_READ_EVENTS = {
    "os.listdir": "os.listdir",
    "os.scandir": "os.scandir",
}


#: `open` and `os.open` both fire `open`. These are the modes that can change a file.
_WRITE_MODES = frozenset("wax+")
_DEVICE_RANDOM = ("/dev/urandom", "/dev/random")
_CTYPES_EVENTS = frozenset({"ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function"})


def _interpreter_dirs():
    """Directories whose reads are not host-state dependence.

    A handler that does a lazy `import` reads a `.py` or `.pyc` file. That is
    code, not host state, so importing has to keep working — including a
    handler importing its own project's modules, which is the ordinary case for
    a product that lazily pulls in an adapter.

    Taken from `sys.path` rather than from `sysconfig`, because `sysconfig` only
    reports the interpreter's own directories. A product installed into the user
    site directory, or a checkout on `PYTHONPATH`, is still code the handler
    needs to import, and neither appears there.
    """
    candidates = []
    for value in list(sys.path):
        if isinstance(value, str) and value:
            candidates.append(value)
    for name in (
        "prefix",
        "base_prefix",
        "exec_prefix",
        "platlibdir",
    ):
        value = getattr(sys, name, None)
        if isinstance(value, str) and value:
            candidates.append(value)
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            value = sysconfig.get_path(key)
        except KeyError:
            value = None
        if isinstance(value, str) and value:
            candidates.append(value)
    # The cwd matters for a product run in place.
    try:
        candidates.append(os.getcwd())
    except OSError:
        pass
    seen = set()
    out = set()
    for path in candidates:
        normalized = _norm(path)
        # A zip or egg is code too, but `_norm` cannot resolve one to a
        # directory, so only real directories are kept.
        if normalized and normalized not in seen and os.path.isdir(normalized):
            seen.add(normalized)
            out.add(normalized)
    # The system temp directory is not a code directory. It matters because a
    # test harness writes its program into a temp file, which puts that whole
    # directory on `sys.path[0]` and would allowlist every file beside it. It
    # also matters for anything nested under it, since a harness may create a
    # subdirectory and put that on the path instead. No product ships modules
    # out of the temp directory.
    out = {path for path in out if not _under_system_temp(path)}
    return out


def _under_system_temp(normalized):
    """True when `normalized` sits inside the system temp directory."""
    roots = []
    for value in (tempfile.gettempdir(), tempfile.gettempdirb() if hasattr(tempfile, "gettempdirb") else None):
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", "replace")
            except UnicodeError:
                continue
        if isinstance(value, str) and value:
            normalized_root = _norm(value)
            if normalized_root:
                roots.append(normalized_root)
    if not roots:
        return False
    return any(
        normalized == root or normalized.startswith(root + os.sep) for root in roots
    )


class Policy:
    """The paths a handler may touch, and the reads it did touch.

    `extra_reads` and `extra_writes` hold paths registered by a product through
    `allow_read` and `allow_write` before the run starts. They survive
    `reset`, because installing the guards must not silently discard a
    registration the product made while building its runtime.

    `sealed` closes registration once the run is under way. Without it a
    handler could call `allow_read` on itself and read the host filesystem,
    and the run would still report `passed`. That was a real hole, found by
    probing the public API rather than by reading the code.

    Registration closes here -- but closing a door is not the same as making the
    hinges unreachable. This object was a module-level singleton whose
    `read_paths`, `write_paths`, `sealed` and `trusted` were plain writable
    attributes, so a handler could assign to the state directly and skip every
    check. `read_paths`/`write_paths` are now read-only views and `trusted` is
    honoured only for a token this module issued, which closes the one-line
    bypasses. The private `_read`, `_write` and `_LIVE_TOKENS` are still
    reachable by code that imports this module, and are part of the trusted
    surface: see the README. The guard is best-effort, not containment.
    """

    def __init__(self):
        self._write = set()
        self._read = set()
        self._write_view = frozenset()
        self._read_view = frozenset()
        self.reads = set()
        # Resolved at construction, not in reset, so `is_interpreter` is correct
        # before the guards are installed and does not depend on call order.
        self.interpreter_paths = _interpreter_dirs()
        self.extra_reads = set()
        self.extra_writes = set()
        self.sealed = False
        self._resolving = False
        self._in_handler = False
        # Set by the `trusted` property; declared here so the attribute exists
        # before anything reads it.
        self._trusted = None

    @property
    def write_paths(self):
        """Read-only view of the paths a handler may write.

        A property returning a frozenset rather than the live set, so
        `POLICY.write_paths.add(...)` raises instead of widening the policy. The
        allowlists were plain public attributes, and a handler could add a host
        path to one and then read or write it while the run still reported
        `passed`. The mutable sets are reachable as `_write`/`_read`, which is
        part of the guard's trusted surface -- see the note on `_LIVE_TOKENS`.
        """
        return self._write_view

    @property
    def read_paths(self):
        """Read-only view of the paths a handler may read. See `write_paths`."""
        return self._read_view

    @property
    def trusted(self):
        """The token of the innermost active `trusted()` block, or None.

        Assigning to this is deliberately ignored: the audit hook decides
        whether the policy stands down by asking whether the token is one it
        issued, so a handler that assigns an arbitrary object here changes
        nothing.
        """
        return self._trusted

    @trusted.setter
    def trusted(self, value):
        self._trusted = value

    def reset(self, write_paths=(), read_paths=()):
        self._resolving = False
        self._write = set()
        self._read = set()
        self._write_view = frozenset()
        self._read_view = frozenset()
        # Re-resolved on reset so a process that changes sys.path mid-life
        # (a test harness, an embedded interpreter) still classifies correctly.
        self.interpreter_paths = _interpreter_dirs()
        for path in write_paths:
            normalized = _norm(path)
            if normalized is not None:
                self._write.add(normalized)
        for path in read_paths:
            normalized = _norm(path)
            if normalized is not None:
                self._read.add(normalized)
        self._write |= self.extra_writes
        self._read |= self.extra_reads
        self.reads = set()
        # Registration closes here. From this point on the only code that may
        # widen the policy is seam itself, and the public views are frozen so a
        # handler cannot do it through the attribute.
        self._write_view = frozenset(self._write)
        self._read_view = frozenset(self._read)
        self.sealed = True
        self._in_handler = False
        self._trusted = None

    def grant_read(self, normalized):
        """Add one already-normalised path to the read policy and its view."""
        self._read.add(normalized)
        self._read_view = frozenset(self._read)

    def grant_write(self, normalized):
        """Add one already-normalised path to the write policy and its view."""
        self._write.add(normalized)
        self._write_view = frozenset(self._write)

    def is_interpreter(self, real):
        """True when the path belongs to the interpreter's own code."""
        for prefix in self.interpreter_paths:
            if real == prefix or real.startswith(prefix + os.sep):
                return True
        return False

    def note_read(self, real):
        # Importing a module is not host-state dependence, so those reads are
        # permitted and not recorded as provenance.
        if not self.is_interpreter(real):
            self.reads.add(real)

    def seen_reads(self):
        return sorted(self.reads)


@contextlib.contextmanager
def _resolving():
    """Let seam's own path resolution reach the real stat.

    `os.path.realpath` stats every path component. Under guards `os.stat` is a
    policy hook, so it is swapped back for the duration and restored after.

    This runs once at import, while `POLICY` is still being constructed, so it
    tolerates the policy not existing yet.
    """
    policy = globals().get("POLICY")
    was_resolving = policy._resolving if policy is not None else False
    stat, lstat = os.stat, os.lstat
    if policy is not None:
        policy._resolving = True
    # The originals, not the sealed wrappers: this runs during POLICY's own
    # construction, before the wrappers exist, and by definition it is seam's
    # own work rather than a handler's.
    sealed = globals().get("_real_stat")
    os.stat, os.lstat = (
        (_real_stat, _real_lstat) if sealed is not None else (stat, lstat)
    )
    try:
        yield
    finally:
        os.stat, os.lstat = stat, lstat
        if policy is not None:
            policy._resolving = was_resolving


def _norm(path):
    try:
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        elif isinstance(path, os.PathLike):
            path = os.fspath(path)
        if not isinstance(path, str):
            return None
        with _resolving():
            return os.path.realpath(path)
    except (ValueError, TypeError, UnicodeError, OSError):
        return None


def _abs(path):
    """Fallback used when the guarded `realpath` stands down: absolute, unresolved."""
    try:
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        elif isinstance(path, os.PathLike):
            path = os.fspath(path)
        if isinstance(path, str):
            return os.path.abspath(path)
    except (ValueError, TypeError, UnicodeError):
        return None
    return None


# Constructed after `_norm` and `_interpreter_dirs` are defined: the Policy
# resolves interpreter paths at construction time.
POLICY = Policy()

#: Tokens handed out by `trusted()` that are currently in scope. Membership is by
#: identity, and this set is module-private: the audit hook asks it rather than
#: reading `POLICY.trusted`, so assigning anything to that attribute cannot
#: switch the policy off. It exists because the guard is best-effort rather than
#: a sandbox -- a handler that reaches this object can delete from it, which is
#: why `Policy` and the guard's internals are documented as part of the trusted
#: surface rather than presented as an isolation boundary.
_LIVE_TOKENS = set()


#: The real `os.stat`, kept before `install_guards` patches it so seam's own path
#: resolution can still work. `os.path.realpath` needs a real stat result for
#: every path component, so the guarded version cannot simply return None.
def _make_sealed_stat(real_stat, real_lstat):
    """Wrap the real stat functions so they refuse to work inside a handler.

    A handler that could reach the raw `os.stat` would read host file metadata
    straight into a port request, and the run would still report `passed` with a
    digest that depended on the host. Closing over the originals means the only
    handles are the ones returned here, which are not exported at module level.

    The refusal keys off `_in_handler`, but it must not fire while seam is
    resolving a path on a handler's behalf: `_check_stat` normalises its argument
    through `_norm`, which is seam's own work happening inside the handler's
    window. `_resolving` marks that, so the two conditions are checked separately.
    """

    def sealed_stat(path, *args, **kwargs):
        if POLICY._in_handler and not POLICY._resolving:
            raise Fault("file_access", op="file.stat_in_handler")
        return real_stat(path, *args, **kwargs)

    def sealed_lstat(path, *args, **kwargs):
        if POLICY._in_handler and not POLICY._resolving:
            raise Fault("file_access", op="file.stat_in_handler")
        return real_lstat(path, *args, **kwargs)

    return sealed_stat, sealed_lstat


_real_stat, _real_lstat = _make_sealed_stat(os.stat, os.lstat)


@contextlib.contextmanager
def trusted():
    """Seam's own file I/O. The filesystem policy does not apply to it.

    Only seam may call this. It is re-entrant, but it is refused once a handler
    is running: a handler that could reach it would have a documented way to
    read the host filesystem and still report `passed`.
    """
    if POLICY.sealed and POLICY._in_handler:
        raise Fault("file_access", op="file.trusted_in_handler")
    token = object()
    previous = POLICY.trusted
    # The token is registered in a module-private set as well as stored on the
    # policy. The audit hook checks membership by identity rather than testing
    # `POLICY.trusted is not None`, because that test could be satisfied by
    # assigning any object to the attribute -- which a handler can do, and which
    # skipped every filesystem check while recording nothing in `fs_reads`.
    _LIVE_TOKENS.add(token)
    POLICY.trusted = token
    try:
        yield
    finally:
        POLICY.trusted = previous
        _LIVE_TOKENS.discard(token)


@contextlib.contextmanager
def _handler_scope():
    """Mark the window in which product handler code is executing.

    Inside it, the policy refuses to be widened and `trusted()` is refused.
    Seam's own bookkeeping still runs: the artifact write happens outside this.
    """
    was = POLICY._in_handler
    POLICY._in_handler = True
    try:
        yield
    finally:
        POLICY._in_handler = was


def allow_write(path):
    """Grant write access to one exact path, before the run starts.

    Safe to call before `install_guards`: the registration survives. Refused
    once a handler is executing, because a handler must not widen its own
    policy. Paths are exact, never prefixes, so granting one file does not
    grant its directory.
    """
    if POLICY.sealed and POLICY._in_handler:
        raise Fault("file_access", op="file.allow_in_handler")
    normalized = _norm(path)
    if normalized is not None:
        POLICY.extra_writes.add(normalized)
        POLICY.grant_write(normalized)


def allow_read(path):
    """Grant read access to one exact path, before the run starts.

    Safe to call before `install_guards`. Refused from inside a handler.
    """
    if POLICY.sealed and POLICY._in_handler:
        raise Fault("file_access", op="file.allow_in_handler")
    normalized = _norm(path)
    if normalized is not None:
        POLICY.extra_reads.add(normalized)
        POLICY.grant_read(normalized)


def _clock(op):
    def blocked(*_args, **_kwargs):
        raise Fault("real_clock", op=op)

    return blocked


def _random(op):
    def blocked(*_args, **_kwargs):
        raise Fault("unseeded_random", op=op)

    return blocked


def _check_open(path, mode, flags):
    """`open` fires for the builtin and for os.open. Refuse anything not allowlisted.

    The builtin passes a mode string like `"rb"`. `os.open` passes mode None and
    the integer flags instead, so write intent has to be read off the flags.
    """
    real = _norm(path)
    if real in _DEVICE_RANDOM:
        raise Fault("unseeded_random", op="os.urandom")
    if real is None:
        # Not a path: either an integer descriptor, or something `_norm` cannot
        # resolve. Neither can be allowlisted, and the import machinery writes
        # its `.pyc` through a descriptor, so an fd is permitted. A non-str,
        # non-PathLike object is refused, because nothing legitimate passes one.
        if isinstance(path, int):
            return
        raise Fault("file_access", op="file.unreadable_path")
    if mode is None:
        # os.open passes mode None and the integer flags instead of a mode
        # string, so write intent has to be read off the flags.
        write_flags = (
            os.O_WRONLY
            | os.O_RDWR
            | getattr(os, "O_CREAT", 0)
            | getattr(os, "O_APPEND", 0)
            | getattr(os, "O_TRUNC", 0)
        )
        writes = True if type(flags) is not int else bool(flags & write_flags)
    else:
        writes = any(flag in _WRITE_MODES for flag in mode)
    if writes:
        if real not in POLICY.write_paths:
            raise Fault("file_write", op="file.write")
        return
    if real not in POLICY.read_paths and not POLICY.is_interpreter(real):
        raise Fault("file_read", op="file.read")
    POLICY.note_read(real)


def _check_path_event(op, code):
    def blocked(*_args, **_kwargs):
        raise Fault(code, op=op)

    return blocked


def _check_stat(path, *args, **kwargs):
    """`os.stat` raises no audit event, so it is patched rather than observed.

    Takes the full stdlib signature. Anything narrower breaks `pathlib`,
    `shutil` and `linecache`, all of which pass `dir_fd` or `follow_symlinks`.
    A `TypeError` there is an opaque `handler_error`, not the `file_read` this is
    supposed to produce.

    Returns a real stat result for an allowlisted path, because `os.path.*`,
    `pathlib` and `linecache` all read `st_*` off the return value. Returning
    None here would make `os.path.exists()` answer True for a path that does not
    exist: a silently wrong result rather than a fault.
    """
    # `os.path.realpath` stats every component of the path. While seam is
    # resolving a path, this hook must be silent or it refuses seam's own work.
    if POLICY._resolving:
        return _real_stat(path, *args, **kwargs)
    real = _norm(path)
    if real is None or (real not in POLICY.read_paths and not POLICY.is_interpreter(real)):
        raise Fault("file_read", op="file.stat")
    POLICY.note_read(real)
    return _real_stat(path, *args, **kwargs)


def _is_bytecode_path(path):
    """True when `path` is the `__pycache__` directory itself, or inside it.

    A handler importing its own project's modules makes the interpreter write
    `.pyc` files. That is the import machinery, not the product reaching for the
    host, and refusing it would make ordinary product code unusable.

    The path is normalised before the test rather than split on separators. An
    earlier version looked for `__pycache__` among the literal components, which
    meant any path of the form `<dir>/__pycache__/../<target>` qualified: the
    handler got arbitrary read, write and delete while the run still reported
    `passed`. Normalising collapses the `..` first, so the exemption is decided
    by where the path actually lands.
    """
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        elif isinstance(path, os.PathLike):
            path = os.fspath(path)
    except (TypeError, ValueError):
        return False
    if not isinstance(path, str):
        return False
    real = _norm(path)
    if real is None:
        return False
    # The path may be the directory itself (mkdir) or a file inside it (the
    # `.pyc` write), so check the basename and then the parent. Testing both
    # rather than only the parent is what lets a not-yet-created directory
    # qualify: `realpath` on a path that does not exist still resolves it, but
    # there is no parent directory to stat for a path like `/app/__pycache__`.
    name = os.path.basename(real.rstrip(os.sep) or os.sep)
    if name == "__pycache__":
        return True
    parent = os.path.dirname(real)
    return os.path.basename(parent) == "__pycache__"


def _is_bytecode(event, args):
    """True for the `__pycache__` writes CPython does when importing."""
    if event not in ("os.mkdir", "os.rename", "os.remove", "os.truncate", "os.chmod"):
        return False
    return any(_is_bytecode_path(arg) for arg in args)


def _audit(event, args):
    mapped = _SOCKET_EVENTS.get(event)
    if mapped is not None:
        raise Fault("real_io", op=mapped)
    if event.startswith("subprocess."):
        raise Fault("real_io", op="subprocess")
    if event in _FORK_EVENTS:
        # A forked or exec'd child is a brand-new process with none of these
        # guards installed. Blocking the audit event here is what stops a
        # handler reaching a real shell, so it is checked before anything else.
        #
        # The bare `exec` audit event is deliberately NOT in the list: CPython
        # raises it for ordinary `exec()` of Python objects, so blocking it
        # breaks any code that evaluates, including unittest itself.
        raise Fault("real_io", op="subprocess")
    if event in _CTYPES_EVENTS:
        # libc is reachable from here, so real clocks and real syscalls are too.
        raise Fault("real_io", op="ctypes")
    # Seam's own I/O is the only thing allowed to stand down. It is recognised by
    # the *identity* of a token `trusted()` registered, not by `POLICY.trusted`
    # merely being non-None: a handler could assign any object to that
    # attribute and skip every filesystem check while recording nothing in
    # `fs_reads`. An earlier version also had a plain `POLICY.armed` boolean,
    # which a handler cleared the same way.
    trusted = POLICY.trusted in _LIVE_TOKENS
    if event in _WRITE_EVENTS:
        if not trusted and not _is_bytecode(event, args):
            raise Fault("file_write", op=_WRITE_EVENTS[event])
        return
    if event in _READ_EVENTS:
        if trusted:
            return
        # Directory listing goes through the same allowlist as `open`. Refusing
        # it outright would make the documented lazy-import promise false: the
        # import system lists a directory to find a module in it, so any cold
        # `import xml.sax` inside a handler would fault.
        for arg in args:
            real = _norm(arg)
            if real is not None and (
                real in POLICY.read_paths or POLICY.is_interpreter(real)
            ):
                POLICY.note_read(real)
                return
        raise Fault("file_read", op=_READ_EVENTS[event])
    if event == "open" and args and not trusted:
        # A `.pyc` write is the import machinery finishing its job.
        if _is_bytecode_path(args[0]):
            return
        _check_open(args[0], args[1], args[2] if len(args) > 2 else None)


class _BlockedDatetime(_datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        raise Fault("real_clock", op="datetime.now")

    @classmethod
    def utcnow(cls):
        raise Fault("real_clock", op="datetime.utcnow")


# A fresh `random.Random()` is the common escape. The module functions are never
# called by an instance, so the instance methods are what actually need blocking.
_RANDOM_METHODS = (
    "random",
    "getrandbits",
    "randint",
    "randrange",
    "choice",
    "choices",
    "sample",
    "shuffle",
    "uniform",
    "randbytes",
    "triangular",
    "expovariate",
    "gammavariate",
    "gauss",
    "normalvariate",
    "lognormvariate",
    "vonmisesvariate",
    "paretovariate",
    "weibullvariate",
    "betavariate",
)

_CLOCK_NAMES = (
    "time",
    "time_ns",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "process_time",
    "process_time_ns",
    "thread_time",
    "thread_time_ns",
)


def install_guards(*, write_paths=(), read_paths=()):
    """Idempotent. There is no uninstall. One simulation process installs this once.

    `write_paths` and `read_paths` are the only paths a handler may touch. The
    runner passes the artifact path as a write path.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    POLICY.reset(write_paths, read_paths)

    for name in _CLOCK_NAMES:
        if hasattr(time, name):
            setattr(time, name, _clock(f"time.{name}"))
    _datetime.datetime = _BlockedDatetime

    # `os.times()` reports real process and wall time, and fires no audit event,
    # so it has to be patched like the rest of the clock.
    if hasattr(os, "times"):
        os.times = _clock("os.times")

    for name in _RANDOM_METHODS + ("seed",):
        if hasattr(random, name):
            setattr(random, name, _random("random"))

    def _blocked_method(_self, *_args, **_kwargs):
        raise Fault("unseeded_random", op="random")

    # `SystemRandom` subclasses `Random` but *overrides* `random`, `getrandbits`,
    # `randbytes` and `seed` with implementations that read `_urrandom`
    # directly. Setting the attribute on the parent does not touch an override,
    # so patching `Random` alone left `SystemRandom` reading OS entropy while
    # reporting `passed`. Both classes are patched for that reason.
    for cls in (random.Random, random.SystemRandom):
        for name in _RANDOM_METHODS + ("seed",):
            if hasattr(cls, name):
                setattr(cls, name, _blocked_method)

    os.urandom = _random("os.urandom")
    # `randbits` is here because `secrets.randbits` is implemented as
    # `SystemRandom().getrandbits(k)`, so blocking only the token helpers left
    # the most obvious way to read host entropy open.
    for name in (
        "token_bytes",
        "token_hex",
        "token_urlsafe",
        "choice",
        "randbelow",
        "randbits",
    ):
        if hasattr(secrets, name):
            setattr(secrets, name, _random("secrets"))
    uuid.uuid4 = _random("uuid")
    uuid.uuid1 = _random("uuid")

    def _start(_self, *_args, **_kwargs):
        raise Fault("thread", op="thread")

    threading.Thread.start = _start

    def _socket(*_args, **_kwargs):
        raise Fault("real_io", op="socket.socket")

    def _getaddrinfo(*_args, **_kwargs):
        raise Fault("real_io", op="socket.getaddrinfo")

    socket.socket = _socket
    socket.getaddrinfo = _getaddrinfo

    for name in ("stat", "lstat"):
        if hasattr(os, name):
            setattr(os, name, _check_stat)

    sys.addaudithook(_audit)