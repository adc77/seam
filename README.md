# seam

Working title. A generalized simulation SDK: run a product's own handlers in a separate seeded process against scripts, cut-off recordings, or isolated stateful backends initialized from production exports. Glass is one consumer, not the SDK's domain.

The original v1 spec is [PLAN.md](PLAN.md); export-backed v2 is in [SIMULATION.md](SIMULATION.md), and opt-in v3 shared dependency worlds and checkpoints are in [WORLDS.md](WORLDS.md). Apache-2.0.

```shell
python3 -m unittest discover -s tests -t .
```

Run that from the repository root. The suite is in-process unit tests plus short subprocesses. Nothing in v1 needs a network, a GPU, or another machine.

## What a run gives you

One artifact file, one sha256 digest, one exit code.

| exit | meaning |
|---|---|
| 0 | every assertion held and the grader returned no failures |
| 1 | the run completed but an assertion or the grader failed |
| 2 | the run faulted: a guard tripped, a timer misfired, a tape ran out |
| 3 | the run refused to start: bad case, bad environment, or a second run in one process |
| 4 | a v3 simulation paused cleanly at a checkpoint; it has not completed |

The digest covers the clock, the events, the port calls, the state, and the stop reason. It does not cover the grader or the assertions, so tightening an expectation never changes the digest of the run it grades.

## What the guard does, and what it is not

The guard is what makes the digest mean something. Without it a handler can read the wall clock, draw from an unseeded RNG, or read a file, and the run will still report `passed` while producing a different digest on every replay.

A handler that does any of those faults the run instead:

| leak | fault |
|---|---|
| `socket`, `subprocess`, `os.system`, `ctypes` | `real_io` |
| `time.time`, `time.sleep`, `perf_counter`, `process_time`, `datetime.now` | `real_clock` |
| `random`, `secrets`, `uuid`, `os.urandom` | `unseeded_random` |
| `threading.Thread.start` | `thread` |
| reading a path that is not allowlisted, `os.stat`, `os.listdir`, `os.scandir` | `file_read` |
| writing a path that is not allowlisted, `os.mkdir`, `os.rename`, `os.remove` | `file_write` |

Reads and writes are refused unless the path is allowlisted. Imports may read code files in import directories, not arbitrary data files beside them. Bytecode writing is disabled; a `__pycache__` directory grants no access. Explicitly allowlisted reads are recorded under `fs_reads`, outside the digest. These paths are provenance, not pinned data: use a hashed dataset for simulation inputs. Seam's own file I/O runs inside `seam.guard.trusted()`.

The policy **seals** when the guards install. `allow_read`, `allow_write` and `trusted()` are for a product to call while it is wiring up its runtime; from inside a handler they raise `file_access`, because a handler that could widen its own policy would defeat the point of having one.

**This is not a sandbox.** It refuses the standard-library paths listed above; it does not contain a process. A name bound before `install_guards()` still points at the original object, C extensions can reach libc without going through `ctypes`, and a determined handler can defeat a monkeypatch. Treat the digest as a strong signal for code that follows the rules, not as a security boundary.

Concretely, the guard's own internals — `seam.guard.POLICY` and the private `_read`, `_write`, `_LIVE_TOKENS` behind it — are part of the trusted surface. `read_paths`, `write_paths` and `trusted` are read-only views and cannot be used to widen the policy, which is why they are exposed as properties rather than plain attributes; but code that reaches the private sets can still widen it, and nothing here is designed to stop that. A handler that imports `seam.guard` and mutates `_read` is outside what this library claims to defend.

### What a handler cannot do

These are consequences of the guard, not oversights. A product that hits them needs a small change, and it is better to know up front:

- **Call `logging` from a handler.** `logging` stamps every record with `time.time_ns()`, so a log call faults with `real_clock`. Log through a port instead, or configure a handler that defers its own timestamps.
- **Use `tempfile`.** It draws names from `random`, which faults with `unseeded_random`. Use a port for that.
- **Write a file from a handler**, unless the path is passed to `allow_write` before the run.
- **Import inside a handler and expect the import's own side effects to be visible.** Imports are allowed, but a module that reads the clock or the filesystem at import time will fault, which is the guard working as intended.
- **Start a thread**, or call a live factory in sim mode. Both fault.
- **Call `os.execv` directly.** Blocking `os.fork` is what stops a handler reaching a shell, and it works. But `os.execv` fires only CPython's bare `exec` audit event, which is indistinguishable from the ordinary `exec()` of a Python object — blocking it would break any code that evaluates, including `unittest` itself. A handler that replaces its own process with another one is therefore the one escape this guard cannot close from inside the process. A product would not do this; a determined one could.

The pattern that avoids all of this: a handler should take time, randomness, ids and side effects from `ctx` and its ports. That is the design, and the guard exists to keep a product honest to it.

## Adopting it

Wrap the product's outside world into ports and route its messages through handlers. Then the same code runs live or simulated with no branching in the product:

```python
from seam import Runtime

rt = Runtime(namespace="shop")
rt.port("payments", make_payments_client)   # called only in live mode
rt.on("message", on_message)
rt.start_live()                              # or seam.main(rt) under SEAM_SIM=1
```

Prefer the supervised launcher, which pins hashing, isolates the environment, enforces a deadline, and validates the artifact:

```python
from seam import run_product

result = run_product("myproduct", "case.json", "sim-shop", "out.json", timeout=30)
print(result.returncode, result.artifact["status"])
```

This matters: string hashing is salted per interpreter, so iterating a `set` in a handler gives a different order in every process. That changed the run digest on every replay while the run still reported `passed`. The salt is chosen at start-up and cannot be fixed from inside the process, which is why it belongs to the launcher.

`sim_env` remains a lower-level launcher helper, but inherits the parent environment and does not supervise a process. A caught simulation `Fault` still fails the run. Expected dependency failures use `PortError`, which scripts and tapes can replay without poisoning a recovered run. Async and generator handlers are rejected, not silently discarded.

The checkout proof in `seam/proof/checkout/` preserves the v1 golden digests. The inventory proof in `seam/proof/inventory/` initializes an isolated in-memory SQLite database from a pinned export: two reservations see each other's writes, and each run starts fresh. See [the adoption guide](SIMULATION.md) for the backend API.

The shared-store proof in `seam/proof/shared_store/` routes separate write/read ports through one SQLite world and resumes pending timers in fresh processes. See [worlds and checkpoints](WORLDS.md) for the lifecycle contract and exact-resume limits.
