"""Errors the runner can put in an artifact. The message is the code and nothing else."""


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


class NoTimerBackend(SeamError):
    def __init__(self):
        super().__init__("no_timer_backend")
