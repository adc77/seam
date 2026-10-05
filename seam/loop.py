"""Single-threaded scheduler. Schedules made by a handler wait until that handler returns."""

from dataclasses import dataclass

from seam.canon import ID_HEX_WIDTH, INT64_MAX, MAX_STRING, TIMER_TOKEN_PREFIX, deep_copy, dumps
from seam.case import TERMINAL
from seam.clock import VirtualClock, format_utc
from seam.ctx import Ctx, assert_int, replace_at
from seam.errors import Fault, PortError, Refuse, fault_scope
from seam.guard import _handler_scope
from seam.queue import Queue
from seam.backend import BackendPort
from seam.rng import Rng


@dataclass
class Result:
    name: str
    namespace: str
    seed: int
    case_digest: str
    start_ns: int
    end_ns: int
    events: list
    port_calls: list
    state_snapshots: list
    final_state: object
    timers: list
    stop_reason: str | None
    terminal: str | None = None
    loop_fault: Fault | None = None
    digest: str | None = None
    status: str | None = None
    assertions: list | None = None
    grader_failures: list | None = None
    post_fault: Fault | None = None
    fs_reads: list | None = None
    backend_states: dict | None = None
    provenance: dict | None = None
    version: int = 1


class Engine:
    def __init__(self, rt, case):
        self.rt = rt
        self.case = case
        self.handlers = rt.handlers
        self.clock = VirtualClock(case.start_ns)
        self.rng = Rng(case.seed)
        self.queue = Queue()
        self.state = deep_copy(case.initial_state)
        self._finger = dumps(self.state)
        self.events = []
        self.port_calls = []
        self.snapshots = []
        self._timers = []
        self._by_token = {}
        self._token_n = 0
        self._during = None
        self._depth = 0
        self.delivered = 0
        self.stop_reason = None
        self.terminal = None
        self._stop_flag = False
        self._stop_name = None
        self.fault = None
        self.result = None

    def fail(self, fault):
        if self.fault is None:
            self.fault = fault
            self.stop_reason = fault.code

    def _handler_fault(self, fault):
        fault.during = self._during
        self.fail(fault)

    def _event(self, row):
        row["i"] = len(self.events)
        self.events.append(row)
        return row

    def now(self):
        return self.clock.t

    def utc(self):
        return format_utc(self.clock.t)

    def rand_u64(self):
        return self.rng.rand_u64()

    def rand_below(self, n):
        return self.rng.rand_below(n)

    def ident(self, prefix):
        from seam.case import IDENT

        if type(prefix) is not str or not IDENT.match(prefix):
            raise Fault("bad_value")
        return f"{prefix}_{self.rng.rand_u64():0{ID_HEX_WIDTH}x}"

    @property
    def namespace(self):
        return self.case.namespace

    @property
    def config(self):
        return self.case.config

    def state_copy(self):
        return deep_copy(self.state)

    def set_state(self, value):
        self._live_only()
        self.state = replace_at(self.state, "", value)

    def patch(self, path, value):
        self._live_only()
        self.state = replace_at(self.state, path, value)

    def stamp(self, row):
        if type(row) is not dict:
            raise Fault("bad_value")
        out = deep_copy(row)
        out["_seam_ns"] = self.case.namespace
        return out

    def _live_only(self):
        if self._depth == 0:
            raise Fault("bad_value")

    def emit(self, port, request):
        self._live_only()
        if type(port) is not str or port not in self.case.ports:
            raise Fault("unknown_port", during=self._during)
        if len(self.port_calls) >= self.case.stop.max_port_calls:
            raise Fault("max_port_calls", during=self._during)
        request_copy = deep_copy(request)
        script = self.case.ports[port]
        try:
            response = script.call(deep_copy(request_copy))
        except PortError as err:
            self._log_call(port, request_copy, None, script.source)
            self.port_calls[-1]["error"] = err.code
            raise
        except Fault as err:
            if isinstance(script, BackendPort) or err.code in ("unmatched_port", "tape_mismatch", "tape_exhausted"):
                self._log_call(port, request_copy, None, script.source)
            raise Fault(err.code, op=err.op, during=self._during, exc_type=err.exc_type) from None
        stored = deep_copy(response)
        self._log_call(port, request_copy, stored, script.source)
        return deep_copy(stored)

    def _log_call(self, port, request, response, source):
        self.port_calls.append(
            {
                "i": len(self.port_calls),
                "during": self._during,
                "at_ns": self.clock.t,
                "port": port,
                "request": request,
                "response": response,
                "source": source,
            }
        )

    def schedule_after(self, delay_ns, handler, body, name=None):
        assert_int(delay_ns, minimum=0)
        at = self.clock.t + delay_ns
        if at > INT64_MAX:
            raise Fault("bad_value")
        return self.schedule_at(at, handler, body, name=name)

    def schedule_at(self, at_ns, handler, body, name=None):
        self._live_only()
        assert_int(at_ns, minimum=0)
        if at_ns < self.clock.t:
            raise Fault("bad_value")
        if type(handler) is not str or handler not in self.handlers:
            raise Fault("unknown_handler", during=self._during)
        if name is not None:
            if type(name) is not str or not TERMINAL.match(name):
                raise Fault("bad_value")
            if any(row.get("name") == name for row in self._timers):
                raise Fault("bad_value")
        body_copy = deep_copy(body)
        self._token_n += 1
        token = f"{TIMER_TOKEN_PREFIX}{self._token_n}"
        seq = self.queue.push_timer(at_ns, handler, body_copy, token)
        row = {
            "token": token,
            "handler": handler,
            "fire_at_ns": at_ns,
            "outcome": "armed",
            "seq": seq,
        }
        if name is not None:
            row["name"] = name
        self._timers.append(row)
        self._by_token[token] = row
        logged = {
            "kind": "schedule",
            "during": self._during,
            "at_ns": self.clock.t,
            "fire_at_ns": at_ns,
            "handler": handler,
            "token": token,
        }
        if name is not None:
            logged["name"] = name
        self._event(logged)
        return token

    def cancel(self, token):
        self._live_only()
        if type(token) is not str or token not in self._by_token:
            raise Fault("unknown_timer", during=self._during)
        row = self._by_token[token]
        if row["outcome"] == "fired":
            raise Fault("cancel_fired", during=self._during)
        if row["outcome"] != "armed":
            raise Fault("unknown_timer", during=self._during)
        row["outcome"] = "cancelled"
        self.queue.cancel(row["seq"], token)
        self._event(
            {
                "kind": "cancel",
                "during": self._during,
                "at_ns": self.clock.t,
                "token": token,
            }
        )

    def stop(self, name):
        self._live_only()
        if type(name) is not str or not TERMINAL.match(name):
            raise Fault("bad_value")
        if self._stop_flag:
            raise Fault("bad_value")
        self._stop_flag = True
        self._stop_name = name
        self._event(
            {
                "kind": "stop",
                "during": self._during,
                "at_ns": self.clock.t,
                "name": name,
            }
        )

    def _snapshot(self, after):
        if self.case.log_state == "end_only":
            return
        try:
            blob = dumps(self.state)
        except (TypeError, ValueError):
            self.fail(Fault("bad_value", during=after))
            return
        if len(blob.encode("ascii")) > MAX_STRING:
            self.fail(Fault("bad_value", during=after))
            return
        if self.case.log_state == "on_change" and blob == self._finger:
            return
        self._finger = blob
        self.snapshots.append({"after": after, "state": deep_copy(self.state)})

    def _set_depth(self, depth):
        self._depth = depth
        self.rt._depth = depth

    def _deliver(self, at_ns, handler, body, token):
        body_copy = deep_copy(body)
        event = self._event(
            {
                "kind": "deliver",
                "handler": handler,
                "at_ns": at_ns,
                "body": body_copy,
                "status": "ok",
            }
        )
        self._during = event["i"]
        if token is not None:
            self._by_token[token]["outcome"] = "fired"
        ctx = Ctx(self, handler)
        self._set_depth(1)
        try:
            # Inside this scope the filesystem policy refuses to be widened and
            # `trusted()` is refused, so a handler cannot grant itself access.
            with _handler_scope(), fault_scope(self._handler_fault):
                result = self.handlers[handler](ctx, deep_copy(body_copy))
                from seam.runtime import require_sync_result

                require_sync_result(result)
        except Fault as err:
            event["status"] = "error"
            if err.during is None:
                err.during = event["i"]
            self.fail(err)
        except Refuse as err:
            event["status"] = "error"
            self.fail(Fault(err.code, op=err.op, during=event["i"]))
        except Exception as err:
            event["status"] = "error"
            self.fail(Fault("handler_error", during=event["i"], exc_type=type(err).__name__))
        except BaseException as err:
            event["status"] = "error"
            self.fail(Fault("handler_error", during=event["i"], exc_type=type(err).__name__))
        finally:
            self._set_depth(0)
            self.queue.flush()
        if self.fault is not None:
            event["status"] = "error"
        self._snapshot(event["i"])

    def _finish_empty(self):
        stop = self.case.stop
        if stop.when == "quiescence" or stop.allow_quiescence:
            self.stop_reason = "quiescence"
            return
        if stop.when == "deadline":
            self.fail(Fault("ended_before_deadline"))
            return
        self.fail(Fault("ended_before_terminal"))

    def _resolve_stop(self, during):
        stop = self.case.stop
        if stop.when == "terminal" and self._stop_name == stop.terminal:
            self.stop_reason = "terminal"
            self.terminal = self._stop_name
            return
        self.fail(Fault("unexpected_terminal", during=during))

    def _timer_view(self):
        view = []
        for row in self._timers:
            outcome = "dropped" if row["outcome"] == "armed" else row["outcome"]
            item = {
                "token": row["token"],
                "handler": row["handler"],
                "fire_at_ns": row["fire_at_ns"],
                "outcome": outcome,
            }
            if "name" in row:
                item["name"] = row["name"]
            view.append(item)
        return view

    def _seal_result(self):
        backend_states = {}
        try:
            with _handler_scope(), fault_scope(self._handler_fault):
                for name, port in self.case.ports.items():
                    if isinstance(port, BackendPort):
                        backend_states[name] = port.state()
        except Fault as err:
            self.fail(err)
        except BaseException as err:
            self.fail(Fault("bad_backend", exc_type=type(err).__name__))
        self.result = Result(
            name=self.case.name,
            namespace=self.case.namespace,
            seed=self.case.seed,
            case_digest=self.case.digest,
            start_ns=self.case.start_ns,
            end_ns=self.clock.t,
            events=self.events,
            port_calls=self.port_calls,
            state_snapshots=self.snapshots,
            final_state=deep_copy(self.state),
            timers=self._timer_view(),
            stop_reason=self.stop_reason,
            terminal=self.terminal,
            loop_fault=self.fault,
            backend_states=backend_states,
            provenance=self.case.provenance,
            version=self.case.normalized["version"],
        )

    def run(self):
        for item in self.case.arrivals:
            self.queue.push_arrival(item["at_ns"], item["handler"], item["body"])
        while self.fault is None:
            nxt = self.queue.peek()
            if nxt is None:
                self._finish_empty()
                break
            at_ns, _seq, handler, body, token = nxt
            deadline = self.case.stop.deadline_ns
            if deadline is not None and at_ns > deadline:
                self.stop_reason = "deadline"
                break
            if self.delivered >= self.case.stop.max_events:
                self.fail(Fault("max_events"))
                break
            self.queue.pop()
            if at_ns < self.clock.t:
                self.fail(Fault("clock_backwards"))
                break
            self.clock.jump(at_ns)
            self.delivered += 1
            self._deliver(at_ns, handler, body, token)
            if self.fault is not None:
                break
            if self._stop_flag:
                self._resolve_stop(self._during)
                break
        if self.stop_reason is None and self.fault is not None:
            self.stop_reason = self.fault.code
        self._seal_result()
        return self.result


def run_sim(rt, case):
    if rt.mode == "live":
        raise Refuse("bad_env")
    rt.mode = "sim"
    rt.namespace = case.namespace
    return Engine(rt, case).run()
