"""Project explicitly selected, verified experiment evidence without importing product adapters."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from seam.artifact import MAX_ARTIFACT
from seam.canon import digest, loads
from seam.experiments import ExperimentError, MAX_REPORT, read_experiment

MAX_EXPERIMENTS = 16
MAX_RUN_BYTES = 64 * 1024 * 1024
LABEL = re.compile(r"[A-Za-z][A-Za-z0-9 _-]{0,63}\Z")


class EvidenceError(ValueError):
    """Evidence is missing, changed, unsupported, or beyond the viewer's bounded read budget."""


def error_hint(code):
    hints = {
        "viewer_directory_missing": "Directory not found. Check your working directory or use an absolute path.",
        "viewer_not_directory": "Select an experiment directory, not a file.",
        "viewer_summary_missing": "No completed summary here. Select the experiment subdirectory of a finished run.",
    }
    return hints.get(
        code,
        "Evidence is invalid, changed, or unavailable. Keep the original evidence; do not bypass verification.",
    )


def pretty(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False
    )


def transport(value):
    """Preserve every integer exactly across the JSON/JavaScript boundary."""
    if type(value) is int:
        return str(value)
    if type(value) is list:
        return [transport(item) for item in value]
    if type(value) is dict:
        return {key: transport(item) for key, item in value.items()}
    return value


def _read(root, name, expected, limit):
    if limit < 0:
        raise EvidenceError("viewer_evidence_size")
    if type(name) is not str or Path(name).is_absolute() or ".." in Path(name).parts:
        raise EvidenceError("viewer_evidence_path")
    path = root
    for part in Path(name).parts:
        path = path / part
        if path.is_symlink():
            raise EvidenceError("viewer_evidence_path")
    if not path.resolve().is_relative_to(root):
        raise EvidenceError("viewer_evidence_path")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise EvidenceError("viewer_evidence_size")
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        raise EvidenceError("viewer_evidence_changed")
    return raw


@dataclass(frozen=True)
class Experiment:
    label: str
    root: Path
    summary: dict | None
    manifest: dict | None
    summary_hash: str | None
    error: str | None

    def check(self):
        if self.error is not None:
            raise EvidenceError(self.error)
        _read(self.root, "summary.json", self.summary_hash, MAX_REPORT)
        _read(
            self.root,
            "manifest.json",
            self.summary["evidence"]["manifest.json"],
            MAX_REPORT,
        )

    def overview(self, index):
        base = {"id": str(index), "label": self.label}
        try:
            self.check()
            if read_experiment(self.root) != self.summary:
                raise EvidenceError("viewer_evidence_changed")
        except (EvidenceError, ExperimentError, OSError) as exc:
            code = (
                str(exc)
                if isinstance(exc, (EvidenceError, ExperimentError))
                else "viewer_evidence_unavailable"
            )
            return {
                **base,
                "available": False,
                "error": code,
                "error_hint": error_hint(code),
            }
        return {
            **base,
            "available": True,
            "status": self.summary["status"],
            "plan": self.manifest["plan"],
            "plan_json": pretty(self.manifest["plan"]),
            "identity_json": pretty(self.manifest["identity"]),
            "units": self.summary["units"],
            "runs": self.summary["runs"],
            "aggregates": self.summary["aggregates"],
            "comparisons": self.summary["comparisons"],
            "pairs_json": {
                name: pretty(comparison["pairs"])
                for name, comparison in self.summary["comparisons"].items()
            },
            "summary_sha256": self.summary_hash,
            "inference": self.summary.get("inference"),
        }

    def run(self, index):
        try:
            self.check()
            if type(index) is not int or not 0 <= index < len(self.summary["runs"]):
                raise EvidenceError("viewer_run_missing")
            row = self.summary["runs"][index]
            view = {
                "id": str(index),
                "row": row,
                "row_json": pretty(row),
                "artifact": None,
            }
            evidence = self.summary["evidence"]
            cell = f"{row['variant']}-{row['seed']}"
            result_name = f"{cell}/result.json"
            result = _read(self.root, result_name, evidence[result_name], MAX_REPORT)
            if loads(result) != row:
                raise EvidenceError("viewer_evidence_changed")
            inference_bytes = 0
            if "inference" in row and "evidence" in row["inference"]:
                name = row["inference"]["evidence"]
                data = _read(self.root, name, evidence[name], MAX_REPORT)
                inference_bytes = len(data)
                view["inference_json"] = pretty(loads(data))
            if "artifact" not in row:
                return view
            raw = _read(
                self.root, row["artifact"], row["artifact_sha256"], MAX_ARTIFACT
            )
            artifact = loads(raw)
            if type(artifact) is not dict or artifact.get("format") != "seam-artifact":
                raise EvidenceError("viewer_artifact_schema")
            populations = {}
            port_worlds = {}
            used = len(raw) + len(result) + inference_bytes
            inputs_name = f"{cell}/inputs.json"
            inputs_raw = _read(
                self.root, inputs_name, evidence[inputs_name], MAX_REPORT
            )
            inputs = loads(inputs_raw)
            used += len(inputs_raw)
            if "trajectory_inputs" in row:
                name = row["trajectory_inputs"]
                data = _read(
                    self.root,
                    name,
                    evidence[name],
                    min(MAX_REPORT, MAX_RUN_BYTES - used),
                )
                used += len(data)
                inputs["sha256"].update(loads(data)["sha256"])
            for relative, sha in inputs["sha256"].items():
                name = f"{cell}/{relative}"
                if evidence.get(name) != sha:
                    raise EvidenceError("viewer_evidence_changed")
                data = _read(
                    self.root, name, sha, min(MAX_ARTIFACT, MAX_RUN_BYTES - used)
                )
                used += len(data)
                document = loads(data)
                if type(document) is dict and document.get("format") == "seam-actors":
                    populations[digest(document)] = document
                if type(document) is dict and document.get("format") == "seam-case":
                    port_worlds = {
                        port: spec["world"]
                        for port, spec in document["ports"].items()
                        if spec["mode"] == "world"
                    }
            view["artifact"] = _project(artifact, populations, port_worlds)
            return view
        except EvidenceError:
            raise
        except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise EvidenceError("viewer_evidence_invalid") from None


