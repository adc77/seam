"""Actual experiment inspection, immutable evidence, exact values, local HTTP boundaries, and packaged assets."""

import copy
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from seam.canon import INT64_MAX, dumps, loads
from seam.experiments import run_experiment
from seam.proof import experiments as adapter
from seam_viewer.evidence import (
    EvidenceError,
    _project,
    _read,
    open_experiments,
    transport,
)
from seam_viewer.server import ViewerServer
from tests.support import REPO


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        spec = adapter.demo_plan()
        spec["seeds"] = [INT64_MAX, 7]
        cls.good = cls.root / "good"
        cls.summary = run_experiment(cls.good, spec, env={"PYTHONPATH": REPO})
        cls.bad = cls.root / "failed"
        failed = copy.deepcopy(spec)
        failed["variants"][1]["parameters"]["goal"] = 0
        run_experiment(cls.bad, failed, env={"PYTHONPATH": REPO})
        original = adapter.prepare

        def wrong_assertion(directory, parameters, seed):
            path = original(directory, parameters, seed)
            document = loads(path.read_bytes())
            document["assertions"] = [
                {"op": "event_count", "handler": "add", "times": 0}
            ]
            path.write_text(dumps(document))
            return path

        cls.product_bad = cls.root / "product-failed"
        with patch.object(adapter, "prepare", wrong_assertion):
            spec["seeds"] = [7]
            run_experiment(cls.product_bad, spec, env={"PYTHONPATH": REPO})

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_reads_actual_personas_observations_memory_and_product_history(self):
        before = hashes(self.good)
        with patch(
            "seam.experiments._adapter",
            side_effect=AssertionError("must not import adapters"),
        ):
            experiment = open_experiments([("Counter", self.good)])[0]
            overview = experiment.overview(0)
            view = experiment.run(0)
        self.assertTrue(overview["available"])
        self.assertEqual(overview["status"], "passed")
        self.assertEqual(overview["runs"], self.summary["runs"])
        actor = view["artifact"]["actors"][0]
        self.assertEqual(actor["id"], "alice")
        self.assertEqual(actor["mode"], "rules")
        self.assertTrue(actor["sealed"])
        self.assertIsNone(actor["next_ns"])
        self.assertEqual(loads(actor["profile_json"])["goal"], 8)
        self.assertEqual(loads(actor["initial_memory_json"]), {"actions": 0})
        first = actor["decisions"][0]
        self.assertEqual(loads(first["memory_before_json"]), {"actions": 0})
        self.assertEqual(loads(first["memory_after_json"]), {"actions": 1})
        self.assertEqual(loads(first["request_json"])["seed"], INT64_MAX)
        self.assertIsNotNone(first["event_i"])
        event = next(
            row for row in view["artifact"]["timeline"] if row["i"] == first["event_i"]
        )
        self.assertEqual(event["handler"], "actors_step")
        self.assertTrue(loads(event["port_calls_json"]))
        self.assertGreater(loads(view["artifact"]["final_state_json"])["count"], 0)
        self.assertEqual(hashes(self.good), before)

    def test_failed_cells_and_excluded_pairs_are_not_hidden_or_zero_filled(self):
        experiment = open_experiments([("Failed", self.bad)])[0]
        overview = experiment.overview(0)
        self.assertTrue(overview["available"])
        self.assertEqual(overview["status"], "failed")
        comparison = overview["comparisons"]["larger"]
        self.assertEqual(comparison["status"], "incomplete")
        self.assertEqual(comparison["excluded_seeds"], [INT64_MAX, 7])
        self.assertIsNone(comparison["delta"]["count"]["mean"])
        view = experiment.run(2)
        self.assertIsNone(view["artifact"])
        self.assertEqual(view["row"]["status"], "failed")
        self.assertNotIn("metrics", view["row"])
        self.assertEqual(view["row"]["error"], "experiment_prepare")

    def test_world_port_aliases_work_and_ambiguous_event_links_are_not_invented(self):
        row = self.summary["runs"][0]
        artifact = loads((self.good / row["artifact"]).read_bytes())
        world = artifact["world_states"].pop("actors")
        artifact["world_states"]["people"] = world
        population = loads(
            (self.good / f"baseline-{INT64_MAX}/input/actors.json").read_bytes()
        )
        populations = {world["population_sha256"]: population}
        view = _project(artifact, populations, {"actors": "people"})
        self.assertIsNotNone(view["actors"][0]["decisions"][0]["event_i"])
        first = next(
            call for call in artifact["port_calls"] if call["request"]["op"] == "decide"
        )
        artifact["port_calls"].append(copy.deepcopy(first))
        view = _project(artifact, populations, {"actors": "people"})
        self.assertIsNone(view["actors"][0]["decisions"][0]["event_i"])

    def test_markup_in_persona_assumptions_is_preserved_as_text_not_interpreted(self):
        original = adapter.prepare
        markup = '</pre><img src=x onerror="window.injected=true">'

        def prepare(directory, parameters, seed):
            case_path = original(directory, parameters, seed)
            case = loads(case_path.read_bytes())
            path = case_path.parent / "actors.json"
            population = loads(path.read_bytes())
            population["actors"][0]["profile"]["note"] = markup
            raw = (dumps(population) + "\n").encode("ascii")
            path.write_bytes(raw)
            case["datasets"]["actors"]["sha256"] = hashlib.sha256(raw).hexdigest()
            case_path.write_text(dumps(case))
            return case_path

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "prepare", prepare),
        ):
            spec = adapter.demo_plan()
            spec["seeds"] = [7]
            root = Path(directory) / "markup"
            run_experiment(root, spec, env={"PYTHONPATH": REPO})
            view = open_experiments([("Markup", root)])[0].run(0)
            self.assertEqual(
                loads(view["artifact"]["actors"][0]["profile_json"])["note"], markup
            )

    def test_incomplete_experiment_stays_visible_beside_valid_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            items = open_experiments(
                [("Incomplete", directory), ("Counter", self.good)]
            )
            first, second = [item.overview(index) for index, item in enumerate(items)]
            self.assertFalse(first["available"])
            self.assertEqual(first["label"], "Incomplete")
            self.assertTrue(second["available"])
            with self.assertRaises(EvidenceError):
                items[0].run(0)

    def test_product_assertion_failure_preserves_decisions_and_failed_assertions(self):
        experiment = open_experiments([("Product failed", self.product_bad)])[0]
        self.assertEqual(experiment.overview(0)["status"], "failed")
        view = experiment.run(0)
        self.assertEqual(view["row"]["error"], "experiment_product_failed")
        self.assertEqual(view["artifact"]["status"], "failed")
        self.assertTrue(view["artifact"]["actors"][0]["decisions"])
        self.assertFalse(loads(view["artifact"]["assertions_json"])[0]["ok"])

    def test_http_refuses_oversized_responses_instead_of_sending_partial_json(self):
        with (
            serving([("Counter", self.good)]) as server,
            patch("seam_viewer.server.MAX_RUN_BYTES", 100),
        ):
            status, _, raw = request(server, server.prefix + "api/experiments")
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(raw)["error"], "viewer_response_size")

    def test_all_integer_values_use_lossless_transport_but_raw_json_keeps_types(self):
        original = {
            "seed": INT64_MAX,
            "sum": INT64_MAX * 1000,
            "zero": 0,
            "flag": True,
            "null": None,
        }
        wire = transport(original)
        self.assertEqual(wire["seed"], "9223372036854775807")
        self.assertEqual(wire["sum"], str(INT64_MAX * 1000))
        self.assertEqual(wire["zero"], "0")
        self.assertIs(wire["flag"], True)
        self.assertIsNone(wire["null"])
        self.assertEqual(original["seed"], INT64_MAX)
        view = open_experiments([("Counter", self.good)])[0].run(0)
        raw = view["artifact"]["actors"][0]["decisions"][0]["request_json"]
        self.assertIn('"seed": 9223372036854775807', raw)

    def test_changed_summary_or_manifest_invalidates_the_pinned_session(self):
        for name in ("summary.json", "manifest.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "copy"
                shutil.copytree(self.good, root)
                experiment = open_experiments([("Counter", root)])[0]
                path = root / name
                path.write_bytes(path.read_bytes() + b" ")
                self.assertFalse(experiment.overview(0)["available"])
                with self.assertRaisesRegex(EvidenceError, "viewer_evidence_changed"):
                    experiment.run(0)

    def test_changed_artifact_inputs_or_cell_record_is_refused(self):
        first = self.summary["runs"][0]
        cell = f"{first['variant']}-{first['seed']}"
        names = [
            first["artifact"],
            f"{cell}/inputs.json",
            f"{cell}/result.json",
            f"{cell}/input/actors.json",
        ]
        for name in names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "copy"
                shutil.copytree(self.good, root)
                experiment = open_experiments([("Counter", root)])[0]
                path = root / name
                path.write_bytes(path.read_bytes() + b" ")
                self.assertFalse(experiment.overview(0)["available"])
                with self.assertRaises(EvidenceError):
                    experiment.run(0)

    def test_preparation_failure_record_is_also_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "copy"
            shutil.copytree(self.bad, root)
            experiment = open_experiments([("Failed", root)])[0]
            path = root / f"larger-{INT64_MAX}/result.json"
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaises(EvidenceError):
                experiment.run(2)

    def test_missing_artifact_and_symlinked_input_directory_are_refused(self):
        first = self.summary["runs"][0]
        for mutation in ("missing", "symlink"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "copy"
                shutil.copytree(self.good, root)
                experiment = open_experiments([("Counter", root)])[0]
                if mutation == "missing":
                    (root / first["artifact"]).unlink()
                else:
                    path = root / f"baseline-{INT64_MAX}/input"
                    relocated = root / "relocated"
                    path.rename(relocated)
                    path.symlink_to(relocated, target_is_directory=True)
                with self.assertRaises(EvidenceError):
                    experiment.run(0)

    def test_invalid_labels_duplicates_counts_and_indices_are_refused(self):
        for selections in (
            [],
            [("Counter", self.good)] * 2,
            [("../escape", self.good)],
            [(f"Item {i}", self.good) for i in range(17)],
        ):
            with self.subTest(selections=selections), self.assertRaises(ValueError):
                open_experiments(selections)
        experiment = open_experiments([("Counter", self.good)])[0]
        for index in (-1, True, "0", 4):
            with self.subTest(index=index), self.assertRaises(EvidenceError):
                experiment.run(index)

    def test_bounded_reader_refuses_oversize_and_external_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data.json").write_bytes(b"12345")
            for name, limit in (
                ("data.json", 4),
                ("data.json", -1),
                ("../data.json", 10),
                (str(root / "data.json"), 10),
            ):
                with (
                    self.subTest(name=name, limit=limit),
                    self.assertRaises(EvidenceError),
                ):
                    _read(root, name, None, limit)

    def test_non_actor_product_can_be_inspected_without_actor_assumptions(self):
        def prepare(directory, parameters, seed):
            directory.mkdir()
            document = loads(
                (Path(REPO) / "seam/proof/checkout/cases/declined.json").read_bytes()
            )
            document["seed"] = seed
            path = directory / "case.json"
            path.write_text(dumps(document))
            return path

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "MODULE", "seam.proof.checkout"),
            patch.object(adapter, "ACTOR_WORLDS", ()),
            patch.object(adapter, "METRICS", {"events": "events"}),
            patch.object(adapter, "prepare", prepare),
            patch.object(
                adapter, "measure", lambda artifact: {"events": len(artifact["events"])}
            ),
        ):
            spec = adapter.demo_plan()
            spec["seeds"] = [7]
            root = Path(directory) / "nonactor"
            run_experiment(root, spec, env={"PYTHONPATH": REPO})
            experiment = open_experiments([("Checkout", root)])[0]
            view = experiment.run(0)
            self.assertEqual(view["row"]["status"], "passed")
            self.assertEqual(view["artifact"]["actors"], [])
            self.assertEqual(view["artifact"]["timeline"][0]["handler"], "message")
            self.assertEqual(
                loads(view["artifact"]["final_state_json"])["order"]["status"],
                "declined",
            )

    def test_cli_check_has_no_server_or_product_execution_and_reports_invalid_selection(
        self,
    ):
        for target, code in ((self.good, 0), (self.root / "missing", 1)):
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "seam_viewer",
                    "--experiment",
                    f"Counter={target}",
                    "--check",
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(process.returncode, code, process.stderr)
            self.assertEqual(
                json.loads(process.stdout)["experiments"][0]["available"], code == 0
            )
        process = subprocess.run(
            [sys.executable, "-m", "seam_viewer", "--experiment", "invalid", "--check"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(process.returncode, 2)

    def test_http_routes_headers_and_lossless_payload(self):
        with serving([("Counter", self.good)]) as server:
            for path, content_type in (
                ("", "text/html"),
                ("app.js", "text/javascript"),
                ("style.css", "text/css"),
                ("favicon.svg", "image/svg+xml"),
                ("api/experiments", "application/json"),
                ("api/experiments/0/runs/0", "application/json"),
            ):
                with self.subTest(path=path):
                    status, headers, data = request(server, server.prefix + path)
                    self.assertEqual(status, 200, data)
                    self.assertTrue(headers["Content-Type"].startswith(content_type))
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                    self.assertIn(
                        "frame-ancestors 'none'", headers["Content-Security-Policy"]
                    )
                    self.assertNotIn("Access-Control-Allow-Origin", headers)
                    self.assertNotIn(str(self.good).encode(), data)
            _, _, data = request(server, server.prefix + "api/experiments")
            self.assertEqual(
                json.loads(data)["experiments"][0]["plan"]["seeds"][0], str(INT64_MAX)
            )
            status, headers, data = request(server, server.prefix, method="HEAD")
            self.assertEqual(status, 200)
            self.assertGreater(int(headers["Content-Length"]), 0)
            self.assertEqual(data, b"")

    def test_http_refuses_writes_arbitrary_paths_and_origin_escapes(self):
        before = hashes(self.good)
        with serving([("Counter", self.good)]) as server:
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                status, _, _ = request(
                    server, server.prefix + "api/experiments", method=method
                )
                self.assertEqual(status, 405, method)
            for path in (
                "/",
                "/v/wrong/api/experiments",
                server.prefix + "../manifest.json",
                server.prefix + "%2e%2e/summary.json",
                server.prefix + "api/experiments?path=/etc/passwd",
                server.prefix + "api/experiments/1/runs/0",
                server.prefix + "api/experiments/0/runs/-1",
            ):
                self.assertEqual(request(server, path)[0], 404, path)
            self.assertEqual(
                request(server, server.prefix + "api/experiments/0/runs/1000")[0], 409
            )
            for headers in (
                {"Host": "foreign.invalid"},
                {"Origin": "https://foreign.invalid"},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                self.assertEqual(
                    request(server, server.prefix, headers=headers)[0], 403
                )
            self.assertEqual(
                request(
                    server,
                    "http://[",
                    headers={"Host": server.origin.removeprefix("http://")},
                )[0],
                404,
            )
            self.assertEqual(
                request(
                    server,
                    server.prefix,
                    headers={"Origin": server.origin, "Sec-Fetch-Site": "same-origin"},
                )[0],
                200,
            )
        self.assertEqual(hashes(self.good), before)

    def test_http_startup_does_not_require_reverse_dns(self):
        with patch(
            "socket.getfqdn", side_effect=AssertionError("reverse DNS must not run")
        ) as lookup:
            with serving([("Counter", self.good)]) as server:
                self.assertEqual(server.server_name, "localhost")
                self.assertEqual(server.server_port, server.server_address[1])
                self.assertTrue(server.server_address[0].startswith("127."))
                self.assertEqual(request(server, server.prefix)[0], 200)
                self.assertEqual(request(server, server.prefix + "api/experiments")[0], 200)
            lookup.assert_not_called()

    def test_http_sessions_have_distinct_tokens_and_no_remote_bind_option(self):
        with (
            ViewerServer([("Counter", self.good)]) as left,
            ViewerServer([("Counter", self.good)]) as right,
        ):
            self.assertNotEqual(left.prefix, right.prefix)
            self.assertTrue(left.url.startswith("http://localhost:"))
            self.assertTrue(left.server_address[0].startswith("127."))
        for port in (-1, 65536, True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                ViewerServer([("Counter", self.good)], port)

    def test_frontend_has_no_dynamic_html_or_external_asset_dependencies(self):
        root = Path(REPO) / "seam_viewer/static"
        script = (root / "app.js").read_text()
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("eval(", script)
        self.assertNotIn("document.write", script)
        self.assertIn("textContent", script)
        for path in root.iterdir():
            if path.suffix != ".svg":
                self.assertNotIn("https://", path.read_text())


@contextmanager
def serving(selections):
    with ViewerServer(selections) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


def request(server, path, method="GET", headers=None):
    connection = http.client.HTTPConnection("localhost", server.server_port, timeout=5)
    try:
        connection.request(method, path, headers={} if headers is None else headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()
