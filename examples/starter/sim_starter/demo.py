"""Run a free scripted integration proof; never silently invoke a model or overwrite evidence."""

import argparse
from pathlib import Path
import shlex
import sys

from seam.canon import dumps
from seam.experiments import ExperimentError, read_experiment
from seam.inference import InferenceError, settings
from seam.inference_experiments import InferenceExperiment, plan, replay_experiment
from seam.personas import choice_prompt
from seam_viewer.evidence import open_experiments

from sim_starter.experiments import variants

PROMPT = choice_prompt(
    "An order needs a confirmed stock reservation before its product-owned fulfillment timer runs. Sold-out orders fail the buyer's goal. A reservation timeout is not proof of failure or fulfillment. Only place my own order and wait for its observed outcome."
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def scripted_provider(envelope):
    require(
        envelope["provider"] == "scripted"
        and envelope["model"] == "first-valid-choice-v1",
        "not a scripted starter request",
    )
    return {
        "request_id": envelope["request_id"],
        "model": envelope["model"],
        "returned_model": envelope["model"],
        "decision": {"choice": envelope["request"]["choices"][0]["id"]},
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_microusd": 0},
    }


def viewer_command(root):
    return shlex.join(
        [
            sys.executable,
            "-m",
            "seam_viewer",
            "--experiment",
            f"Orders={root / 'experiment'}",
            "--port",
            "0",
        ]
    )


def demo(root):
    specification = plan(
        "sim_starter.experiments",
        variants(),
        [7, 19],
        baseline="healthy",
        config=settings("scripted", "first-valid-choice-v1", PROMPT, max_calls=32),
        max_calls=128,
    )
    print(
        "Running four paired scenarios with a scripted controller, not an LLM...",
        file=sys.stderr,
        flush=True,
    )
    with InferenceExperiment.create(root / "experiment", specification) as session:
        summary = session.run(scripted_provider)
    require(
        summary["status"] == "passed" and len(summary["runs"]) == 4,
        "experiment checks failed",
    )
    require(
        read_experiment(root / "experiment") == summary,
        "retained summary failed verification",
    )
    require(
        summary["comparisons"]["scarce"]["paired_seeds"] == [7, 19],
        "missing paired seeds",
    )
    viewer = open_experiments([("Orders", root / "experiment")])[0]
    require(viewer.overview(0)["available"], "viewer evidence unavailable")
    print(
        "Verifying product outcomes and eight offline recorded replays...",
        file=sys.stderr,
        flush=True,
    )
    rows = []
    for index, row in enumerate(summary["runs"]):
        metrics = row["metrics"]
        fulfilled = 2 if row["variant"] == "healthy" else 1
        require(
            metrics
            == {
                "fulfilled": fulfilled,
                "rejected": 2 - fulfilled,
                "pending": 0,
                "ack_unknown": 0,
                "allocated": fulfilled,
                "unfinished_actors": 0,
            },
            "unexpected product outcome",
        )
        require(
            viewer.run(index)["artifact"]["actors"][0]["mode"] == "inference",
            "wrong decision mode",
        )
        left = replay_experiment(
            root / "experiment",
            row["variant"],
            row["seed"],
            root / f"replay-{index}-a.json",
        )
        right = replay_experiment(
            root / "experiment",
            row["variant"],
            row["seed"],
            root / f"replay-{index}-b.json",
        )
        require(
            Path(left.artifact_path).read_bytes()
            == Path(right.artifact_path).read_bytes(),
            "replay mismatch",
        )
        rows.append(
            {"variant": row["variant"], "seed": row["seed"], "metrics": metrics}
        )
        print(
            f"{row['variant']} / seed {row['seed']}: {fulfilled} fulfilled; {2 - fulfilled} rejected; {metrics['allocated']} stock reservations.",
            file=sys.stderr,
        )
    print(
        f"Engine checks passed; rejected orders remain failed buyer goals.\nEvidence: {root}\nOpen the viewer from any folder:\n{viewer_command(root)}",
        file=sys.stderr,
    )
    return {
        "status": "passed",
        "controller": "scripted-not-an-LLM",
        "paid_calls": 0,
        "byte_identical_replays": 8,
        "viewer_verified": True,
        "runs": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scripted", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        root = args.out.expanduser().resolve()
        root.mkdir(mode=0o700, exist_ok=False)
    except OSError as error:
        print(
            f"Cannot create output: {error}\nUse an existing writable parent and a new --out directory. Existing evidence is not overwritten.",
            file=sys.stderr,
        )
        return 2
    try:
        print(dumps(demo(root)))
        return 0
    except (ExperimentError, InferenceError, RuntimeError) as error:
        print(
            f"Demo failed: {error}\nRetained evidence: {root}. Do not overwrite journals or retry uncertain provider calls.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
