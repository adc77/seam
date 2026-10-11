"""Installed fake-provider proof with a fresh-process journal continuation and provider-free replay."""

import argparse
from pathlib import Path
import platform
import subprocess
import sys

from seam import dataset_ref
from seam.actors import inference_population
from seam.canon import dumps, loads
from seam.inference import InferenceRun, settings
from seam.version import __version__


def fake_provider(envelope):
    request = envelope["request"]
    finished = request["observation"]["count"] >= request["profile"]["goal"]
    return {
        "request_id": envelope["request_id"],
        "model": envelope["model"],
        "returned_model": envelope["model"],
        "decision": {
            "action": None if finished else {"type": "add", "body": {"amount": 1}},
            "memory": {
                "actions": request["memory"]["actions"] + (0 if finished else 1)
            },
            "wake_after_ns": None if finished else 1,
        },
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_microusd": 0},
    }


def continue_run(root):
    with InferenceRun(root / "session") as session:
        result = session.run(fake_provider)
        left, right = session.replay(), session.replay()
        assert result.artifact["final_state"] == {"count": 2}, result.artifact
        assert (
            Path(left.artifact_path).read_bytes()
            == Path(right.artifact_path).read_bytes()
        )
        assert len(session._steps()) == 3
        summary = {
            "proof": "passed",
            "provider": "fake",
            "paid_calls": 0,
            "python": platform.python_version(),
            "sdk_version": __version__,
            "fresh_process_recovery": True,
            "byte_identical_replay": True,
            "decision_count": 3,
            "final_state": result.artifact["final_state"],
            "digest": result.artifact["digest"],
            "replay_digest": left.artifact["digest"],
        }
    (root / "summary.json").write_text(dumps(summary) + "\n")
    print(dumps(summary))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--continue", dest="continuing", action="store_true")
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    if args.installed:
        import seam

        if "site-packages" not in Path(seam.__file__).resolve().parts:
            raise SystemExit("proof requires an installed SDK")
    root = args.out.resolve()
    if args.continuing:
        continue_run(root)
        return
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    actors = inference_population(
        [
            {
                "id": "alice",
                "policy": "counter_v1",
                "profile": {"goal": 2, "delay_ns": 1},
                "memory": {"actions": 0},
                "actions": ["add"],
                "start_ns": 0,
                "max_decisions": 5,
            }
        ]
    )
    data = root / "actors.json"
    data.write_text(dumps(actors))
    case = {
        "format": "seam-case",
        "version": 3,
        "name": "inference-counter",
        "namespace": "sim-inference-counter",
        "seed": 77,
        "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "initial_state": {"count": 0},
        "config": {},
        "datasets": {"actors": dataset_ref(data, 0)},
        "worlds": {"actors": {"dataset": "actors"}},
        "ports": {"actors": {"mode": "world", "world": "actors"}},
        "arrivals": [
            {"at_ns": 0, "handler": "actors_start", "body": {}},
            {"at_ns": 30, "handler": "actors_finish", "body": {}},
        ],
        "same_time_order": "timers_first",
        "stop": {"when": "quiescence"},
        "assertions": [{"op": "state_is", "path": "count", "value": 2}],
    }
    path = root / "case.json"
    path.write_text(dumps(case))
    config = settings(
        "fake", "counter-v1", "Return only an authorized counter decision."
    )
    with InferenceRun.create(
        root / "session", "seam.proof.actors", path, config=config
    ) as session:
        session.accept(fake_provider(session.next_request()))
    command = [
        sys.executable,
        "-m",
        "seam.proof.inference",
        "--out",
        str(root),
        "--continue",
    ]
    if args.installed:
        command.append("--installed")
    child = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if child.returncode:
        raise SystemExit(child.stderr)
    assert loads((root / "summary.json").read_text())["proof"] == "passed"
    print(child.stdout.strip())
