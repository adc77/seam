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
# `open` and `os.open` both fire `open`. These are the modes that can change a file.
_WRITE_MODES = frozenset("wax+")
_DEVICE_RANDOM = ("/dev/urandom", "/dev/random")
_CTYPES_EVENTS = frozenset({"ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function"})


def _interpreter_dirs():
    """Directories the interpreter reads its own code from.

    A handler that does a lazy `import` reads a `.py` or `.pyc` file. That is
    not dependence on host state, so these paths are allowlisted for reads.
    Without this, `import subprocess` inside a handler would fault on the module
    file before the real leak was ever reached.
    """
    dirs = []
    for value in (
        getattr(sys, "prefix", None),
        getattr(sys, "base_prefix", None),
        getattr(sys, "exec_prefix", None),
        sysconfig.get_path("stdlib"),
        sysconfig.get_path("platstdlib"),
        sysconfig.get_path("purelib"),
        sysconfig.get_path("platlib"),
    ):
        if isinstance(value, str) and value:
            dirs.append(value)
    seen = set()
    out = set()
    for path in dirs:
        normalized = _norm(path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.add(normalized)
    return out


class Policy:
    """The paths a handler may touch, and the reads it did touch."""

    def __init__(self):
        self.write_paths = set()
        self.read_paths = set()
        self.reads = set()
        self.interpreter_paths = set()
        self.armed = False
        self._resolving = False

    def reset(self, write_paths=(), read_paths=()):
        self._resolving = False
        self.write_paths = set()
        self.read_paths = set()
        self.interpreter_paths = _interpreter_dirs()
        for path in write_paths:
            normalized = _norm(path)
            if normalized is not None:
                self.write_paths.add(normalized)
        for path in read_paths:
            normalized = _norm(path)
            if normalized is not None:
                self.read_paths.add(normalized)
        self.reads = set()
        self.armed = True

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


POLICY = Policy()


@contextlib.contextmanager
def _resolving():
    """Let seam's own path resolution reach the real stat and the real open.

    `os.path.realpath` stats every component of a path. Under guards `os.stat`
    is a policy hook, so it is swapped back for the duration and restored after.
    A handler cannot reach this: it only runs while seam is resolving a path.
    """
    was_resolving = POLICY._resolving
    stat, lstat = os.stat, os.lstat
    POLICY._resolving = True
    os.stat, os.lstat = _REAL_STAT, _REAL_LSTAT
    try:
        yield
    finally:
        os.stat, os.lstat = stat, lstat
        POLICY._resolving = was_resolving


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


@contextlib.contextmanager
def trusted():
    """Seam's own file I/O. The filesystem policy does not apply to it."""
    was = POLICY.armed
    POLICY.armed = False
    try:
        yield
    finally:
        POLICY.armed = was


def allow_write(path):
    """Grant write access to one exact path. Used for the artifact."""
    normalized = _norm(path)
    if normalized is not None:
        POLICY.write_paths.add(normalized)


def allow_read(path):
    normalized = _norm(path)
    if normalized is not None:
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
        raise Fault("file_access", op="file.unreadable_path")
    if mode is None:
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
    if event in _WRITE_EVENTS:
        if POLICY.armed:
            raise Fault("file_write", op=_WRITE_EVENTS[event])
        return
    if event in _READ_EVENTS:
        if POLICY.armed:
            raise Fault("file_read", op=_READ_EVENTS[event])
        return
    if event == "open" and args and POLICY.armed:
        _check_open(args[0], args[1], args[2] if len(args) > 2 else None)


class _BlockedDatetime(_datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        raise Fault("real_clock", op="datetime.now")

    @classmethod
    def utcnow(cls):
        raise Fault("real_clock", op="datetime.utcnow")


#: The real `os.stat`, kept before the patch so path resolution can still work.
#: `os.path.realpath` needs a real stat result for every path component, so the
#: guarded version cannot simply return None; seam's own code has to reach the
#: original while a handler cannot.
_REAL_STAT = os.stat
_REAL_LSTAT = os.lstat

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