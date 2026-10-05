"""Supervise one product process and validate its artifact before reporting success."""

from dataclasses import dataclass
import hashlib
from importlib.machinery import PathFinder
import os
import math
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile

from seam.artifact import MAX_ARTIFACT, refused, write_artifact
from seam.canon import digest, equal, loads
from seam.case import MAX_CASE, normalize
from seam.errors import Refuse
from seam.version import __version__

DEFAULT_TIMEOUT = 30
STDERR_LIMIT = 64 * 1024
MODULE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
DIGEST_KEYS = (
    "clock",
    "events",
    "final_state",
    "namespace",
    "port_calls",
    "seed",
    "state_snapshots",
    "stop_reason",
    "timers",
)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    artifact_path: str
    artifact: dict
    stderr: str


def _product_digest(module, pythonpath):
    root = module.split(".")[0]
    paths = [os.getcwd(), *pythonpath.split(os.pathsep), *sys.path]
    spec = PathFinder.find_spec(root, paths)
    if spec is None or spec.origin is None:
        raise Refuse("product_missing")
    files = {}
    if spec.submodule_search_locations:
        for index, location in enumerate(spec.submodule_search_locations):
            base = Path(location)
            for path in sorted(base.rglob("*.py")):
                files[f"{index}/{path.relative_to(base).as_posix()}"] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    else:
        files[root] = hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
    return digest(files)


def _case_digest(path, namespace):
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_CASE + 1)
        if len(data) > MAX_CASE:
            return None
        raw = loads(data)
        if (
            type(raw) is not dict
            or type(raw.get("ports")) is not dict
            or type(raw.get("arrivals")) is not list
        ):
            return None
        handlers = []
        for arrival in raw["arrivals"]:
            if type(arrival) is not dict or type(arrival.get("handler")) is not str:
                return None
            handlers.append(arrival["handler"])
        norm = normalize(
            raw, ports=raw["ports"], handlers=handlers, namespace=namespace, backends=raw["ports"],
            worlds={name: (None, tuple(port for port, spec in raw["ports"].items()
                                      if type(spec) is dict and spec.get("world") == name))
                    for name in raw.get("worlds", {})},
        )
        return digest(norm)
    except (Refuse, OSError, ValueError, TypeError, RecursionError):
        return None


def _checkpoint_matches_artifact(art):
    from seam.checkpoint import validate_document

    payload = validate_document(art["checkpoint"])["payload"]
    if type(art["clock"]) is not dict or not equal(payload["at_ns"], art["clock"].get("end_ns")):
        return False
    if not equal(payload["state"], art["final_state"]):
        return False
    for key in ("events", "port_calls", "state_snapshots"):
        if type(payload[key]) is not list or not equal(payload[key], art[key]):
            return False
    if type(payload["timers"]) is not list:
        return False
    for timer in payload["timers"]:
        keys = {"token", "handler", "fire_at_ns", "outcome", "seq"}
        if type(timer) is not dict or set(timer) != keys | ({"name"} if "name" in timer else set()):
            return False
    timers = [{key: value for key, value in row.items() if key != "seq"} for row in payload["timers"]]
    if not equal(timers, art["timers"]):
        return False
    worlds = payload["worlds"]
    states = art.get("world_states", {})
    if type(worlds) is not dict or type(states) is not dict or set(worlds) != set(states):
        return False
    for name, frame in worlds.items():
        if type(frame) is not dict or type(frame.get("initialized")) is not bool:
            return False
        if frame["initialized"] and ("snapshot" not in frame or not equal(frame["snapshot"], states[name])):
            return False
    return True


