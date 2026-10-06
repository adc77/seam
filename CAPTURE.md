# Product-owned capture, shared replay packaging

Seam 0.4 adds a small capture contract proven by two independent consumers: Glass's sample pipeline and the local reservation service. It does not turn the SDK into a database exporter or a production recorder.

The product captures a consistent snapshot, incoming deliveries, and observed outcomes. Seam validates the common envelope, packages pinned datasets into an ordinary v3 case, and runs the existing supervised launcher. No product-specific SDK handlers, schemas, or business rules are included.

## Envelope

`capture_bundle(module, *, as_of_ns, until_ns, config, initial_state, datasets, arrivals, assertions, product_data)` creates:

```json
{
  "format": "seam-capture",
  "version": 1,
  "payload": {
    "source": {"module":"owned_product.replay","product_sha256":"...","sdk_version":"0.4.1","sdk_sha256":"..."},
    "as_of_ns": 100,
    "until_ns": 200,
    "config": {},
    "initial_state": {},
    "datasets": {"database":{"as_of_ns":100,"data":{}}},
    "arrivals": [{"at_ns":150,"handler":"event","body":{}}],
    "assertions": [{"op":"state_is","path":"observed","value":{}}],
    "product_data": {}
  },
  "sha256": "..."
}
```

The schema is closed. Times are nonnegative signed-64-bit integer Unix nanoseconds, not booleans. Arrivals must be ordered within the capture window; equal timestamps retain input order. Dataset cutoffs cannot exceed the snapshot boundary. At least one valid v3 outcome assertion is required. The manifest is bounded to 8 MiB and 10,000 arrivals, with the existing canonical JSON limits. Product exporters may impose tighter limits. Ordinary case limits still apply when packaging.

`capture_source(module)` hashes the product's Python package tree and Seam's Python package tree and records the SDK version. `validate_capture(document, *, module)` and `read_capture(path, *, module)` require an exact match with the currently installed sources. Pass an approved module from product code; never select executable code using the manifest's module field. Invalid captures raise `CaptureError`, a `ValueError` subclass.

`write_capture_json(path, value)` writes canonical JSON using exclusive creation, mode 0600, and fsync. It never overwrites an existing path. Checksums detect changes, not malicious forgery or authenticity.

## Product replay plan

The product first validates its own datasets, state, metadata, arrivals, cutoff rules, and outcome assertions. Then it passes a plan to `write_capture_case` or `run_capture`:

```python
from seam import run_capture

result = run_capture(
    validated_capture,
    "/path/to/new-replay",
    module="owned_product.replay",
    name="owned-product-capture",
    namespace="sim-owned-product",
    ports={"database": {"mode": "world", "world": "database"}},
    worlds={"database": {"dataset": "database"}},
    bootstrap=({"handler": "bootstrap", "body": product_bootstrap},),
    finalize=({"handler": "observe", "body": {}},),
    same_time_order="timers_first",
)
```

The replay module registers these handlers and worlds using the existing SDK APIs. Bootstrap receives product-owned metadata and reconstructs in-flight work; it is not a generic timer-restoration handler in Seam. Finalization observes meaningful product and dependency state at the horizon. Boundary handlers execute in supplied order around external arrivals. If they schedule equal-time timers, the case's explicit scheduling policy applies.

Packaging creates a new mode-0700 directory containing `case.json`, `data-<dataset>.json`, and `origin.json`, all mode 0600. Dataset filenames cannot collide with case or origin metadata. The origin records capture, source, and generated-case hashes. A malformed common plan is rejected before creating the output directory. Filesystem failures may leave a partial new directory; use a fresh destination for another attempt. Artifacts retain the ordinary SDK file mode inside that private directory.

`run_capture` packages and invokes `run_product`, returning its `ProcessResult`. It inherits supervision, guarded execution, artifact validation, deadlines, and exit codes. It does not start or access a live service. Passing a valid envelope alone does not guarantee domain correctness: always validate in the product wrapper before calling it.

## Equal-time ordering

Optional v3 case field `same_time_order` accepts `sequence` or `timers_first`. Absent the field, all existing v1/v2/v3 cases retain stable sequence ordering and their existing case normalization. v1/v2 reject the new field.

`timers_first` gives already-armed timers priority over arrivals at the same timestamp. Within each class, sequence order remains stable. Timers armed by a handler become visible only after that handler returns; this is not preemptive execution. Checkpoints retain the policy in workload identity and restore it without changing the checkpoint's queue schema. A resumed case cannot change the policy.

Both live proof services pump due timers before incoming events, so their product importers select this policy explicitly. It is not a new default for every product.

## Counterfactuals

Packaging accepts explicit `arrivals`, `until_ns`, and `config` overrides. It preserves the original capture's assertions and records a different generated-case hash. Glass shifts readings; reservations shifts payments. A changed outcome therefore exits 1 against the original baseline. Applications must interpret the artifact and decide what outcome should be desirable; Seam does not declare any difference a business improvement.

## Ownership and limits

Seam owns envelope integrity, source compatibility, bounded serialization, packaging, dataset hashes, scheduling, and supervised execution. Products own:

- Consistent snapshot boundaries across state, dependencies, inboxes, and timers.
- Domain schemas, row-level cutoff validation, sanitization, and redaction.
- Preservation of live IDs and restoration of pending work from business deadlines or exported timer metadata.
- Adapter semantics, transaction behavior, idempotency, and meaningful observed-state assertions.
- Durable capture, recovery after a crash, and operational authorization.

The shared contract does not provide CDC, a historical database clone, arbitrary-date snapshots, distributed consistency, capture of every source of randomness, concurrent application execution, a dependency lock, or a security sandbox. Python-source identity does not pin native libraries, interpreter builds, configuration, external APIs, or hardware. Keep those environments controlled separately. A matching replay establishes conformance to the captured window, not universal correctness or proof of future behavior.

Consumers need matching SDK source or wheels. Publish the SDK commit before pinning consumer dependencies and CI to its full SHA; a version number alone does not establish availability on a package index. The independent reservation service remains a local integration proof, not a published product.
