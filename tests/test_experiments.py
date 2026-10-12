"""Experiment isolation, paired summaries, failure visibility, integrity, and CLI execution."""

import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from seam.canon import INT64_MAX, dumps, loads
from seam.experiments import (
    ExperimentError,
    _stats,
    plan,
    read_experiment,
    run_experiment,
)
from seam.proof import experiments as adapter
from tests.support import REPO


def specification(seeds=None):
    value = adapter.demo_plan()
    if seeds is not None:
        value["seeds"] = seeds
    return value


def run(root, name="experiment", spec=None):
    return run_experiment(
        root / name, specification() if spec is None else spec, env={"PYTHONPATH": REPO}
    )


class ExperimentTests(unittest.TestCase):
    def test_closed_plan_bounds_names_and_distinct_paired_seeds(self):
        original = specification()
        mutations = [
            {"version": True},
            {"seeds": []},
            {"seeds": [True]},
            {"seeds": [-1]},
            {"seeds": [2**63]},
            {"seeds": [1, 1]},
            {"seeds": list(range(501))},
            {"timeout_s": True},
            {"timeout_s": 0},
            {"timeout_s": 301},
            {"adapter": "../product"},
            {"baseline": []},
            {"baseline": "unknown"},
            {"extra": 1},
            {"variants": []},
            {"variants": [{"name": "../bad", "parameters": {}}]},
            {"variants": [original["variants"][0]] * 2},
            {"variants": [{"name": "baseline", "parameters": {"value": 1.5}}]},
        ]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ExperimentError):
                from seam.experiments import _plan

                _plan({**original, **changes})
        self.assertEqual(
            plan(
                adapter.__name__,
                original["variants"],
                [0, INT64_MAX],
                baseline="baseline",
            ),
            {**original, "seeds": [0, INT64_MAX]},
        )
        duplicate = adapter.demo_plan()
        duplicate["variants"][0]["parameters"]["goal"] = 9
        self.assertEqual(original["variants"][0]["parameters"]["goal"], 8)

    def test_real_product_seeds_repeat_and_variants_are_paired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = run(root, "left"), run(root, "right")
            self.assertEqual(left, right)
            self.assertEqual(len(left["runs"]), 6)
            self.assertEqual(left["status"], "passed")
            self.assertEqual(read_experiment(root / "left"), left)
            comparison = left["comparisons"]["larger"]
            self.assertEqual(comparison["status"], "complete")
            self.assertEqual(comparison["paired_seeds"], [7, 19, 31])
            self.assertEqual(comparison["excluded_seeds"], [])
            self.assertGreater(comparison["delta"]["count"]["min"], 0)
            for pair in comparison["pairs"]:
                rows = {
                    row["variant"]: row
                    for row in left["runs"]
                    if row["seed"] == pair["seed"]
                }
                self.assertEqual(
                    pair["delta"]["count"],
                    rows["larger"]["metrics"]["count"]
                    - rows["baseline"]["metrics"]["count"],
                )
            cases = sorted((root / "left").glob("*/input/case.json"))
            for path in cases:
                case = loads(path.read_bytes())
                actors = loads((path.parent / "actors.json").read_bytes())
                self.assertEqual(case["seed"], actors["seed"])
                self.assertEqual(actors["actors"][0]["memory"], {"actions": 0})

    def test_no_overwrite_of_existing_runs_and_private_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run(root, spec=specification([7]))
            before = (root / "experiment/summary.json").read_bytes()
            with self.assertRaises(FileExistsError):
                run(root)
            self.assertEqual((root / "experiment/summary.json").read_bytes(), before)
            self.assertEqual((root / "experiment").stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (root / "experiment/summary.json").stat().st_mode & 0o777, 0o600
            )

    def test_failed_variant_is_not_silently_dropped_from_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = specification([7, 19])
            spec["variants"][1]["parameters"]["goal"] = 0
            result = run(root, spec=spec)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["aggregates"]["larger"]["failed"], 2)
            comparison = result["comparisons"]["larger"]
            self.assertEqual(comparison["status"], "incomplete")
            self.assertEqual(comparison["excluded_seeds"], [7, 19])
            self.assertEqual(comparison["delta"]["count"]["count"], 0)
            self.assertIsNone(comparison["delta"]["count"]["mean"])
            self.assertEqual(read_experiment(root / "experiment"), result)

    def test_mixed_failures_only_compare_successful_pairs_and_show_exclusions(self):
        original = adapter.prepare

        def prepare(path, parameters, seed):
            if parameters["goal"] == 12 and seed == 19:
                raise ValueError("must not appear in evidence")
            return original(path, parameters, seed)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "prepare", prepare),
        ):
            root = Path(directory)
            result = run(root)
            comparison = result["comparisons"]["larger"]
            self.assertEqual(comparison["paired_seeds"], [7, 31])
            self.assertEqual(comparison["excluded_seeds"], [19])
            self.assertEqual(comparison["status"], "incomplete")
            self.assertNotIn("must not appear", dumps(result))

    def test_product_fault_and_unsealed_actor_world_never_get_metrics(self):
        original = adapter.prepare
        for mode in ("fault", "unsealed"):

            def prepare(path, parameters, seed):
                case_path = original(path, parameters, seed)
                case = loads(case_path.read_bytes())
                if mode == "fault":
                    case["assertions"] = [
                        {"op": "state_is", "path": "count", "value": -1}
                    ]
                else:
                    case["arrivals"] = case["arrivals"][:1]
                case_path.write_text(dumps(case))
                return case_path

            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as directory,
                patch.object(adapter, "prepare", prepare),
                patch.object(adapter, "measure") as measure,
            ):
                result = run(Path(directory), spec=specification([7]))
                self.assertEqual(result["status"], "failed")
                self.assertTrue(all("metrics" not in row for row in result["runs"]))
                measure.assert_not_called()

    def test_invalid_scalar_metrics_fail_instead_of_guessing_types(self):
        for values in (
            {"count": True, "actions": 1},
            {"count": 1.5, "actions": 1},
            {"count": 1},
            {"count": 2**63, "actions": 1},
            {"count": [], "actions": 1},
        ):
            with (
                self.subTest(values=values),
                tempfile.TemporaryDirectory() as directory,
                patch.object(adapter, "measure", return_value=values),
            ):
                result = run(Path(directory), spec=specification([7]))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["runs"][0]["error"], "experiment_metrics")

    def test_wrong_case_or_actor_seed_and_replay_are_rejected_before_dispatch(self):
        original = adapter.prepare
        for mode in ("case_seed", "actor_seed", "replay", "checkpoint"):

            def prepare(path, parameters, seed):
                case_path = original(path, parameters, seed)
                case = loads(case_path.read_bytes())
                if mode == "case_seed":
                    case["seed"] += 1
                elif mode == "checkpoint":
                    case["checkpoint_after"] = 1
                else:
                    from seam import dataset_ref

                    data_path = path / "actors.json"
                    data = loads(data_path.read_bytes())
                    if mode == "actor_seed":
                        data["seed"] += 1
                    else:
                        data["mode"] = "replay"
                    data_path.write_text(dumps(data))
                    case["datasets"]["actors"] = dataset_ref(data_path, 0)
                case_path.write_text(dumps(case))
                return case_path

            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as directory,
                patch.object(adapter, "prepare", prepare),
                patch("seam.experiments.run_product") as dispatch,
            ):
                result = run(Path(directory), spec=specification([7]))
                dispatch.assert_not_called()
                self.assertEqual(result["status"], "failed")

    def test_external_case_and_dataset_paths_are_refused(self):
        original = adapter.prepare
        for mode in ("case", "dataset"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                outside = original(root / "outside", {"goal": 8}, 7)

                def prepare(path, parameters, seed):
                    if mode == "case":
                        return outside
                    case_path = original(path, parameters, seed)
                    data = path / "actors.json"
                    data.unlink()
                    data.symlink_to(outside.parent / "actors.json")
                    return case_path

                with (
                    patch.object(adapter, "prepare", prepare),
                    patch("seam.experiments.run_product") as dispatch,
                ):
                    result = run(root, spec=specification([7]))
                    dispatch.assert_not_called()
                    self.assertEqual(result["status"], "failed")

    def test_changed_execution_identity_aborts_without_a_completed_summary(self):
        from seam.experiments import _identity

        calls = 0

        def identity(*args):
            nonlocal calls
            value = _identity(*args)
            calls += 1
            if calls > 2:
                value["sdk"] = "changed"
            return value

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("seam.experiments._identity", identity),
        ):
            root = Path(directory)
            with self.assertRaisesRegex(ExperimentError, "experiment_identity_changed"):
                run(root)
            self.assertFalse((root / "experiment/summary.json").exists())
            with self.assertRaises(ExperimentError):
                read_experiment(root / "experiment")

    def test_mutated_prior_artifact_prevents_sealing_and_reader_detects_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run(root, spec=specification([7]))
            artifact = root / "experiment/baseline-7/artifact.json"
            artifact.write_text("{}")
            with self.assertRaisesRegex(ExperimentError, "experiment_evidence_changed"):
                read_experiment(root / "experiment")
        original = adapter.prepare

        def prepare(path, parameters, seed):
            if parameters["goal"] == 12:
                (path.parent.parent / "baseline-7/artifact.json").write_text("{}")
            return original(path, parameters, seed)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "prepare", prepare),
        ):
            root = Path(directory)
            with self.assertRaisesRegex(ExperimentError, "experiment_evidence_changed"):
                run(root, spec=specification([7]))
            self.assertFalse((root / "experiment/summary.json").exists())

    def test_controller_process_death_retains_evidence_without_claiming_completion(
        self,
    ):
        code = """
import os, sys
from pathlib import Path
from seam.canon import loads
from seam.experiments import run_experiment
from seam.proof import experiments as adapter
prepare = adapter.prepare
def interrupted(path, parameters, seed):
    if parameters['goal'] == 12:
        os._exit(9)
    return prepare(path, parameters, seed)
adapter.prepare = interrupted
run_experiment(Path(sys.argv[1]), loads(sys.argv[2]))
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(root / "interrupted"),
                    dumps(specification([7])),
                ],
                env={"PYTHONPATH": REPO},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 9, proc.stderr)
            self.assertTrue((root / "interrupted/baseline-7/result.json").is_file())
            self.assertFalse((root / "interrupted/summary.json").exists())
            with self.assertRaises(ExperimentError):
                read_experiment(root / "interrupted")

    def test_metrics_cannot_mutate_the_supervised_artifact(self):
        original = adapter.measure

        def measure(artifact):
            values = original(artifact)
            artifact["final_state"]["count"] = -100
            return values

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "measure", measure),
        ):
            root = Path(directory)
            result = run(root, spec=specification([7]))
            self.assertEqual(result["status"], "passed")
            self.assertGreater(
                loads((root / "experiment/baseline-7/artifact.json").read_bytes())[
                    "final_state"
                ]["count"],
                0,
            )

    def test_timeout_is_a_failed_run_with_no_comparable_metrics(self):
        from seam.experiments import run_product

        def dispatch(module, case, namespace, artifact, **kwargs):
            return run_product(
                module, case, namespace, artifact, **{**kwargs, "timeout": 0.001}
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("seam.experiments.run_product", dispatch),
        ):
            result = run(Path(directory), spec=specification([7]))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(all(row["returncode"] == 2 for row in result["runs"]))
            self.assertEqual(result["comparisons"]["larger"]["excluded_seeds"], [7])

    def test_changed_report_checksum_and_statistics_are_refused(self):
        from seam.canon import digest

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run(root, spec=specification([7]))
            path = root / "experiment/summary.json"
            value = loads(path.read_bytes())
            value["aggregates"]["larger"]["passed"] = 100
            path.write_text(dumps(value))
            with self.assertRaises(ExperimentError):
                read_experiment(root / "experiment")
            value.pop("sha256")
            value["sha256"] = digest(value)
            path.write_text(dumps(value))
            with self.assertRaises(ExperimentError):
                read_experiment(root / "experiment")

    def test_exact_integer_statistics_preserve_large_and_negative_differences(self):
        self.assertEqual(
            _stats([INT64_MAX, INT64_MAX])["mean"],
            {"numerator": 2 * INT64_MAX, "denominator": 2},
        )
        self.assertEqual(_stats([-3, 1])["mean"], {"numerator": -2, "denominator": 2})
        self.assertIsNone(_stats([])["mean"])

    def test_environment_and_adapter_contracts_fail_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ExperimentError):
                run_experiment(
                    root / "bad", specification(), env={"API_KEY": "private"}
                )
            for changes in (
                {"prepare": None},
                {"METRICS": {"count": []}},
                {"ACTOR_WORLDS": ["actors"]},
            ):
                with (
                    self.subTest(changes=changes),
                    patch.multiple(adapter, **changes),
                    self.assertRaises(ExperimentError),
                ):
                    run(root)

            async def prepare(*args):
                return None

            with (
                patch.object(adapter, "prepare", prepare),
                self.assertRaises(ExperimentError),
            ):
                run(root)

    def test_actor_world_requirements_cannot_be_omitted(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "ACTOR_WORLDS", ()),
        ):
            result = run(Path(directory), spec=specification([7]))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["runs"][0]["error"], "experiment_actor_world")

    def test_in_directory_path_aliases_produce_verifiable_canonical_evidence_paths(
        self,
    ):
        original = adapter.prepare

        def prepare(path, parameters, seed):
            case_path = original(path, parameters, seed)
            nested = path / "nested"
            nested.mkdir()
            case = loads(case_path.read_bytes())
            case["datasets"]["actors"]["path"] = "nested/../actors.json"
            case_path.write_text(dumps(case))
            return nested / "../case.json"

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "prepare", prepare),
        ):
            root = Path(directory)
            result = run(root, spec=specification([7]))
            self.assertEqual(result["status"], "passed")
            self.assertEqual(read_experiment(root / "experiment"), result)
            self.assertTrue(
                all(".." not in Path(name).parts for name in result["evidence"])
            )

    def test_cli_malformed_and_noncanonical_json_exit_two_without_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bad.json"
            for value in ('{"x":1.5}', '{"x":1,"x":2}', "{"):
                path.write_text(value)
                process = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "seam.experiments",
                        "--plan",
                        str(path),
                        "--out",
                        str(root / "output"),
                    ],
                    env={"PYTHONPATH": REPO},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(process.returncode, 2, process.stderr)
                self.assertFalse((root / "output").exists())

    def test_non_actor_v1_products_use_the_same_runner(self):
        from tests.support import DECLINED

        def prepare(directory, parameters, seed):
            directory.mkdir(mode=0o700)
            case = loads(Path(DECLINED).read_bytes())
            case["seed"] = seed
            path = directory / "case.json"
            path.write_text(dumps(case))
            return path

        def measure(artifact):
            return {
                "declined": int(
                    artifact["final_state"]["order"]["status"] == "declined"
                )
            }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.multiple(
                adapter,
                MODULE="seam.proof.checkout",
                ACTOR_WORLDS=(),
                METRICS={"declined": "orders"},
                prepare=prepare,
                measure=measure,
            ),
        ):
            root = Path(directory)
            result = run(root)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(
                all(row["metrics"] == {"declined": 1} for row in result["runs"])
            )
            self.assertEqual(read_experiment(root / "experiment"), result)

    def test_parameters_and_metrics_are_copied_and_cli_reports_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = specification([7])
            original = copy.deepcopy(spec)
            path = root / "plan.json"
            path.write_text(dumps(spec))
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "seam.experiments",
                    "--plan",
                    str(path),
                    "--out",
                    str(root / "cli"),
                ],
                env={"PYTHONPATH": REPO},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(spec, original)
            spec["variants"][1]["parameters"]["goal"] = 0
            path.write_text(dumps(spec))
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "seam.experiments",
                    "--plan",
                    str(path),
                    "--out",
                    str(root / "failed"),
                ],
                env={"PYTHONPATH": REPO},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(read_experiment(root / "failed")["status"], "failed")
