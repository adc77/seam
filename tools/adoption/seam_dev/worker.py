"""Inspect installed code and synthetic inputs from an isolated neutral working directory."""

import argparse
import hashlib
import importlib
from importlib import metadata
import inspect
from pathlib import Path
import sys
import sysconfig
import tempfile

from seam.canon import dumps, loads
from seam.dataset import load_datasets
from seam.errors import Refuse
from seam.experiments import ExperimentError, run_experiment

from seam_dev.project import (
    ProjectError,
    project_path,
    read_document,
    read_project,
    scenario,
)

MAX_CELLS = 32


def source_hashes(directory):
    result = {}
    for path in sorted(directory.rglob("*.py")):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ProjectError("source: Python files must not be symlinks")
        result[str(path.relative_to(directory))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    if not result:
        raise ProjectError("source: no Python source files found")
    return result


def installed_project(root, project):
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    sdk = metadata.distribution("seam")
    direct = sdk.read_text("direct_url.json")
    if direct is None:
        raise ProjectError(
            "sdk-pin: reinstall the published immutable Git dependency in a new environment"
        )
    try:
        identity = loads(direct)
    except (ValueError, Refuse, RecursionError):
        raise ProjectError("sdk-pin: invalid installed dependency metadata") from None
    if (
        type(identity) is not dict
        or identity.get("url") != "https://github.com/adc77/seam.git"
        or type(identity.get("vcs_info")) is not dict
        or identity.get("vcs_info", {}).get("commit_id") != project["sdk_commit"]
    ):
        raise ProjectError(
            "sdk-pin: installed SDK does not match the project pin; use a new environment"
        )
    metadata.distribution(project["distribution"])
    package = importlib.import_module(project["package"])
    if package.__file__ is None:
        raise ProjectError("install: expected an ordinary installed product package")
    installed = Path(package.__file__).resolve().parent
    if not installed.is_relative_to(purelib):
        raise ProjectError(
            "install: product is not installed in this environment; do not use editable installs"
        )
    if installed.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ProjectError(
            "install: keep the installed environment outside the system temporary directory so handler guards remain effective"
        )
    expected = source_hashes(root / project["package"])
    actual = source_hashes(installed)
    if actual != expected:
        raise ProjectError(
            "stale-install: product sources changed; install in a new environment and preserve old journals"
        )
    adapter = importlib.import_module(project["adapter"])
    importlib.import_module(project["module"])
    if getattr(adapter, "MODULE", None) != project["module"]:
        raise ProjectError(
            "adapter: MODULE must match the project simulation entry point"
        )
    for name in ("prepare", "measure"):
        value = getattr(adapter, name, None)
        if not callable(value) or inspect.iscoroutinefunction(value):
            raise ProjectError(
                "adapter: prepare and measure must be synchronous callables"
            )
    metrics = getattr(adapter, "METRICS", None)
    if (
        type(metrics) is not dict
        or not metrics
        or any(
            type(key) is not str or type(unit) is not str or not key or not unit
            for key, unit in metrics.items()
        )
    ):
        raise ProjectError("adapter: declare nonempty METRICS names and units")
    worlds = getattr(adapter, "ACTOR_WORLDS", None)
    if (
        type(worlds) is not tuple
        or not worlds
        or any(type(name) is not str or not name for name in worlds)
    ):
        raise ProjectError("adapter: declare ACTOR_WORLDS as a nonempty tuple")
    return adapter, actual, sdk.version


def preflight(directory, relative_plan=None):
    root, project = read_project(directory)
    filename = project["plan"] if relative_plan is None else relative_plan
    specification = scenario(project_path(root, filename), project)
    count = len(specification["variants"]) * len(specification["seeds"])
    if count > MAX_CELLS:
        raise ProjectError(
            "plan: companion v1 is limited to 32 cells; SDK limits are unchanged"
        )
    adapter, hashes, sdk_version = installed_project(root, project)
    with tempfile.TemporaryDirectory(
        prefix=".seam-preflight-", dir=root.parent
    ) as directory:
        work = Path(directory).resolve()
        for index, variant in enumerate(specification["variants"]):
            for seed in specification["seeds"]:
                cell = work / f"cell-{index}-{seed}"
                try:
                    prepared = adapter.prepare(cell, variant["parameters"], seed)
                    if inspect.isawaitable(prepared):
                        if inspect.iscoroutine(prepared):
                            prepared.close()
                        raise ProjectError("preparation: prepare returned an awaitable")
                    case = Path(prepared).resolve(strict=True)
                    if not case.is_relative_to(cell) or not case.is_file():
                        raise ProjectError(
                            "preparation: the case must stay inside its new cell directory"
                        )
                    document = read_document(case)
                    if document["namespace"] != project["namespace"]:
                        raise ProjectError(
                            "preparation: case namespace does not match the project"
                        )
                    load_datasets(
                        case, document["datasets"], document["clock"]["start_ns"]
                    )
                except ProjectError:
                    raise
                except Exception as error:
                    raise ProjectError(
                        f"preparation: cell {variant['name']} / seed {seed} failed ({type(error).__name__}); inspect the product's parameters and snapshot validation"
                    ) from None
    return specification, {
        "status": "ready",
        "project": str(root),
        "adapter": project["adapter"],
        "module": project["module"],
        "controller": "rules-not-an-LLM",
        "model_calls": 0,
        "cells_checked": count,
        "sdk_version": sdk_version,
        "sdk_commit": project["sdk_commit"],
        "source_hashes": hashes,
        "metric_units": adapter.METRICS,
        "limits": "Preflight stages trusted synthetic inputs; actual run still validates handlers and world invariants. This is not a sandbox.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--plan")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        specification, report = preflight(args.project, args.plan)
        if args.command == "run":
            if args.out is None:
                raise ProjectError("output: run requires a new --out directory")
            summary = run_experiment(Path(args.out), specification)
            report = {
                "status": summary["status"],
                "experiment": args.out,
                "controller": "rules-not-an-LLM",
                "model_calls": 0,
                "runs": summary["runs"],
                "note": "Engine checks are not business success; inspect actual product metrics.",
            }
        print(dumps(report))
        return 0 if report["status"] in ("ready", "passed") else 1
    except (
        ProjectError,
        ExperimentError,
        OSError,
        ImportError,
        metadata.PackageNotFoundError,
    ) as error:
        print(f"seam-dev: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
