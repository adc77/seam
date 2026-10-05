"""Registration, live delivery, and the simulation process entry."""

import os
import inspect
import threading
import time

from seam.artifact import refused, write_artifact
from seam.canon import ID_HEX_WIDTH, INT64_MAX, TIMER_TOKEN_PREFIX, U64_BYTES, deep_copy
from seam.case import IDENT, TERMINAL, load_case
from seam.clock import format_utc
from seam.ctx import Ctx, assert_int, replace_at
from seam.errors import Fault, NoTimerBackend, PortError, Refuse
from seam.grade import exit_code, finish
from seam.guard import install_guards, trusted
from seam.loop import run_sim

_CONSUMED = False


def require_sync(fn, *, fault=False):
    """Reject asynchronous and generator callables before delivery."""
    targets = (fn, getattr(fn, "__call__", None))
    if any(inspect.iscoroutinefunction(target) or inspect.isgeneratorfunction(target)
           or inspect.isasyncgenfunction(target) for target in targets):
        raise (Fault if fault else Refuse)("unsupported_handler")


def require_sync_result(result):
    """Catch callables that conceal asynchronous or generator results."""
    if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
        if inspect.iscoroutine(result) or inspect.isgenerator(result):
            result.close()
        raise Fault("unsupported_handler")

#: Environment variables the runner reads. Named once so a typo is one edit
#: rather than a search, and so `SEAM_SIM` in particular cannot be read two
#: different ways in two different places.
ENV_SIM = "SEAM_SIM"
ENV_CASE = "SEAM_CASE"
ENV_NAMESPACE = "SEAM_NAMESPACE"
ENV_ARTIFACT = "SEAM_ARTIFACT"
ENV_RECORD = "SEAM_RECORD"
ENV_HASH_SEED = "PYTHONHASHSEED"


def _sim_requested():
    """True when the environment asks for a simulation.

    Only the exact string "1" counts. An earlier version also treated any other
    non-empty value as a request in `start_live`, which meant `SEAM_SIM=true`
    made `in_sim()` return False while `start_live` refused, so the two
    disagreed about the same environment. One rule, read in one place.
    """
    return os.environ.get(ENV_SIM) == "1"


def in_sim():
    return _sim_requested()


def sim_env(case, namespace, artifact=None, **overrides):
    """The environment for one simulation process, with `PYTHONHASHSEED` pinned.

    String hashing is salted per interpreter, so `for x in {"a", "b", "c"}`
    iterates in a different order in every process. A handler that puts a set
    into a port request therefore produced a different digest on every replay
    while the run still reported `passed`. The salt is chosen at interpreter
    start-up, so it cannot be fixed from inside the process; it has to be set
    before the child is launched. Launch the run with this environment:

        env = sim_env("case.json", "sim-shop", "out.json")
        subprocess.run([sys.executable, "-m", "myproduct"], env=env)

    An existing `PYTHONHASHSEED` is respected, so a caller can pin a different
    seed deliberately. In-process use cannot be fixed this way; a run that
    iterates a set in a handler should sort it instead.
    """
    env = dict(os.environ)
    env[ENV_SIM] = "1"
    env[ENV_CASE] = case
    env[ENV_NAMESPACE] = namespace
    if artifact is not None:
        env[ENV_ARTIFACT] = artifact
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    # After the overrides, so an explicit seed is honoured and an override that
    # clears the key falls back to the pinned default rather than to nothing.
    env.setdefault(ENV_HASH_SEED, "0")
    return env


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
            client = self.fn()
            require_sync_result(client)
            require_sync(client, fault=True)
            if not callable(client):
                raise Fault("bad_value")
            self.client = client
        return self.client

    def __call__(self):
        if in_sim():
            self.calls += 1
            raise Refuse("live_factory_called")
        return self.materialize()


