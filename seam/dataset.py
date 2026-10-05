"""Content-addressed production exports with explicit temporal provenance."""

import hashlib
from pathlib import Path
import re

from seam.canon import INT64_MAX, deep_copy, loads
from seam.errors import Refuse
from seam.ports import resolve_tape

MAX_DATASET = 64 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def dataset_ref(path, as_of_ns):
    """Pin an export relative to the case directory before constructing a case."""
    if type(as_of_ns) is not int or not 0 <= as_of_ns <= INT64_MAX:
        raise Refuse("bad_dataset")
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_DATASET + 1)
    except OSError:
        raise Refuse("bad_dataset") from None
    if len(data) > MAX_DATASET:
        raise Refuse("bad_dataset")
    deep_copy(loads(data), fault=False)
    return {
        "path": Path(path).name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "as_of_ns": as_of_ns,
    }


def load_datasets(case_path, refs, start_ns):
    """Load only pinned exports that existed at the simulation's starting instant."""
    if type(refs) is not dict:
        raise Refuse("bad_dataset")
    from seam.case import IDENT

    datasets = {}
    for name, ref in refs.items():
        if type(name) is not str or not IDENT.fullmatch(name) or type(ref) is not dict:
            raise Refuse("bad_dataset")
        if set(ref) != {"path", "sha256", "as_of_ns"}:
            raise Refuse("bad_dataset")
        if type(ref["sha256"]) is not str or not SHA256.fullmatch(ref["sha256"]):
            raise Refuse("bad_dataset")
        as_of = ref["as_of_ns"]
        if type(as_of) is not int or not 0 <= as_of <= INT64_MAX:
            raise Refuse("bad_dataset")
        if as_of > start_ns:
            raise Refuse("dataset_future")
        path = resolve_tape(case_path, ref["path"])
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_DATASET + 1)
        except OSError:
            raise Refuse("bad_dataset") from None
        if len(data) > MAX_DATASET:
            raise Refuse("bad_dataset")
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise Refuse("dataset_changed")
        try:
            datasets[name] = deep_copy(loads(data), fault=False)
        except (ValueError, RecursionError):
            raise Refuse("bad_dataset") from None
    return datasets
