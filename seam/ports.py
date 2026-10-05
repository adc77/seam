"""Script tables and recording cursors. Neither one calls a live factory."""

import json
import os

from seam.canon import deep_copy, equal, is_any, loads, matches, walk
from seam.errors import ERROR_CODE, Fault, PortError, Refuse

MAX_TAPE = 64 * 1024 * 1024
_TAPE_KEYS = {"format", "version", "at_ns", "port", "request", "response"}


class ScriptPort:
    source = "script"

    def __init__(self, replies, unmatched):
        self.replies = replies
        self.unmatched = unmatched

    def call(self, request):
        for reply in self.replies:
            left = reply["left"]
            if left == 0:
                continue
            if matches(reply["match"], request):
                if left != "forever":
                    reply["left"] = left - 1
                if "error" in reply:
                    raise PortError(reply["error"])
                return deep_copy(reply["response"])
        if self.unmatched == "fail":
            raise Fault("unmatched_port")
        return deep_copy(self.unmatched["response"])


class RecordingPort:
    source = "recording"

    def __init__(self, visible):
        self.visible = visible
        self.cursor = 0

    def call(self, request):
        if self.cursor >= len(self.visible):
            raise Fault("tape_exhausted")
        expected, response = self.visible[self.cursor]
        if not equal(expected, request):
            raise Fault("tape_mismatch")
        self.cursor += 1
        if isinstance(response, PortError):
            raise PortError(response.code)
        return deep_copy(response)


def resolve_tape(case_path, tape):
    if type(tape) is not str or tape == "" or os.path.isabs(tape):
        raise Refuse("tape_path")
    base = os.path.realpath(os.path.dirname(os.path.abspath(case_path)))
    full = os.path.realpath(os.path.join(base, tape))
    if full != base and not full.startswith(base + os.sep):
        raise Refuse("tape_path")
    if not os.path.isfile(full):
        raise Refuse("bad_case")
    return full


def load_tape(path, port, cutoff_ns, *, allow_errors=False):
    """Visible lines are those with `at_ns <= cutoff`. Later lines are not stored."""
    size = os.path.getsize(path)
    if size > MAX_TAPE:
        raise Refuse("bad_case")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeError:
        raise Refuse("tape_torn") from None
    visible = []
    last_at = None

    for raw in text.splitlines():
        if raw.strip() == "":
            raise Refuse("bad_case")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise Refuse("tape_torn") from None
        except Refuse:
            raise
        if type(obj) is not dict or "at_ns" not in obj or type(obj["at_ns"]) is not int:
            raise Refuse("bad_case")
        if obj["at_ns"] > cutoff_ns:
            continue
        obj = loads(raw)
        expected = _TAPE_KEYS if "response" in obj else (_TAPE_KEYS - {"response"}) | {"error"}
        if set(obj) != expected:
            raise Refuse("bad_case")
        if obj["format"] != "seam-tape" or obj["version"] not in (1, 2) or type(obj["version"]) is not int:
            raise Refuse("bad_case")
        at = obj["at_ns"]
        if type(at) is not int:
            raise Refuse("bad_case")

        def on_bad():
            raise Refuse("bad_case")

        try:
            walk(obj["request"], on_bad)
            if "response" in obj:
                walk(obj["response"], on_bad)
            elif (not allow_errors or obj["version"] != 2 or type(obj["error"]) is not str
                  or not ERROR_CODE.fullmatch(obj["error"])):
                raise Refuse("bad_case")
        except Refuse:
            raise
        if obj["port"] != port:
            continue
        if last_at is not None and at < last_at:
            raise Refuse("tape_unsorted")
        last_at = at
        response = obj["response"] if "response" in obj else PortError(obj["error"])
        visible.append((obj["request"], response))
    return visible


def validate_match(node, on_bad):
    if is_any(node):
        return
    if type(node) is dict and "$any" in node:
        on_bad()
        return
    if type(node) is dict:
        for item in node.values():
            validate_match(item, on_bad)
        return
    if type(node) is list:
        for item in node:
            validate_match(item, on_bad)
