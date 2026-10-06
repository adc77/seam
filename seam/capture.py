"""Bounded capture manifests and case packaging; product exporters retain domain ownership."""

import hashlib
import os
from pathlib import Path

from seam.canon import INT64_MAX, deep_copy, digest, dumps, equal, loads
from seam.case import IDENT, MAX_ASSERTIONS, MAX_CASE, normalize, _assertion
from seam.errors import Refuse
from seam.runner import MODULE, _product_digest, run_product
from seam.version import __version__

MAX_CAPTURE = 8 * 1024 * 1024
MAX_CAPTURE_EVENTS = 10_000


class CaptureError(ValueError):
    """An invalid, changed, incompatible, or unpackageable capture."""


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise CaptureError("invalid capture schema")


def _time(value):
    if type(value) is not int or not 0 <= value <= INT64_MAX:
        raise CaptureError("invalid capture time")
    return value


def _copy(value):
    try:
        return deep_copy(value, fault=False)
    except (Refuse, ValueError, TypeError, RecursionError):
        raise CaptureError("capture must contain bounded canonical JSON") from None


def capture_source(module):
    if type(module) is not str or not MODULE.fullmatch(module):
        raise CaptureError("invalid product module")
    root = Path(__file__).parent
    sdk_files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }
    try:
        product = _product_digest(module, os.environ.get("PYTHONPATH", ""))
    except Refuse:
        raise CaptureError("capture product is not installed") from None
    return {
        "module": module,
        "product_sha256": product,
        "sdk_version": __version__,
        "sdk_sha256": digest(sdk_files),
    }


def capture_bundle(
    module,
    *,
    as_of_ns,
    until_ns,
    config,
    initial_state,
    datasets,
    arrivals,
    assertions,
    product_data,
):
    payload = _copy(
        {
            "source": capture_source(module),
            "as_of_ns": as_of_ns,
            "until_ns": until_ns,
            "config": config,
            "initial_state": initial_state,
            "datasets": datasets,
            "arrivals": arrivals,
            "assertions": assertions,
            "product_data": product_data,
        }
    )
    document = {
        "format": "seam-capture",
        "version": 1,
        "payload": payload,
        "sha256": digest(payload),
    }
    validate_capture(document, module=module)
    return document


def _arrivals(arrivals, start, end):
    if type(arrivals) is not list or len(arrivals) > MAX_CAPTURE_EVENTS:
        raise CaptureError("invalid capture arrival count")
    previous = start
    for row in arrivals:
        _object(row, {"at_ns", "handler", "body"})
        at = _time(row["at_ns"])
        if not previous <= at <= end:
            raise CaptureError("capture arrivals are outside the ordered window")
        if type(row["handler"]) is not str or not IDENT.fullmatch(row["handler"]):
            raise CaptureError("invalid capture handler")
        previous = at


