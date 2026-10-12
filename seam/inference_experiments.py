"""Durable sequential inference ensembles with conservative experiment-wide dispatch reservations."""

import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from seam.actors import _decision, _request, inference_population
from seam.artifact import MAX_ARTIFACT
from seam.canon import INT64_MAX, INT64_MIN, deep_copy, digest, dumps, equal, loads
from seam.experiments import (
    ExperimentError,
    MAX_REPORT,
    NAME,
    _adapter,
    _hash,
    _identity,
    _inputs,
    _plan as rule_plan,
    _summary,
    _unchanged,
    plan as rule_spec,
    read_experiment,
)
from seam.inference import InferenceError, InferenceRun, _settings
from seam.runner import ProcessResult, _case_digest, run_product
from seam.runtime import require_sync_result


def plan(
    adapter,
    variants,
    seeds,
    *,
    baseline,
    config,
    max_calls,
    max_cost_microusd=0,
    world="actors",
    timeout_s=30,
):
    """Declare an inference ensemble with explicit per-cell settings and global reservation limits."""
    value = rule_spec(adapter, variants, seeds, baseline=baseline, timeout_s=timeout_s)
    value["version"] = 2
    value["inference"] = {
        "world": world,
        "config": config,
        "budget": {"max_calls": max_calls, "max_cost_microusd": max_cost_microusd},
    }
    return _plan(value)


def _plan(value):
    try:
        value = deep_copy(value)
        if type(value) is not dict or set(value) != {
            "format",
            "version",
            "adapter",
            "variants",
            "seeds",
            "baseline",
            "timeout_s",
            "inference",
        }:
            raise ExperimentError("experiment_inference_plan")
        if type(value["version"]) is not int or value["version"] != 2:
            raise ExperimentError("experiment_inference_plan")
        base = {key: item for key, item in value.items() if key != "inference"}
        base["version"] = 1
        rule_plan(base)
        settings = value["inference"]
        if type(settings) is not dict or set(settings) != {"world", "config", "budget"}:
            raise ExperimentError("experiment_inference_plan")
        if type(settings["world"]) is not str or not NAME.fullmatch(settings["world"]):
            raise ExperimentError("experiment_inference_plan")
        _settings(settings["config"])
        budget = settings["budget"]
        if type(budget) is not dict or set(budget) != {
            "max_calls",
            "max_cost_microusd",
        }:
            raise ExperimentError("experiment_inference_plan")
        if any(
            type(value) is not int or not 0 <= value <= INT64_MAX
            for value in budget.values()
        ):
            raise ExperimentError("experiment_inference_plan")
        if len(dumps(value).encode("ascii")) > 1024 * 1024:
            raise ExperimentError("experiment_inference_plan")
        return value
    except ExperimentError:
        raise
    except Exception:
        raise ExperimentError("experiment_inference_plan") from None


def _read_raw(path, limit=MAX_REPORT):
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ExperimentError("experiment_report_size")
    return raw


def _read_json(path, limit=MAX_REPORT):
    return loads(_read_raw(path, limit))


def _publish(path, value, *, limit=MAX_REPORT):
    data = (dumps(value) + "\n").encode("ascii")
    if len(data) > limit:
        raise ExperimentError("experiment_report_size")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or _read_raw(path, limit) != data:
            raise ExperimentError("experiment_evidence_changed")
        return
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink()


def _accounting(spec, rows):
    keys = (
        "reserved_calls",
        "accepted_cost_microusd",
        "held_cost_microusd",
        "input_tokens",
        "output_tokens",
    )
    if any(
        type(row.get("inference")) is not dict
        or any(
            type(row["inference"].get(key)) is not int or row["inference"][key] < 0
            for key in keys
        )
        for row in rows
    ):
        raise ExperimentError("experiment_budget_changed")
    result = {key: sum(row["inference"][key] for row in rows) for key in keys}
    result["budget"] = spec["inference"]["budget"]
    result["provider"] = spec["inference"]["config"]["provider"]
    result["model"] = spec["inference"]["config"]["model"]
    result["accounted_cost_microusd"] = (
        result["accepted_cost_microusd"] + result["held_cost_microusd"]
    )
    if (
        result["reserved_calls"] > result["budget"]["max_calls"]
        or result["accounted_cost_microusd"] > result["budget"]["max_cost_microusd"]
    ):
        raise ExperimentError("experiment_budget_changed")
    return result


