"""Provider-neutral dispatch, accounting, capability refusal, and normalized completion parsing."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from seam.canon import dumps, loads
from seam.inference import InferenceError
from seam.providers import ChatProvider, litellm_provider
from tests.test_inference import new_run


def completion(model="native-snapshot", **changes):
    value = {
        "model": model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": dumps(
                        {
                            "action": None,
                            "memory": {},
                            "wake_after_ns": None,
                        }
                    )
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }
    return {**value, **changes}


def envelope(model="provider/logical-model"):
    return {
        "request_id": "pinned-request",
        "model": model,
        "returned_models": ["native-snapshot"],
        "temperature_milli": None,
        "prompt": "JSON actor decision context",
        "request": {},
        "limits": {
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "max_cost_microusd": 0,
            "timeout_ms": 1000,
        },
    }


def bridge(transport, **hooks):
    return ChatProvider(
        transport,
        count_tokens=hooks.get("count_tokens", lambda *args: 10),
        quote_microusd=hooks.get("quote_microusd", lambda *args: 0),
        cost_microusd=hooks.get("cost_microusd", lambda *args: 0),
    )


def fake_counter_completion(**kwargs):
    request = loads(kwargs["messages"][0]["content"].rsplit("\n", 1)[-1])
    finished = request["observation"]["count"] >= request["profile"]["goal"]
    decision = {
        "action": None if finished else {"type": "add", "body": {"amount": 1}},
        "memory": {"actions": request["memory"]["actions"] + (0 if finished else 1)},
        "wake_after_ns": None if finished else 1,
    }
    return completion(
        model=kwargs["model"],
        choices=[
            {
                "finish_reason": "stop",
                "message": {"content": dumps(decision)},
            }
        ],
    )


class ProviderTests(unittest.TestCase):
    def test_choice_policy_uses_small_provider_neutral_output_contract(self):
        value = envelope()
        value["request"] = {"choices": [{"id": "wait"}]}
        transport = Mock(
            return_value=completion(
                choices=[
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"choice":"wait"}'},
                    }
                ]
            )
        )
        result = bridge(transport)(value)
        self.assertEqual(result["decision"], {"choice": "wait"})
        self.assertIn(
            'Return only {"choice":',
            transport.call_args.kwargs["messages"][0]["content"],
        )

    def test_provider_routes_are_opaque_not_hardcoded_model_lists(self):
        for model in (
            "openai/model",
            "anthropic/model",
            "gemini/model",
            "azure/model",
            "vertex_ai/model",
            "bedrock/model",
            "ollama/model",
            "custom/model",
        ):
            with self.subTest(model=model):
                transport = Mock(return_value=completion())
                result = bridge(transport)(envelope(model))
                self.assertEqual(result["model"], model)
                self.assertEqual(result["returned_model"], "native-snapshot")
                arguments = transport.call_args.kwargs
                self.assertEqual(arguments["model"], model)
                self.assertEqual(arguments["num_retries"], 0)
                self.assertEqual(arguments["fallbacks"], [])
                self.assertFalse(arguments["drop_params"])
                self.assertFalse(arguments["stream"])
                self.assertNotIn("temperature", arguments)

    def test_budget_quotes_and_accounting_fail_before_dispatch(self):
        for hooks in (
            {"count_tokens": lambda *args: 101},
            {"count_tokens": lambda *args: True},
            {"quote_microusd": lambda *args: 1},
            {"quote_microusd": lambda *args: -1},
        ):
            with self.subTest(hooks=hooks):
                transport = Mock()
                with self.assertRaises(InferenceError):
                    bridge(transport, **hooks)(envelope())
                transport.assert_not_called()

    def test_refusal_truncation_tools_changed_models_and_missing_usage_do_not_fall_back(
        self,
    ):
        invalid = [
            completion(model="unapproved-model"),
            completion(usage={}),
            completion(usage={"prompt_tokens": True, "completion_tokens": 1}),
            completion(usage={"prompt_tokens": 10, "completion_tokens": 101}),
            completion(choices=[]),
        ]
        for changes in (
            {"finish_reason": "length"},
            {"finish_reason": "tool_calls"},
            {"message": {"content": "{}", "refusal": "declined"}},
            {"message": {"content": "{}", "tool_calls": [{"name": "unsafe"}]}},
            {"message": {"content": "not JSON"}},
            {"message": {"content": '{"memory":0,"memory":1}'}},
            {"message": {"content": None}},
        ):
            value = completion()
            value["choices"][0].update(changes)
            invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                transport = Mock(return_value=value)
                with self.assertRaises(InferenceError):
                    bridge(transport)(envelope())
                self.assertEqual(transport.call_count, 1)

    def test_native_model_objects_and_explicit_temperature_are_supported(self):
        class Native:
            def model_dump(self):
                return completion()

        transport = Mock(return_value=Native())
        request = envelope()
        request["temperature_milli"] = 250
        result = bridge(transport)(request)
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(transport.call_args.kwargs["temperature"], 0.25)
        self.assertEqual(result["returned_model"], "native-snapshot")

    def test_actual_cost_is_not_silently_treated_as_free(self):
        with self.assertRaises(InferenceError):
            bridge(lambda **kwargs: completion(), cost_microusd=lambda *args: 1)(
                envelope()
            )

    def test_bridge_drives_the_executor_and_journals_returned_model(self):
        with tempfile.TemporaryDirectory() as directory:
            with new_run(Path(directory)) as session:
                result = session.run(bridge(fake_counter_completion))
                self.assertEqual(result.artifact["final_state"], {"count": 2})
                for row in session._steps():
                    self.assertEqual(loads(row[3])["returned_model"], "counter-v1")
                session.replay()

    def test_missing_optional_library_fails_loudly_without_adding_a_core_dependency(
        self,
    ):
        with patch.dict("sys.modules", {"litellm": None}):
            with self.assertRaisesRegex(InferenceError, "^litellm_not_installed$"):
                litellm_provider(
                    count_tokens=lambda *args: 0,
                    quote_microusd=lambda *args: 0,
                    cost_microusd=lambda *args: 0,
                )

    def test_returned_model_allowlist_is_frozen_in_the_session(self):
        with tempfile.TemporaryDirectory() as directory:
            with new_run(
                Path(directory), returned_models=["approved-snapshot"]
            ) as session:
                request = session.next_request()
                self.assertEqual(request["returned_models"], ["approved-snapshot"])
                from tests.test_inference import response

                value = response(request)
                value["returned_model"] = "wrong-snapshot"
                with self.assertRaises(InferenceError):
                    session.accept(value)
