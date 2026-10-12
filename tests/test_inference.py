"""Execute external decision barriers, journal recovery, budgets, and provider-free strict replay."""

import copy
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from seam import Fault, checkpoint_ref, write_checkpoint
from seam.actors import inference_population
from seam.canon import dumps, loads
from seam.inference import InferenceError, InferenceRun, settings
from seam.runner import ProcessResult
from tests.support import REPO
from tests.test_actors import actor, case, run


def config(**changes):
    return settings(
        "fake", "counter-v1", "Return one authorized counter decision.", **changes
    )


def response(envelope, **changes):
    request = envelope["request"]
    finished = request["observation"]["count"] >= request["profile"]["goal"]
    value = {
        "request_id": envelope["request_id"],
        "model": envelope["model"],
        "returned_model": envelope["model"],
        "decision": {
            "action": None if finished else {"type": "add", "body": {"amount": 1}},
            "memory": {
                "actions": request["memory"]["actions"] + (0 if finished else 1)
            },
            "wake_after_ns": None if finished else request["profile"]["delay_ns"],
        },
        "usage": {"input_tokens": 10, "output_tokens": 10, "cost_microusd": 0},
    }
    return {**value, **changes}


def slow_provider(envelope):
    time.sleep(2)
    return response(envelope)


def broken_provider(envelope):
    raise RuntimeError("this diagnostic must not become recorded provider data")


def new_run(root, **changes):
    value = inference_population([actor(profile={"goal": 2, "delay_ns": 1})])
    document = case(root, value)
    path = root / "case.json"
    path.write_text(dumps(document))
    return InferenceRun.create(
        root / "session",
        "seam.proof.actors",
        path,
        config=config(**changes),
        env={"PYTHONPATH": REPO},
    )


