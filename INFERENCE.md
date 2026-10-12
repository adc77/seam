# External actor inference

This optional 0.6 increment supplies a provider-neutral handoff and optional normalized chat bridge, not a claim of realistic human forecasting. It builds on [ACTORS.md](ACTORS.md); no provider SDK enters the product executor.

## Continuation contract

Use `inference_population(actors, seed=..., responses=...)` instead of a rule population. Actor dataset format/version 1 explicitly selects mode `inference`; its tape is an ordered prefix of objects with exactly `request` and `decision`. Existing rule/replay datasets are unchanged.

The product executes until an actor requires an unprovided decision. Its world raises `actor_inference_required` with the approved canonical request in `fault.op`, before accepting memory or scheduling that action. This barrier is **failed, exit 2**, not passed and not a checkpoint. Only the controller interprets that particular result as a handoff. Other failures remain failures.

The controller journals the immutable request, calls a provider outside the guarded executor, and records a validated response. It starts a **fresh supervised simulation** with the expanded response prefix. Earlier decisions match complete actor requests and pass through product-owned authorization again; accepted records must match the journaled history. Inference mode never invokes rule policies or fills missing decisions.

This is a fresh-run prefix protocol, not checkpoint resume. Ordinary checkpoint input/source/environment pinning stays strict. The executor exits during inference; wall-clock inference latency does not advance virtual time. Only an explicit `wake_after_ns` advances simulated decision timing.

Reexecuting prefixes can take quadratic work and retains per-step evidence on disk. This v1 is for small experiments, not large ensembles. World factories must recreate isolated simulation resources, never normal-operation databases.

## Controller API

```python
from seam.inference import InferenceRun, settings

config = settings(
    "my-provider", "explicit-model", "Return only the required decision schema.",
    max_calls=32, max_input_tokens=8192, max_output_tokens=1024,
    max_cost_microusd=0, timeout_ms=10000, temperature_milli=0,
)
with InferenceRun.create(
    "/path/to/new-private-run", "myproduct.actor_runtime", "case.json",
    config=config,
) as session:
    request = session.next_request()
    session.accept(response)  # Supply an explicitly identified recorded/provider response.
```

The variable `response` comes from your explicitly configured adapter or recorded result; the SDK does not synthesize it. Use a new run directory with an existing parent.

- `next_request()`: return a bounded envelope or a passed, sealed `ProcessResult`. Repeated pending reads never call a provider.
- `invoke(provider)`: at most one external call, or the completed result.
- `run(provider)`: continue until completion or explicit failure.
- `replay()`: replay the completed trajectory through the ordinary strict actor tape, with no inference.
- `InferenceRun(directory)`: reopen the same journal under its original execution identity.

The controller supports fresh v3 cases, one inference actor world, hashed JSON datasets, and inline script/world/backend ports. It refuses ordinary resumes, `checkpoint_after`, and external recording/script tapes. Other actor worlds must not request inference. Case and datasets are frozen in the journal; changing their original files does not change the experiment.

Only `PYTHONPATH` is accepted in the explicit child environment. The supervised product exports its usual minimal environment, not provider credentials. Product/SDK Python-source hashes, interpreter version and supervised environment hash are checked before every handoff and replay. These are not native/dependency locks or a malicious-code sandbox.

## Request and response

The envelope includes manifest hash, sequence, configured provider/model, approved returned-model identifiers, integer-milli temperature, rendered prompt/hash, exact actor request, limits and a SHA-256 `request_id`. Null temperature explicitly omits that parameter for models that do not accept it. The actor request exposes only approved observation, that actor's profile/memory, capabilities, seed, RNG counter, time and step. Keep credentials and unrelated private data out of prompts, profiles, observations, cases and adapter output.

Response fields are closed:

```json
{
  "request_id": "<exact request SHA-256>",
  "model": "<configured logical model route>",
  "returned_model": "<actual approved native model identifier>",
  "decision": {
    "action": {"type": "<allowed action>", "body": {}},
    "memory": {},
    "wake_after_ns": 1
  },
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_microusd": 0
  }
}
```

Action and delay may be null, as in offline actors. Floats, duplicate keys, extra fields, oversized values, model/request mismatches, forbidden actions, invalid delays and over-budget usage are rejected. Body/ownership validation remains product-owned. A validated response is a recorded proposal, not proof of business success.

Output is never executable Python, SQL, unrestricted tool calls or URLs to fetch. Memory/product effects occur only after actor validation in the isolated executor. Prompt injection cannot expand capabilities or bypass the translator.

