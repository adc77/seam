"""Sequential offline experiments with isolated cases, declared metrics, and paired seed comparisons."""

import argparse
import hashlib
from importlib import import_module
import os
from pathlib import Path
import platform
import re
import sys

from seam.actors import _population
from seam.canon import INT64_MAX, INT64_MIN, deep_copy, digest, dumps, loads
from seam.case import MAX_CASE
from seam.dataset import load_datasets
from seam.errors import SeamError
from seam.runner import MODULE, _case_digest, _product_digest, run_product
from seam.runtime import require_sync, require_sync_result
from seam.version import __version__

MAX_RUNS = 1000
MAX_PLAN = 1024 * 1024
MAX_REPORT = 16 * 1024 * 1024
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class ExperimentError(ValueError):
    """An invalid experiment or changed execution contract; existing evidence is never replaced."""


def plan(adapter, variants, seeds, *, baseline, timeout_s=30):
    """Declare named parameter sets and distinct paired seeds without loading product code."""
    value = {
        "format": "seam-experiment",
        "version": 1,
        "adapter": adapter,
        "variants": variants,
        "seeds": seeds,
        "baseline": baseline,
        "timeout_s": timeout_s,
    }
    return _plan(value)


def _plan(value):
    if (
        type(value) is dict
        and type(value.get("version")) is int
        and value["version"] == 2
    ):
        from seam.inference_experiments import _plan as inference_plan

        return inference_plan(value)
    try:
        value = deep_copy(value)
        if (
            type(value) is not dict
            or set(value)
            != {
                "format",
                "version",
                "adapter",
                "variants",
                "seeds",
                "baseline",
                "timeout_s",
            }
            or value["format"] != "seam-experiment"
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["adapter"]) is not str
            or not MODULE.fullmatch(value["adapter"])
        ):
            raise ExperimentError("experiment_plan")
        variants, seeds = value["variants"], value["seeds"]
        if type(variants) is not list or not 1 <= len(variants) <= 32:
            raise ExperimentError("experiment_plan")
        names = []
        for variant in variants:
            if (
                type(variant) is not dict
                or set(variant) != {"name", "parameters"}
                or type(variant["name"]) is not str
                or not NAME.fullmatch(variant["name"])
                or type(variant["parameters"]) is not dict
            ):
                raise ExperimentError("experiment_plan")
            names.append(variant["name"])
        if len(set(names)) != len(names) or value["baseline"] not in names:
            raise ExperimentError("experiment_plan")
        if (
            type(seeds) is not list
            or not seeds
            or len(seeds) * len(variants) > MAX_RUNS
            or any(
                type(seed) is not int or not 0 <= seed <= INT64_MAX for seed in seeds
            )
            or len(set(seeds)) != len(seeds)
            or type(value["timeout_s"]) is not int
            or not 1 <= value["timeout_s"] <= 300
            or len(dumps(value).encode("ascii")) > MAX_PLAN
        ):
            raise ExperimentError("experiment_plan")
        return value
    except ExperimentError:
        raise
    except Exception:
        raise ExperimentError("experiment_plan") from None


def _write(path, value):
    data = (dumps(value) + "\n").encode("ascii")
    if len(data) > MAX_REPORT:
        raise ExperimentError("experiment_report_size")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _hash(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(262144), b""):
            result.update(chunk)
    return result.hexdigest()


def _adapter(name):
    try:
        module = import_module(name)
        if type(module.MODULE) is not str or not MODULE.fullmatch(module.MODULE):
            raise ExperimentError("experiment_adapter")
        if not callable(module.prepare) or not callable(module.measure):
            raise ExperimentError("experiment_adapter")
        require_sync(module.prepare)
        require_sync(module.measure)
        metrics = deep_copy(module.METRICS)
        if type(metrics) is not dict or not 1 <= len(metrics) <= 32:
            raise ExperimentError("experiment_adapter")
        for key, unit in metrics.items():
            if (
                type(key) is not str
                or not NAME.fullmatch(key)
                or type(unit) is not str
                or not NAME.fullmatch(unit)
            ):
                raise ExperimentError("experiment_adapter")
        worlds = module.ACTOR_WORLDS
        if type(worlds) is not tuple or any(
            type(name) is not str or not NAME.fullmatch(name) for name in worlds
        ):
            raise ExperimentError("experiment_adapter")
        if len(set(worlds)) != len(worlds):
            raise ExperimentError("experiment_adapter")
        return module, metrics, worlds
    except ExperimentError:
        raise
    except Exception:
        raise ExperimentError("experiment_adapter") from None


