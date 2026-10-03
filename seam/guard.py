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
    """

    def __init__(self):
        self.write_paths = set()
        self.read_paths = set()
        self.reads = set()
        # Resolved at construction, not in reset, so `is_interpreter` is correct
        # before the guards are installed and does not depend on call order.
        self.interpreter_paths = _interpreter_dirs()
        self.extra_reads = set()
        self.extra_writes = set()
        self.sealed = False
        self._resolving = False
        self._in_handler = False
        #: Sentinel. While a `trusted()` block is active this holds that block's
        #: token; the audit hook tests it against None, so a handler cannot opt
        #: itself out by assigning to a public flag. There is deliberately no
        #: boolean here: an earlier version had one, and a handler set it to
        #: False and read the host filesystem with the run still passing.
        self.trusted = None

    def reset(self, write_paths=(), read_paths=()):
        self._resolving = False
        self.write_paths = set()
        self.read_paths = set()
        # Re-resolved on reset so a process that changes sys.path mid-life
        # (a test harness, an embedded interpreter) still classifies correctly.
        self.interpreter_paths = _interpreter_dirs()
        for path in write_paths:
            normalized = _norm(path)
            if normalized is not None:
                self.write_paths.add(normalized)
        for path in read_paths:
            normalized = _norm(path)
            if normalized is not None:
                self.read_paths.add(normalized)
        self.write_paths |= self.extra_writes
        self.read_paths |= self.extra_reads
        self.reads = set()
        # Registration closes here. From this point on the only code that may
        # widen the policy is seam itself.
        self.sealed = True
        self._in_handler = False
        self.trusted = None

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
    POLICY.trusted = token
    try:
        yield
    finally:
        POLICY.trusted = previous


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
        POLICY.write_paths.add(normalized)


def allow_read(path):
    """Grant read access to one exact path, before the run starts.

    Safe to call before `install_guards`. Refused from inside a handler.
    """
    if POLICY.sealed and POLICY._in_handler:
        raise Fault("file_access", op="file.allow_in_handler")
    normalized = _norm(path)
    if normalized is not None:
        POLICY.extra_reads.add(normalized)
        POLICY.read_paths.add(normalized)


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


def _check_stat(path):
    """`os.stat` raises no audit event, so it is patched rather than observed."""
    # `os.path.realpath` stats every component of the path. While seam is
    # resolving a path, this hook must be silent or it refuses seam's own work.
    if POLICY._resolving:
        return None
    real = _norm(path)
    if real is None or (real not in POLICY.read_paths and not POLICY.is_interpreter(real)):
        raise Fault("file_read", op="file.stat")
    POLICY.note_read(real)


def _is_bytecode_path(path):
    """True when `path` is inside a `__pycache__` directory.

    A handler importing its own project's modules makes the interpreter write
    `.pyc` files. That is the import machinery, not the product reaching for the
    host, and refusing it would make ordinary product code unusable.

    Only paths inside a `__pycache__` directory qualify, so this grants nothing
    else: a handler cannot write an arbitrary file by naming one.
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
    parts = [part for part in path.replace(os.sep, "/").split("/") if part]
    # The path itself may be the `__pycache__` directory (mkdir) or a file
    # inside it (the `.pyc` write), so every component counts.
    return "__pycache__" in parts


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
    if event in ("os.system", "os.posix_spawn", "os.posix_spawnp"):
        raise Fault("real_io", op="subprocess" if event != "os.system" else "os.system")
    if event in _CTYPES_EVENTS:
        # libc is reachable from here, so real clocks and real syscalls are too.
        raise Fault("real_io", op="ctypes")
    # Seam's own I/O is the only thing allowed to stand down, and it says so by
    # holding a trusted token rather than by clearing a flag a handler could
    # also clear. A handler that set `POLICY.armed = False` used to disable the
    # filesystem policy entirely; `_trusted` is not reachable that way.
    trusted = POLICY.trusted
    if event in _WRITE_EVENTS:
        if trusted is None and not _is_bytecode(event, args):
            raise Fault("file_write", op=_WRITE_EVENTS[event])
        return
    if event in _READ_EVENTS:
        if trusted is None:
            raise Fault("file_read", op=_READ_EVENTS[event])
        return
    if event == "open" and args and trusted is None:
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

    for name in _RANDOM_METHODS + ("seed",):
        if hasattr(random, name):
            setattr(random, name, _random("random"))

    def _blocked_method(_self, *_args, **_kwargs):
        raise Fault("unseeded_random", op="random")

    for name in _RANDOM_METHODS:
        if hasattr(random.Random, name):
            setattr(random.Random, name, _blocked_method)

    os.urandom = _random("os.urandom")
    for name in ("token_bytes", "token_hex", "token_urlsafe", "choice", "randbelow"):
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