class Runtime:
    def __init__(self, namespace="live", *, config=None, initial_state=None):
        if type(namespace) is not str or namespace == "" or namespace.startswith("sim-"):
            raise Refuse("namespace")
        self.namespace = namespace
        self.config = {} if config is None else deep_copy(config)
        self.mode = None
        self.handlers = {}
        self.factories = {}
        self.backends = {}
        self._depth = 0
        self._state = {} if initial_state is None else replace_at({}, "", initial_state)
        self._redact = None
        self._timer_backend = None
        self._live_timers = {}
        self._lock = threading.RLock()
        self._token_n = 0
        self._record = None
        self.stopped = None

    def port(self, name, factory):
        if self.mode is not None:
            raise Refuse("bad_env")
        if type(name) is not str or not IDENT.match(name) or name in self.factories or not callable(factory):
            raise Refuse("bad_case")
        require_sync(factory)
        self.factories[name] = Factory(factory)
        return self.factories[name]

    def on(self, name, handler):
        if self.mode is not None:
            raise Refuse("bad_env")
        if type(name) is not str or not IDENT.match(name) or name in self.handlers or not callable(handler):
            raise Refuse("bad_case")
        require_sync(handler)
        self.handlers[name] = handler

    def sim_port(self, name, factory):
        """Register a simulation-only backend factory for an existing product port."""
        if self.mode is not None or name not in self.factories or name in self.backends or not callable(factory):
            raise Refuse("bad_backend")
        require_sync(factory)
        self.backends[name] = factory

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
        # Only the exact string "1" asks for a simulation, and only the exact
        # strings "0" and "" say "definitely not". Anything else non-empty --
        # `true`, `yes`, a typo -- is refused rather than quietly running live,
        # because a caller who meant to simulate and misspelled it would
        # otherwise get a live run and no error. An earlier version refused on
        # any value at all, including "0", which contradicted `in_sim()`: that
        # reports False for "0" while this refused, so the same environment got
        # two different answers. One rule, one place.
        if os.environ.get(ENV_SIM) not in (None, "", "0"):
            raise Refuse("bad_env")
        if os.environ.get(ENV_CASE):
            raise Refuse("bad_env")
        record = os.environ.get(ENV_RECORD)
        if record not in (None, "0", "1"):
            raise Refuse("bad_env")
        if self.mode is not None:
            raise Refuse("bad_env")
        if self.namespace.startswith("sim-"):
            raise Refuse("namespace")
        if record == "1":
            path = os.environ.get(ENV_ARTIFACT)
            if not path:
                raise Refuse("bad_env")
            self._record = open(path, "a", encoding="ascii", newline="\n")
        self.mode = "live"

    def close(self):
        """Close a live runtime and its optional recording file."""
        with self._lock:
            if self.mode != "live" or self._depth:
                raise Refuse("bad_env")
            if self._record is not None:
                self._record.close()
            self.mode = "closed"

    def deliver(self, name, body):
        with self._lock:
            return self._deliver(name, body)

    def _deliver(self, name, body):
        if self.mode is None:
            raise Refuse("bad_env")
        if self.mode != "live" or self._depth:
            raise Fault("reentrant")
        if type(name) is not str or name not in self.handlers:
            raise Fault("unknown_handler")
        ctx = Ctx(self, name)
        self._depth += 1
        try:
            result = self.handlers[name](ctx, deep_copy(body))
            require_sync_result(result)
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
        return int.from_bytes(os.urandom(U64_BYTES), "little")

    def rand_below(self, n):
        if self.mode != "live":
            raise Fault("bad_value")
        # Live draws are OS bytes. The sim stream is the one with a stability promise.
        from seam.rng import Rng

        return Rng(int.from_bytes(os.urandom(U64_BYTES), "little")).rand_below(n)

    def ident(self, prefix):
        if type(prefix) is not str or not IDENT.match(prefix):
            raise Fault("bad_value")
        return f"{prefix}_{self.rand_u64():0{ID_HEX_WIDTH}x}"

    def emit(self, port, request):
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        if type(port) is not str or port not in self.factories:
            raise Fault("unknown_port")
        request_copy = deep_copy(request)
        try:
            client = self.factories[port].materialize()
            value = client(deep_copy(request_copy))
            require_sync_result(value)
            response = deep_copy(value)
        except PortError as err:
            if self._record is not None:
                self._record_line(port, request_copy, None, error=err.code)
            raise
        if self._record is not None:
            self._record_line(port, request_copy, response)
        return deep_copy(response)

    def _record_line(self, port, request, response, *, error=None):
        from seam.canon import dumps

        recorded_request = request
        recorded_response = response
        if self._redact is not None:
            recorded_request = deep_copy(self._redact(deep_copy(request)))
            if error is None:
                recorded_response = deep_copy(self._redact(deep_copy(response)))
        row = {
            "format": "seam-tape",
            "version": 1 if error is None else 2,
            "at_ns": time.time_ns(),
            "port": port,
            "request": recorded_request,
            "response": recorded_response,
        }
        if error is not None:
            del row["response"]
            row["error"] = error
        line = dumps(row)
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
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        if self._timer_backend is None:
            raise NoTimerBackend()
        if type(handler) is not str or handler not in self.handlers:
            raise Fault("unknown_handler")
        if name is not None and (type(name) is not str or not TERMINAL.match(name)):
            raise Fault("bad_value")
        if name is not None and any(row["name"] == name for row in self._live_timers.values()):
            raise Fault("bad_value")
        self._token_n += 1
        token = f"{TIMER_TOKEN_PREFIX}{self._token_n}"
        body_copy = deep_copy(body)
        self._live_timers[token] = {"status": "armed", "handler": handler, "body": body_copy, "name": name}
        try:
            self._timer_backend(token, at_ns, handler, deep_copy(body_copy), name)
        except BaseException:
            del self._live_timers[token]
            raise
        return token

    def cancel(self, token):
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        if type(token) is not str or token not in self._live_timers:
            raise Fault("unknown_timer")
        row = self._live_timers[token]
        if row["status"] == "fired":
            raise Fault("cancel_fired")
        if row["status"] != "armed":
            raise Fault("unknown_timer")
        row["status"] = "cancelled"

    def fire_timer(self, token):
        """Deliver an armed live timer once; ignore canceled or previously fired callbacks."""
        with self._lock:
            if self.mode != "live" or self._depth:
                raise Fault("reentrant")
            if type(token) is not str or token not in self._live_timers:
                raise Fault("unknown_timer")
            row = self._live_timers[token]
            if row["status"] != "armed":
                return False
            row["status"] = "fired"
            self._deliver(row["handler"], row["body"])
            return True

    def stop(self, name):
        if self.mode != "live" or self._depth == 0:
            raise Fault("bad_value")
        if type(name) is not str or not TERMINAL.match(name):
            raise Fault("bad_value")
        self.stopped = name

    def state_copy(self):
        with self._lock:
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
    return os.environ.get(ENV_ARTIFACT) or os.path.join(os.getcwd(), "seam-artifact.json")


def _emit(path, obj):
    # The artifact write is seam's own I/O, not a handler's, so it runs trusted.
    with trusted():
        write_artifact(path, obj)
    print(os.path.abspath(path), flush=True)


def _require_sim():
    if not _sim_requested():
        raise Refuse("bad_env")
    if "SEAM_RECORD" in os.environ:
        raise Refuse("bad_env")
    if "SEAM_NAMESPACE" not in os.environ or "SEAM_CASE" not in os.environ:
        raise Refuse("bad_env")
    namespace = os.environ.get(ENV_NAMESPACE)
    case = os.environ.get(ENV_CASE)
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
            backends=rt.backends,
        )
    except Refuse as err:
        _emit(path, refused(err.code, meta=err.meta, op=err.op))
        return 3
    _CONSUMED = True
    # The artifact is the one path seam itself writes, so it is allowlisted here.
    install_guards(write_paths=(path, path + ".tmp"))
    result = run_sim(rt, case)
    _emit(path, finish(result, case))
    return exit_code(result)
