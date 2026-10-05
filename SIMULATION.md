# Export-backed simulation

Seam supplies the execution boundary. A product supplies its handlers, live adapters, and simulation adapter semantics. The SDK does not contain sample, order, email, or freight-specific logic.

This is the first export-backed implementation, not automatic cloning of arbitrary production databases. Obtain and sanitize an export outside the simulation using authorized read-only access. Seam itself never connects to production.

## Three port modes

| Mode | Use | Limit |
|---|---|---|
| `script` | Controlled responses and dependency failures | No external state |
| `recording` | Exact ordered replay of historical requests | Changed requests fail instead of inventing responses |
| `backend` | Counterfactual requests against copied state | The product must supply faithful adapter semantics |

A backend handles new requests using current simulated state, so a write can affect a later read. Backends may use pure Python or an isolated in-memory database. They run under the same guards as handlers: a normal live client, filesystem database, or remote database is not a simulation backend.

## Adoption

Register both implementations on the same named port:

```python
from seam import Backend, Runtime

def simulated_stock(data, config):
    def handle(request):
        sku, take = request["sku"], request["take"]
        data[sku] -= take
        return {"remaining": data[sku]}
    return Backend(handle, lambda: data)

rt = Runtime(namespace="shop", config={"take": 1}, initial_state={})
rt.port("stock", make_live_stock)
rt.sim_port("stock", simulated_stock)
rt.on("purchase", on_purchase)
```

The simulation factory takes independent JSON copies of `(dataset, config)`. It is instantiated lazily on the first call. It returns `Backend(handle, snapshot)`: both functions are synchronous, responses and snapshots must be valid Seam JSON, and `snapshot` must return the complete deterministic state without mutating it. An unused backend contributes its initial dataset to the final artifact. SDK registration never invokes the live factory in simulation.

`ctx.config` returns a copy of the runtime config live, or the case config in simulation. The case also supplies initial product state. Put behavior-affecting switches in this config rather than reading ambient environment variables inside handlers.

## Version 2 case additions

Version 1 cases remain supported with their original closed schema and behavior digests. Version 2 permits two additional top-level fields, defaulting to empty objects:

```json
{
  "config": {"take": 2},
  "datasets": {
    "stock": {
      "path": "stock.json",
      "sha256": "<64 lowercase hex characters>",
      "as_of_ns": 0
    }
  },
  "ports": {"stock": {"mode": "backend", "dataset": "stock"}}
}
```

This fragment belongs in a full case with `version: 2`; all other required fields and scheduler rules are unchanged. Dataset references have exactly the three shown keys. Each path is relative to the case directory and cannot escape it through traversal or symlinks. Exports are strict Seam JSON, bounded to 64 MiB. Hashes cover the raw export bytes, not canonicalized content. `dataset_ref(path, as_of_ns)` generates a reference for an export stored beside its case; use an explicit relative `path` for nested exports.

Loading refuses modified exports (`dataset_changed`), exports dated after `clock.start_ns` (`dataset_future`), invalid references (`bad_dataset`), and unregistered backend ports (`bad_backend`). The normalized case digest includes config and dataset references. A declared timestamp is not proof of a consistent database snapshot: the exporter must enforce row-level cutoffs, transaction consistency, tenant scope, sanitization, and point-in-time correctness. Seam cannot detect a mislabeled export.

See `seam/proof/inventory/cases/reserve.json` for a runnable example. Its database starts with three books; two requests for two books reserve the first order and reject the second. The export is never modified.

## Recoverable dependency errors

`Fault` means the simulation contract was violated; catching it cannot turn a run green. A recoverable timeout or unavailable dependency is `PortError("timeout")`, with a lowercase identifier code and no raw exception message.

A v2 script reply has exactly one of `response` and `error`:

```json
{"match": {"sample": "s1"}, "error": "timeout", "repeat": 1}
```

The product may catch `PortError`, retry, and pass its outcome assertions. An uncaught `PortError` becomes a handler error. The artifact logs the attempted request, `response: null`, and `error: "timeout"`. Live adapters must normalize their expected SDK/client exceptions to `PortError`. Unexpected backend callback exceptions become sticky `bad_backend` faults, even if the product catches them; attempted backend calls remain logged.

