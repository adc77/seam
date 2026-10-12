"""Installed counter ensemble proof with fresh-process continuation and provider-free strict replays."""

import argparse
from pathlib import Path
import subprocess
import sys

from seam.canon import dumps
from seam.experiments import read_experiment
from seam.inference import settings
from seam.inference_experiments import InferenceExperiment, plan, replay_experiment
from seam.proof.experiments import demo_plan
from seam.proof.inference import fake_provider
from seam_viewer.evidence import open_experiments


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--continue", dest="continuing", action="store_true")
    args = parser.parse_args()
    if args.installed:
        import seam

        assert "site-packages" in Path(seam.__file__).resolve().parts, seam.__file__
    root = args.out.resolve()
    if args.continuing:
        with InferenceExperiment(root / "experiment") as session:
            summary = session.run(fake_provider)
        assert summary["status"] == "passed", summary["runs"]
        return
    root.mkdir(mode=0o700, exist_ok=False)
    base = demo_plan()
    base["variants"][0]["parameters"]["goal"] = 2
    base["variants"][1]["parameters"]["goal"] = 3
    spec = plan(
        base["adapter"],
        base["variants"],
        [7, 19],
        baseline=base["baseline"],
        config=settings(
            "fake", "counter-v1", "Return one authorized counter decision."
        ),
        max_calls=32,
    )
    with InferenceExperiment.create(root / "experiment", spec) as session:
        session.step(fake_provider)
    command = [
        sys.executable,
        "-m",
        "seam.proof.inference_experiments",
        "--out",
        str(root),
        "--continue",
    ]
    if args.installed:
        command.append("--installed")
    subprocess.run(command, check=True, timeout=120)
    summary = read_experiment(root / "experiment")
    assert summary["inference"]["reserved_calls"] == 14, summary["inference"]
    assert summary["comparisons"]["larger"]["paired_seeds"] == [7, 19]
    viewer = open_experiments([("Counter", root / "experiment")])[0]
    assert viewer.overview(0)["available"]
    for index, row in enumerate(summary["runs"]):
        assert viewer.run(index)["artifact"]["actors"][0]["mode"] == "inference"
        left = replay_experiment(
            root / "experiment",
            row["variant"],
            row["seed"],
            root / f"left-{index}.json",
        )
        right = replay_experiment(
            root / "experiment",
            row["variant"],
            row["seed"],
            root / f"right-{index}.json",
        )
        assert (
            Path(left.artifact_path).read_bytes()
            == Path(right.artifact_path).read_bytes()
        )
    print(
        dumps(
            {
                "proof": "passed",
                "installed": args.installed,
                "runs": 4,
                "fresh_process_recovery": True,
                "byte_identical_replay": True,
                "viewer_verified": True,
                "paid_calls": 0,
            }
        )
    )
