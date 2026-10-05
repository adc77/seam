# Shared worlds and simulation checkpoints

Version 3 is opt-in. Version 1 and 2 retain their schemas and behavior digests. A world groups dependency ports around one run-scoped state store; a checkpoint preserves an in-flight simulation between completed synchronous handlers. Neither API captures a live process or connects to production.

## One dependency, multiple ports

Register live ports as usual, then their simulation world:

```python
from seam import Runtime, World

def ledger_world(ctx, data, config):
    def write(request):
        data["balance"] += request["delta"]
        return {"status": "written"}

    def read(request):
        return {"balance": data["balance"], "at_ns": ctx.now()}

    def restore(snapshot):
        data.clear()
        data.update(snapshot)

    return World({"write": write, "read": read}, lambda: data, restore)

rt = Runtime(namespace="ledger")
rt.port("write", make_live_writer)
rt.port("read", make_live_reader)
rt.sim_world("ledger", ledger_world, ports=("write", "read"))
```

The factory receives `(WorldContext, dataset_copy, config_copy)` and runs lazily on the first call, once per run, under simulation guards. Handlers must have exactly the registered port names. A port cannot belong to two worlds or also have a legacy `sim_port` registration. Live delivery never instantiates simulation worlds.

`WorldContext` exposes `now()`, `utc()`, `rand_u64()`, `rand_below(n)`, `id(prefix)`, `namespace`, and copied `config`; it cannot deliver, patch product state, or schedule product timers. Port callbacks receive a copied request and return Seam JSON. Expected failures use `PortError`; contract violations and unexpected exceptions are sticky faults.

`World(handlers, snapshot, restore, close=None)` requires synchronous callbacks:

- `snapshot()` returns all deterministic dependency state as Seam JSON, without changing it.
- `restore(snapshot_copy)` restores that state and returns `None`.
- `close()` releases resources and returns `None`. It runs once for each initialized world at completion, pause, or fault, including restore failures. Cleanup failures fail the run.

Snapshot, restore, and close callbacks cannot draw run randomness. Factories may use the shared run RNG and clock: restoration reconstructs them at their original initialization time and RNG position, then calls `restore` at the checkpoint time without advancing the restored global RNG. Factories must not rely on state outside pinned inputs and supplied context. Snapshot purity and complete state coverage remain adapter responsibilities.

Each world has its own dataset copy. Two worlds referencing the same dataset are intentionally independent; sharing requires routing ports to the same world. An unused world is not instantiated and contributes its initial dataset to `world_states`. Initialized snapshots enter the behavior digest.

## Case schema

A v3 case retains the v2 fields and adds these optional fields:

```json
{
  "worlds": {"ledger": {"dataset": "store"}},
  "ports": {
    "write": {"mode": "world", "world": "ledger"},
    "read": {"mode": "world", "world": "ledger"}
  },
  "checkpoint_after": 1,
  "resume": {"path": "checkpoint.json", "sha256": "<raw file sha256>"}
}
```

Use a complete case with `version: 3` and a hashed `datasets.store` reference. Omit `resume` on the first run; omit `checkpoint_after` to run uninterrupted or resume to completion. World declarations have exactly one `dataset` field and must be referenced by a world-mode port. Script and recording ports can coexist with worlds. Legacy backends remain supported, but requesting checkpoint or resume with any legacy backend refuses with `unsupported_checkpoint`: its snapshot-only API cannot restore dependency state.

## Pause, persist, resume

`checkpoint_after` is a positive delivery count for this process segment, not a clock deadline. Seam pauses after that many clean, completed handlers unless the handler faults or terminates the run. It saves:

- Product state, logical clock, RNG counter, and delivery/port budgets already consumed.
- Pending arrivals and timers, original sequence ordering, token counters, and cancellation history.
- Event/port/state history, script repeat counts, recording cursors, and initialized world snapshots.

Paused artifacts have `stop_reason: "checkpoint"`; pending timers stay `armed`. Assertions and graders still run against the paused state. A clean pause returns exit **4**, `status: "paused"`, not a completed pass. An assertion failure returns exit 1 and retains the checkpoint for an explicit caller decision; a runtime or cleanup fault produces no checkpoint.

```python
import json
from pathlib import Path
from seam import checkpoint_ref, run_product, write_checkpoint

directory = Path("cases")
base = json.loads((directory / "base.json").read_text())
paused_case = {**base, "checkpoint_after": 1, "assertions": []}
(directory / "paused.json").write_text(json.dumps(paused_case))
paused = run_product("myproduct", str(directory / "paused.json"), base["namespace"], "paused-out.json")
assert paused.returncode == 4, paused.artifact
write_checkpoint(directory / "checkpoint.json", paused.artifact["checkpoint"])
resumed_case = {**base, "resume": checkpoint_ref(directory / "checkpoint.json")}
(directory / "resumed.json").write_text(json.dumps(resumed_case))
resumed = run_product("myproduct", str(directory / "resumed.json"), base["namespace"], "resumed-out.json")
assert resumed.returncode == 0, resumed.artifact
```

References are confined to the case directory, including symlink resolution, and pin raw bytes. Documents have a closed schema, checksum, 32 MiB limit, validated scheduler/history/cursor metadata, and SDK version. Workload identity pins seed, config, initial state, dataset references, arrivals, stop budgets, logging mode, and visible tape contents. Only assertions, grader, pause count, and checkpoint reference may change on resume.

Resume also pins product and SDK Python source hashes, Python version, and the launcher's child-environment hash. A different workload or source/environment identity refuses before product callbacks. Use `run_product` consistently; direct `python -m myproduct` derives identity from its ambient environment instead. Direct script execution without a module identity refuses checkpointing.

A correctly implemented world restores the exact uninterrupted behavior digest and full history. Case digest and checkpoint provenance differ because they identify operational pause/resume inputs. Checkpoints are not editable counterfactual starting snapshots; changing configuration or inputs requires a fresh case.

## Runnable proof and boundaries

`seam.proof.shared_store` supplies an in-memory SQLite database shared by separate write/read ports. Its packaged `cases/shared.json` writes, observes, cancels timers, and processes same-time arrivals/timers. Tests compare uninterrupted execution against repeated checkpoints in fresh product processes, including factory-generated IDs, lazy initialization, all snapshot logging modes, errors, replay cursors, and consumed budgets.

Checkpoint files written by `write_checkpoint` use mode `0600`; their embedding artifact retains the existing artifact mode `0644`. Store the entire run in an access-controlled directory. Checkpoints contain product/dependency data, are unsigned, and are not a security boundary. The writer refuses an existing `.tmp` file rather than overwriting it.

This increment does not provide live timer/RNG capture, generic Postgres/queue/object-store cloning, cross-build migration, async/concurrent execution, dependency/image pinning, adapter fidelity guarantees, or automatic point-in-time exports. Export correctness, tenant isolation, sanitization, adapter conformance, and external container isolation still need a real-product proof.
