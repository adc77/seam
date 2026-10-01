"""Script tables and recording cursors. Neither one calls a live factory."""

import json
import os

from seam.canon import deep_copy, equal, is_any, loads, matches, walk
from seam.errors import Fault, Refuse

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


def load_tape(path, port, cutoff_ns):
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
            obj = loads(raw)
        except json.JSONDecodeError:
            raise Refuse("tape_torn") from None
        except Refuse:
            raise
        if type(obj) is not dict or set(obj) != _TAPE_KEYS:
            raise Refuse("bad_case")
        if obj["format"] != "seam-tape" or obj["version"] != 1 or type(obj["version"]) is not int:
            raise Refuse("bad_case")
        at = obj["at_ns"]
        if type(at) is not int:
            raise Refuse("bad_case")

        def on_bad():
            raise Refuse("bad_case")

        try:
            walk(obj["request"], on_bad)
            walk(obj["response"], on_bad)
        except Refuse:
            raise
        if obj["port"] != port:
            continue
        if at > cutoff_ns:
            continue
        if last_at is not None and at < last_at:
            raise Refuse("tape_unsorted")
        last_at = at
        visible.append((obj["request"], obj["response"]))
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
