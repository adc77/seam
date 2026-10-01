"""One canonical artifact file. The temporary name is `path + ".tmp"` because mkstemp reads the OS RNG."""

import os

from seam.canon import dumps
from seam.version import __version__

MAX_ARTIFACT = 32 * 1024 * 1024


def digest_body(result):
    """Keys that enter the run digest. Grader failures stay out. A post-seal fault stays out."""
    body = {
        "clock": {"start_ns": result.start_ns, "end_ns": result.end_ns},
        "events": result.events,
        "final_state": result.final_state,
        "namespace": result.namespace,
        "port_calls": result.port_calls,
        "seed": result.seed,
        "state_snapshots": result.state_snapshots,
        "stop_reason": result.stop_reason,
        "timers": result.timers,
    }
    if result.terminal is not None:
        body["terminal"] = result.terminal
    if result.loop_fault is not None:
        body["fault"] = result.loop_fault.as_dict()
    return body


def assemble(result):
    art = {
        "format": "seam-artifact",
        "version": 1,
        "name": result.name,
        "namespace": result.namespace,
        "seed": result.seed,
        "mode": "sim",
        "package_version": __version__,
        "case_digest": result.case_digest,
        "digest": result.digest,
        "status": result.status,
        "stop_reason": result.stop_reason,
        "clock": {"start_ns": result.start_ns, "end_ns": result.end_ns},
        "events": result.events,
        "port_calls": result.port_calls,
        "state_snapshots": result.state_snapshots,
        "final_state": result.final_state,
        "timers": result.timers,
    }
    if result.terminal is not None:
        art["terminal"] = result.terminal
    fault = result.loop_fault or result.post_fault
    if fault is not None:
        art["fault"] = fault.as_dict()
    if result.assertions is not None:
        art["assertions"] = result.assertions
    if result.grader_failures is not None:
        art["grader"] = result.grader_failures
    return art


def refused(code, *, meta=None, op=None):
    fault = {"code": code}
    if op is not None:
        fault["op"] = op
    art = {
        "format": "seam-artifact",
        "version": 1,
        "mode": "sim",
        "package_version": __version__,
        "digest": None,
        "status": "refused",
        "stop_reason": None,
        "events": [],
        "port_calls": [],
        "state_snapshots": [],
        "timers": [],
        "final_state": None,
        "fault": fault,
    }
    if not meta:
        return art
    for key in ("name", "namespace", "seed", "case_digest"):
        if meta.get(key) is not None:
            art[key] = meta[key]
    if meta.get("start_ns") is not None:
        art["clock"] = {"start_ns": meta["start_ns"], "end_ns": meta["start_ns"]}
    return art


def write_artifact(path, obj):
    text = dumps(obj) + "\n"
    data = text.encode("ascii")
    if len(data) > MAX_ARTIFACT:
        raise OSError("artifact exceeds 32 MiB")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def tape_from_artifact(artifact):
    """One canonical tape line per port call. `at_ns` is the virtual time of the call."""
    lines = []
    for call in artifact["port_calls"]:
        lines.append(
            dumps(
                {
                    "format": "seam-tape",
                    "version": 1,
                    "at_ns": call["at_ns"],
                    "port": call["port"],
                    "request": call["request"],
                    "response": call["response"],
                }
            )
        )
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("ascii")
