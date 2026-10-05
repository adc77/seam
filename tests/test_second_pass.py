"""Regressions reproduced through real child processes and live adapters."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from seam import PortError, Refuse, Runtime, dataset_ref, run_product
from tests.support import DECLINED, REPO, run_product_case, write_json
from tests.test_contracts import case, product, simulate


class BoundaryTest(unittest.TestCase):
    def test_identifier_validation_rejects_trailing_newlines(self):
        with self.assertRaises(Refuse):
            Runtime().on("go\n", lambda ctx, body: None)
        with self.assertRaises(Refuse):
            Runtime().port("p\n", lambda: None)
        for field in ("name", "namespace"):
            data = case()
            data[field] += "\n"
            proc, art = simulate(product("def go(ctx, body):\n    pass"), data)
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertEqual(art["events"], [])

    def test_backend_programming_errors_cannot_be_hidden_and_attempts_are_logged(self):
        factories = (
            "def backend(data, config):\n    try:\n        Backend(None, None)\n"
            "    except Exception:\n        pass\n    return Backend(lambda request: {}, lambda: data)",
            "def backend(data, config):\n    raise ValueError('private details')",
            "def backend(data, config):\n    def handle(request):\n        raise ValueError('private details')\n"
            "    return Backend(handle, lambda: data)",
        )
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "data.json"
            export.write_text("{}")
            data = case()
            data.update(version=2, datasets={"d": dataset_ref(export, 0)})
            data["ports"] = {"p": {"mode": "backend", "dataset": "d"}}
            path, output = Path(directory) / "case.json", Path(directory) / "out.json"
            write_json(path, data)
            for factory in factories:
                with self.subTest(factory=factory):
                    source = (
                        "import sys\nfrom seam import Backend, Runtime, main\n"
                        + factory
                        + "\nrt = Runtime()\nrt.port('p', lambda: None)\nrt.sim_port('p', backend)\n"
                        + "def go(ctx, body):\n    try:\n        ctx.emit('p', {})\n    except Exception:\n        pass\n"
                        + "rt.on('go', go)\nsys.exit(main(rt))\n"
                    )
                    proc = run_product_case(str(path), data["namespace"], str(output), source)
                    art = json.loads(output.read_text())
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(art["fault"]["code"], "bad_backend")
                    self.assertEqual(len(art["port_calls"]), 1)
                    self.assertNotIn("private details", output.read_text() + proc.stderr)

    def test_json_object_keys_and_recursive_values_fault_even_if_caught(self):
        for statement in (
            "ctx.set_state({chr(0xd800): 1})",
            "ctx.set_state({'x' * 1048577: 1})",
            "value = []; value.append(value); ctx.set_state(value)",
        ):
            with self.subTest(statement=statement):
                proc, art = simulate(
                    product(
                        "def go(ctx, body):\n    try:\n        "
                        + statement
                        + "\n    except Exception:\n        pass"
                    )
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(art["fault"]["code"], "bad_value")
                self.assertEqual(art["final_state"], {})

    def test_oversize_initial_state_is_refused_in_every_logging_mode(self):
        for logging in ("end_only", "on_change", "every_event"):
            with self.subTest(logging=logging):
                data = case()
                data["initial_state"] = ["x" * 800_000, "y" * 800_000]
                data["log_state"] = logging
                proc, art = simulate(product("def go(ctx, body):\n    pass"), data)
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertEqual(art["fault"]["code"], "bad_case")
                self.assertEqual(art["events"], [])

    def test_allowlisted_stat_works_but_raw_and_relative_descriptor_access_do_not(self):
        proc, art = simulate(
            product("def go(ctx, body):\n    os.stat('seam/__init__.py').st_mtime_ns", "import os")
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(art["fault"]["code"], "file_read")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.txt"
            fixture.write_text("test")
            setup = (
                f"import os\nfrom seam.guard import allow_read, _real_stat\nFIXTURE = {str(fixture)!r}\n"
                "allow_read(FIXTURE)"
            )
            proc, art = simulate(
                product(
                    "def go(ctx, body):\n    ctx.set_state({'size': os.stat(FIXTURE, follow_symlinks=False).st_size})",
                    setup,
                )
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(art["final_state"], {"size": 4})
            self.assertIn(str(fixture), art["fs_reads"])
            proc, art = simulate(product("def go(ctx, body):\n    _real_stat(FIXTURE)", setup))
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(art["fault"]["code"], "file_access")

        with (
            tempfile.TemporaryDirectory(dir=REPO) as directory,
            tempfile.TemporaryDirectory() as other,
        ):
            fixture = Path(directory) / "fixture.txt"
            fixture.write_text("test")
            relative = os.path.relpath(fixture, REPO)
            setup = (
                f"import os\nfrom seam.guard import allow_read\nFIXTURE = {str(fixture)!r}\n"
                + f"OTHER_FD = os.open({other!r}, os.O_RDONLY)\nallow_read(FIXTURE)"
            )
            proc, art = simulate(
                product(f"def go(ctx, body):\n    os.stat({relative!r}, dir_fd=OTHER_FD)", setup)
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(art["fault"]["op"], "file.relative_dir_fd")

    def test_graders_cannot_widen_the_filesystem_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "policy_grader.py").write_text(
                "from seam.guard import trusted\n"
                "def grade(artifact):\n    try:\n        with trusted():\n            pass\n"
                "    except Exception:\n        return []\n    return []\n"
            )
            data = case()
            data["grader"] = "policy_grader:grade"
            proc, art = simulate(
                product("def go(ctx, body):\n    pass"),
                data,
                extra={"PYTHONPATH": os.pathsep.join((directory, REPO))},
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(art["fault"]["code"], "file_access")

    def test_deep_and_huge_integer_tapes_refuse_with_an_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            tape = Path(directory) / "ports.jsonl"
            data = case()
            data["ports"] = {
                "p": {"mode": "recording", "tape": tape.name, "cutoff_ns": 0, "policy": "ordered"}
            }
            path, output = Path(directory) / "case.json", Path(directory) / "out.json"
            write_json(path, data)
            source = (
                "import sys\nfrom seam import Runtime, main\nrt = Runtime()\n"
                "rt.port('p', lambda: None)\nrt.on('go', lambda ctx, body: ctx.emit('p', {}))\n"
                "sys.exit(main(rt))\n"
            )
            for value in ("9" * 5000, "[" * 1200 + "0" + "]" * 1200):
                with self.subTest(value=value[:12]):
                    tape.write_text(
                        '{"format":"seam-tape","version":1,"at_ns":0,"port":"p","request":{},"response":'
                        + value
                        + "}\n"
                    )
                    proc = run_product_case(str(path), data["namespace"], str(output), source)
                    self.assertEqual(proc.returncode, 3, proc.stderr)
                    self.assertEqual(json.loads(output.read_text())["fault"]["code"], "bad_case")

    def test_unusable_list_assertion_paths_fail_without_losing_the_artifact(self):
        for path in ("²", "0" * 5000, "1" * 5000, "01", "١"):
            with self.subTest(path=path[:12]):
                data = case()
                data["initial_state"] = [1]
                data["assertions"] = [{"op": "state_is", "path": path, "value": 1}]
                proc, art = simulate(product("def go(ctx, body):\n    pass"), data)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(art["assertions"][0]["detail"], "path missing")


class RecordingTest(unittest.TestCase):
    def test_live_factory_failures_are_recorded_and_only_existing_bodies_are_redacted(self):
        previous = {key: os.environ.get(key) for key in ("SEAM_RECORD", "SEAM_ARTIFACT")}
        try:
            with tempfile.TemporaryDirectory() as directory:
                tape = Path(directory) / "ports.jsonl"
                os.environ.update(SEAM_RECORD="1", SEAM_ARTIFACT=str(tape))
                attempts, redacted = [], []

                def factory():
                    attempts.append(1)
                    if len(attempts) == 1:
                        raise PortError("timeout")
                    return lambda request: {"status": "filed"}

                def redact(value):
                    redacted.append(value)
                    return {key: item for key, item in value.items() if key != "secret"}

                rt = Runtime()
                rt.port("p", factory)
                rt.redact(redact)
                rt.on("go", lambda ctx, body: ctx.emit("p", {"secret": "private", "sample": "s1"}))
                rt.start_live()
                with self.assertRaises(PortError):
                    rt.deliver("go", {})
                rt.deliver("go", {})
                rt.close()
                rows = [json.loads(line) for line in tape.read_text().splitlines()]
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["error"], "timeout")
                self.assertNotIn("response", rows[0])
                self.assertEqual(rows[1]["response"], {"status": "filed"})
                self.assertEqual(rows[0]["request"], {"sample": "s1"})
                self.assertEqual(len(redacted), 3)
                self.assertNotIn("private", tape.read_text())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class SupervisorIdentityTest(unittest.TestCase):
    def test_valid_but_wrong_case_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            original = run_product(
                "seam.proof.checkout",
                DECLINED,
                "sim-checkout-declined",
                str(Path(directory) / "original.json"),
                env={"PYTHONPATH": REPO},
            )
            self.assertEqual(original.returncode, 0, original.stderr)
            data = json.loads(Path(DECLINED).read_text())
            data["assertions"] = []
            path = Path(directory) / "changed.json"
            write_json(path, data)
            source = (
                "import os\nfrom pathlib import Path\n"
                + f"Path(os.environ['SEAM_ARTIFACT']).write_bytes(Path({original.artifact_path!r}).read_bytes())\n"
            )
            Path(directory, "wrong_case_product.py").write_text(source)
            result = run_product(
                "wrong_case_product",
                str(path),
                data["namespace"],
                str(Path(directory) / "wrong.json"),
                env={"PYTHONPATH": os.pathsep.join((directory, REPO))},
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.artifact["fault"]["code"], "invalid_artifact")
