"""Model intentions cannot override product-owned capabilities, state, or virtual time."""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from seam import Fault, Refuse, checkpoint_ref, run_product, write_checkpoint
from seam.actors import ActorSystem, choice, inference_population, population
from seam.canon import INT64_MAX, dumps
from seam.inference import InferenceError, InferenceRun, settings
from seam.personas import choice_prompt
from seam.proof.choices import options, policy
from tests.support import REPO
from tests.test_actors import actor, case


def definition(**changes):
    return actor(policy="choice_v1", profile={"goal": 2, "ready_ns": 5}, **changes)


def response(envelope):
    return {
        "request_id": envelope["request_id"],
        "model": envelope["model"],
        "returned_model": envelope["model"],
        "decision": {"choice": envelope["request"]["choices"][0]["id"]},
        "usage": {"input_tokens": 1, "output_tokens": 1, "cost_microusd": 0},
    }


def execute(root, document, label):
    path = root / f"{label}.json"
    path.write_text(dumps(document))
    return run_product(
        "seam.proof.choices",
        str(path),
        "sim-actors",
        str(root / f"{label}-artifact.json"),
        env={"PYTHONPATH": REPO},
    )


class ChoiceTests(unittest.TestCase):
    def world(self, selection, *, chooser=options, definitions=None, mode="rules"):
        self.translations = []

        def translate(actor, action, token):
            self.translations.append(action)
            return {"handler": "add", "body": action["body"]}

        system = ActorSystem(
            {"choice_v1": lambda request, rng: selection},
            lambda *args: {},
            translate,
            choices={"choice_v1": chooser},
        )
        people = [definition()] if definitions is None else definitions
        spec = (
            inference_population(people) if mode == "inference" else population(people)
        )
        world = system._world(SimpleNamespace(now=lambda: 0), spec, {})
        world.handlers["actors"]({"op": "start"})
        return world

    def decide(self, world):
        return world.handlers["actors"](
            {
                "op": "decide",
                "actor_id": "alice",
                "step": 0,
                "observation": {"count": 0},
            }
        )

    def test_early_forged_choices_and_low_level_overrides_have_no_effect(self):
        for decision in (
            {"choice": "add"},
            {"choice": "finish"},
            {"choice": "wait", "wake_after_ns": 0},
            {
                "action": {"type": "add", "body": {"amount": 999}},
                "memory": {},
                "wake_after_ns": 0,
            },
            {"action": None, "memory": {}, "wake_after_ns": None},
        ):
            with self.subTest(decision=decision):
                world = self.world(decision)
                before = world.snapshot()
                with self.assertRaises(Fault):
                    self.decide(world)
                self.assertEqual(world.snapshot(), before)
                self.assertEqual(self.translations, [])

    def test_menu_validation_rejects_invalid_progress_and_unauthorized_actions(self):
        invalid = [
            [],
            [
                choice(
                    "x", "Finish", {"action": None, "memory": {}, "wake_after_ns": None}
                )
            ]
            * 2,
            [
                {
                    "id": "x",
                    "description": "Zero wait",
                    "decision": {"action": None, "memory": {}, "wake_after_ns": 0},
                }
            ],
            [
                {
                    "id": "x",
                    "description": "No follow-up",
                    "decision": {
                        "action": {"type": "add", "body": {}},
                        "memory": {},
                        "wake_after_ns": None,
                    },
                }
            ],
            [
                choice(
                    "x",
                    "Forbidden",
                    {
                        "action": {"type": "other", "body": {}},
                        "memory": {},
                        "wake_after_ns": 0,
                    },
                )
            ],
        ]
        for menu in invalid:
            with self.subTest(menu=menu), self.assertRaises(Fault):
                self.decide(
                    self.world({"choice": "x"}, chooser=lambda request, menu=menu: menu)
                )
        with self.assertRaises(Fault):
            choice(
                "x",
                "line\nbreak",
                {"action": None, "memory": {}, "wake_after_ns": None},
            )
        with self.assertRaises(Fault):
            ActorSystem(
                {"choice_v1": policy},
                lambda *a: {},
                lambda *a: {},
                choices={"unknown": options},
            )

    def test_checkpoint_menu_resolution_and_memory_tampering_are_rejected(self):
        world = self.world({"choice": "wait"})
        self.decide(world)
        original = world.snapshot()
        world.restore(original)
        for field in ("choices", "resolution", "memory"):
            snapshot = copy.deepcopy(original)
            if field == "choices":
                snapshot["decisions"][0]["request"]["choices"][0]["wake_after_ns"] = 1
            elif field == "resolution":
                snapshot["decisions"][0]["resolution"]["wake_after_ns"] = 1
            else:
                snapshot["actors"]["alice"]["memory"] = {"actions": 100}
            with self.subTest(field=field), self.assertRaises(Fault):
                world.restore(snapshot)
            self.assertEqual(world.snapshot(), original)

    def test_choice_registration_and_time_overflow_are_refused(self):
        async def asynchronous(request):
            return []

        with self.assertRaises(Refuse):
            ActorSystem(
                {"choice_v1": policy},
                lambda *a: {},
                lambda *a: {},
                choices={"choice_v1": asynchronous},
            )
        system = ActorSystem(
            {"choice_v1": policy},
            lambda *a: {},
            lambda *a: {},
            choices={
                "choice_v1": lambda request: [
                    choice(
                        "wait",
                        "Wait",
                        {"action": None, "memory": {}, "wake_after_ns": 1},
                    )
                ]
            },
        )
        world = system._world(
            SimpleNamespace(now=lambda: INT64_MAX),
            population([definition(start_ns=INT64_MAX)]),
            {},
        )
        world.handlers["actors"]({"op": "start"})
        with self.assertRaises(Fault):
            self.decide(world)

    def test_checkpoint_forged_delivery_is_rejected(self):
        person = definition()
        person["profile"]["ready_ns"] = 0
        world = self.world({"choice": "add"}, definitions=[person])
        self.decide(world)
        original = world.snapshot()
        tampered = copy.deepcopy(original)
        tampered["decisions"][0]["delivery"]["body"]["amount"] = 999
        with self.assertRaises(Fault):
            world.restore(tampered)
        self.assertEqual(world.snapshot(), original)

    def test_horizon_preserves_future_code_owned_wake_without_delivering_early(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            person = definition()
            person["profile"]["ready_ns"] = 100
            document = case(root, population([person]))
            document["stop"] = {
                "when": "deadline",
                "deadline_ns": 30,
                "allow_quiescence": True,
            }
            result = execute(root, document, "horizon")
            self.assertEqual(result.returncode, 0, result.artifact)
            self.assertEqual(result.artifact["final_state"]["count"], 0)
            state = result.artifact["world_states"]["actors"]
            self.assertTrue(state["sealed"])
            self.assertEqual(state["actors"]["alice"]["next_ns"], 100)

    def test_actual_executor_gates_time_and_recovers_pending_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = case(root, population([definition()]))
            baseline = execute(root, document, "baseline")
            self.assertEqual(baseline.returncode, 0, baseline.artifact)
            additions = [
                row
                for row in baseline.artifact["events"]
                if row["handler"] == "add" and row["kind"] == "deliver"
            ]
            self.assertEqual([row["at_ns"] for row in additions], [5, 5])
            paused = execute(root, {**document, "checkpoint_after": 2}, "paused")
            self.assertEqual(paused.returncode, 4, paused.artifact)
            write_checkpoint(root / "checkpoint.json", paused.artifact["checkpoint"])
            resumed = execute(
                root,
                {**document, "resume": checkpoint_ref(root / "checkpoint.json")},
                "resumed",
            )
            self.assertEqual(resumed.returncode, 0, resumed.artifact)
            self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])
            records = baseline.artifact["world_states"]["actors"]["decisions"]
            replay = execute(
                root, case(root, population([definition()], tape=records)), "replay"
            )
            self.assertEqual(
                replay.artifact["final_state"], baseline.artifact["final_state"]
            )

    def test_real_inference_rejects_choice_before_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = case(root, inference_population([definition()]))
            path = root / "case.json"
            path.write_text(dumps(document))
            with InferenceRun.create(
                root / "run",
                "seam.proof.choices",
                path,
                config=settings(
                    "fake", "fake", choice_prompt("Choices are product-owned.")
                ),
                env={"PYTHONPATH": REPO},
            ) as session:
                request = session.next_request()
                self.assertEqual(
                    [entry["id"] for entry in request["request"]["choices"]], ["wait"]
                )
                bad = response(request)
                bad["decision"] = {"choice": "add"}
                with self.assertRaises(InferenceError):
                    session.accept(bad)
                row = session.db.execute("SELECT state,response FROM steps").fetchone()
                self.assertEqual(row[0], "rejected")
                self.assertIsNone(row[1])

    def test_inference_resume_and_two_byte_identical_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "case.json"
            path.write_text(dumps(case(root, inference_population([definition()]))))
            with InferenceRun.create(
                root / "run",
                "seam.proof.choices",
                path,
                config=settings(
                    "fake", "fake", choice_prompt("Choose toward the inventory goal.")
                ),
                env={"PYTHONPATH": REPO},
            ) as session:
                session.invoke(response)
            with InferenceRun(root / "run") as session:
                result = session.run(response)
                self.assertEqual(result.artifact["final_state"]["count"], 2)
                first = session.replay()
                second = session.replay()
                self.assertEqual(
                    Path(first.artifact_path).read_bytes(),
                    Path(second.artifact_path).read_bytes(),
                )

    def test_valid_deferral_is_a_real_choice_not_a_forced_rule_decision(self):
        def provider(envelope):
            result = response(envelope)
            if envelope["request"]["step"] == 1:
                self.assertEqual(
                    {item["id"] for item in envelope["request"]["choices"]},
                    {"add", "wait"},
                )
                result["decision"] = {"choice": "wait"}
            return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "case.json"
            path.write_text(dumps(case(root, inference_population([definition()]))))
            with InferenceRun.create(
                root / "run",
                "seam.proof.choices",
                path,
                config=settings(
                    "fake", "fake", choice_prompt("Choose toward the inventory goal.")
                ),
                env={"PYTHONPATH": REPO},
            ) as session:
                result = session.run(provider)
                records = result.artifact["world_states"]["actors"]["decisions"]
                self.assertEqual(records[1]["decision"], {"choice": "wait"})
                self.assertEqual(records[2]["request"]["at_ns"], 7)
                self.assertEqual(result.artifact["final_state"]["count"], 2)
