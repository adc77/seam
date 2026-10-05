"""Load a case file. Unknown keys are refused. Defaults are filled before the case digest."""

import os
import re
from dataclasses import dataclass, field

from seam.canon import TIMER_TOKEN_PREFIX, UINT64_MAX, digest, loads, walk
from seam.errors import PortError, Refuse
from seam.backend import BackendPort
from seam.dataset import load_datasets
from seam.ports import RecordingPort, ScriptPort, load_tape, resolve_tape, validate_match

MAX_CASE = 4 * 1024 * 1024

# Ceilings on a case file, so a malformed or hostile case cannot make the runner
# do unbounded work before it starts. These bound the *input*; `max_events` and
# `max_port_calls` below bound the *run*, and default to the same numbers.
MAX_ARRIVALS = 100_000
MAX_REPLIES_PER_PORT = 10_000
MAX_ASSERTIONS = 1_000
DEFAULT_MAX_EVENTS = 100_000
DEFAULT_MAX_PORT_CALLS = 10_000

SIM_NS = re.compile(r"^sim-[a-z0-9]([a-z0-9-]{0,60}[a-z0-9])?$")
CASE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TERMINAL = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
GRADER = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*:[A-Za-z_]\w*$")
EPOCH = "1970-01-01T00:00:00Z"
CASE_KEYS = {
    "format",
    "version",
    "name",
    "seed",
    "namespace",
    "clock",
    "initial_state",
    "arrivals",
    "ports",
    "stop",
    "assertions",
    "grader",
    "log_state",
}
#: Ways a run can end that are not faults. A `stopped` assertion may name one of
#: these, and nothing raises them: the loop sets `stop_reason` directly.
STOP_REASONS = {
    "quiescence",
    "deadline",
    "terminal",
}
#: Fault codes. These are raised as `Fault(...)` somewhere in the package, and a
#: `fault_is` or `stopped` assertion may name one. Kept separate from
#: `STOP_REASONS` because the two are different kinds of thing: adding a fault
#: code means raising it, adding a stop reason means setting it.
FAULT_CODES = {
    "real_io",
    "real_clock",
    "unseeded_random",
    "file_read",
    "file_write",
    "file_access",
    "thread",
    "handler_error",
    "tape_mismatch",
    "tape_exhausted",
    "unmatched_port",
    "unknown_port",
    "unknown_handler",
    "unknown_timer",
    "cancel_fired",
    "reentrant",
    "max_events",
    "max_port_calls",
    "ended_before_deadline",
    "ended_before_terminal",
    "unexpected_terminal",
    "clock_backwards",
    "bad_value",
    "grader_error",
    "unsupported_handler",
    "bad_backend",
}


def _bad():
    raise Refuse("bad_case")


def _obj(value, allowed, required):
    if type(value) is not dict or set(value) - allowed or any(key not in value for key in required):
        _bad()


def _int(value):
    if type(value) is not int:
        _bad()
    return value


@dataclass
class Stop:
    when: str
    deadline_ns: int | None
    terminal: str | None
    allow_quiescence: bool
    max_events: int
    max_port_calls: int


@dataclass
class Case:
    name: str
    seed: int
    namespace: str
    start_ns: int
    initial_state: object
    arrivals: list
    ports: dict
    stop: Stop
    assertions: list
    grader: str | None
    log_state: str
    digest: str
    normalized: dict
    meta: dict = field(default_factory=dict)
    config: object = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


def _match(node):
    def on_bad():
        _bad()

    validate_match(node, on_bad)