class InferenceTests(unittest.TestCase):
    def test_shared_actor_dataset_does_not_authorize_two_inference_worlds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root):
                pass
            path = root / "case.json"
            value = loads(path.read_bytes())
            value["worlds"]["other"] = {"dataset": value["worlds"]["actors"]["dataset"]}
            value["ports"]["other"] = {"mode": "world", "world": "other"}
            path.write_text(dumps(value))
            with self.assertRaisesRegex(InferenceError, "one_inference_world_required"):
                InferenceRun.create(
                    root / "alias",
                    "seam.proof.actors",
                    path,
                    config=config(),
                    env={"PYTHONPATH": REPO},
                )
            self.assertFalse((root / "alias").exists())

    def test_inference_prefix_schema_and_changed_observation_are_strict(self):
        for record in (
            {},
            {"request": {}, "decision": {}},
            {"request": True, "decision": {}},
        ):
            with self.subTest(record=record), self.assertRaises(Fault):
                inference_population([actor()], responses=[record])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root) as session:
                request = session.next_request()
                prefix = [
                    {
                        "request": request["request"],
                        "decision": response(request)["decision"],
                    }
                ]
            document = case(
                root,
                inference_population(
                    [actor(profile={"goal": 2, "delay_ns": 1})], responses=prefix
                ),
            )
            document["initial_state"]["count"] = 1
            result = run(root, document, "changed")
            self.assertEqual(result.returncode, 2, result.artifact)
            self.assertEqual(result.artifact["fault"]["code"], "actor_tape_mismatch")
            self.assertEqual(result.artifact["world_states"]["actors"]["decisions"], [])

    def test_complete_inference_prefix_retains_ordinary_checkpoint_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root) as session:
                complete = session.run(response)
            records = complete.artifact["world_states"]["actors"]["decisions"]
            prefix = [
                {"request": row["request"], "decision": row["decision"]}
                for row in records
            ]
            document = case(
                root,
                inference_population(
                    [actor(profile={"goal": 2, "delay_ns": 1})], responses=prefix
                ),
            )
            baseline = run(root, document, "baseline")
            paused = run(root, {**document, "checkpoint_after": 3}, "paused")
            self.assertEqual(paused.returncode, 4, paused.artifact)
            write_checkpoint(root / "checkpoint.json", paused.artifact["checkpoint"])
            resumed = run(
                root,
                {**document, "resume": checkpoint_ref(root / "checkpoint.json")},
                "resumed",
            )
            self.assertEqual(resumed.returncode, 0, resumed.artifact)
            self.assertEqual(resumed.artifact["digest"], baseline.artifact["digest"])

    def test_provider_observations_do_not_expose_unprojected_product_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root):
                pass
            path = root / "case.json"
            document = loads(path.read_text())
            document["initial_state"]["private_record"] = "not-for-actors"
            path.write_text(dumps(document))
            with InferenceRun.create(
                root / "private-session",
                "seam.proof.actors",
                path,
                config=config(),
                env={"PYTHONPATH": REPO},
            ) as session:
                request = session.next_request()
                self.assertNotIn("not-for-actors", dumps(request))
                self.assertEqual(request["request"]["observation"], {"count": 0})

    def test_changed_journal_request_and_symlinked_run_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root) as session:
                request = session.next_request()
                altered = copy.deepcopy(request)
                altered["request"]["observation"]["count"] = 99
                with session.db:
                    session.db.execute("UPDATE steps SET request=?", (dumps(altered),))
                with self.assertRaisesRegex(InferenceError, "^journal_changed$"):
                    session.accept(response(request))
            (root / "link").symlink_to(root / "session", target_is_directory=True)
            with self.assertRaisesRegex(InferenceError, "^insecure_run_directory$"):
                InferenceRun(root / "link")

    def test_external_barrier_is_not_a_pass_or_an_ordinary_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = case(root, inference_population([actor()]))
            result = run(root, document, "barrier")
            self.assertEqual(result.returncode, 2, result.artifact)
            art = result.artifact
            self.assertEqual(art["status"], "failed")
            self.assertEqual(art["fault"]["code"], "actor_inference_required")
            self.assertNotIn("checkpoint", art)
            self.assertEqual(art["final_state"], {"count": 0})
            self.assertEqual(art["world_states"]["actors"]["decisions"], [])
            request = loads(art["fault"]["op"])
            self.assertEqual(request["observation"], {"count": 0})
            self.assertEqual(request["memory"], {"actions": 0})

    def test_fake_provider_drives_real_handlers_and_replays_without_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            with new_run(Path(directory)) as session:
                result = session.run(response)
                self.assertEqual(result.returncode, 0, result.artifact)
                self.assertEqual(result.artifact["final_state"], {"count": 2})
                records = result.artifact["world_states"]["actors"]["decisions"]
                self.assertEqual(len(records), 3)
                self.assertEqual(
                    [row["request"]["observation"]["count"] for row in records],
                    [0, 1, 2],
                )
                self.assertEqual(len(session._steps()), 3)
                left, right = session.replay(), session.replay()
                self.assertEqual(
                    Path(left.artifact_path).read_bytes(),
                    Path(right.artifact_path).read_bytes(),
                )
                self.assertEqual(
                    left.artifact["world_states"]["actors"]["decisions"], records
                )
                self.assertEqual(
                    left.artifact["final_state"], result.artifact["final_state"]
                )
                self.assertIsInstance(session.run(broken_provider), ProcessResult)
                self.assertEqual(len(session._steps()), 3)

    def test_pending_and_accepted_decisions_recover_in_a_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root) as session:
                pending = session.next_request()
                frozen = copy.deepcopy(pending)
                pending["request"]["memory"]["actions"] = 900
                self.assertEqual(session.next_request(), frozen)
                self.assertEqual(len(session._steps()), 1)
            script = (
                "import os; from seam.inference import InferenceRun; "
                "from tests.test_inference import response; "
                f"s=InferenceRun({str(root / 'session')!r}); "
                "s.accept(response(s.next_request())); os._exit(9)"
            )
            child = subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONPATH": REPO},
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(child.returncode, 9, child.stderr)
            with InferenceRun(root / "session") as session:
                result = session.run(response)
                self.assertEqual(result.artifact["final_state"], {"count": 2})
                self.assertEqual(len(session._steps()), 3)
                self.assertEqual(
                    [
                        row["request"]["step"]
                        for row in result.artifact["world_states"]["actors"][
                            "decisions"
                        ]
                    ],
                    [0, 1, 2],
                )

    def test_crash_after_dispatch_is_uncertain_and_never_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root) as session:
                request = session.next_request()
            script = (
                "import os; from seam.inference import InferenceRun; "
                f"s=InferenceRun({str(root / 'session')!r}); "
                "s.db.execute(\"UPDATE steps SET state='in_flight'\"); s.db.commit(); os._exit(7)"
            )
            child = subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONPATH": REPO},
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(child.returncode, 7, child.stderr)
            with InferenceRun(root / "session") as session:
                with self.assertRaisesRegex(InferenceError, "^inference_uncertain$"):
                    session.run(broken_provider)
                session.accept(response(request))
                self.assertEqual(
                    session.run(response).artifact["final_state"], {"count": 2}
                )

    def test_timeout_and_provider_error_preserve_uncertainty(self):
        for provider in (slow_provider, broken_provider):
            with (
                self.subTest(provider=provider.__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                with new_run(root, timeout_ms=50) as session:
                    with self.assertRaises(InferenceError):
                        session.invoke(provider)
                    self.assertEqual(session._steps()[0][2], "in_flight")
                    self.assertIsNone(session._steps()[0][3])
                with InferenceRun(root / "session") as session:
                    with self.assertRaisesRegex(
                        InferenceError, "^inference_uncertain$"
                    ):
                        session.invoke(response)

    def test_invalid_stale_over_budget_and_unauthorized_responses_are_durable_failures(
        self,
    ):
        cases = [
            {"request_id": "stale"},
            {"model": "unapproved-model"},
            {
                "decision": {
                    "action": {"type": "delete", "body": {}},
                    "memory": {},
                    "wake_after_ns": None,
                }
            },
            {"decision": {"action": None, "memory": {}, "wake_after_ns": True}},
            {"usage": {"input_tokens": 10, "output_tokens": 2000, "cost_microusd": 0}},
            {"usage": {"input_tokens": True, "output_tokens": 10, "cost_microusd": 0}},
            {"extra": "not allowed"},
        ]
        for changes in cases:
            with (
                self.subTest(changes=changes),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                with new_run(root) as session:
                    request = session.next_request()
                    with self.assertRaises(InferenceError):
                        session.accept(response(request, **changes))
                    self.assertEqual(session._steps()[0][2], "rejected")
                with InferenceRun(root / "session") as session:
                    with self.assertRaises(InferenceError):
                        session.next_request()

    def test_duplicate_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with new_run(Path(directory)) as session:
                value = response(session.next_request())
                session.accept(value)
                with self.assertRaisesRegex(
                    InferenceError, "^inference_duplicate_response$"
                ):
                    session.accept(value)
                self.assertEqual(len(session._steps()), 1)

    def test_cumulative_cost_and_call_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            with new_run(Path(directory), max_calls=1) as session:
                session.invoke(response)
                with self.assertRaisesRegex(InferenceError, "^inference_call_budget$"):
                    session.next_request()
        with tempfile.TemporaryDirectory() as directory:
            with new_run(Path(directory), max_cost_microusd=3) as session:
                request = session.next_request()
                session.accept(
                    response(
                        request,
                        usage={
                            "input_tokens": 10,
                            "output_tokens": 10,
                            "cost_microusd": 2,
                        },
                    )
                )
                request = session.next_request()
                with self.assertRaisesRegex(InferenceError, "^inference_cost_budget$"):
                    session.accept(
                        response(
                            request,
                            usage={
                                "input_tokens": 10,
                                "output_tokens": 10,
                                "cost_microusd": 2,
                            },
                        )
                    )

    def test_inputs_are_frozen_and_one_writer_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root) as session:
                with self.assertRaisesRegex(InferenceError, "^inference_writer_busy$"):
                    InferenceRun(root / "session")
                (root / "population.json").write_text("{}")
                (root / "case.json").write_text("{}")
                self.assertEqual(
                    session.run(response).artifact["final_state"], {"count": 2}
                )
                self.assertEqual((root / "session").stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    (root / "session" / "journal.sqlite").stat().st_mode & 0o777, 0o600
                )

    def test_environment_and_manifest_changes_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with new_run(root) as session:
                original = copy.deepcopy(session.manifest)
                session.manifest["config"]["model"] = "mutated-in-memory"
                with self.assertRaisesRegex(InferenceError, "^manifest_changed$"):
                    session.next_request()
                session.manifest = original
                with patch.dict(os.environ, {"PATH": "/different/path"}):
                    with self.assertRaisesRegex(
                        InferenceError, "^inference_identity_changed$"
                    ):
                        session.next_request()
            connection = sqlite3.connect(root / "session" / "journal.sqlite")
            with connection:
                raw = connection.execute(
                    "SELECT value FROM meta WHERE key='manifest'"
                ).fetchone()[0]
                value = loads(raw)
                value["config"]["model"] = "changed"
                connection.execute(
                    "UPDATE meta SET value=? WHERE key='manifest'", (dumps(value),)
                )
            connection.close()
            with self.assertRaisesRegex(InferenceError, "^manifest_changed$"):
                InferenceRun(root / "session")

    def test_configuration_rejects_secrets_and_unknown_fields(self):
        with self.assertRaises(InferenceError):
            config(timeout_ms=True)
        with self.assertRaises(InferenceError):
            config(temperature_milli=3000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = case(root, inference_population([actor()]))
            path = root / "case.json"
            path.write_text(dumps(value))
            with self.assertRaisesRegex(InferenceError, "^bad_environment$"):
                InferenceRun.create(
                    root / "session",
                    "seam.proof.actors",
                    path,
                    config=config(),
                    env={"API_KEY": "never export this"},
                )
            value["resume"] = {}
            path.write_text(dumps(value))
            with self.assertRaisesRegex(
                InferenceError, "^inference_requires_fresh_v3$"
            ):
                InferenceRun.create(
                    root / "session", "seam.proof.actors", path, config=config()
                )

    def test_response_is_data_not_python_and_product_translation_still_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            with new_run(Path(directory)) as session:
                request = session.next_request()
                session.accept(
                    response(
                        request,
                        decision={
                            "action": {
                                "type": "add",
                                "body": {
                                    "amount": "arbitrary Python is never evaluated"
                                },
                            },
                            "memory": {},
                            "wake_after_ns": None,
                        },
                    )
                )
                with self.assertRaisesRegex(
                    InferenceError, "^inference_executor_failed$"
                ):
                    session.next_request()
