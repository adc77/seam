"""Actor schemas, isolation, recorded decisions, and fresh-process checkpoint recovery."""

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from seam import (
    Fault,
    Runtime,
    checkpoint_ref,
    dataset_ref,
    run_product,
    write_checkpoint,
)
from seam.actors import ActorRandom, ActorSystem, population
from seam.canon import INT64_MAX, MAX_STRING, dumps
from tests.support import REPO


def actor(actor_id="alice", **changes):
    value = {
        "id": actor_id,
        "policy": "counter_v1",
        "profile": {"goal": 8, "delay_ns": 1},
        "memory": {"actions": 0},
        "actions": ["add"],
        "start_ns": 0,
        "max_decisions": 20,
    }
    return {**value, **changes}


def case(root, document):
    path = root / "population.json"
    path.write_text(dumps(document))
    return {
        "format": "seam-case",
        "version": 3,
        "name": "actors",
        "namespace": "sim-actors",
        "seed": 77,
        "clock": {"start_ns": 0, "epoch": "1970-01-01T00:00:00Z"},
        "initial_state": {"count": 0},
        "config": {},
        "datasets": {"actors": dataset_ref(path, 0)},
        "worlds": {"actors": {"dataset": "actors"}},
        "ports": {"actors": {"mode": "world", "world": "actors"}},
        "arrivals": [
            {"at_ns": 0, "handler": "actors_start", "body": {}},
            {"at_ns": 30, "handler": "actors_finish", "body": {}},
        ],
        "same_time_order": "timers_first",
        "stop": {"when": "quiescence"},
        "assertions": [],
    }


def run(root, value, label):
    path = root / f"{label}.json"
    path.write_text(dumps(value))
    return run_product(
        "seam.proof.actors",
        str(path),
        value["namespace"],
        str(root / f"{label}-out.json"),
        env={"PYTHONPATH": REPO},
    )


