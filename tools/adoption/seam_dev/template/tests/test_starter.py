"""Actual handlers, finite inventory, legal choices, checkpoints, and the installed demo contract."""

from contextlib import redirect_stderr
import hashlib
import heapq
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from seam import (
    Fault,
    PortError,
    checkpoint_ref,
    run_product,
    write_capture_json,
    write_checkpoint,
)
from seam.canon import loads

from sim_starter.actors import observe, options, population, translate
from sim_starter.demo import main, scripted_provider, viewer_command
from sim_starter.experiments import MODULE, measure, prepare, variants
from sim_starter.inventory import stock_world
from sim_starter.product import SECOND, build, state_view


def inventory(capacity=2, lose_ack=False):
    return stock_world(
        None,
        {"capacity": capacity, "reserved": [], "lost": [], "lose_ack": lose_ack},
        {},
    )


class Live:
    def __init__(self, adapter):
        self.now = 0
        self.timers = []
        self.runtime = build(inventory=adapter)
        self.runtime.now = lambda: self.now
        self.runtime.set_timer_backend(
            lambda token, at, handler, body, name: heapq.heappush(
                self.timers, (at, token)
            )
        )
        self.runtime.start_live()

    def send(self, handler, key):
        self.runtime.deliver(handler, {"order": key})

    def tick(self, until):
        while self.timers and self.timers[0][0] <= until:
            self.now, token = heapq.heappop(self.timers)
            self.runtime.fire_timer(token)
        self.now = until

    @property
    def orders(self):
        return self.runtime.state_copy()["orders"]


