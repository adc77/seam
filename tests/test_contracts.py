"""Regression tests through the product entry, live inbox, and process supervisor."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from seam import Fault, Refuse, Runtime, dataset_ref, run_product, tape_from_artifact
from tests.support import REPO, run_product_case, write_json


def case():
    return {
        "format": "seam-case",
        "version": 1,
        "name": "contracts",
        "seed": 1842,
        "namespace": "sim-contracts",
        "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "initial_state": {},
        "arrivals": [{"at_ns": 0, "handler": "go", "body": {}}],
        "ports": {},
        "stop": {"when": "quiescence"},
        "assertions": [],
    }


def simulate(source, data=None, extra=None):
    data = case() if data is None else data
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "case.json")
        output = os.path.join(directory, "out.json")
        write_json(path, data)
        proc = run_product_case(path, data["namespace"], output, source, extra=extra)
        with open(output, encoding="ascii") as handle:
            return proc, json.load(handle)


def product(handler, setup=""):
    return (
        "import sys\nfrom seam import Runtime, main, Fault\n"
        + setup
        + "\n"
        + handler
        + "\nrt = Runtime()\nrt.on('go', go)\nsys.exit(main(rt))\n"
    )


class HandlerContractTest(unittest.TestCase):
    def test_async_and_generator_callables_are_rejected(self):
        async def async_handler(ctx, body):
            ctx.set_state({"ran": True})

        def generator(ctx, body):
            yield body

        class AsyncCallable:
            async def __call__(self, ctx, body):
                return body

        for handler in (async_handler, generator, AsyncCallable()):
            with self.subTest(handler=handler), self.assertRaises(Refuse) as raised:
                Runtime().on("go", handler)
            self.assertEqual(raised.exception.code, "unsupported_handler")

    def test_concealed_coroutine_faults_without_an_unawaited_warning(self):
        source = product(
            "async def later(ctx, body):\n    ctx.set_state({'ran': True})\n"
            "def go(ctx, body):\n    return later(ctx, body)"
        )
        proc, art = simulate(source)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(art["fault"]["code"], "unsupported_handler")
        self.assertNotIn("was never awaited", proc.stderr)

    def test_caught_guard_and_port_faults_remain_failures(self):
        for operation, code in (
            ("__import__('time').time()", "real_clock"),
            ("ctx.emit('missing', {})", "unknown_port"),
            ("__import__('time').sleep(0)", "real_clock"),
        ):
            with self.subTest(operation=operation):
                source = product(
                    "def go(ctx, body):\n    try:\n        "
                    + operation
                    + "\n    except Exception:\n        ctx.set_state({'caught': True})"
                )
                proc, art = simulate(source)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(art["fault"]["code"], code)
                self.assertEqual(art["events"][0]["status"], "error")
                self.assertEqual(art["final_state"], {"caught": True})

    def test_system_exit_is_a_fault_with_an_artifact(self):
        proc, art = simulate(product("def go(ctx, body):\n    sys.exit(0)"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(art["fault"]["exc_type"], "SystemExit")

    def test_bad_assertion_types_refuse_before_delivery(self):
        for assertion in (
            {"op": "port_called", "port": [], "times": 1},
            {"op": "stopped", "reason": {}},
            {"op": "event_count", "handler": 1, "times": 1},
        ):
            with self.subTest(assertion=assertion):
                data = case()
                data["assertions"] = [assertion]
                proc, art = simulate(
                    product("def go(ctx, body):\n    ctx.set_state({'ran': True})"), data
                )
                self.assertEqual(proc.returncode, 3)
                self.assertEqual(art["events"], [])

    def test_pycache_paths_do_not_grant_access(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "__pycache__"
            cache.mkdir()
            source_file = cache / "ordinary.txt"
            source_file.write_text("unchanged")
            destination = Path(directory) / "destination.txt"
            operations = (
                f"open({str(source_file)!r}, 'w').write('changed')",
                f"os.rename({str(source_file)!r}, {str(destination)!r})",
                f"open({str(source_file)!r}).read()",
            )
            for operation in operations:
                with self.subTest(operation=operation):
                    proc, art = simulate(
                        product("def go(ctx, body):\n    " + operation, "import os")
                    )
                    self.assertEqual(proc.returncode, 2)
                    self.assertIn(art["fault"]["code"], ("file_read", "file_write"))
                    self.assertEqual(source_file.read_text(), "unchanged")
                    self.assertFalse(destination.exists())

    def test_project_data_is_not_mistaken_for_importable_code(self):
        proc, art = simulate(product("def go(ctx, body):\n    open('README.md').read()"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(art["fault"]["code"], "file_read")


class LiveContractTest(unittest.TestCase):
    def test_same_thread_reentry_and_duplicate_timer_names_are_rejected(self):
        rt = Runtime()
        rt.set_timer_backend(lambda *args: None)

        def go(ctx, body):
            with self.assertRaises(Fault) as raised:
                rt.deliver("go", {})
            self.assertEqual(raised.exception.code, "reentrant")
            ctx.schedule_after(0, "later", {}, name="once")
            with self.assertRaises(Fault) as raised:
                ctx.schedule_after(0, "later", {}, name="once")
            self.assertEqual(raised.exception.code, "bad_value")

        rt.on("go", go)
        rt.on("later", lambda ctx, body: None)
        rt.start_live()
        rt.deliver("go", {})
        rt.close()
        with self.assertRaises(Fault):
            rt.deliver("go", {})

    def test_concurrent_callers_are_serialized_and_reentry_is_rejected(self):
        rt = Runtime()
        entered, release, pending = threading.Event(), threading.Event(), threading.Event()
        seen, errors = [], []

        def go(ctx, body):
            if body["n"] == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("release timed out")
            seen.append(body["n"])

        rt.on("go", go)
        rt.start_live()

        def deliver(n):
            if n == 2:
                pending.set()
            try:
                rt.deliver("go", {"n": n})
            except Exception as err:
                errors.append(err)

        first = threading.Thread(target=deliver, args=(1,))
        second = threading.Thread(target=deliver, args=(2,))
        first.start()
        try:
            self.assertTrue(entered.wait(3))
            second.start()
            self.assertTrue(pending.wait(3))
        finally:
            release.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(seen, [1, 2])

    def test_timer_callbacks_respect_cancel_and_fire_once(self):
        rt = Runtime()
        armed, fired = [], []
        rt.set_timer_backend(lambda *args: armed.append(args))

        def go(ctx, body):
            ctx.schedule_after(0, "later", {"n": 1})
            token = ctx.schedule_after(0, "later", {"n": 2})
            ctx.cancel(token)

        rt.on("go", go)
        rt.on("later", lambda ctx, body: fired.append(body["n"]))
        rt.start_live()
        rt.deliver("go", {})
        self.assertTrue(rt.fire_timer(armed[0][0]))
        self.assertFalse(rt.fire_timer(armed[0][0]))
        self.assertFalse(rt.fire_timer(armed[1][0]))
        self.assertEqual(fired, [1])


class BackendTest(unittest.TestCase):
    def test_inventory_proof_uses_current_database_state_not_a_response_tape(self):
        path = Path(REPO) / "seam/proof/inventory/cases/reserve.json"
        with tempfile.TemporaryDirectory() as directory:
            results = [
                run_product(
                    "seam.proof.inventory",
                    str(path),
                    "sim-inventory-reserve",
                    str(Path(directory) / f"out-{index}.json"),
                    env={"PYTHONPATH": REPO},
                )
                for index in range(2)
            ]
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.artifact["backend_states"], {"stock": {"book": 1, "pen": 1}}
                )
            self.assertEqual(
                Path(results[0].artifact_path).read_bytes(),
                Path(results[1].artifact_path).read_bytes(),
            )
            changed = json.loads(path.read_text())
            for arrival in changed["arrivals"]:
                arrival["body"]["quantity"] = 1
            changed["assertions"][1]["value"] = "reserved"
            export = Path(directory) / "stock.json"
            export.write_bytes(path.with_name("stock.json").read_bytes())
            changed_path = Path(directory) / "case.json"
            write_json(changed_path, changed)
            result = run_product(
                "seam.proof.inventory",
                str(changed_path),
                changed["namespace"],
                str(Path(directory) / "changed.json"),
                env={"PYTHONPATH": REPO},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.artifact["final_state"], {"o1": "reserved", "o2": "reserved"})
            self.assertEqual(result.artifact["backend_states"], {"stock": {"book": 1, "pen": 1}})

    def test_backend_faults_cannot_be_hidden_and_coroutines_are_closed(self):
        factories = (
            ("def backend(data, config):\n    return None", "bad_backend"),
            (
                "async def later(request):\n    return {}\n"
                "def backend(data, config):\n    return Backend(later, lambda: data)",
                "unsupported_handler",
            ),
            (
                "async def later():\n    return {}\ndef backend(data, config):\n    return later()",
                "unsupported_handler",
            ),
            (
                "def backend(data, config):\n"
                "    def snapshot():\n        try:\n            time.time()\n        except Exception:\n            return data\n"
                "    return Backend(lambda request: {}, snapshot)",
                "real_clock",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "data.json"
            export.write_text("{}")
            data = case()
            data.update(version=2, datasets={"data": dataset_ref(export, 0)})
            data["ports"] = {"p": {"mode": "backend", "dataset": "data"}}
            path, output = Path(directory) / "case.json", Path(directory) / "out.json"
            write_json(path, data)
            for factory, expected in factories:
                with self.subTest(expected=expected, factory=factory):
                    source = (
                        "import time, sys\nfrom seam import Backend, Runtime, main\n"
                        + factory
                        + "\nrt = Runtime()\nrt.port('p', lambda: None)\nrt.sim_port('p', backend)\n"
                        + "def go(ctx, body):\n    try:\n        ctx.emit('p', {})\n    except Exception:\n        pass\n"
                        + "rt.on('go', go)\nsys.exit(main(rt))\n"
                    )
                    proc = run_product_case(str(path), data["namespace"], str(output), source)
                    art = json.loads(output.read_text())
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(art["fault"]["code"], expected)
                    self.assertNotIn("was never awaited", proc.stderr)

    def test_export_backed_database_has_read_after_write_and_isolated_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "stock.json"
            export.write_text('{"book":3}')
            data = case()
            data.update(version=2, datasets={"stock": dataset_ref(export, 0)}, config={"take": 2})
            data["ports"] = {"stock": {"mode": "backend", "dataset": "stock"}}
            path = Path(directory) / "case.json"
            write_json(path, data)
            source = """import sqlite3, sys
