"""Goal contracts are strict, detached, and independent of any provider or product."""

import unittest

from seam.canon import loads
from seam.personas import choice_prompt, goal_prompt, persona, validate_persona


class PersonaTests(unittest.TestCase):
    def test_choice_prompt_is_explicit_and_keeps_legacy_prompt_unchanged(self):
        prompt = choice_prompt("My product owns transitions and timing.")
        self.assertEqual(loads(prompt)["version"], 4)
        self.assertIn("request.choices", prompt)
        self.assertEqual(loads(goal_prompt("Legacy contract."))["version"], 3)
        with self.assertRaises(ValueError):
            choice_prompt("")

    def test_round_trip_and_detachment(self):
        original = persona(
            "patient", ["Obtain one item"], constraints=["Retry at most twice"]
        )
        copied = validate_persona(original)
        copied["goals"].append("changed")
        self.assertEqual(original["goals"], ["Obtain one item"])
        self.assertEqual(original["provenance"], "synthetic")

    def test_invalid_contracts_are_refused(self):
        good = persona("patient", ["Obtain one item"])
        for key, bad in (
            ("version", True),
            ("version", 2),
            ("provenance", "observed"),
            ("name", ""),
            ("name", "x\n"),
            ("name", "x" * 129),
            ("goals", []),
            ("goals", ["same", "same"]),
            ("goals", [False]),
            ("goals", ["x"] * 17),
            ("constraints", None),
            ("constraints", ["x" * 1025]),
        ):
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                validate_persona({**good, key: bad})
        with self.assertRaises(ValueError):
            validate_persona({**good, "api_key": "not allowed"})

    def test_goal_prompt_is_stable_and_contains_no_actor_or_expected_decision(self):
        left = goal_prompt(
            "Add increases the inventory count; only owned items are visible."
        )
        self.assertEqual(left, goal_prompt(loads(left)["product_contract"]))
        self.assertEqual(loads(left)["version"], 3)
        self.assertNotIn("api_key", left)
        self.assertNotIn("expected_decision", left)
        self.assertIn("capabilities", left)
        self.assertIn("profile.persona", left)
        for invalid in ("", "line\nbreak", False, "x" * 12001):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                goal_prompt(invalid)

    def test_strings_are_not_silently_split_into_goals(self):
        with self.assertRaises(ValueError):
            persona("patient", "buy")
        with self.assertRaises(ValueError):
            persona("patient", ["buy"], constraints="wait")

    def test_persona_and_goal_policy_survive_real_executor_recovery_and_replay(self):
        import tempfile
        from pathlib import Path

        from seam.actors import inference_population
        from seam.canon import dumps
        from seam.inference import InferenceRun, settings
        from tests.support import REPO
        from tests.test_actors import actor, case
        from tests.test_inference import response

        declared = persona("buyer", ["Obtain two items"])
        prompt = goal_prompt(
            "add increases count; memory.actions counts issued actions."
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {"goal": 2, "delay_ns": 1, "persona": declared}
            document = case(root, inference_population([actor(profile=profile)]))
            path = root / "case.json"
            path.write_text(dumps(document))
            config = settings("fake", "counter-v1", prompt)
            with InferenceRun.create(
                root / "run",
                "seam.proof.actors",
                path,
                config=config,
                env={"PYTHONPATH": REPO},
            ) as session:
                self.assertEqual(
                    session.next_request()["request"]["profile"]["persona"], declared
                )
                session.invoke(response)
            with InferenceRun(root / "run") as resumed:
                result = resumed.run(response)
                self.assertEqual(result.artifact["final_state"]["count"], 2)
                self.assertEqual(resumed.manifest["config"]["prompt"], prompt)
                replay = resumed.replay()
                self.assertEqual(
                    replay.artifact["final_state"], result.artifact["final_state"]
                )
