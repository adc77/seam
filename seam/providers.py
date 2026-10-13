"""Optional normalized chat-completion bridge; the core inference contract accepts any provider callable."""

from seam.actors import _decision
from seam.canon import INT64_MAX, loads
from seam.inference import InferenceError

INSTRUCTION = (
    'Return only one JSON object with exactly "action", "memory", and "wake_after_ns". '
    'Action is null or {"type": an allowed action, "body": product action data}. '
    "Memory is bounded JSON. Wake_after_ns is a nonnegative integer or null to finish. "
    "Use only the provided observation and capabilities. Never return code or tool calls."
)


def _integer(value):
    if type(value) is not int or not 0 <= value <= INT64_MAX:
        raise InferenceError("provider_accounting_invalid")
    return value


class ChatProvider:
    """Bridge a normalized completion transport with explicit trusted token and cost accounting."""

    def __init__(self, completion, *, count_tokens, quote_microusd, cost_microusd):
        for fn in (completion, count_tokens, quote_microusd, cost_microusd):
            if not callable(fn):
                raise InferenceError("bad_provider_adapter")
        self.completion = completion
        self.count_tokens = count_tokens
        self.quote_microusd = quote_microusd
        self.cost_microusd = cost_microusd

    def __call__(self, envelope):
        instruction = INSTRUCTION
        if "choices" in envelope["request"]:
            instruction = (
                'Return only {"choice": "one offered choice id"} from request.choices. '
                "Do not invent actions, memory, delays, or choice IDs."
            )
        messages = [
            {"role": "user", "content": instruction + "\n" + envelope["prompt"]}
        ]
        model, limits = envelope["model"], envelope["limits"]
        inputs = _integer(self.count_tokens(model, messages))
        if inputs > limits["max_input_tokens"]:
            raise InferenceError("provider_input_budget")
        quote = _integer(
            self.quote_microusd(model, inputs, limits["max_output_tokens"])
        )
        if quote > limits["max_cost_microusd"]:
            raise InferenceError("provider_cost_budget")
        arguments = dict(
            model=model,
            messages=messages,
            max_tokens=limits["max_output_tokens"],
            stream=False,
            num_retries=0,
            fallbacks=[],
            drop_params=False,
            timeout=limits["timeout_ms"] / 1000,
        )
        if envelope["temperature_milli"] is not None:
            arguments["temperature"] = envelope["temperature_milli"] / 1000
        raw = self.completion(**arguments)
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        if (
            type(raw) is not dict
            or type(raw.get("choices")) is not list
            or len(raw["choices"]) != 1
        ):
            raise InferenceError("provider_response_invalid")
        native_model = raw.get("model")
        if (
            type(native_model) is not str
            or native_model not in envelope["returned_models"]
        ):
            raise InferenceError("provider_model_changed")
        choice = raw["choices"][0]
        if type(choice) is not dict or choice.get("finish_reason") != "stop":
            raise InferenceError("provider_incomplete")
        message = choice.get("message")
        if (
            type(message) is not dict
            or message.get("refusal")
            or message.get("tool_calls")
            or message.get("function_call")
        ):
            raise InferenceError("provider_refused_or_tool_call")
        content = message.get("content")
        if type(content) is not str or len(content.encode("utf-8")) > 1024 * 1024:
            raise InferenceError("provider_response_invalid")
        try:
            decision = _decision(loads(content))
        except Exception:
            raise InferenceError("provider_invalid_decision") from None
        usage = raw.get("usage")
        if (
            type(usage) is not dict
            or "prompt_tokens" not in usage
            or "completion_tokens" not in usage
        ):
            raise InferenceError("provider_usage_missing")
        actual_input = _integer(usage["prompt_tokens"])
        actual_output = _integer(usage["completion_tokens"])
        cost = _integer(self.cost_microusd(model, raw))
        if (
            actual_input > limits["max_input_tokens"]
            or actual_output > limits["max_output_tokens"]
            or cost > limits["max_cost_microusd"]
        ):
            raise InferenceError("provider_usage_budget")
        return {
            "request_id": envelope["request_id"],
            "model": model,
            "returned_model": native_model,
            "decision": decision,
            "usage": {
                "input_tokens": actual_input,
                "output_tokens": actual_output,
                "cost_microusd": cost,
            },
        }


def litellm_provider(*, count_tokens, quote_microusd, cost_microusd):
    """Use an application-owned LiteLLM SDK installation without making it a Seam dependency."""
    try:
        from litellm import completion
    except ImportError:
        raise InferenceError("litellm_not_installed") from None
    return ChatProvider(
        completion,
        count_tokens=count_tokens,
        quote_microusd=quote_microusd,
        cost_microusd=cost_microusd,
    )