Live client initialization failures are recorded too. Error records redact the request only: there is no response body to redact. JSON validation covers object keys as well as values, and initial product state is limited to 1 MiB regardless of snapshot logging mode. Explicitly allowlisted file metadata reads work; unapproved code-file metadata and relative `dir_fd` access remain blocked. Grader bodies cannot widen the filesystem policy.

Live recordings and `tape_from_artifact` encode errors as `seam-tape` version 2 with `error` instead of `response`. Successful records remain version 1. Version 2 cases can replay both and rethrow recorded errors; v1 cases refuse visible errors to preserve their artifact schema. Future tape bodies are excluded before response validation; syntactically torn JSON still refuses the tape. Visible-request mismatch errors never include the expected historical body.

## Artifact and provenance

Version 2 cases produce version 2 artifacts. `backend_states` maps backend port names to their final snapshots and enters the behavior digest. Graders can inspect it to distinguish attempted calls from durable outcomes.

`provenance.datasets` retains dataset references, and `provenance.tapes` hashes each port's visible ordered requests and responses/errors. Hidden future records do not affect those hashes. Tape content hashes are separate from the historical v1 case digest; retain provenance as well as the case digest when identifying replay inputs.

The supervised `run_product` launcher adds the product module/source hash, SDK version, Python version, and a hash of the child environment. These remain outside the behavior digest. The source hash covers `.py` files in the product's top-level package, not third-party dependencies, native extensions, wheels, or an image. Environment values themselves are not stored. This is useful provenance, not a complete reproducibility lockfile.

## Process and live contracts

`run_product(module, case, namespace, artifact, timeout=30, env=None)` starts a fresh POSIX process with a fresh staging artifact. It inherits only `PATH` and `PYTHONPATH`, pins hash seed zero and UTC, disables bytecode writes, and adds explicitly supplied `env`. Callers cannot override `SEAM_` variables this way. Parent credentials and other application variables are not inherited. Avoid explicitly passing secrets to simulations.

Timeouts kill the child's process group. Timeout, missing artifact, and invalid artifact produce a supervisor failure with exit 2, `digest: null`, and `supervisor: true`, never success inferred from exit zero. Completed artifacts must match the requested namespace and the normalized case digest captured before launch, not merely contain a valid behavior digest. Valid product artifacts retain the existing 0/1/2/3 exit meanings. Handler `SystemExit`, concealed coroutines, and caught guard violations generate failed artifacts. Launch failures and output-write failures propagate to the caller. The supervisor is not an OS sandbox and does not impose memory or filesystem quotas.

Live `deliver` serializes concurrent callers; same-thread reentry is still forbidden. A live timer backend receives `(token, at_ns, handler, body, name)` and must call `rt.fire_timer(token)` after the handler returns, at the requested instant. `fire_timer` consumes an armed token once and ignores callbacks for canceled or already-fired tokens. Delivery failures propagate and consumed tokens are not automatically retried. Unique timer names and cancellation faults match simulation. The product owns scheduling and durable timer persistence. `rt.close()` closes live recording and prevents further delivery.

## Explicit limits

- Synchronous JSON handlers only; no async scheduling, awaited ports, or concurrent simulation workers.
- No generic Postgres snapshot/restore, CDC, schema migration, queue clone, or cloud-storage clone.
- No automatic production capture, credential management, PII redaction, or tenant/cutoff discovery.
- No arbitrary latency, nondeterministic fault injection, or distributed-system simulation. Product-written timers and scripts cover explicit delayed events and failures.
- No guarantee that a backend matches the live adapter. Test adapter conformance using shared request/response contracts before relying on its predictions.
- Monkeypatch guards are best-effort. Prebound references, native extensions, setup-time I/O, and private runtime internals remain trusted. Use isolated containers with no network and least privilege for production-derived data.
- Artifacts and recordings can contain sensitive data and are mode `0644` for v1 compatibility. Store them in access-controlled directories; do not commit real customer exports.

Version 3 adds [shared dependency worlds and resumable simulation checkpoints](WORLDS.md), including an explicit cleanup lifecycle. The next increment should prove adapter fidelity and point-in-time inputs on a real product. Glass and the SDK proofs test the boundary; none establish that arbitrary products are plug-and-play.
