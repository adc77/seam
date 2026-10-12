"""Actual model-mode ensembles, global reservations, fresh-process recovery, and strict offline replay."""

import copy
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from seam.canon import INT64_MAX, dumps, loads
from seam.experiments import ExperimentError, read_experiment, run_experiment
from seam.inference import InferenceRun, settings
from seam.inference_experiments import (
    InferenceExperiment,
    _plan,
    plan,
    replay_experiment,
)
from seam.proof import experiments as adapter
from seam.proof.inference import fake_provider
from seam_viewer.evidence import EvidenceError, open_experiments
from tests.support import REPO


def specification(
    *,
    seeds=(7,),
    goals=(1, 2),
    max_calls=50,
    cell_max_calls=32,
    cost=0,
    **settings_changes,
):
    base = adapter.demo_plan()
    for variant, goal in zip(base["variants"], goals):
        variant["parameters"]["goal"] = goal
    return plan(
        base["adapter"],
        base["variants"],
        list(seeds),
        baseline=base["baseline"],
        config=settings(
            "fake",
            "counter-v1",
            "Return an authorized counter decision.",
            max_calls=cell_max_calls,
            **settings_changes,
        ),
        max_calls=max_calls,
        max_cost_microusd=cost,
    )


def create(root, spec=None):
    return InferenceExperiment.create(
        root / "experiment",
        specification() if spec is None else spec,
        env={"PYTHONPATH": REPO},
    )


def slow_provider(request):
    time.sleep(2)
    return fake_provider(request)


def priced_provider(request):
    result = fake_provider(request)
    result["usage"] = {"input_tokens": 2, "output_tokens": 3, "cost_microusd": 1}
    return result


def invalid_provider(request):
    result = fake_provider(request)
    result["decision"]["action"] = {"type": "forbidden", "body": {}}
    return result