def _ledger_accounting(entries):
    return {
        "reserved_calls": len(entries),
        "accepted_cost_microusd": sum(
            entry["usage"]["cost_microusd"]
            for entry in entries
            if entry["state"] == "accepted"
        ),
        "held_cost_microusd": sum(
            entry["reserved_cost_microusd"]
            for entry in entries
            if entry["state"] != "accepted"
        ),
        "input_tokens": sum(
            entry["usage"]["input_tokens"]
            for entry in entries
            if entry["state"] == "accepted"
        ),
        "output_tokens": sum(
            entry["usage"]["output_tokens"]
            for entry in entries
            if entry["state"] == "accepted"
        ),
    }


def _verify_inference(root, spec, rows, evidence):
    remaining = spec["inference"]["budget"]["max_cost_microusd"]
    for row in rows:
        accounting = row["inference"]
        if "evidence" not in accounting:
            if accounting != _ledger_accounting([]):
                raise ExperimentError("experiment_budget_changed")
            continue
        expected_name = f"{row['variant']}-{row['seed']}/inference.json"
        if accounting["evidence"] != expected_name or expected_name not in evidence:
            raise ExperimentError("experiment_evidence_changed")
        snapshot = _read_json(root / expected_name)
        config = deep_copy(spec["inference"]["config"])
        config["max_cost_microusd"] = min(config["max_cost_microusd"], remaining)
        if (
            snapshot["config"] != config
            or snapshot["format"] != "seam-experiment-inference"
            or type(snapshot["version"]) is not int
            or snapshot["version"] != 1
        ):
            raise ExperimentError("experiment_report_changed")
        steps, ledger = snapshot["steps"], snapshot["ledger"]
        if (
            type(steps) is not list
            or type(ledger) is not list
            or not len(ledger) <= len(steps) <= len(ledger) + 1
        ):
            raise ExperimentError("experiment_budget_changed")
        spent = 0
        for index, step in enumerate(steps):
            request = step["request"]
            raw_request = {
                key: value for key, value in request.items() if key != "request_id"
            }
            _request(request["request"])
            if (
                step["sequence"] != index
                or request["sequence"] != index
                or request["request_id"] != digest(raw_request)
                or request["model"] != config["model"]
                or request["provider"] != config["provider"]
                or request["limits"]["max_cost_microusd"]
                != config["max_cost_microusd"] - spent
            ):
                raise ExperimentError("experiment_report_changed")
            if step["state"] not in (
                "pending",
                "in_flight",
                "responded",
                "rejected",
            ) or (step["state"] != "responded" and index != len(steps) - 1):
                raise ExperimentError("experiment_report_changed")
            if index == len(ledger):
                if step["state"] != "pending":
                    raise ExperimentError("experiment_unreserved_dispatch")
                continue
            entry = ledger[index]
            if (
                entry["sequence"] != index
                or entry["request_id"] != request["request_id"]
                or entry["reserved_cost_microusd"]
                != request["limits"]["max_cost_microusd"]
            ):
                raise ExperimentError("experiment_budget_changed")
            if step["state"] == "responded":
                response = step["response"]
                _decision(response["decision"])
                if (
                    response["request_id"] != request["request_id"]
                    or response["model"] != config["model"]
                    or response["returned_model"] not in config["returned_models"]
                    or entry["state"] != "accepted"
                    or entry["usage"] != response["usage"]
                ):
                    raise ExperimentError("experiment_report_changed")
                usage = response["usage"]
                for key, maximum in (
                    ("input_tokens", config["max_input_tokens"]),
                    ("output_tokens", config["max_output_tokens"]),
                    ("cost_microusd", entry["reserved_cost_microusd"]),
                ):
                    if type(usage[key]) is not int or not 0 <= usage[key] <= maximum:
                        raise ExperimentError("experiment_budget_changed")
                spent += usage["cost_microusd"]
            elif entry["state"] != "reserved" or step["response"] is not None:
                raise ExperimentError("experiment_report_changed")
        expected = _ledger_accounting(ledger)
        if (
            snapshot["accounting"] != expected
            or {key: value for key, value in accounting.items() if key != "evidence"}
            != expected
        ):
            raise ExperimentError("experiment_budget_changed")
        remaining -= expected["accepted_cost_microusd"] + expected["held_cost_microusd"]
        if remaining < 0:
            raise ExperimentError("experiment_budget_changed")
        if row["status"] == "passed":
            artifact = _read_json(root / row["artifact"], MAX_ARTIFACT)
            decisions = artifact["world_states"][spec["inference"]["world"]][
                "decisions"
            ]
            proposals = [
                {
                    "request": step["request"]["request"],
                    "decision": step["response"]["decision"],
                }
                for step in steps
                if step["state"] == "responded"
            ]
            accepted = [
                {"request": item["request"], "decision": item["decision"]}
                for item in decisions
            ]
            if (
                len(steps) != len(ledger)
                or not equal(proposals, accepted)
                or row["replay"] not in evidence
                or row["replay_case"] not in evidence
                or row["trajectory_inputs"] not in evidence
            ):
                raise ExperimentError("experiment_report_changed")


