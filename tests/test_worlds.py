"""World and checkpoint contracts exercised through fresh supervised product processes."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from seam import Fault, Refuse, Runtime, World, checkpoint_ref, dataset_ref, run_product, write_checkpoint
from seam.canon import digest
from tests.support import REPO
from seam.rng import Rng
from seam.world import WorldManager
from types import SimpleNamespace

PRODUCT = "seam.proof.shared_store"


def shared_case(directory):
    export = Path(directory) / "store.json"
    export.write_text('{"balance":0}\n')
    return {
        "format": "seam-case", "version": 3, "name": "shared-store", "seed": 1842,
        "namespace": "sim-shared-store", "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "initial_state": {}, "config": {}, "datasets": {"store": dataset_ref(export, 0)},
        "worlds": {"ledger": {"dataset": "store"}},
        "ports": {port: {"mode": "world", "world": "ledger"} for port in ("write", "read")},
        "arrivals": [{"at_ns": 0, "handler": "write", "body": {"delta": 10}},
                     {"at_ns": 10, "handler": "observe", "body": {}}],
        "stop": {"when": "quiescence"},
        "assertions": [{"op": "state_is", "path": "after_write.balance", "value": 10}],
    }


def execute(directory, case, label="run", product=PRODUCT, pythonpath=REPO, env=None):
    path = Path(directory) / f"{label}.json"
    path.write_text(json.dumps(case) + "\n")
    return run_product(product, str(path), case["namespace"], str(Path(directory) / f"{label}-out.json"),
                       env={"PYTHONPATH": pythonpath, **({} if env is None else env)})


def repack(document):
    document["digest"] = digest({key: value for key, value in document.items() if key != "digest"})
    return document


def custom_product(directory, factory):
    source = (
        "import sys\nfrom seam import Runtime, World, Fault, PortError, main\n"
        + factory + "\nrt = Runtime()\nrt.port('p', lambda: None)\n"
        "rt.sim_world('w', factory, ports=('p',))\n"
        "def go(ctx, body):\n    try:\n        ctx.patch('result', ctx.emit('p', body))\n"
        "    except PortError:\n        ctx.patch('error', True)\n"
        "    except Exception:\n        pass\n"
        "rt.on('go', go)\nsys.exit(main(rt))\n"
    )
    Path(directory, "consumer.py").write_text(source)
    export = Path(directory, "custom-data.json")
    export.write_text("{}")
    case = {
        "format": "seam-case", "version": 3, "name": "custom", "seed": 1842,
        "namespace": "sim-custom", "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "initial_state": {}, "datasets": {"d": dataset_ref(export, 0)},
        "worlds": {"w": {"dataset": "d"}}, "ports": {"p": {"mode": "world", "world": "w"}},
        "arrivals": [{"at_ns": at, "handler": "go", "body": {"n": at}} for at in (0, 10)],
        "stop": {"when": "quiescence"}, "assertions": [],
    }
    return case, os.pathsep.join((directory, REPO))


class WorldRegistrationTest(unittest.TestCase):
    def test_older_cases_keep_closed_assertion_schema(self):
        from seam.case import normalize
        from tests.test_contracts import case

        for version in (1, 2):
            for assertion in ({"op": "stopped", "reason": "checkpoint"},
                              {"op": "timer_outcome", "token": "t1", "outcome": "armed"}):
                with self.subTest(version=version, assertion=assertion):
                    data = case()
                    data.update(version=version, assertions=[assertion])
                    with self.assertRaises(Refuse):
                        normalize(data, ports=("p",), handlers=("go",), namespace=data["namespace"])

    def test_world_registration_is_explicit_and_disjoint(self):
        rt = Runtime()
        rt.port("p", lambda: None)
        rt.port("q", lambda: None)
        for name, ports in (("bad-name", ("p",)), ("ok", ()), ("ok", ("missing",)),
                            ("ok", ("p", "p")), ("ok", "p"), ("ok", ([],))):
            with self.subTest(name=name, ports=ports), self.assertRaises(Refuse):
                rt.sim_world(name, lambda *args: None, ports=ports)
        rt.sim_world("ok", lambda *args: None, ports=("p",))
        with self.assertRaises(Refuse):
            rt.sim_world("other", lambda *args: None, ports=("p",))
        with self.assertRaises(Refuse):
            rt.sim_port("p", lambda *args: None)
        rt.sim_port("q", lambda *args: None)
        with self.assertRaises(Refuse):
            rt.sim_world("other", lambda *args: None, ports=("q",))

    def test_async_world_factories_are_refused_and_live_does_not_instantiate_worlds(self):
        async def factory(*args):
            pass

        rt = Runtime()
        rt.port("p", lambda: lambda request: {})
        with self.assertRaises(Refuse):
            rt.sim_world("w", factory, ports=("p",))
        rt.sim_world("w", lambda *args: self.fail("Simulation world called live"), ports=("p",))
        rt.on("go", lambda ctx, body: ctx.emit("p", {}))
        rt.start_live()
        rt.deliver("go", {})
        rt.close()


class SharedWorldTest(unittest.TestCase):
    def test_world_cleanup_is_lazy_and_runs_once(self):
        closed = []
        host = SimpleNamespace(now=lambda: 0, rng=Rng(1), namespace="sim-test", config={})
        world = WorldManager(lambda *args: World({"p": lambda request: {}}, lambda: {},
                                                lambda state: None, lambda: closed.append(1)), ("p",), {}, {})
        world.bind(host)
        self.assertEqual(world.state(), {})
        world.call("p", {})
        world.close()
        world.close()
        self.assertEqual(closed, [1])
        with self.assertRaises(Fault):
            world.call("p", {})

    def test_world_failures_are_sticky_scrubbed_and_cleaned_up(self):
        factories = (
            "def factory(ctx, data, config):\n    try:\n        World(None, None, None)\n"
            "    except Exception:\n        pass\n    return World({'p': lambda r: {}}, lambda: {}, lambda s: None)",
            "def factory(ctx, data, config):\n    raise ValueError('private details')",
            "def factory(ctx, data, config):\n    return World({}, lambda: {}, lambda s: None)",
            "def factory(ctx, data, config):\n    async def handle(r):\n        return {}\n"
            "    return World({'p': handle}, lambda: {}, lambda s: None)",
            "def factory(ctx, data, config):\n    def snapshot():\n        return {'draw': ctx.rand_u64()}\n"
            "    return World({'p': lambda r: {}}, snapshot, lambda s: None)",
            "def factory(ctx, data, config):\n    def close():\n        raise ValueError('private details')\n"
            "    return World({'p': lambda r: {}}, lambda: {}, lambda s: None, close)",
            "def factory(ctx, data, config):\n    async def result():\n        return {}\n"
            "    return World({'p': lambda r: result()}, lambda: {}, lambda s: None)",
            "def factory(ctx, data, config):\n    return World({'p': lambda r: {}}, lambda: {}, lambda s: None, lambda: 1)",
        )
        for index, factory in enumerate(factories):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                case, pythonpath = custom_product(directory, factory)
                result = execute(directory, {**case, "checkpoint_after": 1}, product="consumer", pythonpath=pythonpath)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(result.artifact["fault"]["code"], ("bad_backend", "unsupported_handler"))
                self.assertNotIn("checkpoint", result.artifact)
                self.assertNotIn("private details", result.stderr + Path(result.artifact_path).read_text())
                self.assertNotIn("was never awaited", result.stderr)

    def test_write_and_read_share_database_and_runs_are_fresh_and_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            case = shared_case(directory)
            runs = [execute(directory, case, f"run{index}") for index in range(2)]
            for result in runs:
                self.assertEqual(result.returncode, 0, result.stderr)
                art = result.artifact
                self.assertEqual(art["final_state"]["final"], {"balance": 10, "at_ns": 10})
                self.assertEqual(art["world_states"]["ledger"]["balance"], 10)
                self.assertEqual([row["outcome"] for row in art["timers"]], ["fired", "cancelled", "cancelled", "fired"])
            self.assertEqual(Path(runs[0].artifact_path).read_bytes(), Path(runs[1].artifact_path).read_bytes())
            self.assertEqual(Path(directory, "store.json").read_text(), '{"balance":0}\n')

    def test_world_spec_is_closed_and_requires_registered_dataset_and_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            original = shared_case(directory)
            changes = (
                lambda c: c.update(version=2),
                lambda c: c["ports"]["read"].update(world="missing"),
                lambda c: c["worlds"]["ledger"].update(dataset="missing"),
                lambda c: c["worlds"]["ledger"].update(extra=True),
                lambda c: c["worlds"].update(other={"dataset": "store"}),
            )
            for change in changes:
                case = copy.deepcopy(original)
                change(case)
                result = execute(directory, case)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.artifact["events"], [])


class CheckpointTest(unittest.TestCase):
    def test_pause_resume_and_repeated_pause_match_uninterrupted_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            baseline = execute(directory, base, "baseline")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            paused_case = {**base, "checkpoint_after": 1}
            for index in range(3):
                paused = execute(directory, paused_case, f"pause{index}")
                self.assertEqual(paused.returncode, 4, paused.stderr)
                self.assertEqual(paused.artifact["status"], "paused")
                self.assertEqual(paused.artifact["stop_reason"], "checkpoint")
                checkpoint = Path(directory) / f"checkpoint{index}.json"
                write_checkpoint(checkpoint, paused.artifact["checkpoint"])
                self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o600)
                resumed_case = {**base, "resume": checkpoint_ref(checkpoint)}
                resumed = execute(directory, resumed_case, f"resume{index}")
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])
                for key in ("events", "timers", "port_calls", "state_snapshots", "final_state", "world_states"):
                    self.assertEqual(resumed.artifact[key], baseline.artifact[key], key)
                paused_case = {**resumed_case, "checkpoint_after": 1}

    def test_case_changes_and_mutated_checkpoint_refuse_before_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            checkpoint = Path(directory) / "checkpoint.json"
            write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            resume = {**base, "resume": checkpoint_ref(checkpoint)}
            for key, value in (("seed", 1), ("config", {"changed": True}), ("initial_state", {"changed": True})):
                changed = copy.deepcopy(resume)
                changed[key] = value
                result = execute(directory, changed)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.artifact["fault"]["code"], "checkpoint_mismatch")
            result = execute(directory, resume, env={"MODE": "changed"})
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(result.artifact["fault"]["code"], "checkpoint_mismatch")
            checkpoint.write_text(checkpoint.read_text() + " ")
            result = execute(directory, resume)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.artifact["fault"]["code"], "checkpoint_changed")

    def test_valid_checksum_does_not_make_invalid_pending_work_or_counters_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1})
            self.assertEqual(paused.returncode, 4, paused.stderr)
            original = paused.artifact["checkpoint"]
            changes = (
                lambda p: p.update(delivered=True),
                lambda p: p.update(rng_counter=-1),
                lambda p: p["queue"].update(next_seq=0),
                lambda p: p["queue"]["entries"].pop(0),
                lambda p: p["timers"][0].update(token="t99"),
                lambda p: p["worlds"]["ledger"]["bootstrap"].update(at_ns=999),
                lambda p: p["events"][0].update(status="error"),
                lambda p: p["timers"][0].update(seq=True),
                lambda p: p["events"][1].update(during=True),
                lambda p: p["events"][1].update(extra=True),
                lambda p: p["queue"]["entries"][0].__setitem__(1, {}),
                lambda p: p.update(at_ns=999),
            )
            for index, change in enumerate(changes):
                with self.subTest(index=index):
                    document = copy.deepcopy(original)
                    change(document["payload"])
                    checkpoint = Path(directory) / f"invalid{index}.json"
                    write_checkpoint(checkpoint, repack(document))
                    result = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)})
                    self.assertEqual(result.returncode, 3, result.stderr)
                    self.assertEqual(result.artifact["fault"]["code"], "bad_checkpoint")

    def test_empty_world_remains_lazy_and_end_only_history_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            base["log_state"] = "end_only"
            base["assertions"] = []
            base["arrivals"] = [{"at_ns": 10, "handler": "done", "body": {}}, *base["arrivals"]]
            base["arrivals"][1]["at_ns"] = 20
            base["arrivals"][2]["at_ns"] = 30
            baseline = execute(directory, base, "baseline")
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            self.assertEqual(paused.artifact["checkpoint"]["payload"]["worlds"], {"ledger": {"initialized": False}})
            checkpoint = Path(directory) / "cp.json"
            write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)}, "resumed")
            self.assertEqual(resumed.returncode, baseline.returncode, resumed.stderr)
            self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])

    def test_script_and_recording_cursors_and_errors_survive_resume(self):
        factory = "def factory(ctx, data, config):\n    return World({'p': lambda r: {}}, lambda: {}, lambda s: None)"
        for mode in ("script", "recording"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                base, pythonpath = custom_product(directory, factory)
                base["worlds"] = {}
                if mode == "script":
                    base["ports"]["p"] = {"mode": mode, "replies": [
                        {"match": {"$any": True}, "error": "timeout", "repeat": 1},
                        {"match": {"$any": True}, "response": {"ok": True}, "repeat": 1},
                    ]}
                else:
                    rows = [{"format": "seam-tape", "version": 2, "at_ns": at, "port": "p",
                             "request": {"n": at}, **value}
                            for at, value in ((0, {"error": "timeout"}), (10, {"response": {"ok": True}}))]
                    tape = Path(directory, "tape.jsonl")
                    tape.write_text("".join(json.dumps(row) + "\n" for row in rows))
                    base["ports"]["p"] = {"mode": mode, "tape": tape.name, "cutoff_ns": 10, "policy": "ordered"}
                baseline = execute(directory, base, "baseline", "consumer", pythonpath)
                self.assertEqual(baseline.returncode, 0, baseline.stderr)
                paused = execute(directory, {**base, "checkpoint_after": 1}, "paused", "consumer", pythonpath)
                self.assertEqual(paused.returncode, 4, paused.stderr)
                checkpoint = Path(directory, "cp.json")
                write_checkpoint(checkpoint, paused.artifact["checkpoint"])
                resume = {**base, "resume": checkpoint_ref(checkpoint)}
                resumed = execute(directory, resume, "resumed", "consumer", pythonpath)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])
                damaged = copy.deepcopy(paused.artifact["checkpoint"])
                if mode == "script":
                    damaged["payload"]["ports"]["p"]["left"][0] = 1
                else:
                    damaged["payload"]["ports"]["p"]["cursor"] = 0
                write_checkpoint(Path(directory, "damaged.json"), repack(damaged))
                refused = execute(directory, {**base, "resume": checkpoint_ref(Path(directory, "damaged.json"))},
                                  "damaged", "consumer", pythonpath)
                self.assertEqual(refused.returncode, 3, refused.stderr)
                self.assertEqual(refused.artifact["fault"]["code"], "bad_checkpoint")
                if mode == "recording":
                    rows[1]["response"] = {"changed": True}
                    tape.write_text("".join(json.dumps(row) + "\n" for row in rows))
                    refused = execute(directory, resume, "changed", "consumer", pythonpath)
                    self.assertEqual(refused.returncode, 3)
                    self.assertEqual(refused.artifact["fault"]["code"], "checkpoint_mismatch")

    def test_source_changes_refuse_and_restore_failures_cannot_pause(self):
        factory = (
            "def factory(ctx, data, config):\n    def restore(state):\n        raise ValueError('private restore details')\n"
            "    return World({'p': lambda r: {}}, lambda: {}, restore)"
        )
        with tempfile.TemporaryDirectory() as directory:
            base, pythonpath = custom_product(directory, factory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused", "consumer", pythonpath)
            self.assertEqual(paused.returncode, 4, paused.stderr)
            checkpoint = Path(directory, "cp.json")
            write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            resume = {**base, "resume": checkpoint_ref(checkpoint)}
            restored = execute(directory, resume, "restore", "consumer", pythonpath)
            self.assertEqual(restored.returncode, 2, restored.stderr)
            self.assertEqual(restored.artifact["fault"]["code"], "bad_backend")
            self.assertEqual(len(restored.artifact["events"]), len(paused.artifact["events"]))
            source = Path(directory, "consumer.py")
            source.write_text(source.read_text() + "\n# Different product build.\n")
            changed = execute(directory, resume, "source-changed", "consumer", pythonpath)
            self.assertEqual(changed.returncode, 3, changed.stderr)
            self.assertEqual(changed.artifact["fault"]["code"], "checkpoint_mismatch")

    def test_pause_does_not_reset_event_or_port_budgets(self):
        for budget in ({"max_events": 1}, {"max_port_calls": 2}):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
                base = shared_case(directory)
                base["stop"].update(budget)
                baseline = execute(directory, base, "baseline")
                self.assertEqual(baseline.returncode, 2, baseline.stderr)
                paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
                self.assertEqual(paused.returncode, 4, paused.stderr)
                checkpoint = Path(directory, "cp.json")
                write_checkpoint(checkpoint, paused.artifact["checkpoint"])
                resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)}, "resumed")
                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])

    def test_all_snapshot_logging_modes_and_empty_pending_queue_resume(self):
        for mode in ("every_event", "on_change", "end_only"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                base = shared_case(directory)
                base["log_state"] = mode
                baseline = execute(directory, base, "baseline")
                self.assertEqual(baseline.returncode, 0, baseline.stderr)
                paused = execute(directory, {**base, "checkpoint_after": 4}, "paused")
                self.assertEqual(paused.returncode, 4, paused.stderr)
                self.assertEqual(paused.artifact["checkpoint"]["payload"]["queue"]["entries"], [])
                checkpoint = Path(directory, "cp.json")
                write_checkpoint(checkpoint, paused.artifact["checkpoint"])
                resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)}, "resumed")
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])

    def test_checkpoint_io_rejects_unknown_fields_invalid_json_and_escaped_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            checkpoint = Path(directory, "cp.json")
            document = copy.deepcopy(paused.artifact["checkpoint"])
            document["unexpected"] = True
            with self.assertRaises(Refuse):
                write_checkpoint(checkpoint, repack(document))
            checkpoint.write_text('{"broken":')
            with self.assertRaises(Refuse):
                checkpoint_ref(checkpoint)
            write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            ref = checkpoint_ref(checkpoint)
            for path in ("../cp.json", str(checkpoint), ""):
                refused = execute(directory, {**base, "resume": {**ref, "path": path}})
                self.assertEqual(refused.returncode, 3, refused.stderr)
                self.assertEqual(refused.artifact["events"], [])

    def test_paused_assertions_are_not_treated_as_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            base["assertions"] = [{"op": "state_is", "path": "done", "value": True}]
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 1, paused.stderr)
            self.assertEqual(paused.artifact["status"], "failed")
            self.assertIn("checkpoint", paused.artifact)

    def test_legacy_backend_checkpoints_refuse_instead_of_losing_dependency_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            base["worlds"] = {}
            base["ports"] = {port: {"mode": "backend", "dataset": "store"} for port in ("write", "read")}
            base["checkpoint_after"] = 1
            result = execute(directory, base)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.artifact["events"], [])

    def test_checkpoint_writer_does_not_overwrite_an_existing_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1})
            self.assertEqual(paused.returncode, 4, paused.stderr)
            checkpoint = Path(directory) / "cp.json"
            Path(str(checkpoint) + ".tmp").write_text("unrelated")
            with self.assertRaises(FileExistsError):
                write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            self.assertEqual(Path(str(checkpoint) + ".tmp").read_text(), "unrelated")


if __name__ == "__main__":
    unittest.main()