class StarterTests(unittest.TestCase):
    def test_live_handlers_fulfill_once_and_refuse_early_fulfillment(self):
        world = inventory()
        live = Live(world.handlers["stock"])
        try:
            live.send("place", "buyer_a")
            live.send("place", "buyer_a")
            live.send("place", "buyer_b")
            self.assertEqual(world.snapshot()["reserved"], ["buyer_a", "buyer_b"])
            self.assertEqual(live.orders["buyer_a"]["status"], "processing")
            with self.assertRaises(Fault) as error:
                live.send("fulfill", "buyer_a")
            self.assertEqual(error.exception.code, "order_not_ready")
            live.tick(2 * SECOND)
            self.assertTrue(
                all(row["status"] == "fulfilled" for row in live.orders.values())
            )
            live.send("fulfill", "buyer_a")
            self.assertEqual(live.orders["buyer_a"]["attempts"], 1)
        finally:
            live.runtime.close()

    def test_lost_ack_retry_does_not_double_allocate_or_resurrect_rejection(self):
        world = inventory(1, True)
        live = Live(world.handlers["stock"])
        try:
            live.send("place", "buyer_a")
            live.send("place", "buyer_b")
            self.assertEqual(live.orders["buyer_a"]["status"], "pending")
            self.assertEqual(live.orders["buyer_b"]["status"], "rejected")
            live.send("place", "buyer_a")
            self.assertEqual(live.orders["buyer_a"]["attempts"], 1)
            live.tick(3 * SECOND)
            live.send("place", "buyer_b")
            self.assertEqual(live.orders["buyer_a"]["status"], "fulfilled")
            self.assertEqual(live.orders["buyer_b"]["status"], "rejected")
            self.assertEqual(world.snapshot()["reserved"], ["buyer_a"])
            self.assertEqual(live.orders["buyer_a"]["attempts"], 2)
        finally:
            live.runtime.close()

    def test_exhausted_timeouts_are_unknown_not_success_or_sold_out(self):
        def broken(body):
            raise PortError("timeout")

        live = Live(broken)
        try:
            live.send("place", "buyer_a")
            live.tick(8 * SECOND)
            self.assertEqual(
                live.orders["buyer_a"],
                {"status": "unknown", "attempts": 2, "ready_ns": None},
            )
            live.send("place", "buyer_a")
            live.send("reserve", "buyer_a")
            self.assertEqual(live.orders["buyer_a"]["attempts"], 2)
        finally:
            live.runtime.close()

    def test_invalid_or_other_order_acknowledgements_fault(self):
        for response in (
            {"order": "other", "status": "reserved"},
            {"order": "buyer_a", "status": "maybe"},
            {"order": "buyer_a", "status": "reserved", "extra": 1},
        ):
            with self.subTest(response=response):
                live = Live(lambda body: response)
                try:
                    with self.assertRaises(Fault) as error:
                        live.send("place", "buyer_a")
                    self.assertEqual(error.exception.code, "bad_stock_ack")
                finally:
                    live.runtime.close()

    def test_live_dependency_has_no_silent_simulated_fallback(self):
        runtime = build()
        runtime.start_live()
        try:
            with self.assertRaisesRegex(RuntimeError, "explicit inventory adapter"):
                runtime.deliver("place", {"order": "buyer_a"})
        finally:
            runtime.close()

    def test_world_snapshot_restore_preserves_idempotency(self):
        world = inventory(1, True)
        with self.assertRaises(PortError):
            world.handlers["stock"]({"order": "buyer_a"})
        snapshot = world.snapshot()
        restored = inventory()
        restored.restore(snapshot)
        snapshot["reserved"].clear()
        self.assertEqual(
            restored.handlers["stock"]({"order": "buyer_a"}),
            {"order": "buyer_a", "status": "reserved"},
        )
        self.assertEqual(
            restored.handlers["stock"]({"order": "buyer_b"}),
            {"order": "buyer_b", "status": "sold_out"},
        )
        self.assertEqual(restored.snapshot()["reserved"], ["buyer_a"])

    def test_world_rejects_invalid_snapshots_and_requests(self):
        base = {"capacity": 1, "reserved": [], "lost": [], "lose_ack": True}
        for value in (
            {**base, "capacity": True},
            {**base, "reserved": ["buyer_a", "buyer_b"]},
            {**base, "reserved": ["buyer_a", "buyer_a"]},
            {**base, "lost": ["buyer_a"]},
            {**base, "extra": 0},
            {**base, "reserved": [1]},
            {**base, "reserved": ["buyer_a"], "lost": ["buyer_a"], "lose_ack": False},
        ):
            with self.subTest(value=value), self.assertRaises(Fault):
                stock_world(None, value, {})
        for body in (
            {},
            {"order": "../other"},
            {"order": 1},
            {"order": "buyer_a", "extra": True},
        ):
            with self.subTest(body=body), self.assertRaises(Fault):
                inventory().handlers["stock"](body)

    def test_observation_and_translation_keep_other_buyers_private(self):
        actor = population()[0]
        row = {"status": "processing", "attempts": 1, "ready_ns": 2 * SECOND}
        observation = observe(
            actor, {"orders": {actor["id"]: row, "private_buyer": {"secret": 42}}}, 0
        )
        self.assertEqual(observation, {"order": row})
        observation["order"]["status"] = "fulfilled"
        self.assertEqual(row["status"], "processing")
        observation = {"order": row}
        request = {"actor_id": actor["id"], "observation": observation}
        self.assertEqual([item["id"] for item in options(request)], ["wait"])
        with self.assertRaises(Fault):
            translate(actor, {"type": "place", "body": {"order": "private_buyer"}}, "d")
        with self.assertRaises(Fault):
            translate(actor, {"type": "fulfill", "body": {"order": actor["id"]}}, "d")

    def test_choices_cannot_finish_unobserved_or_pending_work(self):
        for status in (
            None,
            "pending",
            "processing",
            "fulfilled",
            "rejected",
            "unknown",
        ):
            request = {
                "actor_id": "buyer_a",
                "observation": {
                    "order": None if status is None else {"status": status}
                },
            }
            option = options(request)[0]
            expected = (
                "place"
                if status is None
                else "wait"
                if status in ("pending", "processing")
                else "finish"
            )
            self.assertEqual(option["id"], expected)
            if expected == "wait":
                self.assertEqual(option["decision"]["wake_after_ns"], SECOND)

    def test_invalid_observations_and_product_states_fail_loudly(self):
        for row in (42, {}, {"status": "invented"}):
            with self.subTest(row=row), self.assertRaises(Fault):
                options({"actor_id": "buyer_a", "observation": {"order": row}})
        for row in (
            {"status": "processing", "attempts": 0, "ready_ns": SECOND},
            {"status": "processing", "attempts": 1, "ready_ns": True},
            {"status": "pending", "attempts": 2, "ready_ns": None},
            {"status": "unknown", "attempts": 1, "ready_ns": None},
            {"status": "rejected", "attempts": 0, "ready_ns": None},
            {"status": "invented", "attempts": 1, "ready_ns": None},
        ):
            with self.subTest(row=row), self.assertRaises(Fault):
                state_view(SimpleNamespace(state={"orders": {"buyer_a": row}}))

    def test_documented_empty_stock_plan_runs_without_changing_proof_assertions(self):
        from seam.experiments import read_experiment, run_experiment

        plan = loads(
            (Path(__file__).resolve().parents[1] / "scenario-empty.json").read_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "empty"
            summary = run_experiment(root, plan)
            self.assertEqual(read_experiment(root), summary)
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(
                [row["metrics"]["fulfilled"] for row in summary["runs"]], [2, 2, 0, 0]
            )
            self.assertEqual(
                [row["metrics"]["rejected"] for row in summary["runs"]], [0, 0, 2, 2]
            )

    def test_fulfillment_at_cutoff_is_processed_before_window_seals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = prepare(
                root / "case",
                {**variants()[0]["parameters"], "until_ns": 2 * SECOND},
                31,
            )
            result = run_product(
                MODULE, str(path), "sim-starter-orders", str(root / "artifact.json")
            )
            self.assertEqual(result.returncode, 0, (result.stderr, result.artifact))
            measured = measure(result.artifact)
            self.assertEqual(measured["fulfilled"], 2)
            self.assertEqual(measured["pending"], 0)
            self.assertEqual(measured["unfinished_actors"], 0)

    def test_invalid_parameters_create_no_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            for key, value in (
                ("capacity", True),
                ("capacity", 3),
                ("lose_ack", 1),
                ("fulfill_ns", 0),
                ("until_ns", 0),
                ("until_ns", True),
            ):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare(
                        Path(directory) / "case",
                        {**variants()[0]["parameters"], key: value},
                        7,
                    )
                self.assertFalse((Path(directory) / "case").exists())

    def test_actual_simulation_and_checkpoint_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = prepare(root / "case", variants()[1]["parameters"], 7)
            full = run_product(
                MODULE, str(path), "sim-starter-orders", str(root / "full.json")
            )
            self.assertEqual(full.returncode, 0, (full.stderr, full.artifact))
            self.assertEqual(
                measure(full.artifact),
                {
                    "fulfilled": 1,
                    "rejected": 1,
                    "pending": 0,
                    "ack_unknown": 0,
                    "allocated": 1,
                    "unfinished_actors": 0,
                },
            )
            case = loads(path.read_bytes())
            write_capture_json(
                path.parent / "paused.json", {**case, "checkpoint_after": 6}
            )
            paused = run_product(
                MODULE,
                str(path.parent / "paused.json"),
                "sim-starter-orders",
                str(root / "paused.json"),
            )
            self.assertEqual(paused.returncode, 4, (paused.stderr, paused.artifact))
            write_checkpoint(
                path.parent / "checkpoint.json", paused.artifact["checkpoint"]
            )
            write_capture_json(
                path.parent / "resumed.json",
                {**case, "resume": checkpoint_ref(path.parent / "checkpoint.json")},
            )
            resumed = run_product(
                MODULE,
                str(path.parent / "resumed.json"),
                "sim-starter-orders",
                str(root / "resumed.json"),
            )
            self.assertEqual(resumed.returncode, 0, (resumed.stderr, resumed.artifact))
            self.assertEqual(resumed.artifact["digest"], full.artifact["digest"])
            self.assertEqual(measure(resumed.artifact), measure(full.artifact))

    def test_short_horizon_keeps_pending_orders_distinct_from_fulfilled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = prepare(
                root / "case", {**variants()[0]["parameters"], "until_ns": SECOND}, 19
            )
            result = run_product(
                MODULE, str(path), "sim-starter-orders", str(root / "artifact.json")
            )
            self.assertEqual(result.returncode, 0, (result.stderr, result.artifact))
            self.assertEqual(
                measure(result.artifact),
                {
                    "fulfilled": 0,
                    "rejected": 0,
                    "pending": 2,
                    "ack_unknown": 0,
                    "allocated": 2,
                    "unfinished_actors": 2,
                },
            )

    def test_controller_is_explicit_and_selects_only_offered_choices(self):
        envelope = {
            "provider": "scripted",
            "model": "first-valid-choice-v1",
            "request_id": "d",
            "request": {"choices": [{"id": "wait"}]},
        }
        self.assertEqual(scripted_provider(envelope)["decision"], {"choice": "wait"})
        with self.assertRaises(RuntimeError):
            scripted_provider({**envelope, "provider": "paid"})
        with self.assertRaises(KeyError):
            scripted_provider({**envelope, "request": {}})

    def test_cli_does_not_overwrite_or_create_missing_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "marker"
            marker.write_text("keep")
            for out in (root, root / "missing/demo", marker / "demo"):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    self.assertEqual(main(["--scripted", "--out", str(out)]), 2)
                self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(marker.read_text(), "keep")
            self.assertFalse((root / "missing").exists())
            with self.assertRaises(SystemExit):
                main(["--out", str(root / "no-controller")])
            self.assertFalse((root / "no-controller").exists())

    def test_actual_demo_json_replays_and_portable_viewer_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "demo with spaces"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sim_starter.demo",
                    "--scripted",
                    "--out",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr))
            report = json.loads(proc.stdout)
            self.assertEqual(report["byte_identical_replays"], 8)
            self.assertEqual(
                [row["metrics"]["fulfilled"] for row in report["runs"]], [2, 2, 1, 1]
            )
            self.assertEqual(report["controller"], "scripted-not-an-LLM")
            self.assertIn(viewer_command(root), proc.stderr)
            before = {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*")
                if p.is_file()
            }
            command = shlex.split(viewer_command(root))[:-2] + ["--check"]
            viewer = subprocess.run(
                command, cwd=directory, capture_output=True, text=True, timeout=30
            )
            self.assertEqual(viewer.returncode, 0, viewer.stderr)
            after = {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)