class InferenceExperiment:
    """Own an ensemble journal; only an explicit known response can resolve a possibly dispatched call."""

    @classmethod
    def create(cls, directory, specification, *, env=None):
        spec = _plan(specification)
        env = {} if env is None else deep_copy(env)
        if (
            type(env) is not dict
            or set(env) - {"PYTHONPATH"}
            or any(type(value) is not str for value in env.values())
        ):
            raise ExperimentError("experiment_environment")
        adapter, metrics, worlds = _adapter(spec["adapter"])
        if spec["inference"]["world"] not in worlds:
            raise ExperimentError("experiment_actor_world")
        root = Path(directory).resolve()
        root.mkdir(mode=0o700, exist_ok=False)
        manifest = {
            "format": "seam-experiment-manifest",
            "version": 2,
            "plan": spec,
            "identity": _identity(spec["adapter"], adapter.MODULE, env),
            "units": metrics,
            "actor_worlds": list(worlds),
            "env": env,
        }
        _publish(root / "manifest.json", manifest)
        fd = os.open(
            root / "ensemble.sqlite", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        os.close(fd)
        result = cls(root, _creating=True)
        try:
            result.manifest = manifest
            result.state = {
                "manifest_sha256": _hash(root / "manifest.json"),
                "cells": [
                    {
                        "variant": variant["name"],
                        "seed": seed,
                        "phase": "new",
                        "ledger": [],
                    }
                    for variant in spec["variants"]
                    for seed in spec["seeds"]
                ],
                "evidence": {"manifest.json": _hash(root / "manifest.json")},
            }
            result.db.execute(
                "CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL, sha256 TEXT NOT NULL)"
            )
            result._save()
            return result
        except BaseException:
            result.close()
            raise

    def __init__(self, directory, *, _creating=False):
        self._broken = False
        root = Path(directory)
        if (
            root.is_symlink()
            or not root.is_dir()
            or root.stat().st_mode & 0o077
            or root.stat().st_uid != os.getuid()
        ):
            raise ExperimentError("experiment_insecure_directory")
        self.root = root.resolve()
        path = self.root / "ensemble.sqlite"
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_mode & 0o077
            or path.stat().st_uid != os.getuid()
        ):
            raise ExperimentError("experiment_insecure_journal")
        self.lock_fd = os.open(
            self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise ExperimentError("experiment_writer_busy") from None
        try:
            self.db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA trusted_schema=OFF")
            if not _creating:
                self.manifest = _read_json(self.root / "manifest.json")
                _plan(self.manifest["plan"])
                row = self.db.execute(
                    "SELECT value,sha256 FROM state WHERE id=1"
                ).fetchone()
                if (
                    row is None
                    or len(row[0].encode("ascii")) > MAX_REPORT
                    or hashlib.sha256(row[0].encode("ascii")).hexdigest() != row[1]
                ):
                    raise ExperimentError("experiment_journal_changed")
                self.state = loads(row[0])
                self._check()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            os.close(self.lock_fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()
        os.close(self.lock_fd)

    def _save(self):
        if self._broken:
            raise ExperimentError("experiment_journal_unavailable")
        self._broken = True
        raw = dumps(self.state)
        if len(raw.encode("ascii")) > MAX_REPORT:
            raise ExperimentError("experiment_report_size")
        with self.db:
            self.db.execute(
                "INSERT INTO state VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value,sha256=excluded.sha256",
                (raw, hashlib.sha256(raw.encode("ascii")).hexdigest()),
            )
        self._broken = False

    def _check(self):
        if self._broken:
            raise ExperimentError("experiment_journal_unavailable")
        expected = [
            (variant["name"], seed)
            for variant in self.manifest["plan"]["variants"]
            for seed in self.manifest["plan"]["seeds"]
        ]
        if [
            (cell["variant"], cell["seed"]) for cell in self.state["cells"]
        ] != expected or any(
            cell["phase"] not in ("new", "preparing", "prepared", "done")
            for cell in self.state["cells"]
        ):
            raise ExperimentError("experiment_journal_changed")
        for name in self.state["evidence"]:
            if (
                type(name) is not str
                or Path(name).is_absolute()
                or ".." in Path(name).parts
            ):
                raise ExperimentError("experiment_evidence_changed")
            path = self.root
            for part in Path(name).parts:
                path = path / part
                if path.is_symlink():
                    raise ExperimentError("experiment_evidence_changed")
        if _hash(self.root / "manifest.json") != self.state[
            "manifest_sha256"
        ] or not _unchanged(self.root, self.state["evidence"]):
            raise ExperimentError("experiment_evidence_changed")
        adapter, metrics, worlds = _adapter(self.manifest["plan"]["adapter"])
        if (
            _identity(
                self.manifest["plan"]["adapter"], adapter.MODULE, self.manifest["env"]
            )
            != self.manifest["identity"]
            or metrics != self.manifest["units"]
            or list(worlds) != self.manifest["actor_worlds"]
        ):
            raise ExperimentError("experiment_identity_changed")
        return adapter

    def _cell_root(self, cell):
        path = self.root / f"{cell['variant']}-{cell['seed']}"
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ExperimentError("experiment_evidence_changed")
        return path

    def _totals(self, cell):
        return _ledger_accounting(cell["ledger"])

    def progress(self):
        self._check()
        rows = [
            {
                "variant": cell["variant"],
                "seed": cell["seed"],
                "phase": cell["phase"],
                "inference": self._totals(cell),
            }
            for cell in self.state["cells"]
        ]
        return {
            "cells": rows,
            "inference": _accounting(self.manifest["plan"], rows),
            "complete": (self.root / "summary.json").exists(),
        }

    def _retain(self, cell, name, value):
        path = self._cell_root(cell) / name
        _publish(
            path,
            value,
            limit=MAX_ARTIFACT
            if name in ("artifact.json", "replay.json")
            else MAX_REPORT,
        )
        relative = str(path.relative_to(self.root))
        self.state["evidence"][relative] = _hash(path)
        self._save()
        return relative

    def _prepare(self, cell, adapter):
        cell["phase"] = "preparing"
        self._save()
        root = self._cell_root(cell)
        root.mkdir(mode=0o700)
        variant = next(
            item
            for item in self.manifest["plan"]["variants"]
            if item["name"] == cell["variant"]
        )
        path = adapter.prepare(
            root / "original", deep_copy(variant["parameters"]), cell["seed"]
        )
        require_sync_result(path)
        case, hashes = _inputs(
            path, root, cell["seed"], tuple(self.manifest["actor_worlds"])
        )
        for name, sha in hashes.items():
            self.state["evidence"][f"{root.name}/{name}"] = sha
        generated = root / "input"
        generated.mkdir(mode=0o700)
        dataset = case["worlds"][self.manifest["plan"]["inference"]["world"]]["dataset"]
        if sum(value["dataset"] == dataset for value in case["worlds"].values()) != 1:
            raise ExperimentError("experiment_shared_inference_dataset")
        for index, (name, ref) in enumerate(case["datasets"].items()):
            data = loads((Path(path).parent / ref["path"]).read_bytes())
            if name == dataset:
                data = inference_population(data["actors"], seed=cell["seed"])
            destination = generated / f"input-{index}.json"
            _publish(destination, data)
            ref["path"] = destination.name
            ref["sha256"] = _hash(destination)
        destination = generated / "case.json"
        _publish(destination, case)
        hashes = {str(destination.relative_to(root)): _hash(destination)}
        hashes.update(
            {
                str((generated / ref["path"]).relative_to(root)): ref["sha256"]
                for ref in case["datasets"].values()
            }
        )
        cell["case"] = str(destination.relative_to(root))
        cell["config"] = deep_copy(self.manifest["plan"]["inference"]["config"])
        accounting = self.progress()["inference"]
        remaining = (
            accounting["budget"]["max_cost_microusd"]
            - accounting["accounted_cost_microusd"]
        )
        cell["config"]["max_cost_microusd"] = min(
            cell["config"]["max_cost_microusd"], remaining
        )
        for name, sha in hashes.items():
            self.state["evidence"][f"{root.name}/{name}"] = sha
        self._retain(
            cell,
            "inputs.json",
            {
                "sha256": hashes,
                "case_digest": _case_digest(str(destination), case["namespace"]),
            },
        )
        cell["phase"] = "prepared"
        self._save()

    def _session(self, cell):
        path = self._cell_root(cell) / "session"
        if path.exists():
            session = InferenceRun(path)
            if session.manifest["config"] != cell["config"] or session.manifest[
                "original_case_sha256"
            ] != _hash(self._cell_root(cell) / cell["case"]):
                session.close()
                raise ExperimentError("experiment_journal_changed")
            return session
        return InferenceRun.create(
            path,
            self._check().MODULE,
            self._cell_root(cell) / cell["case"],
            config=cell["config"],
            world=self.manifest["plan"]["inference"]["world"],
            env=self.manifest["env"],
            timeout_s=self.manifest["plan"]["timeout_s"],
        )

    def _reconcile(self, cell, session):
        steps = session._steps()
        for entry in cell["ledger"]:
            n = entry["sequence"]
            if (
                n >= len(steps)
                or loads(steps[n][1])["request_id"] != entry["request_id"]
            ):
                raise ExperimentError("experiment_journal_changed")
            request = loads(steps[n][1])
            if not equal(request, session._envelope(request["request"], n)):
                raise ExperimentError("experiment_journal_changed")
            if steps[n][2] == "responded":
                response = loads(steps[n][3])
                session._validate_response(request, response)
                if response["usage"]["cost_microusd"] > entry["reserved_cost_microusd"]:
                    raise ExperimentError("experiment_budget_changed")
                if entry["state"] == "accepted" and entry["usage"] != response["usage"]:
                    raise ExperimentError("experiment_journal_changed")
                entry.update(state="accepted", usage=response["usage"])
            elif entry["state"] == "accepted":
                raise ExperimentError("experiment_journal_changed")
        if len(steps) > len(cell["ledger"]) + 1:
            raise ExperimentError("experiment_journal_changed")
        if len(steps) == len(cell["ledger"]) + 1 and steps[-1][2] != "pending":
            raise ExperimentError("experiment_unreserved_dispatch")
        self._save()

    def _reserve(self, cell, request):
        n = request["sequence"]
        if n < len(cell["ledger"]):
            if cell["ledger"][n]["request_id"] != request["request_id"]:
                raise ExperimentError("experiment_journal_changed")
            return
        if n != len(cell["ledger"]):
            raise ExperimentError("experiment_journal_changed")
        totals = self.progress()["inference"]
        cost = request["limits"]["max_cost_microusd"]
        if totals["reserved_calls"] >= totals["budget"]["max_calls"]:
            raise InferenceError("experiment_call_budget")
        if (
            totals["accounted_cost_microusd"] + cost
            > totals["budget"]["max_cost_microusd"]
        ):
            raise InferenceError("experiment_cost_budget")
        cell["ledger"].append(
            {
                "sequence": n,
                "request_id": request["request_id"],
                "state": "reserved",
                "reserved_cost_microusd": cost,
            }
        )
        self._save()

    def _snapshot(self, cell, session):
        return {
            "format": "seam-experiment-inference",
            "version": 1,
            "config": cell["config"],
            "accounting": self._totals(cell),
            "ledger": deep_copy(cell["ledger"]),
            "steps": [
                {
                    "sequence": n,
                    "request": loads(request),
                    "state": state,
                    "response": None if response is None else loads(response),
                    "error": error,
                }
                for n, request, state, response, error in session._steps()
            ],
        }

    def _finish(self, cell, *, session=None, result=None, error=None):
        row = {
            "variant": cell["variant"],
            "seed": cell["seed"],
            "status": "failed",
            "inference": self._totals(cell),
        }
        if session is not None:
            self._reconcile(cell, session)
            row["inference"] = self._totals(cell)
            self._retain(cell, "inference.json", self._snapshot(cell, session))
            row["inference"]["evidence"] = (
                f"{self._cell_root(cell).name}/inference.json"
            )
            if result is None:
                last = session._get("last_result")
                if last is not None:
                    path = session.root / last["artifact"]
                    if _hash(path) != last["sha256"]:
                        raise ExperimentError("experiment_evidence_changed")
                    result = ProcessResult(
                        last["returncode"], str(path), loads(path.read_bytes()), ""
                    )
        if result is not None:
            name = self._retain(cell, "artifact.json", result.artifact)
            row.update(
                returncode=result.returncode,
                artifact=name,
                artifact_sha256=self.state["evidence"][name],
                digest=result.artifact["digest"],
            )
            trajectory = self._cell_root(cell) / "trajectory-input"
            trajectory.mkdir(mode=0o700, exist_ok=True)
            case = loads((Path(result.artifact_path).parent / "case.json").read_bytes())
            hashes = {}
            for ref in case["datasets"].values():
                relative = f"trajectory-input/{ref['path']}"
                retained = self._retain(
                    cell,
                    relative,
                    loads(
                        (Path(result.artifact_path).parent / ref["path"]).read_bytes()
                    ),
                )
                hashes[relative] = self.state["evidence"][retained]
            retained = self._retain(cell, "trajectory-input/case.json", case)
            hashes["trajectory-input/case.json"] = self.state["evidence"][retained]
            row["trajectory_inputs"] = self._retain(
                cell, "trajectory.json", {"sha256": hashes}
            )
        if error is None and result is not None:
            try:
                if result.returncode != 0 or result.artifact["status"] != "passed":
                    raise ExperimentError("experiment_product_failed")
                if any(
                    result.artifact.get("world_states", {}).get(world, {}).get("sealed")
                    is not True
                    for world in self.manifest["actor_worlds"]
                ):
                    raise ExperimentError("experiment_unsealed_actors")
                replay = session.replay()
                for world, frame in result.artifact["world_states"].items():
                    if world != self.manifest["plan"]["inference"][
                        "world"
                    ] and not equal(frame, replay.artifact["world_states"][world]):
                        raise ExperimentError("experiment_replay_failed")
                self._retain(cell, "replay.json", replay.artifact)
                replay_root = self._cell_root(cell) / "replay-input"
                replay_root.mkdir(mode=0o700, exist_ok=True)
                replay_case = loads(
                    (Path(replay.artifact_path).parent / "case.json").read_bytes()
                )
                for ref in replay_case["datasets"].values():
                    self._retain(
                        cell,
                        f"replay-input/{ref['path']}",
                        loads(
                            (
                                Path(replay.artifact_path).parent / ref["path"]
                            ).read_bytes()
                        ),
                    )
                self._retain(cell, "replay-input/case.json", replay_case)
                values = self._check().measure(deep_copy(result.artifact))
                require_sync_result(values)
                if (
                    type(values) is not dict
                    or set(values) != set(self.manifest["units"])
                    or any(
                        type(value) is not int or not INT64_MIN <= value <= INT64_MAX
                        for value in values.values()
                    )
                ):
                    raise ExperimentError("experiment_metrics")
                self._check()
                row.update(
                    status="passed",
                    metrics=values,
                    replay=f"{self._cell_root(cell).name}/replay.json",
                    replay_case=f"{self._cell_root(cell).name}/replay-input/case.json",
                )
            except (ExperimentError, InferenceError) as exc:
                error = str(exc)
            except Exception:
                error = "experiment_metrics"
        if error is not None:
            row["error"] = error
        self._retain(cell, "result.json", row)
        cell.update(phase="done", row=row)
        self._save()

    def step(self, provider):
        """Advance at most one provider dispatch; repeated completion reads never call a provider."""
        adapter = self._check()
        if not callable(provider):
            raise ExperimentError("bad_provider")
        cell = next(
            (item for item in self.state["cells"] if item["phase"] != "done"), None
        )
        if cell is None:
            rows = [item["row"] for item in self.state["cells"]]
            summary = _summary(self.manifest["plan"], self.manifest["units"], rows)
            summary["evidence"] = self.state["evidence"]
            summary["sha256"] = digest(summary)
            _publish(self.root / "summary.json", summary)
            return read_experiment(self.root)
        if cell["phase"] == "new":
            try:
                self._prepare(cell, adapter)
            except ExperimentError as exc:
                if str(exc) in (
                    "experiment_identity_changed",
                    "experiment_evidence_changed",
                    "experiment_report_size",
                    "experiment_journal_unavailable",
                ):
                    raise
                self._finish(cell, error=str(exc))
                return None
            except Exception:
                self._finish(cell, error="experiment_prepare")
                return None
        elif cell["phase"] == "preparing":
            self._cell_root(cell).mkdir(mode=0o700, exist_ok=True)
            self._finish(cell, error="experiment_prepare_uncertain")
            return None
        with self._session(cell) as session:
            self._reconcile(cell, session)
            try:
                request = session.next_request()
                if isinstance(request, ProcessResult):
                    self._finish(cell, session=session, result=request)
                else:
                    self._reserve(cell, request)
                    session.invoke(provider, _close_fds=(self.lock_fd,))
                    self._reconcile(cell, session)
            except InferenceError as exc:
                if "uncertain" in str(exc):
                    raise ExperimentError("experiment_inference_uncertain") from None
                if str(exc) in (
                    "inference_identity_changed",
                    "manifest_changed",
                    "journal_changed",
                ):
                    raise ExperimentError(str(exc)) from None
                self._finish(cell, session=session, error=str(exc))
        return None

    def run(self, provider):
        while True:
            summary = self.step(provider)
            if summary is not None:
                return summary

    def accept(self, response):
        """Resolve the current reserved request with an explicitly supplied known response, never a retry."""
        self._check()
        cell = next(
            (item for item in self.state["cells"] if item["phase"] == "prepared"), None
        )
        if cell is None:
            raise ExperimentError("experiment_no_pending_response")
        with self._session(cell) as session:
            steps = session._steps()
            if not steps or len(cell["ledger"]) != len(steps):
                raise ExperimentError("experiment_no_reserved_request")
            session.accept(response)
            self._reconcile(cell, session)

    def abandon_uncertain(self):
        """Explicitly fail an uncertain cell while retaining its full reservation and dispatch evidence."""
        self._check()
        cell = next(
            (item for item in self.state["cells"] if item["phase"] == "prepared"), None
        )
        if cell is None:
            raise ExperimentError("experiment_no_uncertain_request")
        with self._session(cell) as session:
            steps = session._steps()
            if not steps or steps[-1][2] != "in_flight":
                raise ExperimentError("experiment_no_uncertain_request")
            self._finish(
                cell, session=session, error="experiment_inference_abandoned_uncertain"
            )


def replay_experiment(directory, variant, seed, output, *, env=None):
    """Execute retained strict tapes in a new output file without touching journals or calling providers."""
    root = Path(directory).resolve()
    summary = read_experiment(root)
    manifest = loads((root / "manifest.json").read_bytes())
    if summary["version"] != 2:
        raise ExperimentError("experiment_not_inference")
    env = manifest["env"] if env is None else env
    adapter, _, _ = _adapter(manifest["plan"]["adapter"])
    if (
        _identity(manifest["plan"]["adapter"], adapter.MODULE, env)
        != manifest["identity"]
    ):
        raise ExperimentError("experiment_identity_changed")
    rows = [
        row
        for row in summary["runs"]
        if row["variant"] == variant and type(seed) is int and row["seed"] == seed
    ]
    if len(rows) != 1 or rows[0]["status"] != "passed":
        raise ExperimentError("experiment_replay_unavailable")
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(root):
        raise ExperimentError("experiment_replay_output")
    row = rows[0]
    path = root / row["replay_case"]
    case = loads(path.read_bytes())
    result = run_product(
        adapter.MODULE,
        str(path),
        case["namespace"],
        str(output),
        env=env,
        timeout=manifest["plan"]["timeout_s"],
    )
    if result.returncode != 0 or not equal(
        result.artifact, loads((root / row["replay"]).read_bytes())
    ):
        raise ExperimentError("experiment_replay_failed")
    return result
