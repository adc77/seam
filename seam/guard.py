"""Fail the obvious standard-library leaks. This is not a sandbox.

`datetime.datetime` is a C type, so `now` cannot be assigned on it. The module
attribute is replaced with a subclass. A name bound before `install_guards()`
still points at the original type.

`socket.create_connection` looks up the address before it connects. The guard
fails that lookup. Waiting for `socket.connect` would already have touched the network.
"""

import datetime as _datetime
import os
import random
import secrets
import socket
import sys
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


def _clock(op):
    def blocked(*_args, **_kwargs):
        raise Fault("real_clock", op=op)

    return blocked


def _random(op):
    def blocked(*_args, **_kwargs):
        raise Fault("unseeded_random", op=op)

    return blocked


def _path(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeError:
            return None
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, str):
        return value
    return None


def _audit(event, args):
    mapped = _SOCKET_EVENTS.get(event)
    if mapped is not None:
        raise Fault("real_io", op=mapped)
    if event.startswith("subprocess."):
        raise Fault("real_io", op="subprocess")
    if event in ("os.system", "os.posix_spawn", "os.posix_spawnp"):
        raise Fault("real_io", op="subprocess" if event != "os.system" else "os.system")
    if event == "open" and args:
        path = _path(args[0])
        if path in ("/dev/urandom", "/dev/random"):
            raise Fault("unseeded_random", op="os.urandom")


class _BlockedDatetime(_datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        raise Fault("real_clock", op="datetime.now")

    @classmethod
    def utcnow(cls):
        raise Fault("real_clock", op="datetime.utcnow")


def install_guards():
    """Idempotent. There is no uninstall. One simulation process installs this once."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    time.time = _clock("time.time")
    time.time_ns = _clock("time.time_ns")
    time.monotonic = _clock("time.monotonic")
    time.monotonic_ns = _clock("time.monotonic_ns")
    _datetime.datetime = _BlockedDatetime

    for name in (
        "random",
        "uniform",
        "randint",
        "randrange",
        "choice",
        "choices",
        "sample",
        "shuffle",
        "getrandbits",
        "randbytes",
        "seed",
    ):
        if hasattr(random, name):
            setattr(random, name, _random("random"))

    def _blocked_method(_self, *_args, **_kwargs):
        raise Fault("unseeded_random", op="random")

    random.Random.random = _blocked_method
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
    sys.addaudithook(_audit)
