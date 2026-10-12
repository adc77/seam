# Model-driven paired experiments

Seam 0.8 adds `seam.inference_experiments`, connecting the provider-neutral inference controller to sequential scenario comparisons. A caller supplies an explicit trusted provider callable. No provider is discovered, imported, substituted, or called by merely reading a plan or report.

## Declare and run

```python
from seam.inference import settings
from seam.inference_experiments import InferenceExperiment, plan

spec = plan(
    "myproduct.experiments",
    [
        {"name": "baseline", "parameters": {"hold_ns": 10}},
        {"name": "long_hold", "parameters": {"hold_ns": 30}},
    ],
    [7, 19, 31],
    baseline="baseline",
    config=settings(
        "explicit-provider", "explicit-model", "Return one authorized decision.",
        max_calls=32, max_input_tokens=8192, max_output_tokens=1024,
        max_cost_microusd=0, timeout_ms=10000,
    ),
    max_calls=100,
    max_cost_microusd=0,
    timeout_s=30,
)
with InferenceExperiment.create("/path/to/new-ensemble", spec) as experiment:
    summary = experiment.run(provider)
```

`provider` is an application-owned callable satisfying [the inference contract](INFERENCE.md). Zero-cost settings permit only zero-reported-cost fake/recorded responses, not paid inference. All compatibility and accounting caveats of the underlying controller still apply. This SDK supports extensible adapters; it does not certify every vendor/model.

Plans use the closed `seam-experiment` version 2 schema: the version 1 fields plus `inference`, containing exactly `world`, `config`, and `budget`. `world` defaults to `actors`. The budget has nonnegative signed-64-bit `max_calls` and `max_cost_microusd`, excluding booleans. Variant/seed bounds remain unchanged. The existing version 1 rule-mode API and CLI are unchanged and cannot silently execute version 2 plans.

## Product isolation and pairing

Reuse the product's existing `MODULE`, `ACTOR_WORLDS`, `METRICS`, `prepare`, and `measure` adapter. Preparation still creates a fresh rule-mode population with the supplied seed. The controller retains these original inputs, copies the case/datasets into a separate directory, and explicitly converts only the selected actor world's population to empty-prefix inference mode. It does not change profiles, private initial memory, start times, capabilities, seeds, product handlers, or the population-generation recipe. Other actor worlds retain their existing rules.

The selected world must be declared by the adapter. The existing inference controller supports one inference world and fresh v3 cases; ordinary checkpoint resumes, external tapes, and checkpoint forks remain unsupported. Product authorization validates proposals again inside the isolated executor. There is no rule-policy fallback for malformed, refused, or missing model decisions.

Matching seeds pair the scenario inputs; they do not make independent model responses identical or establish causal validity. Fake-provider baseline equivalence tests establish plumbing, not realistic human behavior. Live model outputs need sensitivity analysis and behavioral calibration before predictive claims.

## Durable progress and recovery

```python
with InferenceExperiment("/path/to/existing-ensemble") as experiment:
    progress = experiment.progress()
    summary = experiment.run(provider)
```

Each experiment has a mode-0700 directory and mode-0600 SQLite progress journal with full synchronous transactions and an exclusive nonblocking writer lock. Each cell has its own existing inference journal. Complete cell records are never overwritten or rerun with inference. `step(provider)` advances at most one dispatch; `run(provider)` continues to a completed report or explicit interruption. Reopening requires unchanged adapter/product/SDK source hashes, Python, minimal supervised environment, manifest, and retained evidence.

A failed progress-journal write invalidates the live controller instance; close and reopen it before continuing. Uncommitted in-memory reservations never authorize dispatch.

Preparation is trusted application code, not a transaction or sandbox. Its phase is committed before invocation. If a process dies during preparation, reopening marks that cell `experiment_prepare_uncertain` rather than silently calling preparation twice. A process killed during initial journal creation can leave an invalid incomplete directory; it is refused, not overwritten or repaired automatically.