def _project(artifact, populations, port_worlds):
    events = artifact.get("events", [])
    calls = artifact.get("port_calls", [])
    actor_worlds = {
        name
        for name, state in artifact.get("world_states", {}).items()
        if type(state) is dict and state.get("format") == "seam-actor-state"
    }
    calls_by_event, decision_events = {}, {}
    for call in calls:
        calls_by_event.setdefault(call["during"], []).append(call)
        request = call["request"]
        world = port_worlds.get(call["port"])
        if (
            world in actor_worlds
            and type(request) is dict
            and request.get("op") == "decide"
        ):
            key = (world, request["actor_id"], request["step"], call["at_ns"])
            decision_events.setdefault(key, []).append(call["during"])
    actors = []
    for world_name, world in artifact.get("world_states", {}).items():
        if type(world) is not dict or world.get("format") != "seam-actor-state":
            continue
        population = populations.get(world["population_sha256"])
        if population is None:
            raise EvidenceError("viewer_population_missing")
        records = {}
        for record in world["decisions"]:
            records.setdefault(record["request"]["actor_id"], []).append(record)
        for actor in population["actors"]:
            decisions = []
            for record in records.get(actor["id"], []):
                request = record["request"]
                resolved = (
                    record["resolution"]
                    if "resolution" in record
                    else record["decision"]
                )
                key = (world_name, actor["id"], request["step"], request["at_ns"])
                matching = decision_events.get(key, [])
                decisions.append(
                    {
                        "at_ns": request["at_ns"],
                        "step": request["step"],
                        "event_i": matching[0] if len(matching) == 1 else None,
                        "observation_json": pretty(request["observation"]),
                        "memory_before_json": pretty(request["memory"]),
                        "memory_after_json": pretty(resolved["memory"]),
                        "decision_json": pretty(record["decision"]),
                        "resolution_json": pretty(resolved),
                        "choices_json": pretty(request["choices"])
                        if "choices" in request
                        else None,
                        "delivery_json": pretty(record["delivery"]),
                        "request_json": pretty(request),
                        "rng_before": request["rng_before"],
                        "rng_after": record["rng_after"],
                    }
                )
            current = world["actors"][actor["id"]]
            actors.append(
                {
                    "world": world_name,
                    "id": actor["id"],
                    "policy": actor["policy"],
                    "mode": population["mode"],
                    "start_ns": actor["start_ns"],
                    "next_ns": current["next_ns"],
                    "step": current["step"],
                    "sealed": world["sealed"],
                    "profile_json": pretty(actor["profile"]),
                    "actions_json": pretty(actor["actions"]),
                    "initial_memory_json": pretty(actor["memory"]),
                    "current_memory_json": pretty(current["memory"]),
                    "decisions": decisions,
                }
            )
    timeline = [
        {
            "i": row["i"],
            "at_ns": row["at_ns"],
            "kind": row["kind"],
            "handler": row.get("handler", ""),
            "status": row.get("status", ""),
            "detail_json": pretty(row),
            "port_calls_json": pretty(calls_by_event.get(row["i"], [])),
        }
        for row in events
    ]
    return {
        "status": artifact["status"],
        "stop_reason": artifact["stop_reason"],
        "clock": artifact.get("clock"),
        "digest": artifact["digest"],
        "actors": actors,
        "timeline": timeline,
        "final_state_json": pretty(artifact["final_state"]),
        "assertions_json": pretty(artifact.get("assertions", [])),
        "failure_json": pretty(
            {key: artifact[key] for key in ("fault", "grader") if key in artifact}
        ),
        "timers_json": pretty(artifact["timers"]),
        "provenance_json": pretty(artifact.get("provenance", {})),
        "world_states_json": pretty(artifact.get("world_states", {})),
    }


def open_experiments(selections):
    if not 1 <= len(selections) <= MAX_EXPERIMENTS:
        raise ValueError("select between 1 and 16 experiments")
    labels = [label for label, _ in selections]
    if any(type(label) is not str or not LABEL.fullmatch(label) for label in labels):
        raise ValueError("invalid experiment label")
    if len(set(labels)) != len(labels):
        raise ValueError("duplicate experiment label")
    experiments = []
    for label, directory in selections:
        root = Path(directory).resolve()
        try:
            if not root.exists():
                raise EvidenceError("viewer_directory_missing")
            if not root.is_dir():
                raise EvidenceError("viewer_not_directory")
            if not (root / "summary.json").exists():
                raise EvidenceError("viewer_summary_missing")
            summary = read_experiment(root)
            manifest = loads(
                _read(
                    root,
                    "manifest.json",
                    summary["evidence"]["manifest.json"],
                    MAX_REPORT,
                )
            )
            raw = _read(root, "summary.json", None, MAX_REPORT)
            if loads(raw) != summary:
                raise EvidenceError("viewer_evidence_changed")
            experiments.append(
                Experiment(
                    label,
                    root,
                    summary,
                    manifest,
                    hashlib.sha256(raw).hexdigest(),
                    None,
                )
            )
        except (ExperimentError, EvidenceError) as exc:
            experiments.append(Experiment(label, root, None, None, None, str(exc)))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
        ):
            experiments.append(
                Experiment(label, root, None, None, None, "viewer_evidence_invalid")
            )
    return tuple(experiments)