from seam import Backend, Runtime, main
def make_backend(data, config):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE stock (sku TEXT PRIMARY KEY, quantity INTEGER)')
    db.executemany('INSERT INTO stock VALUES (?, ?)', data.items())
    def handle(request):
        db.execute('UPDATE stock SET quantity = quantity - ? WHERE sku = ?', (request['take'], request['sku']))
        return {'remaining': db.execute('SELECT quantity FROM stock WHERE sku = ?', (request['sku'],)).fetchone()[0]}
    def snapshot():
        return dict(db.execute('SELECT sku, quantity FROM stock ORDER BY sku'))
    return Backend(handle, snapshot)
rt = Runtime()
rt.port('stock', lambda: (_ for _ in ()).throw(RuntimeError('live factory called')))
rt.sim_port('stock', make_backend)
def go(ctx, body):
    ctx.set_state(ctx.emit('stock', {'sku': 'book', 'take': ctx.config['take']}))
rt.on('go', go)
sys.exit(main(rt))
"""
            artifacts = []
            for index in range(2):
                output = Path(directory) / f"out-{index}.json"
                proc = run_product_case(str(path), data["namespace"], str(output), source)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                artifacts.append(output.read_bytes())
            self.assertEqual(artifacts[0], artifacts[1])
            art = json.loads(artifacts[0])
            self.assertEqual(art["final_state"], {"remaining": 1})
            self.assertEqual(art["backend_states"], {"stock": {"book": 1}})
            self.assertEqual(art["provenance"]["datasets"]["stock"], data["datasets"]["stock"])
            self.assertEqual(export.read_text(), '{"book":3}')

    def test_changed_and_future_exports_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "stock.json"
            export.write_text("{}")
            data = case()
            data.update(version=2, datasets={"stock": dataset_ref(export, 1)})
            proc, art = simulate(product("def go(ctx, body):\n    pass"), data)
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(art["fault"]["code"], "dataset_future")
            data["datasets"]["stock"]["as_of_ns"] = 0
            export.write_text('{"changed":true}')
            path, output = Path(directory) / "case.json", Path(directory) / "out.json"
            write_json(path, data)
            proc = run_product_case(
                str(path), data["namespace"], str(output), product("def go(ctx, body):\n    pass")
            )
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertEqual(json.loads(output.read_text())["fault"]["code"], "dataset_changed")


class RecordingContractTest(unittest.TestCase):
    def test_expected_dependency_errors_survive_artifact_to_tape_replay(self):
        data = case()
        data["version"] = 2
        data["ports"] = {
            "p": {
                "mode": "script",
                "replies": [
                    {"match": {}, "error": "timeout"},
                    {"match": {}, "response": {"ok": True}},
                ],
            }
        }
        source = (
            "import sys\nfrom seam import Runtime, PortError, main\nrt = Runtime()\n"
            "rt.port('p', lambda: None)\n"
            "def go(ctx, body):\n    try:\n        ctx.emit('p', {})\n    except PortError:\n        pass\n"
            "    ctx.set_state(ctx.emit('p', {}))\nrt.on('go', go)\nsys.exit(main(rt))\n"
        )
        proc, original = simulate(source, data)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(original["port_calls"][0]["error"], "timeout")
        with tempfile.TemporaryDirectory() as directory:
            tape = Path(directory) / "ports.jsonl"
            tape.write_bytes(tape_from_artifact(original))
            data["ports"] = {
                "p": {"mode": "recording", "tape": tape.name, "cutoff_ns": 0, "policy": "ordered"}
            }
            path, output = Path(directory) / "case.json", Path(directory) / "out.json"
            write_json(path, data)
            proc = run_product_case(str(path), data["namespace"], str(output), source)
            replay = json.loads(output.read_text())
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(replay["final_state"], original["final_state"])
            self.assertEqual(replay["port_calls"][0]["error"], "timeout")
            provenance = replay["provenance"]["tapes"]
            with tape.open("a") as handle:
                handle.write('{"at_ns":1,"response":{"future_float":1.5}}\n')
            proc = run_product_case(str(path), data["namespace"], str(output), source)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(output.read_text())["provenance"]["tapes"], provenance)


class SupervisorTest(unittest.TestCase):
    def test_supervisor_preserves_assertion_failure_loop_fault_and_refusal(self):
        from tests.support import DECLINED

        for change, expected in (("assertion", 1), ("fault", 2), ("refusal", 3)):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                data = json.loads(Path(DECLINED).read_text())
                if change == "assertion":
                    data["assertions"] = [
                        {"op": "state_is", "path": "order.status", "value": "paid"}
                    ]
                elif change == "fault":
                    data["arrivals"][0]["body"]["leak"] = "clock"
                else:
                    data["version"] = 99
                path = Path(directory) / "case.json"
                write_json(path, data)
                result = run_product(
                    "seam.proof.checkout",
                    str(path),
                    data["namespace"],
                    str(Path(directory) / "out.json"),
                    env={"PYTHONPATH": REPO},
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertFalse(result.artifact.get("supervisor", False))

    def test_invalid_artifacts_and_stale_output_are_not_success(self):
        sources = (
            "",
            "import os\nopen(os.environ['SEAM_ARTIFACT'], 'w').write('{\"status\":\"passed\"}')",
        )
        for source in sources:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                Path(directory, "audit_invalid.py").write_text(source)
                path, output = Path(directory) / "case.json", Path(directory) / "out.json"
                write_json(path, case())
                write_json(output, {"status": "passed", "digest": "stale"})
                result = run_product(
                    "audit_invalid",
                    str(path),
                    "sim-contracts",
                    str(output),
                    env={"PYTHONPATH": os.pathsep.join((directory, REPO))},
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.artifact["fault"]["code"],
                    "invalid_artifact" if source else "process_exit",
                )
                self.assertIsNone(json.loads(output.read_text())["digest"])

    def test_parent_secrets_are_not_inherited_and_post_seal_faults_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            module = "audit_environment"
            source = (
                "import os, sys\nfrom seam import Runtime, main, Fault\n"
                "rt = Runtime()\n"
                "def go(ctx, body):\n    ctx.set_state({'secret_present': 'SEAM_AUDIT_SECRET' in os.environ})\n"
                "def grade(artifact):\n    try:\n        __import__('time').time()\n    except Fault:\n        return []\n"
                "rt.on('go', go)\nif __name__ == '__main__':\n    sys.exit(main(rt))\n"
            )
            Path(directory, module + ".py").write_text(source)
            data = case()
            data["grader"] = f"{module}:grade"
            path = Path(directory) / "case.json"
            write_json(path, data)
            previous = os.environ.get("SEAM_AUDIT_SECRET")
            os.environ["SEAM_AUDIT_SECRET"] = "must-not-inherit"
            try:
                result = run_product(
                    module,
                    str(path),
                    data["namespace"],
                    str(Path(directory) / "out.json"),
                    env={"PYTHONPATH": os.pathsep.join((directory, REPO))},
                )
            finally:
                if previous is None:
                    os.environ.pop("SEAM_AUDIT_SECRET")
                else:
                    os.environ["SEAM_AUDIT_SECRET"] = previous
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.artifact["fault"]["code"], "real_clock")
            self.assertEqual(result.artifact["final_state"], {"secret_present": False})

    def test_missing_artifacts_and_timeouts_cannot_report_success(self):
        for name, source, expected in (
            ("empty", "", "process_exit"),
            ("loop", "while True: pass", "process_timeout"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = "audit_" + name
                Path(directory, module + ".py").write_text(source)
                path = Path(directory) / "case.json"
                write_json(path, case())
                sys.path.insert(0, directory)
                try:
                    result = run_product(
                        module,
                        str(path),
                        "sim-contracts",
                        str(Path(directory) / "out.json"),
                        timeout=0.3,
                        env={"PYTHONPATH": os.pathsep.join((directory, REPO))},
                    )
                finally:
                    sys.path.remove(directory)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.artifact["fault"]["code"], expected)
                self.assertTrue(Path(result.artifact_path).is_file())

    def test_real_product_runs_with_a_clean_environment_and_provenance(self):
        from tests.support import DECLINED, RUN_DIGEST

        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "out.json")
            result = run_product(
                "seam.proof.checkout",
                DECLINED,
                "sim-checkout-declined",
                output,
                env={"PYTHONPATH": REPO},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.artifact["digest"], RUN_DIGEST)
            self.assertEqual(len(result.artifact["provenance"]["product"]["sha256"]), 64)