def hashes(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


class InferenceExperimentTests(unittest.TestCase):
    def test_closed_plan_and_rule_controller_cannot_silently_run_inference(self):
        original = specification()
        for changes in (
            {"version": True},
            {"extra": 1},
            {"inference": {}},
            {"timeout_s": 0},
        ):
            with self.subTest(changes=changes), self.assertRaises(ExperimentError):
                _plan({**original, **changes})
        for key, value in (
            ("max_calls", True),
            ("max_calls", -1),
            ("max_cost_microusd", -1),
            ("max_calls", INT64_MAX + 1),
        ):
            bad = copy.deepcopy(original)
            bad["inference"]["budget"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ExperimentError):
                _plan(bad)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                ExperimentError, "requires_inference_controller"
            ):
                run_experiment(root / "wrong", original)
            self.assertFalse((root / "wrong").exists())

    def test_actual_inference_populations_paired_metrics_and_repeatable_fake_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root, specification(seeds=(7, 19))) as session:
                summary = session.run(fake_provider)
                self.assertEqual(session.run(fake_provider), summary)
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(summary["comparisons"]["larger"]["paired_seeds"], [7, 19])
            self.assertEqual(
                summary["comparisons"]["larger"]["delta"]["count"]["min"], 1
            )
            self.assertEqual(summary["inference"]["reserved_calls"], 10)
            self.assertEqual(read_experiment(root / "experiment"), summary)
            for row in summary["runs"]:
                view = open_experiments([("Counter", root / "experiment")])[0].run(
                    summary["runs"].index(row)
                )
                self.assertEqual(view["artifact"]["actors"][0]["mode"], "inference")
                record = loads(
                    (root / "experiment" / row["inference"]["evidence"]).read_bytes()
                )
                self.assertTrue(
                    all(step["state"] == "responded" for step in record["steps"])
                )
                self.assertEqual(
                    len(record["steps"]), row["inference"]["reserved_calls"]
                )

    def test_global_call_limit_is_not_reset_for_each_cell_and_failed_pairs_are_excluded(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root, specification(max_calls=3)) as session:
                summary = session.run(fake_provider)
            self.assertEqual(summary["inference"]["reserved_calls"], 3)
            self.assertEqual(summary["runs"][0]["status"], "passed")
            self.assertEqual(summary["runs"][1]["error"], "experiment_call_budget")
            self.assertNotIn("metrics", summary["runs"][1])
            self.assertEqual(summary["comparisons"]["larger"]["excluded_seeds"], [7])
            failed = open_experiments([("Counter", root / "experiment")])[0].run(1)
            self.assertEqual(failed["artifact"]["actors"][0]["mode"], "inference")
            self.assertIn('"state": "pending"', failed["inference_json"])

    def test_zero_global_calls_and_per_cell_limits_are_explicit_failures(self):
        for spec, error in (
            (specification(max_calls=0), "experiment_call_budget"),
            (specification(cell_max_calls=1), "inference_call_budget"),
            (specification(max_calls=10, max_input_tokens=1), "inference_usage_budget"),
            (
                specification(max_calls=10, max_output_tokens=1),
                "inference_usage_budget",
            ),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with create(root, spec) as session:
                    provider = (
                        priced_provider
                        if error == "inference_usage_budget"
                        else fake_provider
                    )
                    summary = session.run(provider)
                self.assertTrue(all(row["error"] == error for row in summary["runs"]))

    def test_global_cost_accounting_and_next_cell_allowance_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = specification(goals=(1, 1), cost=4, max_cost_microusd=4)
            with create(root, spec) as session:
                session.step(priced_provider)
                self.assertEqual(
                    session.progress()["inference"]["accepted_cost_microusd"], 1
                )
            with InferenceExperiment(root / "experiment") as session:
                summary = session.run(priced_provider)
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(summary["inference"]["accepted_cost_microusd"], 4)
            self.assertEqual(summary["inference"]["input_tokens"], 8)
            self.assertEqual(summary["inference"]["output_tokens"], 12)
            second = loads((root / "experiment/larger-7/inference.json").read_bytes())
            self.assertEqual(second["config"]["max_cost_microusd"], 2)
            self.assertEqual(
                [
                    step["request"]["limits"]["max_cost_microusd"]
                    for step in second["steps"]
                ],
                [2, 1],
            )

    def test_recovery_between_reservation_and_dispatch_reuses_the_same_reservation(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                with patch.object(
                    InferenceRun, "invoke", side_effect=KeyboardInterrupt
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        session.step(fake_provider)
                self.assertEqual(session.progress()["inference"]["reserved_calls"], 1)
            with InferenceExperiment(root / "experiment") as session:
                summary = session.run(fake_provider)
            self.assertEqual(summary["inference"]["reserved_calls"], 5)

    def test_fresh_process_crash_after_response_commit_does_not_repeat_a_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root):
                pass
            code = """
import os, sys
from pathlib import Path
from seam.canon import dumps
from seam.inference_experiments import InferenceExperiment
from seam.proof.inference import fake_provider
original = InferenceExperiment._reconcile
def crash(self, cell, session):
    if any(row[2] == 'responded' for row in session._steps()):
        os._exit(23)
    return original(self, cell, session)
def provider(request):
    with (Path(sys.argv[1]) / 'calls').open('a') as handle:
        handle.write(request['request_id'] + '\\n')
    return fake_provider(request)
InferenceExperiment._reconcile = crash
with InferenceExperiment(Path(sys.argv[1]) / 'experiment') as session:
    session.step(provider)
"""
            process = subprocess.run(
                [sys.executable, "-c", code, str(root)],
                env={"PATH": os.environ["PATH"], "PYTHONPATH": REPO},
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(process.returncode, 23, process.stderr)
            self.assertEqual(len((root / "calls").read_text().splitlines()), 1)
            with InferenceExperiment(root / "experiment") as session:
                summary = session.run(fake_provider)
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(summary["inference"]["reserved_calls"], 5)

    def test_uncertain_call_never_retries_and_known_response_can_resolve_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(
                root, specification(timeout_ms=500, cost=5, max_cost_microusd=5)
            ) as session:
                with self.assertRaisesRegex(
                    ExperimentError, "experiment_inference_uncertain"
                ):
                    session.step(slow_provider)
                totals = session.progress()["inference"]
                self.assertEqual(totals["reserved_calls"], 1)
                self.assertEqual(totals["held_cost_microusd"], 5)
            with InferenceExperiment(root / "experiment") as session:
                with self.assertRaisesRegex(
                    ExperimentError, "experiment_inference_uncertain"
                ):
                    session.step(fake_provider)
                with session._session(session.state["cells"][0]) as core:
                    envelope = loads(core._steps()[-1][1])
                session.accept(fake_provider(envelope))
                summary = session.run(fake_provider)
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(summary["inference"]["reserved_calls"], 5)
            self.assertEqual(summary["inference"]["held_cost_microusd"], 0)

    def test_abandoned_uncertainty_remains_failed_and_holds_the_full_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(
                root, specification(timeout_ms=500, cost=5, max_cost_microusd=5)
            ) as session:
                with self.assertRaises(ExperimentError):
                    session.step(slow_provider)
                session.abandon_uncertain()
                summary = session.run(fake_provider)
            self.assertEqual(summary["inference"]["held_cost_microusd"], 5)
            self.assertEqual(
                summary["runs"][0]["error"], "experiment_inference_abandoned_uncertain"
            )
            self.assertEqual(summary["runs"][1]["status"], "passed")
            self.assertEqual(summary["comparisons"]["larger"]["status"], "incomplete")
            self.assertEqual(read_experiment(root / "experiment"), summary)

    def test_provider_rejection_is_not_a_zero_outcome_or_rule_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                summary = session.run(invalid_provider)
            self.assertEqual(summary["inference"]["reserved_calls"], 2)
            self.assertTrue(
                all(
                    row["error"] == "inference_action_forbidden"
                    for row in summary["runs"]
                )
            )
            self.assertTrue(all("metrics" not in row for row in summary["runs"]))

    def test_strict_replay_is_byte_identical_provider_free_and_does_not_mutate_evidence(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                summary = session.run(fake_provider)
            before = hashes(root / "experiment")
            replay_experiment(root / "experiment", "baseline", 7, root / "left.json")
            replay_experiment(root / "experiment", "baseline", 7, root / "right.json")
            self.assertEqual(
                (root / "left.json").read_bytes(), (root / "right.json").read_bytes()
            )
            self.assertEqual(
                (root / "left.json").read_bytes(),
                (root / "experiment" / summary["runs"][0]["replay"]).read_bytes(),
            )
            self.assertEqual(hashes(root / "experiment"), before)
            for output in (root / "left.json", root / "experiment/new.json"):
                with self.assertRaises(ExperimentError):
                    replay_experiment(root / "experiment", "baseline", 7, output)
            with self.assertRaises(ExperimentError):
                replay_experiment(
                    root / "experiment", "baseline", True, root / "wrong.json"
                )

    def test_changed_inputs_manifest_or_evidence_refuse_continuation(self):
        for target in ("manifest.json", "baseline-7/input/input-0.json"):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                with create(root) as session:
                    session.step(fake_provider)
                path = root / "experiment" / target
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaisesRegex(
                    ExperimentError, "experiment_evidence_changed"
                ):
                    InferenceExperiment(root / "experiment")

    def test_exclusive_lock_private_files_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                with self.assertRaisesRegex(ExperimentError, "experiment_writer_busy"):
                    InferenceExperiment(root / "experiment")
                with self.assertRaises(FileExistsError):
                    create(root)
                self.assertEqual(session.root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    (session.root / "ensemble.sqlite").stat().st_mode & 0o777, 0o600
                )
                session.step(fake_provider)
                self.assertEqual(
                    (session.root / "baseline-7/session/journal.sqlite").stat().st_mode
                    & 0o777,
                    0o600,
                )

    def test_interrupted_preparation_is_not_silently_executed_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                with patch.object(adapter, "prepare", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        session.step(fake_provider)
            with InferenceExperiment(root / "experiment") as session:
                summary = session.run(fake_provider)
            self.assertEqual(
                summary["runs"][0]["error"], "experiment_prepare_uncertain"
            )
            self.assertEqual(summary["runs"][1]["status"], "passed")

    def test_source_identity_changes_stop_before_another_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                session.step(fake_provider)
                with patch("seam.inference_experiments._identity", return_value={}):
                    with self.assertRaisesRegex(
                        ExperimentError, "experiment_identity_changed"
                    ):
                        session.step(fake_provider)

    def test_missing_summary_is_incomplete_not_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                session.step(fake_provider)
            with self.assertRaises(ExperimentError):
                read_experiment(root / "experiment")
            self.assertFalse(
                open_experiments([("Counter", root / "experiment")])[0].overview(0)[
                    "available"
                ]
            )

    def test_sqlite_state_checksum_changes_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root):
                pass
            with sqlite3.connect(root / "experiment/ensemble.sqlite") as database:
                database.execute("UPDATE state SET value='{}'")
            with self.assertRaisesRegex(ExperimentError, "experiment_journal_changed"):
                InferenceExperiment(root / "experiment")

    def test_retained_inference_changes_are_refused_by_reader_and_viewer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                session.run(fake_provider)
            viewer = open_experiments([("Counter", root / "experiment")])[0]
            path = root / "experiment/baseline-7/inference.json"
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaises(ExperimentError):
                read_experiment(root / "experiment")
            with self.assertRaises(EvidenceError):
                viewer.run(0)

    def test_failed_product_and_measurement_retain_evidence_without_success_metrics(
        self,
    ):
        original = adapter.prepare

        def prepare(path, parameters, seed):
            result = original(path, parameters, seed)
            case = loads(result.read_bytes())
            case["assertions"] = [{"op": "state_is", "path": "count", "value": -1}]
            result.write_text(dumps(case))
            return result

        for failure in ("product", "metrics"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                with create(root) as session:
                    target = (
                        patch.object(adapter, "prepare", prepare)
                        if failure == "product"
                        else patch.object(
                            adapter,
                            "measure",
                            return_value={"count": True, "actions": 0},
                        )
                    )
                    with target:
                        summary = session.run(fake_provider)
                self.assertTrue(
                    all(
                        row["status"] == "failed" and "metrics" not in row
                        for row in summary["runs"]
                    )
                )
                self.assertTrue(all("artifact" in row for row in summary["runs"]))
                self.assertEqual(read_experiment(root / "experiment"), summary)

    def test_report_accounting_is_recomputed_from_request_response_evidence(self):
        from seam.inference_experiments import _verify_inference

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                summary = session.run(fake_provider)
                manifest = session.manifest
            rows = copy.deepcopy(summary["runs"])
            rows[0]["inference"]["reserved_calls"] -= 1
            with self.assertRaisesRegex(ExperimentError, "experiment_budget_changed"):
                _verify_inference(
                    root / "experiment", manifest["plan"], rows, summary["evidence"]
                )

    def test_provider_worker_does_not_keep_a_dead_controllers_writer_lock(self):
        import signal

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root):
                pass
            code = """
import os, sys, time
from pathlib import Path
from seam.inference_experiments import InferenceExperiment
from seam.proof.inference import fake_provider
def provider(request):
    (Path(sys.argv[1]) / 'worker').write_text(str(os.getpid()))
    time.sleep(10)
    return fake_provider(request)
with InferenceExperiment(Path(sys.argv[1]) / 'experiment') as session:
    session.step(provider)
"""
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(root)],
                env={"PATH": os.environ["PATH"], "PYTHONPATH": REPO},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            worker = None
            try:
                deadline = time.monotonic() + 10
                while not (root / "worker").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((root / "worker").exists())
                worker = int((root / "worker").read_text())
                process.terminate()
                process.wait(timeout=5)
                with InferenceExperiment(root / "experiment") as session:
                    with self.assertRaisesRegex(
                        ExperimentError, "experiment_inference_uncertain"
                    ):
                        session.step(fake_provider)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if worker is not None:
                    os.kill(worker, signal.SIGTERM)

    def test_failed_reservation_commit_requires_reopen_before_any_dispatch(self):
        original = InferenceExperiment._reserve

        def reserve(self, cell, request):
            with patch("seam.inference_experiments.MAX_REPORT", 1):
                return original(self, cell, request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root) as session:
                session.step(fake_provider)
                with patch.object(InferenceExperiment, "_reserve", reserve):
                    with self.assertRaisesRegex(
                        ExperimentError, "experiment_report_size"
                    ):
                        session.step(fake_provider)
                with self.assertRaisesRegex(
                    ExperimentError, "experiment_journal_unavailable"
                ):
                    session.step(fake_provider)
            with InferenceExperiment(root / "experiment") as session:
                summary = session.run(fake_provider)
            self.assertEqual(summary["inference"]["reserved_calls"], 5)
            self.assertEqual(summary["status"], "passed")

    def test_shared_dataset_cannot_turn_two_worlds_into_inference(self):
        original = adapter.prepare

        def prepare(path, parameters, seed):
            result = original(path, parameters, seed)
            case = loads(result.read_bytes())
            case["worlds"]["other"] = {"dataset": "actors"}
            case["ports"]["other"] = {"mode": "world", "world": "other"}
            result.write_text(dumps(case))
            return result

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "ACTOR_WORLDS", ("actors", "other")),
        ):
            root = Path(directory)
            with create(root) as session, patch.object(adapter, "prepare", prepare):
                summary = session.run(fake_provider)
            self.assertEqual(summary["inference"]["reserved_calls"], 0)
            self.assertTrue(
                all(
                    row["error"] == "experiment_shared_inference_dataset"
                    for row in summary["runs"]
                )
            )

    def test_invalid_preparation_contract_is_an_explicit_failed_cell(self):
        spec = specification(goals=(0, 1))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with create(root, spec) as session:
                summary = session.run(fake_provider)
            self.assertEqual(summary["runs"][0]["error"], "experiment_prepare")
            self.assertEqual(summary["runs"][1]["status"], "passed")