def _assertion(item):
    if type(item) is not dict or "op" not in item or type(item["op"]) is not str:
        _bad()
    op = item["op"]
    keys = set(item)
    if op == "port_called":
        if not keys <= {"op", "port", "times", "match"} or not {"op", "port", "times"} <= keys:
            _bad()
        if type(item["port"]) is not str or not IDENT.match(item["port"]) or type(item["times"]) is not int or item["times"] < 0:
            _bad()
        if "match" in item:
            _match(item["match"])
    elif op == "port_not_called":
        if keys != {"op", "port"} or type(item["port"]) is not str or not IDENT.match(item["port"]):
            _bad()
    elif op == "port_response":
        if keys != {"op", "port", "i", "match"}:
            _bad()
        if type(item["port"]) is not str or not IDENT.match(item["port"]) or type(item["i"]) is not int or item["i"] < 0:
            _bad()
        _match(item["match"])
    elif op == "event_count":
        if keys != {"op", "handler", "times"}:
            _bad()
        if type(item["handler"]) is not str or not IDENT.match(item["handler"]) or type(item["times"]) is not int or item["times"] < 0:
            _bad()
    elif op == "timer_outcome":
        if "outcome" not in item or item["outcome"] not in ("fired", "cancelled", "dropped"):
            _bad()
        named = "name" in item
        tokened = "token" in item
        if named == tokened:
            _bad()
        if keys != ({"op", "outcome", "name"} if named else {"op", "outcome", "token"}):
            _bad()
        if named and (type(item["name"]) is not str or not TERMINAL.match(item["name"])):
            _bad()
        if tokened and (type(item["token"]) is not str or not item["token"].startswith(TIMER_TOKEN_PREFIX)):
            _bad()
    elif op == "state_is":
        if keys != {"op", "path", "value"} or type(item["path"]) is not str:
            _bad()
    elif op == "stopped":
        if not keys <= {"op", "reason", "terminal"} or "reason" not in keys:
            _bad()
        # A `stopped` assertion may name a stop reason or a fault code: the loop
        # reports either through `stop_reason`.
        if type(item["reason"]) is not str or item["reason"] not in (STOP_REASONS | FAULT_CODES):
            _bad()
        if "terminal" in item and (type(item["terminal"]) is not str or not TERMINAL.match(item["terminal"])):
            _bad()
    elif op == "digest_is":
        if keys != {"op", "sha256"} or type(item["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            _bad()
    elif op == "fault_is":
        if keys != {"op", "code"} or type(item["code"]) is not str:
            _bad()
    else:
        _bad()


def normalize(raw, *, ports, handlers, namespace, backends=()):
    if type(raw) is not dict:
        _bad()
    # Seed is allowed to be unsigned 64-bit, so it is checked before the int64 walk.
    if "seed" not in raw or type(raw["seed"]) is not int or not 0 <= raw["seed"] <= UINT64_MAX:
        _bad()
    body = {key: value for key, value in raw.items() if key != "seed"}
    walk(body, _bad)
    version = raw.get("version")
    if type(version) is not int or version not in (1, 2):
        _bad()
    keys = CASE_KEYS if version == 1 else CASE_KEYS | {"datasets", "config"}
    _obj(raw, keys, {"format", "version", "name", "seed", "namespace", "clock", "initial_state", "arrivals", "ports", "stop"})
    if raw["format"] != "seam-case":
        _bad()
    if type(raw["name"]) is not str or not CASE_NAME.match(raw["name"]):
        _bad()
    if type(raw["namespace"]) is not str or not SIM_NS.match(raw["namespace"]) or raw["namespace"] != namespace:
        raise Refuse("namespace")
    clock = raw["clock"]
    _obj(clock, {"start_ns", "epoch"}, {"start_ns", "epoch"})
    if clock["epoch"] != EPOCH or type(clock["start_ns"]) is not int or clock["start_ns"] < 0:
        _bad()
    if type(raw["arrivals"]) is not list or len(raw["arrivals"]) > MAX_ARRIVALS:
        _bad()
    arrivals = []
    for item in raw["arrivals"]:
        _obj(item, {"at_ns", "handler", "body"}, {"at_ns", "handler", "body"})
        if type(item["at_ns"]) is not int:
            _bad()
        if item["at_ns"] < clock["start_ns"]:
            raise Refuse("arrival_before_start")
        if type(item["handler"]) is not str or not IDENT.match(item["handler"]) or item["handler"] not in handlers:
            _bad()
        arrivals.append({"at_ns": item["at_ns"], "handler": item["handler"], "body": item["body"]})
    if type(raw["ports"]) is not dict:
        _bad()
    have = set(ports)
    want = set(raw["ports"])
    if want - have:
        raise Refuse("unknown_port_in_case")
    if have - want:
        raise Refuse("unscripted_port")
    norm_ports = {}
    for name, spec in raw["ports"].items():
        if type(name) is not str or not IDENT.match(name) or type(spec) is not dict or "mode" not in spec:
            _bad()
        mode = spec["mode"]
        if mode == "generator":
            raise Refuse("unsupported")
        if mode == "backend":
            if version != 2 or name not in backends:
                raise Refuse("bad_backend")
            _obj(spec, {"mode", "dataset"}, {"mode", "dataset"})
            dataset = spec["dataset"]
            if type(dataset) is not str or not IDENT.fullmatch(dataset):
                raise Refuse("bad_dataset")
            norm_ports[name] = {"mode": "backend", "dataset": dataset}
        elif mode == "script":
            _obj(spec, {"mode", "unmatched", "replies"}, {"mode", "replies"})
            unmatched = spec.get("unmatched", "fail")
            if unmatched != "fail":
                if type(unmatched) is not dict or set(unmatched) != {"response"}:
                    _bad()
            replies_in = spec["replies"]
            if type(replies_in) is not list or len(replies_in) > MAX_REPLIES_PER_PORT:
                _bad()
            replies = []
            for reply in replies_in:
                allowed = {"match", "response", "repeat"} if version == 1 else {"match", "response", "repeat", "error"}
                _obj(reply, allowed, {"match"})
                if ("response" in reply) == ("error" in reply):
                    _bad()
                if "error" in reply and (type(reply["error"]) is not str or not IDENT.fullmatch(reply["error"])):
                    _bad()
                repeat = reply.get("repeat", 1)
                if repeat != "forever" and (type(repeat) is not int or repeat < 1):
                    _bad()
                _match(reply["match"])
                value_key = "response" if "response" in reply else "error"
                replies.append({"match": reply["match"], value_key: reply[value_key], "repeat": repeat})
            norm_ports[name] = {"mode": "script", "unmatched": unmatched, "replies": replies}
        elif mode == "recording":
            if "cutoff_ns" not in spec or type(spec.get("cutoff_ns")) is not int:
                raise Refuse("cutoff_required")
            _obj(spec, {"mode", "tape", "cutoff_ns", "policy"}, {"mode", "tape", "cutoff_ns", "policy"})
            if spec["policy"] != "ordered" or type(spec["tape"]) is not str:
                _bad()
            norm_ports[name] = {
                "mode": "recording",
                "tape": spec["tape"],
                "cutoff_ns": spec["cutoff_ns"],
                "policy": "ordered",
            }
        else:
            _bad()
    stop_in = raw["stop"]
    _obj(
        stop_in,
        {"when", "deadline_ns", "terminal", "allow_quiescence", "max_events", "max_port_calls"},
        {"when"},
    )
    when = stop_in["when"]
    if type(when) is not str or when not in ("quiescence", "deadline", "terminal"):
        _bad()
    allow = stop_in.get("allow_quiescence", False)
    if type(allow) is not bool:
        _bad()
    max_events = stop_in.get("max_events", DEFAULT_MAX_EVENTS)
    max_calls = stop_in.get("max_port_calls", DEFAULT_MAX_PORT_CALLS)
    if type(max_events) is not int or type(max_calls) is not int or max_events < 1 or max_calls < 1:
        _bad()
    deadline = stop_in.get("deadline_ns")
    terminal = stop_in.get("terminal")
    if when == "deadline" and "deadline_ns" not in stop_in:
        _bad()
    if when == "terminal" and "terminal" not in stop_in:
        _bad()
    if deadline is not None and (type(deadline) is not int or deadline < clock["start_ns"]):
        _bad()
    if terminal is not None and (type(terminal) is not str or not TERMINAL.match(terminal)):
        _bad()
    assertions = raw.get("assertions", [])
    if type(assertions) is not list or len(assertions) > MAX_ASSERTIONS:
        _bad()
    for item in assertions:
        _assertion(item)
    log_state = raw.get("log_state", "on_change")
    if type(log_state) is not str or log_state not in ("on_change", "every_event", "end_only"):
        _bad()
    grader = raw.get("grader")
    if grader is not None and (type(grader) is not str or not GRADER.match(grader)):
        _bad()
    norm = {
        "format": "seam-case",
        "version": version,
        "name": raw["name"],
        "seed": raw["seed"],
        "namespace": raw["namespace"],
        "clock": {"start_ns": clock["start_ns"], "epoch": EPOCH},
        "initial_state": raw["initial_state"],
        "arrivals": arrivals,
        "ports": norm_ports,
        "stop": {
            "when": when,
            "allow_quiescence": allow,
            "max_events": max_events,
            "max_port_calls": max_calls,
        },
        "assertions": assertions,
        "log_state": log_state,
    }
    if deadline is not None:
        norm["stop"]["deadline_ns"] = deadline
    if terminal is not None:
        norm["stop"]["terminal"] = terminal
    if grader is not None:
        norm["grader"] = grader
    if version == 2:
        norm["datasets"] = raw.get("datasets", {})
        norm["config"] = raw.get("config", {})
    return norm


def _compile(path, norm, backends, datasets):
    compiled = {}
    for name, spec in norm["ports"].items():
        if spec["mode"] == "script":
            replies = [
                {**reply, "left": reply["repeat"]}
                for reply in spec["replies"]
            ]
            compiled[name] = ScriptPort(replies, spec["unmatched"])
        elif spec["mode"] == "backend":
            dataset = spec["dataset"]
            if dataset not in datasets:
                raise Refuse("bad_dataset")
            compiled[name] = BackendPort(backends[name], datasets[dataset], norm["config"])
        else:
            full = resolve_tape(path, spec["tape"])
            visible = load_tape(full, name, spec["cutoff_ns"], allow_errors=norm["version"] == 2)
            compiled[name] = RecordingPort(visible)
    return compiled


def load_case(path, *, ports, handlers, namespace, backends=None):
    if type(namespace) is not str or not SIM_NS.match(namespace):
        raise Refuse("namespace")
    if not os.path.isfile(path) or os.path.getsize(path) > MAX_CASE:
        raise Refuse("bad_case")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        raise Refuse("bad_case") from None
    try:
        raw = loads(text)
    except (ValueError, RecursionError):
        raise Refuse("bad_case") from None
    if backends is None:
        backends = {}
    norm = normalize(raw, ports=set(ports), handlers=set(handlers), namespace=namespace, backends=set(backends))
    case_digest = digest(norm)
    compiled = {}
    try:
        datasets = load_datasets(path, norm.get("datasets", {}), norm["clock"]["start_ns"])
        compiled = _compile(path, norm, backends, datasets)
    except Refuse as err:
        err.meta.update(
            name=norm["name"],
            namespace=norm["namespace"],
            seed=norm["seed"],
            case_digest=case_digest,
            start_ns=norm["clock"]["start_ns"],
        )
        raise
    stop_n = norm["stop"]
    provenance = {}
    if norm["version"] == 2:
        provenance["datasets"] = norm["datasets"]
    tapes = {}
    for name, port in compiled.items():
        if isinstance(port, RecordingPort):
            visible = [(request, {"error": response.code} if isinstance(response, PortError) else {"response": response})
                       for request, response in port.visible]
            tapes[name] = digest(visible)
    if tapes:
        provenance["tapes"] = tapes
    return Case(
        name=norm["name"],
        seed=norm["seed"],
        namespace=norm["namespace"],
        start_ns=norm["clock"]["start_ns"],
        initial_state=norm["initial_state"],
        arrivals=norm["arrivals"],
        ports=compiled,
        stop=Stop(
            when=stop_n["when"],
            deadline_ns=stop_n.get("deadline_ns"),
            terminal=stop_n.get("terminal"),
            allow_quiescence=stop_n["allow_quiescence"],
            max_events=stop_n["max_events"],
            max_port_calls=stop_n["max_port_calls"],
        ),
        assertions=norm["assertions"],
        grader=norm.get("grader"),
        log_state=norm["log_state"],
        digest=case_digest,
        normalized=norm,
        config=norm.get("config", {}),
        provenance=provenance,
        meta={"name": norm["name"], "namespace": norm["namespace"], "seed": norm["seed"], "case_digest": case_digest},
    )