The trusted callable receives one copied envelope in a bounded POSIX fork worker and returns one response object. Use a single-threaded controller parent; fork does not sandbox untrusted adapters. Provider exceptions preserve uncertainty without recording potentially secret exception messages. Native adapters must handle response/refusal schemas, tokens, returned-model identity, pricing and credentials.

## Provider compatibility

The core accepts any callable satisfying the response contract: native cloud clients, private gateways, locally hosted models and recorded responses. There is no core vendor/model allowlist or mandatory provider dependency.

`seam.providers.ChatProvider` bridges normalized chat-completion transports. It accepts explicit trusted callbacks `count_tokens(model, messages)`, `quote_microusd(model, input_tokens, max_output_tokens)`, and `cost_microusd(model, normalized_response)`. Missing or invalid accounting fails; unknown pricing is never silently treated as free. Pre-dispatch token/cost checks happen before the completion transport.

`litellm_provider(...)` lazily loads an application-owned LiteLLM installation and uses its direct completion API. LiteLLM provides a normalized interface across supported cloud/local providers; it is optional, not an SDK dependency or infrastructure service. Use a reviewed, pinned version in the consuming application's environment. Do not pass a Router or gateway configured to retry/fail over behind this adapter. [LiteLLM SDK documentation](https://docs.litellm.ai/docs/) describes its provider surface; [completion parameters](https://docs.litellm.ai/docs/completion/input) describe retry/fallback controls.

The bridge passes route names unchanged and requests one nonstreamed result, no retries/fallbacks, and no dropped unsupported parameters. It uses plain JSON prompting plus SDK validation rather than assuming every model supports native structured-output enforcement. Refusal, truncation, tool calls, invalid JSON, missing usage and unapproved returned models stop the handoff. OpenAI also documents model-dependent structured-output support and refusal handling in its [official structured-output guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat).

Configure logical routes and native snapshot aliases explicitly, for example `settings(provider, route, prompt, returned_models=["approved-native-snapshot"], temperature_milli=None)`. Both configured route and actual returned model are journaled. Aliases do not authorize automatic model substitution.

Compatibility means an extensible transport contract and optional bridge, not certification that every model/provider supports every parameter, structured output, tokenizer or usage field. Routing/normalization/budget tests use fake transports; no live vendor was called. Providers outside the normalized chat surface use their own callable adapter without changing actors or product handlers.

## Budgets and recovery

At most `max_calls` responses and one dispatch attempt per request; no automatic retry, model substitution or rule fallback. Responses are bounded by actor JSON and input/output token limits. Accepted cost is cumulative integer micro-USD; each request carries the remaining allowance. Workers have explicit timeouts and are stopped by their own process handle.

Zero cost permits only zero-reported-cost fake/recorded responses; it does not authorize paid inference. Adapter usage is accounting, not a way to revoke an already billed request. A real adapter must enforce token caps and conservative pre-dispatch cost estimates. Rejected/interrupted calls may still be billed. The SDK does not guarantee an invoice ceiling or exactly-once paid inference.

Run directories are new, user-owned and mode 0700. Journals are mode 0600 SQLite with full synchronous transactions and a nonblocking exclusive writer lock. Inputs/settings/identity commit before requests. Settings have no credential fields; bounded request/response JSON and accepted history are retained. Checksums provide integrity checking, not authentication against the owner.

State is `pending -> in_flight -> responded`, or an explicit rejection. Possible dispatch is committed before starting a worker.

- Crash before dispatch: reopen the identical pending request.
- Crash after response commit: reopen and reexecute its prefix without asking again.
- Crash, timeout or error after dispatch without a durable response: report `inference_uncertain`, never silently retry.
- A known recorded response may resolve uncertainty via `accept(response)`; all identity/schema/budget checks still apply.
- Duplicate responses are refused. Stale/invalid responses durably reject and stop the run; create a separate explicit experiment rather than overwriting evidence.
- Changed source, interpreter, environment or manifest refuses continuation. Do not edit journals to bypass those checks.

No automatic uncertain-result recovery, journal migration, parallel writers or checkpoint continuation is provided.

## Executable proof

From an installed SDK in a neutral directory:

```sh
python -m seam.proof.inference --out /path/to/new-proof --installed
```

It commits a fake-provider response, continues in a fresh Python process, drives actual counter handlers, and produces two byte-identical strict replays. No network or paid call is used.

Tests also exercise forced exits at handoffs, timeout/error uncertainty, explicit resolution, stale/duplicate/malformed/unauthorized decisions, call/cost budgets, frozen inputs, locking and identity changes.

Full digests may differ between inference and replay because input/provenance differs. Accepted actor records and domain outcomes must match; repeated identical strict replay artifacts must match byte-for-byte.
