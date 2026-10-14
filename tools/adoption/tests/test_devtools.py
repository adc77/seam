"""Safe generation, preflight refusals, and installed renamed products running real simulations."""

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from seam_dev.cli import main
from seam_dev.project import (
    ProjectError,
    SDK_COMMIT,
    TEMPLATE_FILES,
    generate,
    identity,
    project_path,
    read_document,
    read_project,
    scenario,
)
from seam_dev.worker import installed_project, preflight
from seam_dev.project import manifest
from seam_dev.cli import worker

ROOT = Path(__file__).resolve().parents[1]
SEAM_ROOT = ROOT.parents[1]


def hashes(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


class GeneratorTests(unittest.TestCase):
    def test_mismatched_or_malformed_sdk_metadata_is_refused_before_product_import(
        self,
    ):
        for value in (
            None,
            True,
            {},
            {"url": "https://github.com/adc77/seam.git", "vcs_info": "wrong"},
            {
                "url": "https://github.com/adc77/seam.git",
                "vcs_info": {"commit_id": "wrong"},
            },
        ):
            direct = None if value is None else json.dumps(value)
            sdk = SimpleNamespace(read_text=lambda name: direct, version="0.10.0")
            with (
                self.subTest(value=value),
                patch("seam_dev.worker.metadata.distribution", return_value=sdk),
            ):
                with self.assertRaisesRegex(ProjectError, "sdk-pin"):
                    installed_project(Path.cwd(), manifest("orders"))

    def test_worker_strips_ambient_credentials_and_source_paths(self):
        with patch(
            "seam_dev.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            self.assertEqual(worker("check", Path.cwd()), 0)
        arguments = run.call_args.args[0]
        self.assertIn("-I", arguments)
        self.assertEqual(
            set(run.call_args.kwargs["env"]),
            {"PATH", "TZ", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE"},
        )

    def test_preflight_refuses_excess_cells_before_product_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orders"
            generate(root, "orders")
            path = root / "scenario.json"
            value = read_document(path)
            path.write_text(json.dumps({**value, "seeds": list(range(17))}))
            with patch("seam_dev.worker.installed_project") as installed:
                with self.assertRaisesRegex(ProjectError, "32 cells"):
                    preflight(root)
                installed.assert_not_called()

    def test_preflight_rejects_changed_future_and_escaped_dataset_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orders"
            generate(root, "orders")
            for defect in ("hash", "future", "escape"):

                def prepare(cell, parameters, seed):
                    cell.mkdir()
                    data = b"{}"
                    (cell / "world.json").write_bytes(data)
                    reference = {
                        "path": "world.json",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "as_of_ns": 0,
                    }
                    if defect == "hash":
                        reference["sha256"] = "0" * 64
                    elif defect == "future":
                        reference["as_of_ns"] = 1
                    else:
                        reference["path"] = "../world.json"
                    case = cell / "case.json"
                    case.write_text(
                        json.dumps(
                            {
                                "namespace": "sim-orders",
                                "clock": {"start_ns": 0},
                                "datasets": {"world": reference},
                            }
                        )
                    )
                    return case

                adapter = SimpleNamespace(
                    prepare=prepare, METRICS={"fulfilled": "orders"}
                )
                with (
                    self.subTest(defect=defect),
                    patch(
                        "seam_dev.worker.installed_project",
                        return_value=(adapter, {}, "0.10.0"),
                    ),
                ):
                    with self.assertRaisesRegex(ProjectError, "preparation"):
                        preflight(root)
                self.assertFalse(list(root.parent.glob(".seam-preflight-*")))

    def test_bundled_template_matches_the_canonical_starter(self):
        for relative in TEMPLATE_FILES:
            with self.subTest(relative=relative):
                self.assertEqual(
                    (ROOT / "seam_dev/template" / relative).read_bytes(),
                    (SEAM_ROOT / "examples/starter" / relative).read_bytes(),
                )

    def test_renames_all_modules_scripts_and_namespaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "renamed"
            generate(root, "lab-orders")
            _, project = read_project(root)
            self.assertEqual(project["package"], "lab_orders_sim")
            self.assertEqual(project["namespace"], "sim-lab-orders")
            self.assertEqual(project["sdk_commit"], SDK_COMMIT)
            for path in root.rglob("*.py"):
                source = path.read_text()
                compile(source, str(path), "exec")
                self.assertNotIn("sim_starter", source)
                self.assertNotIn("sim-starter-orders", source)
            metadata = (root / "pyproject.toml").read_text()
            self.assertIn('name = "lab-orders-seam-product"', metadata)
            self.assertIn('lab-orders-demo = "lab_orders_sim.demo:main"', metadata)
            specification = scenario(root / "scenario.json", project)
            self.assertEqual(
                [v["name"] for v in specification["variants"]], ["healthy", "scarce"]
            )

    def test_names_cannot_shadow_sdk_or_standard_library_packages(self):
        for name in ("seam", "json", "random", "importlib"):
            self.assertTrue(identity(name)["package"].endswith("_sim"))

    def test_invalid_names_create_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (
                "",
                "../other",
                "Aman",
                "a_b",
                "a--b",
                "a-",
                "1name",
                "a" * 25,
                "é",
                True,
            ):
                root = Path(directory) / "new"
                with self.subTest(name=name), self.assertRaises(ProjectError):
                    generate(root, name)
                self.assertFalse(root.exists())

    def test_existing_outputs_missing_parents_and_symlinks_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "keep"
            marker.write_text("original")
            link = root / "link"
            link.symlink_to(root, target_is_directory=True)
            for out in (root, marker, link, root / "missing/product"):
                with self.subTest(out=out), self.assertRaises(ProjectError):
                    generate(out, "orders")
            self.assertEqual(marker.read_text(), "original")
            self.assertFalse((root / "missing").exists())

    def test_unknown_manifest_fields_and_boolean_versions_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orders"
            generate(root, "orders")
            path = root / "seam-project.json"
            original = read_document(path)
            for value in (
                {**original, "extra": 1},
                {**original, "version": True},
                {**original, "sdk_commit": "other"},
            ):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ProjectError):
                    read_project(root)

    def test_plan_is_closed_rule_only_and_product_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orders"
            generate(root, "orders")
            _, project = read_project(root)
            path = root / "scenario.json"
            original = read_document(path)
            for value in (
                {**original, "version": 2},
                {**original, "inference": {}},
                {**original, "adapter": "other.product"},
                {**original, "version": True},
            ):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ProjectError):
                    scenario(path, project)

    def test_plan_paths_cannot_escape_or_follow_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "orders"
            generate(root, "orders")
            outside = Path(directory) / "outside.json"
            outside.write_text("{}")
            (root / "linked.json").symlink_to(outside)
            for name in ("../outside.json", str(outside), "linked.json"):
                with self.subTest(name=name), self.assertRaises(ProjectError):
                    project_path(root, name)

    def test_json_size_and_duplicate_fields_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for body in ('{"version":1,"version":2}', " " * (1024 * 1024 + 1)):
                path.write_text(body)
                with self.assertRaises(ProjectError):
                    read_document(path)

    def test_cli_reports_bad_paths_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(["init", directory, "--name", "orders"])
            self.assertEqual(code, 2)
            self.assertNotIn("Traceback", stderr.getvalue())


class InstalledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(
            prefix=".adoption-test-", dir=Path.cwd()
        )
        cls.addClassCleanup(cls.workspace.cleanup)
        cls.root = Path(cls.workspace.name).resolve()
        cls.env = cls.root / "environment"
        cls.call([sys.executable, "-m", "venv", str(cls.env)])
        cls.python = cls.env / "bin/python"
        build_source = cls.root / "companion-source"
        shutil.copytree(
            ROOT,
            build_source,
            ignore=shutil.ignore_patterns(
                "build", "dist", "*.egg-info", "__pycache__", ".adoption-test-*"
            ),
        )
        cls.call(
            [
                str(cls.python),
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                str(build_source),
                "--wheel-dir",
                str(cls.root / "wheels"),
            ]
        )
        (wheel,) = (cls.root / "wheels").glob("*.whl")
        cls.call([str(cls.python), "-m", "pip", "install", str(wheel)])
        cls.command = cls.env / "bin/seam-dev"
        cls.projects = {}
        for name in ("lab-orders", "seam"):
            project = cls.root / (name + " with spaces")
            cls.call([str(cls.command), "init", str(project), "--name", name])
            cls.call([str(cls.python), "-m", "pip", "install", str(project)])
            cls.projects[name] = project

    @classmethod
    def call(cls, arguments, *, expected=0, cwd=None, env=None):
        result = subprocess.run(
            arguments,
            cwd=cls.root if cwd is None else cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != expected:
            raise AssertionError(
                (arguments, result.returncode, result.stdout, result.stderr)
            )
        return result

    def test_installed_products_run_checks_and_actual_scenarios(self):
        for name, project in self.projects.items():
            with self.subTest(name=name):
                checked = self.call(
                    [str(self.command), "check", "--project", str(project)]
                )
                report = json.loads(checked.stdout)
                self.assertEqual(report["status"], "ready")
                self.assertEqual(report["cells_checked"], 4)
                self.assertEqual(report["model_calls"], 0)
                out = self.root / (name + "-experiment")
                run = self.call(
                    [
                        str(self.command),
                        "run",
                        "--project",
                        str(project),
                        "--out",
                        str(out),
                    ]
                )
                result = json.loads(run.stdout)
                self.assertEqual(
                    [row["metrics"]["fulfilled"] for row in result["runs"]],
                    [2, 2, 1, 1],
                )
                self.assertEqual(
                    [row["metrics"]["rejected"] for row in result["runs"]], [0, 0, 1, 1]
                )
                before = hashes(out)
                self.call(
                    [str(self.command), "view", "--experiment", str(out), "--check"]
                )
                self.assertEqual(hashes(out), before)
                rejected = self.call(
                    [
                        str(self.command),
                        "run",
                        "--project",
                        str(project),
                        "--out",
                        str(out),
                    ],
                    expected=2,
                )
                self.assertIn("existing evidence", rejected.stderr)
                self.assertEqual(hashes(out), before)

    def test_documented_empty_stock_scenario(self):
        project = self.projects["lab-orders"]
        out = self.root / "empty"
        self.call(
            [
                str(self.command),
                "check",
                "--project",
                str(project),
                "--plan",
                "scenario-empty.json",
            ]
        )
        result = self.call(
            [
                str(self.command),
                "run",
                "--project",
                str(project),
                "--plan",
                "scenario-empty.json",
                "--out",
                str(out),
            ]
        )
        self.assertEqual(
            [row["metrics"]["fulfilled"] for row in json.loads(result.stdout)["runs"]],
            [2, 2, 0, 0],
        )

    def test_scripted_decision_proof_runs_eight_actual_recorded_replays(self):
        result = self.call(
            [
                str(self.env / "bin/lab-orders-demo"),
                "--scripted",
                "--out",
                str(self.root / "recorded"),
            ]
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["byte_identical_replays"], 8)
        self.assertEqual(report["controller"], "scripted-not-an-LLM")

    def test_generated_product_retains_the_entire_original_test_suite(self):
        result = self.call(
            [
                str(self.python),
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-t",
                ".",
                "-v",
            ],
            cwd=self.projects["lab-orders"],
        )
        self.assertIn("Ran 18 tests", result.stderr)

    def test_stale_install_refused_before_any_experiment_is_created(self):
        project = self.projects["lab-orders"]
        path = project / "lab_orders_sim/product.py"
        body = path.read_bytes()
        try:
            path.write_bytes(body + b"\n")
            out = self.root / "stale"
            result = self.call(
                [
                    str(self.command),
                    "run",
                    "--project",
                    str(project),
                    "--out",
                    str(out),
                ],
                expected=2,
            )
            self.assertIn("stale-install", result.stderr)
            self.assertFalse(out.exists())
        finally:
            path.write_bytes(body)

    def test_version_two_inference_plan_is_refused_without_dispatch(self):
        project = self.projects["lab-orders"]
        path = project / "inference.json"
        value = read_document(project / "scenario.json")
        path.write_text(json.dumps({**value, "version": 2, "inference": {}}))
        out = self.root / "no-paid-calls"
        result = self.call(
            [
                str(self.command),
                "run",
                "--project",
                str(project),
                "--plan",
                path.name,
                "--out",
                str(out),
            ],
            expected=2,
        )
        self.assertIn("no model calls", result.stderr)
        self.assertFalse(out.exists())

    def test_invalid_product_parameters_fail_preflight_without_run_outputs(self):
        project = self.projects["lab-orders"]
        path = project / "invalid.json"
        value = read_document(project / "scenario.json")
        value["variants"][0]["parameters"]["capacity"] = True
        path.write_text(json.dumps(value))
        result = self.call(
            [
                str(self.command),
                "check",
                "--project",
                str(project),
                "--plan",
                path.name,
            ],
            expected=2,
        )
        self.assertIn("preparation", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_neutral_worker_ignores_source_checkout_pythonpath(self):
        import os

        environment = {**os.environ, "PYTHONPATH": str(SEAM_ROOT)}
        result = self.call(
            [str(self.command), "check", "--project", str(self.projects["lab-orders"])],
            env=environment,
        )
        self.assertEqual(json.loads(result.stdout)["status"], "ready")

    def test_missing_installed_product_has_an_actionable_refusal(self):
        project = self.root / "not-installed"
        self.call([str(self.command), "init", str(project), "--name", "absent"])
        result = self.call(
            [str(self.command), "check", "--project", str(project)], expected=2
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("absent-seam-product", result.stderr)


if __name__ == "__main__":
    unittest.main()
