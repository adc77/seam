"""Errors the runner can put in an artifact."""

from contextlib import contextmanager
from contextvars import ContextVar
import re

_FAULT_SINK = ContextVar("seam_fault_sink", default=None)
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


@contextmanager
def fault_scope(sink):
    """Keep simulation faults observable even when product code catches them."""
    token = _FAULT_SINK.set(sink)
    try:
        yield
    finally:
        _FAULT_SINK.reset(token)


class SeamError(Exception):
    def __init__(self, code, op=None, during=None, exc_type=None):
        super().__init__(code)
        self.code = code
        self.op = op
        self.during = during
        self.exc_type = exc_type
        self.meta = {}

    def as_dict(self):
        out = {"code": self.code}
        if self.op is not None:
            out["op"] = self.op
        if self.during is not None:
            out["during"] = self.during
        if self.exc_type is not None:
            out["exc_type"] = self.exc_type
        return out


class Refuse(SeamError):
    """The run did not start. Exit 3."""


class Fault(SeamError):
    """The run started and then broke. Exit 2, unless a grader raised it after the seal."""

    def __init__(self, code, op=None, during=None, exc_type=None):
        super().__init__(code, op, during, exc_type)
        sink = _FAULT_SINK.get()
        if sink is not None:
            sink(self)


class NoTimerBackend(SeamError):
    def __init__(self):
        super().__init__("no_timer_backend")


class PortError(Exception):
    """An expected dependency failure that product code may handle and replay."""

    def __init__(self, code):
        if type(code) is not str or not ERROR_CODE.fullmatch(code):
            raise Fault("bad_value")
        super().__init__(code)
        self.code = code
