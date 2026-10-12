"""A packaged, provider-free experiment adapter exercising actual seeded counter handlers."""

import argparse
from pathlib import Path

from seam import dataset_ref
from seam.actors import population
from seam.canon import dumps, loads
from seam.experiments import _write, plan, read_experiment, run_experiment

MODULE = "seam.proof.actors"
ACTOR_WORLDS = ("actors",)
METRICS = {"count": "items", "actions": "actions"}


def prepare(directory, parameters, seed):
    if type(parameters) is not dict or set(parameters) != {"goal"}:
        raise ValueError("counter parameters require goal")
    goal = parameters["goal"]
    if type(goal) is not int or not 1 <= goal <= 30:
        raise ValueError("counter goal must be between 1 and 30")
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    data = directory / "actors.json"
    _write(
        data,
        population(
            [
                {
                    "id": "alice",
                    "policy": "counter_v1",
                    "profile": {"goal": goal, "delay_ns": 1},
                    "memory": {"actions": 0},
                    "actions": ["add"],
                    "start_ns": 0,
                    "max_decisions": 40,
                }
            ],
            seed=seed,
        ),
    )
    case = {
        "format": "seam-case",
        "version": 3,
        "name": "counter-experiment",
        "namespace": "sim-counter-experiment",
        "seed": seed,
        "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "config": {},
        "initial_state": {"count": 0},
        "datasets": {"actors": dataset_ref(data, 0)},
        "worlds": {"actors": {"dataset": "actors"}},
        "ports": {"actors": {"mode": "world", "world": "actors"}},
        "arrivals": [
            {"at_ns": 0, "handler": "actors_start", "body": {}},
            {"at_ns": 40, "handler": "actors_finish", "body": {}},
        ],
        "same_time_order": "timers_first",
        "stop": {"when": "quiescence"},
        "assertions": [],
    }
    path = directory / "case.json"
    _write(path, case)
    return path


def measure(artifact):
    return {
        "count": artifact["final_state"]["count"],
        "actions": sum(
            row["delivery"] is not None
            for row in artifact["world_states"]["actors"]["decisions"]
        ),
    }


def demo_plan():
    return plan(
        __name__,
        [
            {"name": "baseline", "parameters": {"goal": 8}},
            {"name": "larger", "parameters": {"goal": 12}},
        ],
        [7, 19, 31],
        baseline="baseline",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    if args.installed:
        import seam

        assert "site-packages" in Path(seam.__file__).resolve().parts, seam.__file__
    summary = run_experiment(args.out, demo_plan())
    assert summary["status"] == "passed", summary
    assert summary["comparisons"]["larger"]["paired_seeds"] == [7, 19, 31]
    assert summary["comparisons"]["larger"]["delta"]["count"]["min"] > 0
    assert loads((args.out / "summary.json").read_bytes()) == summary
    assert read_experiment(args.out) == summary
    print(
        dumps(
            {
                "proof": "passed",
                "runs": len(summary["runs"]),
                "paired_seeds": [7, 19, 31],
                "paid_calls": 0,
            }
        )
    )