Before every dispatch, the ensemble commits a unique reservation keyed by cell, sequence, and request ID. The underlying inference journal then marks its request in flight before entering the provider worker. A crash between these commits can reuse the same reservation only while the inference request is still pending. A committed response is reconciled into ensemble accounting on reopen without a duplicate call. Worker processes close inherited journal-lock descriptors so a dead controller does not leave its lease held by a surviving provider worker.

A possible dispatch without a durable accepted response raises `experiment_inference_uncertain` and stops the ensemble. There is no automatic retry. To continue, supply the exact known response with `experiment.accept(response)`, which applies the ordinary identity/schema/capability/usage checks. Alternatively, explicitly call `experiment.abandon_uncertain()` to fail that cell and retain the full reservation before continuing other cells. Abandonment is not a refund or a claim that no call occurred.

## Experiment-wide budgets

Per-cell settings still bound calls, input/output tokens, accepted cost, and provider-worker timeout. The ensemble additionally bounds call reservations and accounted cost across **all** variants and seeds. A new cell's immutable allowance is the smaller of its configured cost cap and the remaining global allowance. Each request reserves its full remaining per-cell cost allowance before possible dispatch. Accepted responses replace that hold with their validated reported cost; rejected/uncertain responses retain the full hold. Reservations count conservatively even if a crash occurred before actual dispatch. A zero global call cap prevents dispatch entirely.

Reports distinguish `reserved_calls`, `accepted_cost_microusd`, `held_cost_microusd`, `accounted_cost_microusd`, and accepted input/output tokens. These are conservative application accounting values, **not invoices**. Unknown pricing must never become zero. Real adapters must enforce conservative pre-dispatch quotes and token caps; a callable that ignores its limits can still cause an externally billed overrun. No SDK can revoke a dispatched request, guarantee an invoice ceiling, or provide exactly-once paid inference. No paid or live-provider certification has run.

`timeout_s` applies to every supervised product-prefix/replay execution; provider workers use `config.timeout_ms`. Prefix reexecution remains potentially quadratic in decisions and retains per-step evidence. This v1 is for small sequential ensembles, not high-throughput concurrent workloads. Preparation/measurement are trusted synchronous code without separate process timeouts.

## Evidence, failures, and replay

Completed reports use the same `read_experiment` API and paired-delta semantics. Every cell retains original/converted inputs, a verified final or failure artifact when available, explicit accounting, an immutable `inference.json` snapshot of requests/responses/reservations, and the exact trajectory inputs matching that artifact. Failed cells never contribute zero-valued outcomes or successful metrics; affected pairs are excluded and labeled incomplete.

Passing cells also retain strict replay inputs and an offline replay artifact. Before accepting metrics, the controller verifies replayed actor decisions, final product state, and all non-actor world states. Replay is not another model call.

```python
from seam.inference_experiments import replay_experiment

result = replay_experiment(
    "/path/to/completed-ensemble", "baseline", 7,
    "/path/to/new-replay-output.json",
)
```

The output must be new and outside the evidence directory. Replay verifies the completed report and execution identity, runs the recorded tape, and requires the full replay artifact to match. It does not open or modify inference/progress journals. A completed viewer session likewise reads immutable JSON evidence only; it shows exact persona observations/memory/decisions, model accounting, and retained private request/response records without running product code or calling providers. Incomplete active ensembles remain unavailable in the viewer; it is not a live monitor.

Metadata/report writes are bounded at 16 MiB, artifacts at 32 MiB, and plan JSON at 1 MiB. A full snapshot or report exceeding its bound is explicitly refused. Disk quotas, journal migration, checkpoint forks, parallel scheduling, and a scenario editor/job launcher remain separate work. Keep old execution environments for old captures/journals; source identity checks are not relaxed by this feature.

## Installed proof

```sh
env -u PYTHONPATH python -m seam.proof.inference_experiments \
  --out /path/to/new-proof --installed
```

Four actual counter cells run through fake inference, continue in a fresh process, verify their viewer projection, and produce byte-identical provider-free replays. CI runs this on Linux/macOS with Python 3.11, 3.13, and 3.14. Separate owned-product proofs exercise Glass and local Reservations through the same generic controller; no organizational data, live providers, payments, or infrastructure are used.