def validate_capture(document, *, module):
    _object(document, {"format", "version", "payload", "sha256"})
    if (
        document["format"] != "seam-capture"
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise CaptureError("unsupported capture format")
    document = _copy(document)
    if len((dumps(document) + "\n").encode("ascii")) > MAX_CAPTURE:
        raise CaptureError("capture exceeds size limit")
    payload = document["payload"]
    _object(
        payload,
        {
            "source",
            "as_of_ns",
            "until_ns",
            "config",
            "initial_state",
            "datasets",
            "arrivals",
            "assertions",
            "product_data",
        },
    )
    if type(document["sha256"]) is not str or digest(payload) != document["sha256"]:
        raise CaptureError("capture checksum mismatch")
    if not equal(payload["source"], capture_source(module)):
        raise CaptureError("capture product or SDK provenance mismatch")
    start, end = _time(payload["as_of_ns"]), _time(payload["until_ns"])
    if end < start:
        raise CaptureError("capture window is reversed")
    if (
        type(payload["datasets"]) is not dict
        or type(payload["product_data"]) is not dict
    ):
        raise CaptureError("invalid capture datasets or product metadata")
    for name, export in payload["datasets"].items():
        if not IDENT.fullmatch(name):
            raise CaptureError("invalid capture dataset name")
        _object(export, {"as_of_ns", "data"})
        if _time(export["as_of_ns"]) > start:
            raise CaptureError("capture dataset is from the future")
    _arrivals(payload["arrivals"], start, end)
    assertions = payload["assertions"]
    if type(assertions) is not list or not 0 < len(assertions) <= MAX_ASSERTIONS:
        raise CaptureError("capture requires bounded outcome assertions")
    try:
        for assertion in assertions:
            _assertion(assertion, 3)
    except Refuse:
        raise CaptureError("invalid capture assertion") from None


def read_capture(path, *, module):
    with open(path, "rb") as handle:
        data = handle.read(MAX_CAPTURE + 1)
    if len(data) > MAX_CAPTURE:
        raise CaptureError("capture exceeds size limit")
    try:
        document = loads(data)
    except (Refuse, ValueError):
        raise CaptureError("invalid capture JSON") from None
    validate_capture(document, module=module)
    return document


def write_capture_json(path, value):
    data = (dumps(_copy(value)) + "\n").encode("ascii")
    if len(data) > MAX_CAPTURE:
        raise CaptureError("export exceeds size limit")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_capture_case(
    document,
    directory,
    *,
    module,
    name,
    namespace,
    ports,
    worlds,
    bootstrap=(),
    finalize=(),
    arrivals=None,
    until_ns=None,
    config=None,
    same_time_order="sequence",
):
    validate_capture(document, module=module)
    payload = document["payload"]
    start = payload["as_of_ns"]
    end = payload["until_ns"] if until_ns is None else _time(until_ns)
    external = _copy(payload["arrivals"] if arrivals is None else arrivals)
    _arrivals(external, start, end)

    def boundary(handlers, at):
        if type(handlers) not in (list, tuple):
            raise CaptureError("invalid capture boundary handlers")
        rows = []
        for row in handlers:
            _object(row, {"handler", "body"})
            rows.append({"at_ns": at, **_copy(row)})
        return rows

    rows = boundary(bootstrap, start) + external + boundary(finalize, end)
    if type(ports) is not dict or type(worlds) is not dict:
        raise CaptureError("invalid capture port or world plan")
    refs, exports = {}, {}
    for key, export in payload["datasets"].items():
        data = (dumps(export["data"]) + "\n").encode("ascii")
        filename = f"data-{key}.json"
        exports[filename] = export["data"]
        refs[key] = {
            "path": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            "as_of_ns": export["as_of_ns"],
        }
    case = {
        "format": "seam-case",
        "version": 3,
        "name": name,
        "seed": 1842,
        "namespace": namespace,
        "clock": {"start_ns": start, "epoch": "1970-01-01T00:00:00Z"},
        "config": _copy(payload["config"] if config is None else config),
        "initial_state": payload["initial_state"],
        "datasets": refs,
        "ports": _copy(ports),
        "worlds": _copy(worlds),
        "arrivals": rows,
        "assertions": payload["assertions"],
        "stop": {"when": "deadline", "deadline_ns": end, "allow_quiescence": True},
        "same_time_order": same_time_order,
    }
    if len((dumps(case) + "\n").encode("ascii")) > MAX_CASE:
        raise CaptureError("capture case exceeds SDK size limit")
    try:
        world_factories = {
            key: (
                None,
                tuple(
                    port
                    for port, spec in ports.items()
                    if type(spec) is dict and spec.get("world") == key
                ),
            )
            for key in worlds
        }
        normalize(
            case,
            ports=ports,
            handlers=[row["handler"] for row in rows],
            namespace=namespace,
            backends=ports,
            worlds=world_factories,
        )
    except Refuse:
        raise CaptureError("invalid capture replay plan") from None
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    for path, data in exports.items():
        write_capture_json(directory / path, data)
    write_capture_json(
        directory / "origin.json",
        {
            "capture_sha256": document["sha256"],
            "source": payload["source"],
            "case_sha256": digest(case),
        },
    )
    write_capture_json(directory / "case.json", case)
    return directory / "case.json"


def run_capture(document, directory, *, module, **plan):
    path = write_capture_case(document, directory, module=module, **plan)
    return run_product(
        module, str(path), plan["namespace"], str(path.parent / "artifact.json")
    )
