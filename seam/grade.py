"""Assertions and the optional grader. Both run after the digest is sealed."""

import importlib
from copy import deepcopy

from seam.artifact import assemble, digest_body
from seam.canon import digest, equal, matches
from seam.errors import Fault, fault_scope
from seam.guard import _handler_scope, trusted


def _port_calls(art, port):
    return [call for call in art["port_calls"] if call["port"] == port]


def _lookup(state, path):
    if path == "":
        return True, state
    cur = state
    for seg in path.split("."):
        if type(cur) is dict and seg in cur:
            cur = cur[seg]
            continue
        if type(cur) is list and seg.isascii() and seg.isdigit() and not (len(seg) > 1 and seg[0] == "0"):
            if len(seg) <= len(str(len(cur))):
                i = int(seg)
                if i < len(cur):
                    cur = cur[i]
                    continue
        return False, None
    return True, cur


def _timer(art, item):
    if "name" in item:
        found = [row for row in art["timers"] if row.get("name") == item["name"]]
    else:
        found = [row for row in art["timers"] if row["token"] == item["token"]]
    if len(found) != 1:
        return None
    return found[0]


def check_one(art, item):
    op = item["op"]
    if op == "port_called":
        calls = _port_calls(art, item["port"])
        if "match" in item:
            calls = [call for call in calls if matches(item["match"], call["request"])]
        if len(calls) != item["times"]:
            return False, f"got {len(calls)}"
        return True, None
    if op == "port_not_called":
        n = len(_port_calls(art, item["port"]))
        if n != 0:
            return False, f"got {n}"
        return True, None
    if op == "port_response":
        calls = _port_calls(art, item["port"])
        if item["i"] >= len(calls):
            return False, "missing"
        if not matches(item["match"], calls[item["i"]]["response"]):
            return False, "mismatch"
        return True, None
    if op == "event_count":
        n = sum(
            1
            for event in art["events"]
            if event.get("kind") == "deliver" and event.get("handler") == item["handler"]
        )
        if n != item["times"]:
            return False, f"got {n}"
        return True, None
    if op == "timer_outcome":
        row = _timer(art, item)
        if row is None:
            return False, "missing"
        if row["outcome"] != item["outcome"]:
            return False, f"got {row['outcome']}"
        return True, None
    if op == "state_is":
        found, value = _lookup(art["final_state"], item["path"])
        if not found:
            return False, "path missing"
        if not equal(value, item["value"]):
            return False, "mismatch"
        return True, None
    if op == "stopped":
        if art.get("stop_reason") != item["reason"]:
            return False, f"got {art.get('stop_reason')}"
        if "terminal" in item and art.get("terminal") != item["terminal"]:
            return False, "terminal mismatch"
        return True, None
    if op == "digest_is":
        if art.get("digest") != item["sha256"]:
            return False, "digest mismatch"
        return True, None
    if op == "fault_is":
        fault = art.get("fault")
        code = fault.get("code") if type(fault) is dict else None
        if code is None:
            return False, "got none"
        if code != item["code"]:
            return False, f"got {code}"
        return True, None
    return False, "mismatch"


def check_all(art, assertions):
    rows = []
    for item in assertions:
        ok, detail = check_one(art, item)
        row = dict(item)
        row["ok"] = ok
        if not ok:
            row["detail"] = detail
        rows.append(row)
    return rows


def _load_grader(spec):
    module_name, func_name = spec.split(":")
    # Importing the grader reads its module file. That is seam's own work, not
    # a handler's, so it runs trusted. The grader body itself does not.
    with trusted():
        module = importlib.import_module(module_name)
    fn = getattr(module, func_name)
    if not callable(fn):
        raise Fault("grader_error")
    from seam.runtime import require_sync

    require_sync(fn)
    return fn


def _failed(result):
    if result.loop_fault is not None or result.post_fault is not None:
        return True
    if result.assertions and any(not row["ok"] for row in result.assertions):
        return True
    if result.grader_failures:
        return True
    return False


def finish(result, case):
    """Seal the digest, then run every assertion, then the grader."""
    # Filesystem provenance is recorded outside the digest on purpose: it is a
    # fact about the host, not an input to the run. A run that read nothing
    # leaves the key out entirely so artifacts stay byte-comparable.
    from seam.guard import POLICY

    result.fs_reads = POLICY.seen_reads()
    result.digest = digest(digest_body(result))
    result.assertions = check_all(assemble(result), case.assertions)
    result.status = "failed" if _failed(result) else ("paused" if result.checkpoint is not None else "passed")
    if case.grader:
        def post_fault(err):
            if result.loop_fault is None and result.post_fault is None:
                result.post_fault = err

        try:
            fn = _load_grader(case.grader)
            with _handler_scope(), fault_scope(post_fault):
                out = fn(deepcopy(assemble(result)))
                from seam.runtime import require_sync_result

                require_sync_result(out)
            if type(out) is not list or any(type(item) is not str for item in out):
                raise Fault("grader_error", exc_type="ValueError")
            result.grader_failures = out
        except Fault as err:
            if result.loop_fault is None and result.post_fault is None:
                result.post_fault = err
        except Exception as err:
            if result.loop_fault is None and result.post_fault is None:
                result.post_fault = Fault("grader_error", exc_type=type(err).__name__)
        except BaseException as err:
            if result.loop_fault is None and result.post_fault is None:
                result.post_fault = Fault("grader_error", exc_type=type(err).__name__)
    result.status = "failed" if _failed(result) else ("paused" if result.checkpoint is not None else "passed")
    return assemble(result)


def exit_code(result):
    if result.status == "paused":
        return 4
    if result.status == "passed":
        return 0
    if result.loop_fault is not None:
        return 2
    if result.post_fault is not None and result.post_fault.code != "grader_error":
        return 2
    return 1