class SchemaTests(unittest.TestCase):
    def test_closed_profiles_and_population(self):
        invalid = [
            actor(id=True),
            actor(start_ns=True),
            actor(max_decisions=0),
            actor(profile=[]),
            actor(actions=["add", "add"]),
            actor(actions=[{}]),
            actor(policy="bad-name"),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(Fault):
                population([value])
        for values in ([], [actor(), actor()], [actor(memory=1.5)]):
            with self.assertRaises(Fault):
                population(values)
        with self.assertRaises(Fault):
            population([actor()], seed=True)
        extra = actor()
        extra["unknown"] = 1
        with self.assertRaises(Fault):
            population([extra])

    def test_actor_rng_is_separate_bounded_and_reconstructible(self):
        left, right = ActorRandom(7, "alice", 0), ActorRandom(7, "alice", 0)
        self.assertEqual(
            [left.rand_below(10) for _ in range(5)],
            [right.rand_below(10) for _ in range(5)],
        )
        restored = ActorRandom(7, "alice", left.counter)
        self.assertEqual(left.rand_u64(), restored.rand_u64())
        self.assertNotEqual(
            ActorRandom(7, "alice", 0).rand_u64(), ActorRandom(7, "bob", 0).rand_u64()
        )
        with self.assertRaises(Fault):
            restored.rand_below(True)
        limited = ActorRandom(0, "alice", 0)
        for _ in range(1024):
            limited.rand_u64()
        with self.assertRaises(Fault):
            limited.rand_u64()

    def test_registration_is_explicit_and_not_a_live_actor_service(self):
        system = ActorSystem(
            {"counter_v1": lambda request, rng: None},
            lambda *args: {},
            lambda *args: {},
        )
        rt = Runtime()
        system.install(rt)
        with self.assertRaises(Fault):
            system.install(rt)
        rt.start_live()
        with self.assertRaises(Fault) as raised:
            rt.deliver("actors_start", {})
        self.assertEqual(raised.exception.code, "actor_live_forbidden")
        rt.close()


class ActorProcessTests(unittest.TestCase):
    def test_actor_policy_guards_remain_sticky_even_when_the_product_catches_faults(
        self,
    ):
        from tests.test_worlds import custom_product, execute

        for expression, code in (
            ("__import__('time').time_ns()", "real_clock"),
            ("__import__('random').randrange(2)", "unseeded_random"),
            ("__import__('socket').socket()", "real_io"),
        ):
            with (
                self.subTest(expression=expression),
                tempfile.TemporaryDirectory() as directory,
            ):
                factory = (
                    "def factory(ctx, data, config):\n"
                    "    from seam.actors import ActorSystem, population\n"
                    "    def policy(request, rng):\n"
                    f"        {expression}\n"
                    "        return {'action': None, 'memory': {}, 'wake_after_ns': None}\n"
                    "    actor = {'id':'alice','policy':'test_v1','profile':{},'memory':{},"
                    "'actions':[],'start_ns':0,'max_decisions':2}\n"
                    "    system = ActorSystem({'test_v1':policy},lambda *a:{},lambda *a:{},name='p')\n"
                    "    inner = system._world(ctx,population([actor]),config)\n"
                    "    inner.handlers['p']({'op':'start'})\n"
                    "    def call(request):\n"
                    "        return inner.handlers['p']({'op':'decide','actor_id':'alice','step':0,'observation':{}})\n"
                    "    return World({'p':call},inner.snapshot,inner.restore)\n"
                )
                value, pythonpath = custom_product(directory, factory)
                result = execute(
                    directory, value, product="consumer", pythonpath=pythonpath
                )
                self.assertEqual(result.returncode, 2, result.artifact)
                self.assertEqual(result.artifact["fault"]["code"], code)
                self.assertNotIn("checkpoint", result.artifact)

    def test_policy_and_translation_failures_do_not_commit_memory(self):
        invalid = [
            {"action": None, "memory": {}, "wake_after_ns": True},
            {"action": None, "memory": {}, "wake_after_ns": -1},
            {"action": None, "memory": {}, "wake_after_ns": None, "extra": 1},
            {
                "action": {"type": "other", "body": {}},
                "memory": {},
                "wake_after_ns": None,
            },
            {"action": None, "memory": "x" * (MAX_STRING + 1), "wake_after_ns": None},
        ]
        for value in invalid:
            with self.subTest(decision=type(value["memory"]).__name__):
                system = ActorSystem(
                    {"counter_v1": lambda request, rng: value},
                    lambda *args: {},
                    lambda *args: {},
                )
                world = system._world(
                    SimpleNamespace(now=lambda: 0), population([actor()]), {}
                )
                world.handlers["actors"]({"op": "start"})
                before = world.snapshot()
                with self.assertRaises(Fault):
                    world.handlers["actors"](
                        {
                            "op": "decide",
                            "actor_id": "alice",
                            "step": 0,
                            "observation": {},
                        }
                    )
                self.assertEqual(world.snapshot(), before)

    def test_zero_delay_loop_is_bounded_and_product_rng_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = case(
                root,
                population(
                    [actor(profile={"goal": 100, "delay_ns": 0}, max_decisions=2)]
                ),
            )
            failed = run(root, value, "bounded")
            self.assertEqual(failed.returncode, 2, failed.artifact)
            self.assertEqual(failed.artifact["fault"]["code"], "actor_budget")
            value = case(root, population([actor()]))
            paused = run(root, {**value, "checkpoint_after": 3}, "paused")
            self.assertEqual(paused.returncode, 4, paused.artifact)
            self.assertEqual(paused.artifact["checkpoint"]["payload"]["rng_counter"], 0)
            self.assertGreater(
                paused.artifact["world_states"]["actors"]["actors"]["alice"][
                    "rng_counter"
                ],
                0,
            )

    def test_horizon_seals_only_eligible_work_and_preserves_future_actor_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = case(
                root, population([actor(profile={"goal": 8, "delay_ns": 100})])
            )
            value["stop"] = {
                "when": "deadline",
                "deadline_ns": 30,
                "allow_quiescence": True,
            }
            result = run(root, value, "horizon")
            self.assertEqual(result.returncode, 0, result.artifact)
            state = result.artifact["world_states"]["actors"]
            self.assertTrue(state["sealed"])
            self.assertEqual(state["actors"]["alice"]["next_ns"], 100)
            value = case(root, population([actor()]))
            value["arrivals"] = [
                {"at_ns": 0, "handler": "actors_start", "body": {}},
                {"at_ns": 0, "handler": "actors_finish", "body": {}},
            ]
            value.pop("same_time_order")
            result = run(root, value, "early-seal")
            self.assertEqual(result.artifact["fault"]["code"], "actor_pending")

    def test_replay_does_not_execute_policy_and_unknown_policy_is_rejected(self):
        def policy(request, rng):
            return {"action": None, "memory": request["memory"], "wake_after_ns": None}

        host = SimpleNamespace(now=lambda: 0)
        original = ActorSystem(
            {"counter_v1": policy}, lambda *args: {}, lambda *args: {}
        )
        world = original._world(host, population([actor()]), {})
        world.handlers["actors"]({"op": "start"})
        world.handlers["actors"](
            {"op": "decide", "actor_id": "alice", "step": 0, "observation": {}}
        )

        def forbidden(*args):
            self.fail("recorded policy executed a fresh decision")

        replay = ActorSystem(
            {"counter_v1": forbidden}, lambda *args: {}, lambda *args: {}
        )
        world = replay._world(
            host, population([actor()], tape=world.snapshot()["decisions"]), {}
        )
        world.handlers["actors"]({"op": "start"})
        world.handlers["actors"](
            {"op": "decide", "actor_id": "alice", "step": 0, "observation": {}}
        )
        world.handlers["actors"]({"op": "seal"})
        with self.assertRaises(Fault):
            original._world(host, population([actor(policy="unknown_v1")]), {})

    def test_overflow_and_async_callbacks_are_rejected(self):
        definition = actor(start_ns=INT64_MAX)

        def policy(request, rng):
            return {"action": None, "memory": {}, "wake_after_ns": 1}

        system = ActorSystem({"counter_v1": policy}, lambda *args: {}, lambda *args: {})
        world = system._world(
            SimpleNamespace(now=lambda: INT64_MAX), population([definition]), {}
        )
        world.handlers["actors"]({"op": "start"})
        with self.assertRaises(Fault):
            world.handlers["actors"](
                {"op": "decide", "actor_id": "alice", "step": 0, "observation": {}}
            )

        async def asynchronous(*args):
            return {}

        from seam import Refuse

        for policies, observe_fn, translate_fn in (
            ({"counter_v1": asynchronous}, lambda *args: {}, lambda *args: {}),
            ({"counter_v1": policy}, asynchronous, lambda *args: {}),
            ({"counter_v1": policy}, lambda *args: {}, asynchronous),
        ):
            with self.assertRaises(Refuse):
                ActorSystem(policies, observe_fn, translate_fn)

    def test_reactive_run_and_recorded_replay_are_offline_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = case(root, population([actor()], seed=9))
            first, second = run(root, base, "first"), run(root, base, "second")
            self.assertEqual(first.returncode, 0, (first.stderr, first.artifact))
            self.assertEqual(first.artifact, second.artifact)
            state = first.artifact["world_states"]["actors"]
            self.assertTrue(state["sealed"])
            self.assertGreaterEqual(first.artifact["final_state"]["count"], 8)
            self.assertIsNone(state["actors"]["alice"]["next_ns"])
            recorded = case(
                root, population([actor()], seed=9, tape=state["decisions"])
            )
            one, two = (
                run(root, recorded, "replay-one"),
                run(root, recorded, "replay-two"),
            )
            self.assertEqual(one.returncode, 0, (one.stderr, one.artifact))
            self.assertEqual(one.artifact, two.artifact)
            self.assertEqual(one.artifact["final_state"], first.artifact["final_state"])
            self.assertEqual(
                one.artifact["world_states"]["actors"]["decisions"], state["decisions"]
            )
            self.assertNotIn("actors", first.artifact["final_state"])

    def test_repeated_fresh_process_resume_preserves_all_history_and_actor_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = case(root, population([actor(), actor("bob")], seed=99))
            baseline = run(root, base, "baseline")
            self.assertEqual(baseline.returncode, 0, baseline.artifact)
            current = dict(base, checkpoint_after=2)
            for index in range(40):
                result = run(root, current, f"segment-{index}")
                if result.returncode != 4:
                    break
                checkpoint = root / f"checkpoint-{index}.json"
                write_checkpoint(checkpoint, result.artifact["checkpoint"])
                current = {
                    **base,
                    "checkpoint_after": 2,
                    "resume": checkpoint_ref(checkpoint),
                }
            self.assertEqual(result.returncode, 0, (result.stderr, result.artifact))
            for field in (
                "digest",
                "events",
                "port_calls",
                "timers",
                "final_state",
                "world_states",
            ):
                self.assertEqual(
                    result.artifact[field], baseline.artifact[field], field
                )

    def test_tape_mismatch_exhaustion_extra_entries_and_changed_delivery_fail_loudly(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = run(root, case(root, population([actor()])), "first")
            tape = first.artifact["world_states"]["actors"]["decisions"]
            for label, changed, code in (
                ("empty", [], "actor_tape_exhausted"),
                ("extra", tape + [tape[-1]], "actor_tape_unused"),
                ("missing", tape[:-1], "actor_tape_exhausted"),
            ):
                result = run(
                    root, case(root, population([actor()], tape=changed)), label
                )
                self.assertEqual(result.returncode, 2, result.artifact)
                self.assertEqual(result.artifact["fault"]["code"], code)
            changed = copy.deepcopy(tape)
            changed[0]["request"]["observation"]["count"] += 1
            result = run(
                root, case(root, population([actor()], tape=changed)), "mismatch"
            )
            self.assertEqual(result.artifact["fault"]["code"], "actor_tape_mismatch")
            changed = copy.deepcopy(tape)
            changed[0]["delivery"]["body"]["amount"] += 1
            result = run(
                root, case(root, population([actor()], tape=changed)), "delivery"
            )
            self.assertEqual(result.artifact["fault"]["code"], "actor_tape_mismatch")

            for label, people, seed in (
                ("seed", [actor()], 1),
                ("capabilities", [actor(actions=["add", "other"])], 0),
            ):
                result = run(
                    root, case(root, population(people, seed=seed, tape=tape)), label
                )
                self.assertEqual(
                    result.artifact["fault"]["code"], "actor_tape_mismatch"
                )

    def test_budget_and_capabilities_and_stale_wakeups_fault(self):
        for label, definition, code in (
            ("budget", actor(max_decisions=1), "actor_budget"),
            ("forbidden", actor(actions=[]), "actor_action_forbidden"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = run(root, case(root, population([definition])), label)
                self.assertEqual(result.returncode, 2, result.artifact)
                self.assertEqual(result.artifact["fault"]["code"], code)
                self.assertNotIn("checkpoint", result.artifact)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = case(root, population([actor()]))
            value["arrivals"].insert(
                1,
                {
                    "at_ns": 1,
                    "handler": "actors_step",
                    "body": {"actor_id": "alice", "step": 0},
                },
            )
            result = run(root, value, "stale")
            self.assertEqual(result.artifact["fault"]["code"], "actor_stale")

    def test_projection_and_policy_receive_only_copies(self):
        def policy(request, rng):
            request["profile"]["secret"] = "changed"
            request["observation"]["count"] = 99
            request["memory"]["actions"] = 1
            return {"action": None, "memory": request["memory"], "wake_after_ns": None}

        system = ActorSystem({"counter_v1": policy}, lambda *args: {}, lambda *args: {})
        host = SimpleNamespace(now=lambda: 0)
        world = system._world(host, population([actor()]), {})
        world.handlers["actors"]({"op": "start"})
        observation = {"count": 0}
        world.handlers["actors"](
            {"op": "decide", "actor_id": "alice", "step": 0, "observation": observation}
        )
        record = world.snapshot()["decisions"][0]
        self.assertEqual(observation, {"count": 0})
        self.assertNotIn("secret", record["request"]["profile"])
        self.assertEqual(record["request"]["memory"], {"actions": 0})

    def test_corrupt_actor_snapshot_cannot_restore(self):
        system = ActorSystem(
            {"counter_v1": lambda *args: None}, lambda *args: {}, lambda *args: {}
        )
        world = system._world(SimpleNamespace(now=lambda: 0), population([actor()]), {})
        snapshot = world.snapshot()
        for key, value in (
            ("step", 1),
            ("memory", {}),
            ("rng_counter", True),
            ("next_ns", None),
        ):
            changed = copy.deepcopy(snapshot)
            changed["actors"]["alice"][key] = value
            with self.subTest(key=key), self.assertRaises(Fault):
                world.restore(changed)
