"""Validated, content-addressed checkpoints captured between simulation deliveries."""

import hashlib
import os
from pathlib import Path
import platform
import sys

from seam.canon import INT64_MAX, deep_copy, digest, dumps, equal, loads, walk
from seam.ctx import MAX_STATE
from seam.dataset import SHA256
from seam.errors import Fault, PortError, Refuse
from seam.ports import RecordingPort, ScriptPort, resolve_tape
from seam.version import __version__

MAX_CHECKPOINT = 32 * 1024 * 1024
DOCUMENT_KEYS = {"format", "version", "sdk_version", "identity", "workload_digest", "payload", "digest"}
PAYLOAD_KEYS = {"at_ns", "rng_counter", "state", "queue", "timers", "token_n", "events",
                "port_calls", "state_snapshots", "delivered", "delivered_sequences", "ports", "worlds"}


def _bad():
    raise Refuse("bad_checkpoint")


def _object(value, keys):
    if type(value) is not dict or set(value) != keys:
        _bad()


def _integer(value, minimum=0, maximum=INT64_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        _bad()


def product_identity():
    """Bind checkpoints to product and SDK Python sources before guards install."""
    from seam.runner import _product_digest

    product = os.environ.get("SEAM_PRODUCT_SHA256")
    pythonpath = os.environ.get("PYTHONPATH", "")
    if product is None:
        spec = getattr(sys.modules["__main__"], "__spec__", None)
        if spec is None:
            raise Refuse("checkpoint_identity")
        product = _product_digest(spec.name, pythonpath)
    if type(product) is not str or not SHA256.fullmatch(product):
        raise Refuse("checkpoint_identity")
    environment = os.environ.get("SEAM_ENVIRONMENT_SHA256")
    if environment is None:
        environment = digest({key: value for key, value in os.environ.items() if not key.startswith("SEAM_")})
    if type(environment) is not str or not SHA256.fullmatch(environment):
        raise Refuse("checkpoint_identity")
    return {"product_sha256": product, "sdk_sha256": _product_digest("seam", pythonpath),
            "environment_sha256": environment, "python": platform.python_version()}


def validate_document(document):
    """Validate the closed envelope and checksum independently of a destination case."""
    try:
        _object(document, DOCUMENT_KEYS)
        walk(document, _bad)
        if (document["format"] != "seam-checkpoint" or type(document["version"]) is not int
                or document["version"] != 1 or document["sdk_version"] != __version__):
            _bad()
        _object(document["identity"], {"product_sha256", "sdk_sha256", "environment_sha256", "python"})
        if type(document["identity"]["python"]) is not str or document["identity"]["python"] != platform.python_version():
            _bad()
        hashes = [document["identity"][key] for key in ("product_sha256", "sdk_sha256", "environment_sha256")]
        for value in (*hashes, document["workload_digest"], document["digest"]):
            if type(value) is not str or not SHA256.fullmatch(value):
                _bad()
        _object(document["payload"], PAYLOAD_KEYS)
        if digest({key: value for key, value in document.items() if key != "digest"}) != document["digest"]:
            _bad()
        if len(dumps(document).encode("ascii")) > MAX_CHECKPOINT:
            _bad()
    except (TypeError, ValueError, RecursionError):
        _bad()
    return document


def _read(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_CHECKPOINT + 1)
        if len(data) > MAX_CHECKPOINT:
            _bad()
        document = loads(data)
        validate_document(document)
    except (OSError, ValueError, RecursionError, Refuse):
        _bad()
    return data, document


def checkpoint_ref(path):
    """Pin a checkpoint file stored beside its destination case."""
    data, _ = _read(path)
    return {"path": Path(path).name, "sha256": hashlib.sha256(data).hexdigest()}


def write_checkpoint(path, document):
    """Atomically write a validated checkpoint with owner-only permissions."""
    validate_document(document)
    path = os.fspath(path)
    tmp = path + ".tmp"
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(dumps(document) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_checkpoint(case_path, ref, case, identity):
    _object(ref, {"path", "sha256"})
    if type(ref["sha256"]) is not str or not SHA256.fullmatch(ref["sha256"]):
        _bad()
    data, document = _read(resolve_tape(case_path, ref["path"]))
    if hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise Refuse("checkpoint_changed")
    if document["workload_digest"] != case.workload_digest or document["identity"] != identity:
        raise Refuse("checkpoint_mismatch")
    try:
        validate_payload(document["payload"], case)
    except (TypeError, ValueError, RecursionError):
        _bad()
    return document


def validate_payload(payload, case):
    """Check counters, history, cursors, and pending work before running product callbacks."""
    _object(payload, PAYLOAD_KEYS)
    _integer(payload["at_ns"], case.start_ns)
    if case.stop.deadline_ns is not None and payload["at_ns"] > case.stop.deadline_ns:
        _bad()
    _integer(payload["rng_counter"])
    _integer(payload["token_n"])
    _integer(payload["delivered"], maximum=case.stop.max_events)
    if len(dumps(payload["state"]).encode("ascii")) > MAX_STATE:
        _bad()
    for key in ("events", "port_calls", "state_snapshots", "timers", "delivered_sequences"):
        if type(payload[key]) is not list:
            _bad()
    if len(payload["port_calls"]) > case.stop.max_port_calls:
        _bad()
    events = payload["events"]
    deliveries = []
    schedules = {}
    cancelled = set()
    previous_at = case.start_ns
    for index, event in enumerate(events):
        if type(event) is not dict or type(event.get("i")) is not int or event["i"] != index:
            _bad()
        if event.get("kind") not in ("deliver", "schedule", "cancel"):
            _bad()
        _integer(event.get("at_ns"), case.start_ns, payload["at_ns"])
        if event["at_ns"] < previous_at:
            _bad()
        previous_at = event["at_ns"]
        if event["kind"] == "deliver":
            _object(event, {"i", "kind", "handler", "at_ns", "body", "status"})
            if event["status"] != "ok":
                _bad()
            deliveries.append(event)
        else:
            keys = {"i", "kind", "during", "at_ns", "token"}
            if event["kind"] == "schedule":
                keys |= {"fire_at_ns", "handler"} | ({"name"} if "name" in event else set())
            _object(event, keys)
            _integer(event["during"], maximum=index - 1)
            if (events[event["during"]]["kind"] != "deliver"
                    or events[event["during"]]["at_ns"] != event["at_ns"]
                    or type(event["token"]) is not str):
                _bad()
            if event["kind"] == "schedule":
                if event["token"] in schedules:
                    _bad()
                schedules[event["token"]] = event
            else:
                if event["token"] not in schedules or event["token"] in cancelled:
                    _bad()
                cancelled.add(event["token"])
    if len(deliveries) != payload["delivered"]:
        _bad()
    if not deliveries or deliveries[-1]["at_ns"] != payload["at_ns"]:
        _bad()
    delivered = payload["delivered_sequences"]
    if len(delivered) != len(deliveries) or any(type(seq) is not int for seq in delivered):
        _bad()
    if len(set(delivered)) != len(delivered):
        _bad()
    for index, call in enumerate(payload["port_calls"]):
        if (type(call) is not dict or type(call.get("i")) is not int or call["i"] != index
                or type(call.get("port")) is not str or call["port"] not in case.ports):
            _bad()
        _object(call, {"i", "during", "at_ns", "port", "request", "response", "source"}
                | ({"error"} if "error" in call else set()))
        _integer(call["during"], maximum=len(events) - 1)
        if events[call["during"]]["kind"] != "deliver":
            _bad()
        if call["at_ns"] != events[call["during"]]["at_ns"] or call["source"] != case.ports[call["port"]].source:
            _bad()
        if "error" in call:
            from seam.errors import ERROR_CODE

            if type(call["error"]) is not str or not ERROR_CODE.fullmatch(call["error"]) or call["response"] is not None:
                _bad()
    for snapshot in payload["state_snapshots"]:
        _object(snapshot, {"after", "state"})
        _integer(snapshot["after"], maximum=len(events) - 1)
        if events[snapshot["after"]]["kind"] != "deliver" or len(dumps(snapshot["state"]).encode("ascii")) > MAX_STATE:
            _bad()
    queue = payload["queue"]
    _object(queue, {"next_seq", "entries"})
    _integer(queue["next_seq"])
    if queue["next_seq"] != len(case.arrivals) + payload["token_n"] or type(queue["entries"]) is not list:
        _bad()
    queued = {}
    previous = None
    for entry in queue["entries"]:
        if type(entry) is not list or len(entry) != 5:
            _bad()
        at, seq, handler, body, token = entry
        _integer(at, payload["at_ns"])
        _integer(seq, maximum=queue["next_seq"] - 1)
        if seq in queued or (previous is not None and (at, seq) <= previous):
            _bad()
        previous = (at, seq)
        if type(handler) is not str or handler not in case.handlers:
            _bad()
        queued[seq] = entry
        if seq < len(case.arrivals):
            arrival = case.arrivals[seq]
            if token is not None or not equal(entry[:1] + entry[2:4], [arrival["at_ns"], arrival["handler"], arrival["body"]]):
                _bad()
        elif type(token) is not str:
            _bad()
    if set(queued) & set(delivered):
        _bad()
    for seq in delivered:
        _integer(seq, maximum=queue["next_seq"] - 1)
    if not set(range(len(case.arrivals))) <= set(queued) | set(delivered):
        _bad()
    if len(payload["timers"]) != payload["token_n"]:
        _bad()
    timers = {}
    names = set()
    for index, timer in enumerate(payload["timers"]):
        _object(timer, {"token", "handler", "fire_at_ns", "outcome", "seq"} | ({"name"} if "name" in timer else set()))
        _integer(timer["seq"])
        if timer["token"] != f"t{index + 1}" or timer["seq"] != len(case.arrivals) + index:
            _bad()
        if type(timer["handler"]) is not str or timer["handler"] not in case.handlers:
            _bad()
        _integer(timer["fire_at_ns"], case.start_ns)
        schedule = schedules.get(timer["token"])
        if schedule is None or not equal(
            {key: schedule[key] for key in ("token", "handler", "fire_at_ns", "name") if key in schedule},
            {key: timer[key] for key in ("token", "handler", "fire_at_ns", "name") if key in timer},
        ) or timer["fire_at_ns"] < schedule["at_ns"]:
            _bad()
        if (timer["outcome"] == "cancelled") != (timer["token"] in cancelled):
            _bad()
        seq = timer["seq"]
        if timer["outcome"] == "armed":
            if seq not in queued or not equal([queued[seq][0], queued[seq][2], queued[seq][4]],
                                               [timer["fire_at_ns"], timer["handler"], timer["token"]]):
                _bad()
        elif timer["outcome"] == "fired":
            if seq not in delivered:
                _bad()
        elif timer["outcome"] == "cancelled":
            if seq in queued or seq in delivered:
                _bad()
        else:
            _bad()
        if "name" in timer:
            from seam.case import TERMINAL

            name = timer["name"]
            if type(name) is not str or not TERMINAL.fullmatch(name) or name in names:
                _bad()
            names.add(name)
        timers[seq] = timer
    if len(schedules) != len(timers):
        _bad()
    for seq, event in zip(delivered, deliveries):
        if seq < len(case.arrivals):
            arrival = case.arrivals[seq]
            if not equal([event["at_ns"], event["handler"], event["body"]],
                         [arrival["at_ns"], arrival["handler"], arrival["body"]]):
                _bad()
        elif seq not in timers or (event["at_ns"], event["handler"]) != (timers[seq]["fire_at_ns"], timers[seq]["handler"]):
            _bad()
    expected_ports = {name for name, port in case.ports.items() if isinstance(port, (ScriptPort, RecordingPort))}
    if type(payload["ports"]) is not dict or set(payload["ports"]) != expected_ports:
        _bad()
    calls_by_port = {name: [] for name in expected_ports}
    for call in payload["port_calls"]:
        if call["port"] in calls_by_port:
            calls_by_port[call["port"]].append(call)
    for name, state in payload["ports"].items():
        port = case.ports[name]
        if isinstance(port, ScriptPort):
            _object(state, {"left"})
            if type(state["left"]) is not list or len(state["left"]) != len(port.replies):
                _bad()
            for left, reply in zip(state["left"], port.replies):
                if reply["repeat"] == "forever":
                    if left != "forever":
                        _bad()
                else:
                    _integer(left, maximum=reply["repeat"])
            replay = ScriptPort([{**deep_copy(reply), "left": reply["repeat"]} for reply in port.replies], port.unmatched)
            for call in calls_by_port[name]:
                _validate_replayed_call(replay, call)
            if not equal(state["left"], [reply["left"] for reply in replay.replies]):
                _bad()
        else:
            _object(state, {"cursor"})
            _integer(state["cursor"], maximum=len(port.visible))
            replay = RecordingPort(port.visible)
            for call in calls_by_port[name]:
                _validate_replayed_call(replay, call)
            if state["cursor"] != replay.cursor:
                _bad()
    if type(payload["worlds"]) is not dict or set(payload["worlds"]) != set(case.worlds):
        _bad()
    for state in payload["worlds"].values():
        if type(state) is not dict or type(state.get("initialized")) is not bool:
            _bad()
        if state["initialized"]:
            _object(state, {"initialized", "bootstrap", "snapshot"})
            _object(state["bootstrap"], {"at_ns", "rng_counter"})
            _integer(state["bootstrap"]["at_ns"], case.start_ns, payload["at_ns"])
            _integer(state["bootstrap"]["rng_counter"], maximum=payload["rng_counter"])
        else:
            _object(state, {"initialized"})


def _validate_replayed_call(port, call):
    try:
        response = port.call(call["request"])
    except PortError as err:
        if call.get("error") != err.code or call["response"] is not None:
            _bad()
    except Fault:
        _bad()
    else:
        if "error" in call or not equal(response, call["response"]):
            _bad()


def capture(engine, snapshots):
    ports = {}
    for name, port in engine.case.ports.items():
        if isinstance(port, ScriptPort):
            ports[name] = {"left": [reply["left"] for reply in port.replies]}
        elif isinstance(port, RecordingPort):
            ports[name] = {"cursor": port.cursor}
    payload = {
        "at_ns": engine.now(), "rng_counter": engine.rng.counter, "state": engine.state_copy(),
        "queue": engine.queue.snapshot(), "timers": deep_copy(engine._timers), "token_n": engine._token_n,
        "events": deep_copy(engine.events), "port_calls": deep_copy(engine.port_calls),
        "state_snapshots": deep_copy(engine.snapshots), "delivered": engine.delivered,
        "delivered_sequences": list(engine.delivered_sequences), "ports": ports,
        "worlds": {name: world.checkpoint(snapshots[name]) for name, world in engine.case.worlds.items()},
    }
    document = {"format": "seam-checkpoint", "version": 1, "sdk_version": __version__,
                "identity": engine.case.identity, "workload_digest": engine.case.workload_digest, "payload": payload}
    document["digest"] = digest(document)
    validate_document(document)
    validate_payload(payload, engine.case)
    return document
