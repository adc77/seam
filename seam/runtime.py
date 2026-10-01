"""Registration, live delivery, and the simulation process entry."""

import os
import time

from seam.artifact import refused, write_artifact
from seam.canon import INT64_MAX, deep_copy
from seam.case import IDENT, TERMINAL, load_case
from seam.clock import format_utc
from seam.ctx import Ctx, assert_int, replace_at
from seam.errors import Fault, NoTimerBackend, Refuse
from seam.grade import exit_code, finish
from seam.guard import install_guards
from seam.loop import run_sim

_CONSUMED = False


def in_sim():
    return os.environ.get("SEAM_SIM") == "1"


class Factory:
    """The object the runtime registered. In sim, calling it does not call the user's factory."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0
        self.client = None

    def materialize(self):
        if in_sim():
            self.calls += 1
            raise Refuse("live_factory_called")
        if self.client is None:
            self.calls += 1
            self.client = self.fn()
        return self.client

    def __call__(self):
        if in_sim():
            self.calls += 1
            raise Refuse("live_factory_called")
        return self.materialize()


class Runtime:
    def __init__(self, namespace="live"):
        if type(namespace) is not str or namespace == "" or namespace.startswith("sim-"):
            raise Refuse("namespace")
        self.namespace = namespace
        self.mode = None
        self.handlers = {}
        self.factories = {}
        self._depth = 0
        self._state = {}
        self._redact = None
        self._timer_backend = None
        self._live_timers = {}
        self._token_n = 0
        self._record = None
        self.stopped = None

    def port(self, name, factory):
        if self.mode is not None:
            raise Refuse("bad_env")
        if type(name) is not str or not IDENT.match(name) or name in self.factories or not callable(factory):
            raise Refuse("bad_case")
        self.factories[name] = Factory(factory)
        return self.factories[name]

    def on(self, name, handler):
        if self.mode is not None:
            raise Refuse("bad_env")
        if type(name) is not str or not IDENT.match(name) or name in self.handlers or not callable(handler):
            raise Refuse("bad_case")
        self.handlers[name] = handler

    def redact(self, fn):
        if not callable(fn):
            raise Refuse("bad_case")
        self._redact = fn
        return self

    def set_timer_backend(self, fn):
        if not callable(fn):
            raise Refuse("bad_case")
        self._timer_backend = fn
        return self

    def start_live(self):
        sim = os.environ.get("SEAM_SIM")
        if sim not in (None, "0"):
            raise Refuse("bad_env")
        if os.environ.get("SEAM_CASE"):
            raise Refuse("bad_env")
        record = os.environ.get("SEAM_RECORD")
        if record not in (None, "0", "1"):
            raise Refuse("bad_env")
        if self.mode is not None:
            raise Refuse("bad_env")
        if self.namespace.startswith("sim-"):
            raise Refuse("namespace")
        if record == "1":
            path = os.environ.get("SEAM_ARTIFACT")
            if not path:
                raise Refuse("bad_env")
            self._record = open(path, "a", encoding="ascii", newline="\n")
        self.mode = "live"

    def deliver(self, name, body):
        if self.mode is None:
            raise Refuse("bad_env")
        if self.mode != "live" or self._depth:
            raise Fault("reentrant")
        if type(name) is not str or name not in self.handlers:
            raise Fault("unknown_handler")
        ctx = Ctx(self, name)
        self._depth += 1
        try:
            self.handlers[name](ctx, deep_copy(body))
        finally:
            self._depth -= 1

    def now(self):
        if self.mode != "live":
            raise Fault("bad_value")
        return time.time_ns()

    def utc(self):
        return format_utc(self.now())

    def rand_u64(self):
        if self.mode != "live":
            raise Fault("bad_value")
        return int.from_bytes(os.urandom(8), "little")

    def rand_below(self, n):
        if self.mode != "live":
            raise Fault("bad_value")
        # Live draws are OS bytes. The sim stream is the one with a stability promise.
        from seam.rng import Rng

        return Rng(int.from_bytes(os.urandom(8), "little")).rand_below(n)

    def ident(self, prefix):
        if type(prefix) is not str or not IDENT.match(prefix):
            raise Fault("bad_value")
        return f"{prefix}_{self.rand_u64():016x}"

    def emit(self, port, request):
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        if type(port) is not str or port not in self.factories:
            raise Fault("unknown_port")
        request_copy = deep_copy(request)
        client = self.factories[port].materialize()
        response = deep_copy(client(deep_copy(request_copy)))
        if self._record is not None:
            self._record_line(port, request_copy, response)
        return deep_copy(response)

    def _record_line(self, port, request, response):
        from seam.canon import dumps

        recorded_request = request
        recorded_response = response
        if self._redact is not None:
            recorded_request = deep_copy(self._redact(deep_copy(request)))
            recorded_response = deep_copy(self._redact(deep_copy(response)))
        line = dumps(
            {
                "format": "seam-tape",
                "version": 1,
                "at_ns": time.time_ns(),
                "port": port,
                "request": recorded_request,
                "response": recorded_response,
            }
        )
        self._record.write(line + "\n")
        self._record.flush()
        os.fsync(self._record.fileno())

    def schedule_after(self, delay_ns, handler, body, name=None):
        assert_int(delay_ns, minimum=0)
        at = self.now() + delay_ns
        if at > INT64_MAX:
            raise Fault("bad_value")
        # One sample. Checking `at` against a second now() rejects a delay of zero.
        return self._arm(at, handler, body, name)

    def schedule_at(self, at_ns, handler, body, name=None):
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        assert_int(at_ns, minimum=0)
        if at_ns < self.now():
            raise Fault("bad_value")
        return self._arm(at_ns, handler, body, name)

    def _arm(self, at_ns, handler, body, name):
        if self._timer_backend is None:
            raise NoTimerBackend()
        if type(handler) is not str or handler not in self.handlers:
            raise Fault("unknown_handler")
        if name is not None and (type(name) is not str or not TERMINAL.match(name)):
            raise Fault("bad_value")
        self._token_n += 1
        token = f"t{self._token_n}"
        self._live_timers[token] = "armed"
        self._timer_backend(token, at_ns, handler, deep_copy(body), name)
        return token

    def cancel(self, token):
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        if type(token) is not str or self._live_timers.get(token) != "armed":
            raise Fault("unknown_timer")
        self._live_timers[token] = "cancelled"

    def stop(self, name):
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        if type(name) is not str or not TERMINAL.match(name):
            raise Fault("bad_value")
        self.stopped = name

    def state_copy(self):
        return deep_copy(self._state)

    def set_state(self, value):
        if self._depth == 0:
            raise Fault("bad_value")
        self._state = replace_at(self._state, "", value)

    def patch(self, path, value):
        if self._depth == 0:
            raise Fault("bad_value")
        self._state = replace_at(self._state, path, value)

    def stamp(self, row):
        if type(row) is not dict:
            raise Fault("bad_value")
        out = deep_copy(row)
        out["_seam_ns"] = self.namespace
        return out


def _artifact_path():
    return os.environ.get("SEAM_ARTIFACT") or os.path.join(os.getcwd(), "seam-artifact.json")


def _emit(path, obj):
    write_artifact(path, obj)
    print(os.path.abspath(path), flush=True)


def _require_sim():
    if os.environ.get("SEAM_SIM") != "1":
        raise Refuse("bad_env")
    if "SEAM_RECORD" in os.environ:
        raise Refuse("bad_env")
    if "SEAM_NAMESPACE" not in os.environ or "SEAM_CASE" not in os.environ:
        raise Refuse("bad_env")
    namespace = os.environ.get("SEAM_NAMESPACE")
    case = os.environ.get("SEAM_CASE")
    if namespace == "" or case == "":
        raise Refuse("bad_env")
    return namespace, case


def main(rt):
    """Run one case in this process. A second call is refused. Stdout is the artifact path."""
    global _CONSUMED
    path = _artifact_path()
    if _CONSUMED:
        _emit(path, refused("second_run"))
        return 3
    try:
        namespace, case_path = _require_sim()
        if any(factory.calls for factory in rt.factories.values()):
            raise Refuse("live_factory_called")
        case = load_case(
            case_path,
            ports=set(rt.factories),
            handlers=set(rt.handlers),
            namespace=namespace,
        )
    except Refuse as err:
        _emit(path, refused(err.code, meta=err.meta, op=err.op))
        return 3
    _CONSUMED = True
    install_guards()
    result = run_sim(rt, case)
    _emit(path, finish(result, case))
    return exit_code(result)
