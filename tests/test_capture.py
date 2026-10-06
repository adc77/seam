"""Closed capture envelopes, bounded exports, source compatibility, and equal-time replay."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from seam import (
    CaptureError,
    capture_bundle,
    capture_source,
    read_capture,
    run_capture,
    validate_capture,
    write_capture_case,
    write_capture_json,
)
from seam.canon import digest
from seam.case import normalize
from seam.errors import Refuse
from seam.queue import Queue
from tests.test_contracts import case, simulate

MODULE = "seam.proof.shared_store"


def document():
    return capture_bundle(
        MODULE,
        as_of_ns=0,
        until_ns=11,
        config={},
        initial_state={},
        datasets={"store": {"as_of_ns": 0, "data": {"balance": 0}}},
        arrivals=[
            {"at_ns": 0, "handler": "write", "body": {"delta": 10}},
            {"at_ns": 10, "handler": "observe", "body": {}},
        ],
        assertions=[{"op": "state_is", "path": "final.balance", "value": 10}],
        product_data={},
    )


def plan():
    return {
        "name": "shared-store-capture",
        "namespace": "sim-shared-store",
        "ports": {
            port: {"mode": "world", "world": "ledger"} for port in ("write", "read")
        },
        "worlds": {"ledger": {"dataset": "store"}},
    }


class CaptureTests(unittest.TestCase):
    def test_package_replay_and_no_duplicate_arrivals_without_boundary_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = run_capture(document(), root / "one", module=MODULE, **plan())
            second = run_capture(document(), root / "two", module=MODULE, **plan())
            self.assertEqual(first.returncode, 0, (first.stderr, first.artifact))
            self.assertEqual(first.artifact, second.artifact)
            self.assertEqual(
                json.loads((root / "one/case.json").read_text())["arrivals"],
                document()["payload"]["arrivals"],
            )
            origin = json.loads((root / "one/origin.json").read_text())
            self.assertEqual(
                origin["case_sha256"],
                digest(json.loads((root / "one/case.json").read_text())),
            )
            self.assertEqual((root / "one").stat().st_mode & 0o777, 0o700)
            for path in (root / "one").glob("*.json"):
                if path.name != "artifact.json":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_roundtrip_does_not_overwrite_and_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            original = document()
            write_capture_json(path, original)
            self.assertEqual(read_capture(path, module=MODULE), original)
            with self.assertRaises(FileExistsError):
                write_capture_json(path, original)
            path.write_text('{"version":1,"version":1}')
            with self.assertRaises(CaptureError):
                read_capture(path, module=MODULE)

    def test_dataset_names_cannot_collide_with_packaged_metadata(self):
        original = document()
        for name in ("case", "origin", "artifact"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                changed = copy.deepcopy(original)
                changed["payload"]["datasets"][name] = changed["payload"][
                    "datasets"
                ].pop("store")
                changed["sha256"] = digest(changed["payload"])
                wiring = plan()
                wiring["worlds"]["ledger"]["dataset"] = name
                case_path = write_capture_case(
                    changed, Path(directory) / "out", module=MODULE, **wiring
                )
                packaged = json.loads(case_path.read_text())
                reference = packaged["datasets"][name]
                self.assertEqual(reference["path"], f"data-{name}.json")
                self.assertEqual(
                    json.loads((case_path.parent / reference["path"]).read_text()),
                    {"balance": 0},
                )

    def test_closed_envelope_integer_types_times_and_assertions(self):
        original = document()
        for mutate in (
            lambda d: d.update(extra=True),
            lambda d: d.update(version=True),
            lambda d: d["payload"].update(extra=True),
            lambda d: d["payload"].update(as_of_ns=True),
            lambda d: d["payload"].update(until_ns=-1),
            lambda d: d["payload"]["datasets"]["store"].update(as_of_ns=1),
            lambda d: d["payload"]["arrivals"][0].update(at_ns=12),
            lambda d: d["payload"]["arrivals"][1].update(at_ns=-1),
            lambda d: d["payload"]["arrivals"][0].update(handler="bad-handler"),
            lambda d: d["payload"]["arrivals"][0].update(extra="private"),
            lambda d: d["payload"].update(assertions=[]),
            lambda d: d["payload"].update(assertions=[{"op": "unknown"}]),
            lambda d: d["payload"].update(product_data=[]),
            lambda d: d["payload"].update(
                datasets={"../escape": {"as_of_ns": 0, "data": {}}}
            ),
        ):
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(original)
                mutate(changed)
                changed["sha256"] = digest(changed["payload"])
                with self.assertRaises(CaptureError):
                    validate_capture(changed, module=MODULE)

    def test_changed_checksum_product_or_sdk_cannot_replay(self):
        original = document()
        changed = copy.deepcopy(original)
        changed["payload"]["config"]["secret"] = "never_echo_this"
        with self.assertRaises(CaptureError) as error:
            validate_capture(changed, module=MODULE)
        self.assertNotIn("never_echo_this", str(error.exception))
        for key in ("product_sha256", "sdk_sha256", "sdk_version", "module"):
            with self.subTest(key=key):
                changed = copy.deepcopy(original)
                changed["payload"]["source"][key] = "changed"
                changed["sha256"] = digest(changed["payload"])
                with self.assertRaises(CaptureError):
                    validate_capture(changed, module=MODULE)
        with self.assertRaises(CaptureError):
            validate_capture(original, module="seam.proof.checkout")
        self.assertEqual(capture_source(MODULE), original["payload"]["source"])

    def test_invalid_case_plan_fails_before_creating_an_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bad"
            for changes in (
                {"same_time_order": "unknown"},
                {"namespace": "live"},
                {"ports": []},
                {"until_ns": -1},
                {"bootstrap": [{"handler": "x"}]},
            ):
                with self.subTest(changes=changes), self.assertRaises(CaptureError):
                    write_capture_case(
                        document(), output, module=MODULE, **{**plan(), **changes}
                    )
                self.assertFalse(output.exists())

    def test_capture_size_and_json_bounds_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for value in ({"x": float("nan")}, {"x": 2**63}, {"x": "x" * 1_048_577}):
                with (
                    self.subTest(value=type(value["x"])),
                    self.assertRaises(CaptureError),
                ):
                    write_capture_json(path, value)
                self.assertFalse(path.exists())
            path.write_bytes(b" " * (8 * 1024 * 1024 + 1))
            with self.assertRaises(CaptureError):
                read_capture(path, module=MODULE)


class EqualTimeTests(unittest.TestCase):
    def test_policy_is_opt_in_and_closed_for_legacy_cases(self):
        for version in (1, 2):
            data = case()
            data.update(version=version, same_time_order="timers_first")
            with self.assertRaises(Refuse):
                normalize(data, ports=(), handlers=("go",), namespace=data["namespace"])
        data = case()
        data.update(version=3)
        original = normalize(
            data, ports=(), handlers=("go",), namespace=data["namespace"]
        )
        self.assertNotIn("same_time_order", original)
        for invalid in (True, [], "arrival_first", "timers_first\n"):
            with self.subTest(invalid=invalid), self.assertRaises(Refuse):
                normalize(
                    {**data, "same_time_order": invalid},
                    ports=(),
                    handlers=("go",),
                    namespace=data["namespace"],
                )

    def test_cancel_flush_and_restore_respect_policy(self):
        for timers_first in (False, True):
            with self.subTest(timers_first=timers_first):
                queue = Queue(timers_first=timers_first)
                queue.push_arrival(10, "arrival", {})
                queue.push_timer(10, "timer", {}, "t1")
                ghost = queue.push_timer(5, "ghost", {}, "t2")
                queue.cancel(ghost, "t2")
                queue.flush()
                snapshot = queue.snapshot()
                restored = Queue(timers_first=timers_first)
                restored.restore(snapshot)
                order = [restored.pop()[2], restored.pop()[2]]
                self.assertEqual(
                    order,
                    ["timer", "arrival"] if timers_first else ["arrival", "timer"],
                )
                self.assertIsNone(restored.pop())

    def test_timer_precedence_in_actual_product_process(self):
        source = (
            "import sys\nfrom seam import Runtime, main\n"
            "def go(ctx, body):\n    ctx.patch('order', ctx.state.get('order', []) + [body['kind']])\n"
            "    if body['kind'] == 'boot':\n        ctx.schedule_at(10, 'go', {'kind':'timer'})\n"
            "rt=Runtime()\nrt.on('go', go)\nsys.exit(main(rt))\n"
        )
        data = case()
        data.update(
            version=3,
            arrivals=[
                {"at_ns": 0, "handler": "go", "body": {"kind": "boot"}},
                {"at_ns": 10, "handler": "go", "body": {"kind": "arrival"}},
            ],
        )
        normal_proc, normal = simulate(source, data)
        priority_proc, priority = simulate(
            source, {**data, "same_time_order": "timers_first"}
        )
        self.assertEqual(normal_proc.returncode, 0, normal_proc.stderr)
        self.assertEqual(priority_proc.returncode, 0, priority_proc.stderr)
        self.assertEqual(normal["final_state"]["order"], ["boot", "arrival", "timer"])
        self.assertEqual(priority["final_state"]["order"], ["boot", "timer", "arrival"])