def _validate(art, returncode, namespace, case_digest, identity=None):
    if (
        type(art) is not dict
        or art.get("format") != "seam-artifact"
        or type(art.get("version")) is not int
        or art["version"] not in (1, 2, 3)
    ):
        return False
    if type(art.get("provenance", {})) is not dict:
        return False
    if returncode == 3:
        return (
            art.get("status") == "refused"
            and art.get("digest") is None
            and type(art.get("fault")) is dict
            and type(art["fault"].get("code")) is str
        )
    status = "passed" if returncode == 0 else ("paused" if returncode == 4 else "failed")
    if returncode not in (0, 1, 2, 4) or art.get("status") != status:
        return False
    if (
        case_digest is None
        or art.get("namespace") != namespace
        or art.get("case_digest") != case_digest
    ):
        return False
    if not all(key in art for key in DIGEST_KEYS):
        return False
    if returncode in (0, 4):
        if "fault" in art or art.get("grader"):
            return False
        if any(
            type(row) is not dict or row.get("ok") is not True for row in art.get("assertions", [])
        ):
            return False
    if returncode == 4:
        if art["version"] != 3 or art.get("stop_reason") != "checkpoint":
            return False
    if "checkpoint" in art:
        if art["version"] != 3 or returncode == 0 or art.get("stop_reason") != "checkpoint":
            return False
        if not _checkpoint_matches_artifact(art):
            return False
        if identity is not None and art["checkpoint"]["identity"] != identity:
            return False
    elif art.get("stop_reason") == "checkpoint":
        return False
    body = {key: art[key] for key in DIGEST_KEYS}
    for key in ("terminal", "backend_states", "world_states"):
        if key in art:
            body[key] = art[key]
    if returncode == 2:
        if type(art.get("fault")) is not dict:
            return False
        if digest(body) == art.get("digest"):
            return True
        body["fault"] = art["fault"]
    return digest(body) == art.get("digest")


def _process_fault(code):
    art = refused(code)
    art.update(version=2, status="failed", stop_reason=code, supervisor=True)
    return art


def run_product(module, case, namespace, artifact, *, timeout=DEFAULT_TIMEOUT, env=None):
    """Run a product with a clean environment, a deadline, and a fresh artifact path."""
    if type(module) is not str or not MODULE.fullmatch(module):
        raise Refuse("product_missing")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise Refuse("bad_env")
    if os.name != "posix":
        raise Refuse("unsupported_platform")
    output = os.path.abspath(artifact)
    case_digest = _case_digest(case, namespace)
    child_env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ}
    child_env.update(PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", TZ="UTC")
    if env is not None:
        if type(env) is not dict or any(
            type(key) is not str or type(value) is not str or key.startswith("SEAM_")
            for key, value in env.items()
        ):
            raise Refuse("bad_env")
        child_env.update(env)
    child_env["PYTHONHASHSEED"] = "0"
    provenance = {
        "product": {
            "module": module,
            "sha256": _product_digest(module, child_env.get("PYTHONPATH", "")),
        },
        "sdk_version": __version__,
        "python": platform.python_version(),
        "environment_sha256": digest(child_env),
    }
    with tempfile.TemporaryDirectory(prefix="seam-run-") as directory:
        child_artifact = os.path.join(directory, "artifact.json")
        launch_env = dict(
            child_env,
            SEAM_SIM="1",
            SEAM_CASE=os.path.abspath(case),
            SEAM_NAMESPACE=namespace,
            SEAM_ARTIFACT=child_artifact,
            SEAM_PRODUCT_SHA256=provenance["product"]["sha256"],
            SEAM_ENVIRONMENT_SHA256=provenance["environment_sha256"],
        )
        with tempfile.TemporaryFile() as stderr_file:
            proc = subprocess.Popen(
                [sys.executable, "-m", module],
                env=launch_env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                start_new_session=True,
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            stderr_file.seek(0, os.SEEK_END)
            length = stderr_file.tell()
            stderr_file.seek(max(0, length - STDERR_LIMIT))
            stderr = stderr_file.read().decode("utf-8", "replace")
        if timed_out:
            art, code = _process_fault("process_timeout"), 2
        elif not os.path.isfile(child_artifact):
            art, code = _process_fault("process_exit"), 2
        else:
            try:
                with open(child_artifact, "rb") as handle:
                    data = handle.read(MAX_ARTIFACT + 1)
                art = loads(data) if len(data) <= MAX_ARTIFACT else None
                identity = None
                if type(art) is dict and "checkpoint" in art:
                    identity = {"product_sha256": provenance["product"]["sha256"],
                                "sdk_sha256": _product_digest("seam", child_env.get("PYTHONPATH", "")),
                                "environment_sha256": provenance["environment_sha256"],
                                "python": provenance["python"]}
                valid = _validate(art, proc.returncode, namespace, case_digest, identity)
            except (Refuse, OSError, ValueError, RecursionError, TypeError):
                valid = False
            if valid:
                code = proc.returncode
            else:
                art, code = _process_fault("invalid_artifact"), 2
        existing = art.get("provenance", {})
        art["provenance"] = {**existing, **provenance}
        write_artifact(output, art)
        return ProcessResult(code, output, art, stderr)
