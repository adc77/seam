"""Canonical JSON. The reference encoder is CPython json with these flags."""

import hashlib
import json

from seam.errors import Fault, Refuse

INT64_MIN = -2**63
INT64_MAX = 2**63 - 1
UINT64_MAX = 2**64 - 1

#: Bytes in a 64-bit value, for the `.to_bytes` calls that seed the RNG.
U64_BYTES = 8

#: Width of the hex in an identifier, so every id is the same length and ids
#: from a live run and a simulated one are interchangeable.
ID_HEX_WIDTH = 16

#: Prefix on a timer token. Live and sim both mint these, and the case loader
#: checks a `timer_outcome` token has it, so it is defined once here rather than
#: spelled three times.
TIMER_TOKEN_PREFIX = "t"

#: Largest single JSON string accepted, in bytes of its UTF-8 encoding.
#: Also the ceiling for a whole state snapshot, which is canonicalised to one
#: string. One limit rather than two, because a snapshot is not meaningfully
#: different from a large string: both bound how much a run can hold at once.
MAX_STRING = 1024 * 1024


def dumps(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(dumps(value).encode("ascii")).hexdigest()


def loads(text):
    """Parse JSON. Floats, NaN, and duplicate keys are refused. The error text is only the code."""

    def pairs(seq):
        out = {}
        for key, val in seq:
            if key in out:
                raise Refuse("bad_case")
            out[key] = val
        return out

    def no_float(_text):
        raise Refuse("bad_case")

    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=no_float,
            parse_constant=no_float,
        )
    except Refuse:
        raise
    except json.JSONDecodeError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise Refuse("bad_case") from None


def walk(value, on_bad):
    """Reject anything that is not JSON, plus surrogates, huge strings, and ints outside signed int64.

    The case seed is checked separately. It is the only unsigned 64-bit integer in the format.
    """
    kind = type(value)
    if value is None or kind is bool:
        return
    if kind is int:
        if value < INT64_MIN or value > INT64_MAX:
            on_bad()
        return
    if kind is str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            on_bad()
            return
        if size > MAX_STRING:
            on_bad()
        return
    if kind is list:
        for item in value:
            walk(item, on_bad)
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                on_bad()
                return
            walk(key, on_bad)
            walk(item, on_bad)
        return
    on_bad()


def deep_copy(value, *, fault=True):
    def on_bad():
        if fault:
            raise Fault("bad_value")
        raise Refuse("bad_case")

    try:
        walk(value, on_bad)
        return json.loads(dumps(value))
    except RecursionError:
        on_bad()


def equal(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is list:
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    if type(left) is dict:
        if set(left) != set(right):
            return False
        return all(equal(left[key], right[key]) for key in left)
    return left == right


def is_any(node):
    return type(node) is dict and set(node) == {"$any"} and node["$any"] is True


def matches(match, value):
    """Subset match. `{"$any": true}` matches any JSON value. No regex, no ranges."""
    if is_any(match):
        return True
    if type(match) is dict:
        if type(value) is not dict:
            return False
        for key, item in match.items():
            if key not in value or not matches(item, value[key]):
                return False
        return True
    if type(match) is list:
        if type(value) is not list or len(match) != len(value):
            return False
        return all(matches(a, b) for a, b in zip(match, value))
    return type(match) is type(value) and match == value