def _identity(adapter, module, env):
    child = {
        key: os.environ[key] for key in ("PATH", "PYTHONPATH") if key in os.environ
    }
    child.update(PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", TZ="UTC")
    child.update(env)
    path = child.get("PYTHONPATH", "")
    return {
        "adapter": _product_digest(adapter, path),
        "product": _product_digest(module, path),
        "sdk": _product_digest("seam", path),
        "python": platform.python_version(),
        "sdk_version": __version__,
        "environment": digest(child),
    }


def _inputs(path, cell, seed, worlds):
    path = Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(cell.resolve()):
        raise ExperimentError("experiment_case_path")
    with path.open("rb") as handle:
        raw = handle.read(MAX_CASE + 1)
    if len(raw) > MAX_CASE:
        raise ExperimentError("experiment_case_size")
    case = loads(raw)
    if type(case) is not dict or "resume" in case or "checkpoint_after" in case:
        raise ExperimentError("experiment_requires_fresh_case")
    if type(case.get("seed")) is not int or case["seed"] != seed:
        raise ExperimentError("experiment_seed")
    if _case_digest(str(path), case.get("namespace")) is None:
        raise ExperimentError("experiment_case")
    if any(
        spec.get("mode") == "recording" or "tape" in spec
        for spec in case["ports"].values()
    ):
        raise ExperimentError("experiment_external_tape")
    refs = case.get("datasets", {})
    for ref in refs.values():
        candidate = path.parent / ref["path"]
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(
            cell.resolve()
        ):
            raise ExperimentError("experiment_dataset_path")
    datasets = load_datasets(str(path), refs, case["clock"]["start_ns"])
    actor_datasets = set()
    for name, data in datasets.items():
        if type(data) is dict and data.get("format") == "seam-actors":
            actor_datasets.add(name)
            actors = _population(data)
            if actors["mode"] != "rules" or actors["seed"] != seed:
                raise ExperimentError("experiment_actor_seed_or_mode")
    actual_worlds = {
        name
        for name, world in case.get("worlds", {}).items()
        if world["dataset"] in actor_datasets
    }
    if actual_worlds != set(worlds):
        raise ExperimentError("experiment_actor_world")
    for name in worlds:
        data = datasets[case["worlds"][name]["dataset"]]
        if type(data) is not dict or data.get("format") != "seam-actors":
            raise ExperimentError("experiment_actor_world")
    hashes = {str(path.resolve().relative_to(cell.resolve())): _hash(path)}
    hashes.update(
        {
            str((path.parent / ref["path"]).resolve().relative_to(cell.resolve())): ref[
                "sha256"
            ]
            for ref in refs.values()
        }
    )
    return case, hashes


def _unchanged(cell, hashes):
    return all(_hash(cell / name) == value for name, value in hashes.items())


def _stats(values):
    if not values:
        return {"count": 0, "sum": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "sum": sum(values),
        "min": min(values),
        "max": max(values),
        "mean": {"numerator": sum(values), "denominator": len(values)},
    }


def _summary(spec, metrics, rows):
    lookup = {(row["variant"], row["seed"]): row for row in rows}
    aggregates, comparisons = {}, {}
    for variant in spec["variants"]:
        name = variant["name"]
        passed = [
            lookup[name, seed]
            for seed in spec["seeds"]
            if lookup[name, seed]["status"] == "passed"
        ]
        aggregates[name] = {
            "passed": len(passed),
            "failed": len(spec["seeds"]) - len(passed),
            "metrics": {
                key: _stats([row["metrics"][key] for row in passed]) for key in metrics
            },
        }
        if name == spec["baseline"]:
            continue
        pairs, excluded = [], []
        for seed in spec["seeds"]:
            base, other = lookup[spec["baseline"], seed], lookup[name, seed]
            if base["status"] != "passed" or other["status"] != "passed":
                excluded.append(seed)
            else:
                pairs.append(
                    {
                        "seed": seed,
                        "delta": {
                            key: other["metrics"][key] - base["metrics"][key]
                            for key in metrics
                        },
                    }
                )
        comparisons[name] = {
            "baseline": spec["baseline"],
            "status": "complete" if not excluded else "incomplete",
            "paired_seeds": [row["seed"] for row in pairs],
            "excluded_seeds": excluded,
            "pairs": pairs,
            "delta": {
                key: _stats([row["delta"][key] for row in pairs]) for key in metrics
            },
        }
    result = {
        "format": "seam-experiment-summary",
        "version": 1,
        "status": "passed"
        if all(row["status"] == "passed" for row in rows)
        else "failed",
        "plan_sha256": digest(spec),
        "units": metrics,
        "runs": rows,
        "aggregates": aggregates,
        "comparisons": comparisons,
    }
    if spec["version"] == 2:
        from seam.inference_experiments import _accounting

        result["version"] = 2
        result["inference"] = _accounting(spec, rows)
    return result


def run_experiment(directory, specification, *, env=None):
    """Run each variant/seed in a fresh directory; failures remain visible and incomplete pairs are labeled."""
    spec = _plan(specification)
    if spec["version"] != 1:
        raise ExperimentError("experiment_requires_inference_controller")
    if env is None:
        env = {}
    if (
        type(env) is not dict
        or set(env) - {"PYTHONPATH"}
        or any(type(value) is not str for value in env.values())
    ):
        raise ExperimentError("experiment_environment")
    env = dict(env)
    adapter, metrics, worlds = _adapter(spec["adapter"])
    module, prepare, measure = adapter.MODULE, adapter.prepare, adapter.measure
    identity = _identity(spec["adapter"], module, env)
    root = Path(directory).resolve()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    _write(
        root / "manifest.json",
        {
            "format": "seam-experiment-manifest",
            "version": 1,
            "plan": spec,
            "identity": identity,
            "units": metrics,
            "actor_worlds": list(worlds),
        },
    )
    evidence = {"manifest.json": _hash(root / "manifest.json")}
    rows = []
    for variant in spec["variants"]:
        for seed in spec["seeds"]:
            if _identity(spec["adapter"], module, env) != identity:
                raise ExperimentError("experiment_identity_changed")
            if _hash(root / "manifest.json") != evidence["manifest.json"]:
                raise ExperimentError("experiment_evidence_changed")
            cell = root / f"{variant['name']}-{seed}"
            cell.mkdir(mode=0o700)
            row = {"variant": variant["name"], "seed": seed, "status": "failed"}
            try:
                path = prepare(cell / "input", deep_copy(variant["parameters"]), seed)
                require_sync_result(path)
                case, hashes = _inputs(path, cell, seed, worlds)
                _write(
                    cell / "inputs.json",
                    {
                        "sha256": hashes,
                        "case_digest": _case_digest(str(path), case["namespace"]),
                    },
                )
                evidence.update(
                    {f"{cell.name}/{name}": sha for name, sha in hashes.items()}
                )
                evidence[f"{cell.name}/inputs.json"] = _hash(cell / "inputs.json")
            except ExperimentError as exc:
                row["error"] = str(exc)
            except Exception:
                row["error"] = "experiment_prepare"
            else:
                if _identity(spec["adapter"], module, env) != identity:
                    raise ExperimentError("experiment_identity_changed")
                result = run_product(
                    module,
                    str(path),
                    case["namespace"],
                    str(cell / "artifact.json"),
                    timeout=spec["timeout_s"],
                    env=env,
                )
                row.update(
                    returncode=result.returncode,
                    artifact=f"{cell.name}/artifact.json",
                    artifact_sha256=_hash(cell / "artifact.json"),
                    digest=result.artifact["digest"],
                )
                evidence[row["artifact"]] = row["artifact_sha256"]
                try:
                    if not _unchanged(cell, hashes):
                        raise ExperimentError("experiment_inputs_changed")
                    if result.returncode != 0 or result.artifact["status"] != "passed":
                        raise ExperimentError("experiment_product_failed")
                    provenance = result.artifact["provenance"]
                    if (
                        provenance["product"]["sha256"] != identity["product"]
                        or provenance["python"] != identity["python"]
                        or provenance["sdk_version"] != identity["sdk_version"]
                        or provenance["environment_sha256"] != identity["environment"]
                    ):
                        raise ExperimentError("experiment_identity_changed")
                    states = result.artifact.get("world_states", {})
                    if any(
                        states.get(name, {}).get("sealed") is not True
                        for name in worlds
                    ):
                        raise ExperimentError("experiment_unsealed_actors")
                    values = measure(deep_copy(result.artifact))
                    require_sync_result(values)
                    if (
                        type(values) is not dict
                        or set(values) != set(metrics)
                        or any(
                            type(value) is not int
                            or not INT64_MIN <= value <= INT64_MAX
                            for value in values.values()
                        )
                    ):
                        raise ExperimentError("experiment_metrics")
                    if (
                        not _unchanged(cell, hashes)
                        or _hash(cell / "artifact.json") != row["artifact_sha256"]
                    ):
                        raise ExperimentError("experiment_inputs_changed")
                    row.update(status="passed", metrics=dict(values))
                except ExperimentError as exc:
                    row["error"] = str(exc)
                except Exception:
                    row["error"] = "experiment_metrics"
            if _identity(spec["adapter"], module, env) != identity:
                raise ExperimentError("experiment_identity_changed")
            _write(cell / "result.json", row)
            evidence[f"{cell.name}/result.json"] = _hash(cell / "result.json")
            rows.append(row)
    if not _unchanged(root, evidence):
        raise ExperimentError("experiment_evidence_changed")
    summary = _summary(spec, metrics, rows)
    summary["evidence"] = evidence
    summary["sha256"] = digest(summary)
    _write(root / "summary.json", summary)
    return summary


def read_experiment(directory):
    """Verify a completed report and its retained inputs/artifacts without rerunning product code."""
    root = Path(directory)
    try:
        with (root / "summary.json").open("rb") as handle:
            raw = handle.read(MAX_REPORT + 1)
        if len(raw) > MAX_REPORT:
            raise ExperimentError("experiment_report_size")
        summary = loads(raw)
        checksum = summary.pop("sha256")
        if digest(summary) != checksum:
            raise ExperimentError("experiment_report_changed")
        evidence = summary["evidence"]
        if type(evidence) is not dict or "manifest.json" not in evidence:
            raise ExperimentError("experiment_evidence_changed")
        for name, sha in evidence.items():
            path = root / name
            if (
                type(name) is not str
                or Path(name).is_absolute()
                or ".." in Path(name).parts
                or path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve())
                or _hash(path) != sha
            ):
                raise ExperimentError("experiment_evidence_changed")
        manifest = loads((root / "manifest.json").read_bytes())
        specification = _plan(manifest["plan"])
        cells = [
            (variant["name"], seed)
            for variant in specification["variants"]
            for seed in specification["seeds"]
        ]
        if [(row["variant"], row["seed"]) for row in summary["runs"]] != cells:
            raise ExperimentError("experiment_report_changed")
        for row in summary["runs"]:
            name = f"{row['variant']}-{row['seed']}/result.json"
            if name not in evidence or loads((root / name).read_bytes()) != row:
                raise ExperimentError("experiment_report_changed")
            if (
                "artifact" in row
                and evidence.get(row["artifact"]) != row["artifact_sha256"]
            ):
                raise ExperimentError("experiment_evidence_changed")
        if specification["version"] == 2:
            from seam.inference_experiments import _verify_inference

            _verify_inference(root, specification, summary["runs"], evidence)
        expected = _summary(specification, manifest["units"], summary["runs"])
        expected["evidence"] = evidence
        if expected != summary:
            raise ExperimentError("experiment_report_changed")
        summary["sha256"] = checksum
        return summary
    except ExperimentError:
        raise
    except Exception:
        raise ExperimentError("experiment_incomplete_or_invalid") from None


def main():
    parser = argparse.ArgumentParser(
        description="Run isolated offline scenario comparisons."
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        with args.plan.open("rb") as handle:
            data = handle.read(MAX_PLAN + 1)
        if len(data) > MAX_PLAN:
            raise ExperimentError("experiment_plan")
        summary = run_experiment(args.out, loads(data))
    except (ExperimentError, OSError, ValueError, SeamError) as exc:
        code = str(exc) if isinstance(exc, ExperimentError) else "experiment_input"
        print(code, file=sys.stderr)
        return 2
    print(
        dumps({"status": summary["status"], "summary": str(args.out / "summary.json")})
    )
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
