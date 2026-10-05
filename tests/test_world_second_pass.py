"""Adversarial checkpoint boundaries and dependency restore regressions."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from seam import checkpoint_ref, run_product, write_checkpoint
from seam.runner import _validate
from tests.test_worlds import custom_product, execute, repack, shared_case
from tests.support import REPO


class RestoreContractTest(unittest.TestCase):
    def test_recoverable_factory_errors_can_checkpoint_without_initializing_worlds(self):
        with tempfile.TemporaryDirectory() as directory:
            base, pythonpath = custom_product(directory, "def factory(*args):\n    raise PortError('unavailable')")
            baseline = execute(directory, base, "baseline", "consumer", pythonpath)
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused", "consumer", pythonpath)
            self.assertEqual(paused.returncode, 4, paused.stderr)
            self.assertEqual(paused.artifact["checkpoint"]["payload"]["worlds"], {"w": {"initialized": False}})
            checkpoint = Path(directory, "checkpoint.json")
            write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)}, "resumed", "consumer", pythonpath)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])

    def test_incomplete_restore_faults_before_new_delivery(self):
        factory = (
            "def factory(ctx, data, config):\n    data['value'] = 0\n"
            "    def handle(request):\n        data['value'] += 1\n        return dict(data)\n"
            "    return World({'p': handle}, lambda: data, lambda state: None)"
        )
        with tempfile.TemporaryDirectory() as directory:
            base, pythonpath = custom_product(directory, factory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused", "consumer", pythonpath)
            self.assertEqual(paused.returncode, 4, paused.stderr)
            checkpoint = Path(directory, "checkpoint.json")
            write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)}, "resumed", "consumer", pythonpath)
            self.assertEqual(resumed.returncode, 2, resumed.artifact)
            self.assertEqual(resumed.artifact["fault"]["op"], "world.restore_state")
            self.assertEqual(resumed.artifact["events"], paused.artifact["events"])

    def test_successful_world_calls_cannot_restore_as_unused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            document = copy.deepcopy(paused.artifact["checkpoint"])
            document["payload"]["worlds"]["ledger"] = {"initialized": False}
            checkpoint = Path(directory, "checkpoint.json")
            write_checkpoint(checkpoint, repack(document))
            resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)}, "resumed")
            self.assertEqual(resumed.returncode, 3, resumed.artifact)
            self.assertEqual(resumed.artifact["fault"]["code"], "bad_checkpoint")
            self.assertEqual(resumed.artifact["events"], [])


class PausedArtifactTest(unittest.TestCase):
    def test_supervisor_rejects_another_products_checkpoint_in_a_real_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            base = {**shared_case(directory), "checkpoint_after": 1}
            paused = execute(directory, base, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            fixture = Path(directory, "forged.json")
            fixture.write_text(json.dumps(paused.artifact))
            Path(directory, "forger.py").write_text(
                "import json, os, sys\n"
                + f"artifact = json.load(open({str(fixture)!r}))\n"
                + "with open(os.environ['SEAM_ARTIFACT'], 'w') as handle:\n    json.dump(artifact, handle)\n"
                + "sys.exit(4)\n"
            )
            result = run_product("forger", str(Path(directory, "paused.json")), base["namespace"],
                                 str(Path(directory, "forged-out.json")),
                                 env={"PYTHONPATH": os.pathsep.join((directory, REPO))})
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.artifact["fault"]["code"], "invalid_artifact")

    def test_checkpoint_identity_must_match_the_supervised_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            art = paused.artifact
            identity = art["checkpoint"]["identity"]
            self.assertTrue(_validate(art, 4, base["namespace"], art["case_digest"], identity))
            for key in ("product_sha256", "sdk_sha256", "environment_sha256", "python"):
                with self.subTest(key=key):
                    changed = {**identity, key: "different"}
                    self.assertFalse(_validate(art, 4, base["namespace"], art["case_digest"], changed))

    def test_supervisor_rejects_pause_disguised_as_completed_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            art = copy.deepcopy(paused.artifact)
            art["status"] = "passed"
            self.assertFalse(_validate(art, 0, base["namespace"], art["case_digest"]))

    def test_supervisor_rejects_checkpoint_payload_not_matching_its_artifact(self):
        changes = (
            lambda p: p.update(state={"wrong": True}),
            lambda p: p.update(events=[]),
            lambda p: p.update(port_calls=[]),
            lambda p: p.update(state_snapshots=[]),
            lambda p: p.update(at_ns=1),
            lambda p: p["timers"][0].update(outcome="cancelled"),
            lambda p: p["worlds"]["ledger"].update(snapshot={"balance": 0}),
        )
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            for index, change in enumerate(changes):
                with self.subTest(index=index):
                    art = copy.deepcopy(paused.artifact)
                    change(art["checkpoint"]["payload"])
                    repack(art["checkpoint"])
                    self.assertFalse(_validate(art, 4, base["namespace"], art["case_digest"]))


class StopBoundaryTest(unittest.TestCase):
    def test_deadlines_and_terminal_stops_preserve_their_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = shared_case(directory)
            base["stop"] = {"when": "deadline", "deadline_ns": 5}
            baseline = execute(directory, base, "baseline")
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            paused = execute(directory, {**base, "checkpoint_after": 1}, "paused")
            self.assertEqual(paused.returncode, 4, paused.stderr)
            checkpoint = Path(directory, "checkpoint.json")
            write_checkpoint(checkpoint, paused.artifact["checkpoint"])
            resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)}, "resumed")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])
        with tempfile.TemporaryDirectory() as directory:
            base, pythonpath = custom_product(directory, "def factory(*args):\n    raise AssertionError('unused world')")
            source = Path(directory, "consumer.py")
            source.write_text(source.read_text().replace("ctx.patch('result', ctx.emit('p', body))", "ctx.stop('done')"))
            base["stop"] = {"when": "terminal", "terminal": "done"}
            result = execute(directory, {**base, "checkpoint_after": 1}, "terminal", "consumer", pythonpath)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.artifact["terminal"], "done")
            self.assertNotIn("checkpoint", result.artifact)


MULTI_WORLD_PRODUCT = """
import sys
from seam import Runtime, World, main

def world(ctx, data, config, name):
    state = {'balance': data['balance'], 'boot_id': ctx.id(name), 'boot_ns': ctx.now(), 'factor': config['factor']}
    config['factor'] = -1
    ctx.config['factor'] = -2
    def write(request):
        state['balance'] += request['delta'] * state['factor']
        return dict(state)
    def read(request):
        return {**state, 'at_ns': ctx.now(), 'utc': ctx.utc(), 'draw': ctx.rand_below(1000)}
    def restore(snapshot):
        state.clear()
        state.update(snapshot)
    def close():
        sys.stderr.write('CLOSED ' + name + '\\n')
    return World({name + '_write': write, name + '_read': read}, lambda: state, restore, close)

rt = Runtime()
for name in ('alpha', 'zeta'):
    for suffix in ('write', 'read'):
        rt.port(name + '_' + suffix, lambda: None)
    rt.sim_world(name, lambda ctx, data, config, name=name: world(ctx, data, config, name),
                 ports=(name + '_write', name + '_read'))

def go(ctx, body):
    name = body['world']
    write = ctx.emit(name + '_write', {'delta': body['delta']})
    read = ctx.emit(name + '_read', {})
    ctx.patch('log', ctx.state['log'] + [{'write': write, 'read': read, 'job': ctx.id('job')}])
    ctx.schedule_after(body['delay'], 'timer', {'world': name})
    ghost = ctx.schedule_after(0, 'timer', {})
    ctx.cancel(ghost)

def timer(ctx, body):
    read = ctx.emit(body['world'] + '_read', {})
    ctx.patch('log', ctx.state['log'] + [{'read': read, 'job': ctx.id('job')}])

rt.on('go', go)
rt.on('timer', timer)
sys.exit(main(rt))
"""


class MultipleWorldTest(unittest.TestCase):
    def test_reverse_initialization_order_shared_export_and_tied_work_resume_exactly(self):
        for seed in (0, 1842, 2**64 - 1):
            for mode in ("every_event", "on_change", "end_only"):
                with self.subTest(seed=seed, mode=mode), tempfile.TemporaryDirectory() as directory:
                    Path(directory, "multi.py").write_text(MULTI_WORLD_PRODUCT)
                    base = shared_case(directory)
                    base.update(seed=seed, log_state=mode, config={"factor": 2}, initial_state={"log": []}, assertions=[])
                    base["worlds"] = {name: {"dataset": "store"} for name in ("alpha", "zeta")}
                    base["ports"] = {name + "_" + suffix: {"mode": "world", "world": name}
                                     for name in ("alpha", "zeta") for suffix in ("write", "read")}
                    base["arrivals"] = [
                        {"at_ns": at, "handler": "go", "body": {"world": name, "delta": delta, "delay": delay}}
                        for at, name, delta, delay in ((20, "alpha", 3, 0), (10, "zeta", 7, 10), (30, "zeta", 2, 5))
                    ]
                    pythonpath = os.pathsep.join((directory, REPO))
                    baseline = execute(directory, base, "baseline", "multi", pythonpath)
                    self.assertEqual(baseline.returncode, 0, baseline.stderr)
                    self.assertEqual(baseline.artifact["world_states"]["alpha"]["balance"], 6)
                    self.assertEqual(baseline.artifact["world_states"]["zeta"]["balance"], 18)
                    self.assertEqual(baseline.stderr.count("CLOSED alpha"), 1)
                    self.assertEqual(baseline.stderr.count("CLOSED zeta"), 1)
                    for count in (1, 3, 6):
                        paused = execute(directory, {**base, "checkpoint_after": count}, f"paused{count}", "multi", pythonpath)
                        self.assertEqual(paused.returncode, 4, paused.stderr)
                        checkpoint = Path(directory, "checkpoint.json")
                        write_checkpoint(checkpoint, paused.artifact["checkpoint"])
                        resumed = execute(directory, {**base, "resume": checkpoint_ref(checkpoint)},
                                          f"resumed{count}", "multi", pythonpath)
                        self.assertEqual(resumed.returncode, 0, resumed.stderr)
                        self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])
                        for key in ("events", "timers", "port_calls", "state_snapshots", "final_state", "world_states"):
                            self.assertEqual(resumed.artifact[key], baseline.artifact[key], key)
                        self.assertEqual(resumed.stderr.count("CLOSED alpha"), 1)
                        self.assertEqual(resumed.stderr.count("CLOSED zeta"), 1)
                    self.assertEqual(base["config"], {"factor": 2})
                    self.assertEqual(Path(directory, "store.json").read_text(), '{"balance":0}\n')


if __name__ == "__main__":
    unittest.main()
