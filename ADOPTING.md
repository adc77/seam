# Connect one product to Seam

Start with the [copyable starter](examples/starter/README.md). It is a separate product
package with a pinned published SDK dependency. No SDK changes, organization code, live
accounts, or model calls are required. Glass is a fuller lab example; the starter deliberately
keeps the integration small enough to trace from a buyer decision to a product outcome.

This is the frozen developer-v1 integration path, not automatic simulation of arbitrary
unmodified software. You supply the business boundaries and sanitized inputs. Seam supplies
the clock/RNG boundary, isolated dependency execution, actors, evidence, and strict replay.

## 1. Choose one workflow and define success first

Pick a workflow you own and can run locally: one order, booking, support request, or
approval flow. Start with synthetic inputs, two personas, one dependency, and a short
observation window. Do not connect production services to the first simulation.

Write down the observable business result and the states that are not success. In the
starter, `fulfilled` achieves the buyer's goal; `allocated` merely records a stock side
effect. `rejected`, pending work, and an unknown acknowledgement are not fulfillment.
Ending an actor's decisions and passing engine checks are not business success.

Identify the invariant that should hold under failure: the starter cannot allocate the
same item twice after an acknowledgement is lost. Choose a healthy scenario and one
counterfactual that tests the invariant while allowing an honest unsuccessful outcome.

## 2. Put real business behavior behind explicit handlers and ports

Keep existing business logic as the source of truth. The starter's `product.build()`
registers `place`, `reserve`, and `fulfill` once. Live-mode tests and simulated runs execute
those same functions; there is no separately implemented "simulation succeeds" branch.

- Read and update product state through `ctx.state`, `ctx.set_state`, or `ctx.patch`.
- Use `ctx.now()` for integer virtual nanoseconds, `ctx.id` for runtime IDs, and the
  context's RNG for random behavior. Do not read wall time or ambient randomness in handlers.
- Use `ctx.emit` for each outside dependency and `ctx.schedule_after`/`schedule_at` for
  timers. Do not create HTTP clients, threads, files, or sleep inside handlers.
- Register a real live adapter explicitly with `Runtime.port`. A missing live adapter
  must fail loudly, never silently call a synthetic dependency.
- Keep internal timer/retry handlers out of the customer's authorized actions. The
  starter also rejects a premature fulfillment even if a trusted test driver calls it.

The SDK guard detects common boundary leaks; it is not a security sandbox. If the product
uses native code, pre-bound I/O functions, or ambient side effects, audit them yourself.
See [the guard and its limits](README.md#what-the-guard-does-and-what-it-is-not).

## 3. Make each dependency an isolated, restorable world

The starter's `inventory.stock_world(ctx, data, config)` returns a public `World`:
`World(handlers, snapshot, restore)`. Its handlers implement the same port response/error
contract as the live adapter. `World.handlers` is the handler mapping, not a callable world.

Provide one sanitized initial snapshot per world. Return detached snapshots and validate
the complete restored schema, including types, bounds, ownership, and invariants. The
stock world rejects duplicate reservations, over-capacity ledgers, and lost acknowledgements
for orders that were never reserved. Its mutable ledger belongs to one run, not a global
singleton or real shared inventory service.

Model important failure semantics, not just a failure flag. Here a write commits before
the acknowledgement is lost; retrying the same order key returns its existing reservation.
The product bounds retries and retains uncertainty when no acknowledgement is obtained.
This does not establish transaction guarantees for an unrelated live backend.

## 4. Separate persona assumptions from authorization

In `actors.py`, declare a population with stable IDs, arrivals, profiles/goals, private
memory, action capabilities, and bounded decision counts. Synthetic persona prose describes
an assumption; it never grants permissions or proves realistic behavior.

Three product-owned functions define the boundary:

| Function | What your product must own |
| --- | --- |
| `observe(actor, state, at_ns)` | Only facts this actor is allowed to see, not full state or other users' private data |
| `options(request)` | Valid intentions, concrete action parameters, memory changes, and next wake timing |
| `translate(actor, action, decision_id)` | A strict mapping to authorized handlers, rechecking actor/entity ownership |

Install these through `ActorSystem`, including the `choices` mapping. A choice's action
executes immediately; `wake_after_ns` delays only the next decision. A wait choice therefore
has no action. A null wake finishes this actor permanently; only offer it for an observed
terminal outcome. Product timers still own fulfillment, expiry, and retries.

Keep the first controller scripted and label it explicitly. After verifying the product
integration, you can use the [provider-neutral inference controller](INFERENCE_EXPERIMENTS.md)
and `choice_prompt` with an explicit provider, credentials, and limits. Models select one
offered choice, not handler names, arbitrary delays, or private identifiers. Real model
reasoning, costs, and behavioral validation remain separate from the free starter proof.

## 5. Build a closed experiment adapter

The runner loads these declarations from your adapter module:

| Declaration | Starter example |
| --- | --- |
| `MODULE` | `sim_starter.simulation`, the supervised product entry point |
| `ACTOR_WORLDS` | `("actors",)`, actor worlds that must be sealed at the cutoff |
| `METRICS` | Explicit integer outcome names and units, including pending and unsuccessful work |
| `prepare(directory, parameters, seed)` | Validate parameters, prepare hashed inputs, return the new case path |
| `measure(artifact)` | Read actual final product/dependency states, never infer goals from prose |

Use public `capture_bundle` and `write_capture_case` as the starter does; do not copy
private SDK helpers into the product. Preparation runs outside the simulated handler guard.
Validation should reject unknown parameter fields, booleans pretending to be integers,
invalid bounds, and malformed dependency snapshots before creating evidence.

Keep cases, referenced datasets, and checkpoints within the case's directory tree. Declare
same-time event ordering deliberately; the starter uses `timers_first`. Shorten the horizon
as a negative test: the result must retain pending orders and unfinished actors rather than
turning a sealed observation window into false goal completion.

Seeds pair declared scenario inputs. They do not make independent model answers equal,
turn deterministic cohorts into statistical samples, or establish significance. In the
starter, stable buyer ordering fixes the scarce-stock winner; swap declared arrivals/order
if you want to test that assumption, and retain both results.

## 6. Add production-derived inputs only after the synthetic workflow passes

You must implement your product's exporter; `capture_bundle` does not connect to a database
or discover historical state. Use authorized read-only access and a consistent cutoff.
Record the product/configuration snapshot, dependency exports, ordered arrivals, and
pending timer reconstruction required by that workflow. Do not mix today's state with
historical arrivals and call it a historical simulation.

Keep raw exports and credentials outside public repos. Export only what the simulation
needs; actor observations must still omit facts unavailable to that user at that time.
Document missing history or reconstruction assumptions rather than silently inventing it.
Capture checksums pin bytes and source identity, not exporter authenticity or completeness.
See [capture contracts](CAPTURE.md) and [export-backed simulations](SIMULATION.md).

## 7. Test the integration, not just the adapter's happy path

Before calling it usable, run actual handlers and subprocess simulations:

- Healthy and adverse outcomes against the real business functions.
- Live/sim behavior agreement with an explicit test adapter and controlled test timer driver.
- Duplicate requests, commit-before-lost-ack retries, invalid acknowledgements, and exhausted
  retries. Retain uncertainty rather than guessing that the side effect succeeded or failed.
- Observation privacy and forbidden actions, including forged entity IDs and early timing.
- Invalid initial/restore snapshots, cutoff truncation, and checkpoint resume at pending work.
- A normal non-editable package install and runs from outside the source checkout.
- Read-only viewer evidence, paired comparisons, and duplicate exact recorded replay.

The included starter suite exercises these boundaries. Its in-memory live tests are not a
forced-crash durability proof. Your product needs its own persistence/transaction/capture
tests if you claim that live state or timers survive crashes. Glass supplies a fuller example.

SDK/product source and environment identities are intentionally pinned. After changing
code, install into a new environment and create new output directories; preserve the old
environment for old journals. Never erase evidence or relax identity checks to make a new
version replay an old journal. Replay correctness is not calibrated human forecasting.

## 8. Independent tryout: the next acceptance gate

Send someone only the [starter README](examples/starter/README.md), not a preconfigured
environment. They should choose a fresh working folder, install normally, run the demo,
open the viewer, run the included empty-stock scenario, and explain why a passing run
contains a failed buyer goal. Then ask them to sketch the six boundaries for one synthetic
workflow they own. Give help only after recording where they got stuck.

Record their OS/Python version, exact command and first error, working directory, elapsed
setup time, expected/observed outcome, and which instruction they needed. Remove private
data and secrets before sharing logs. Use the included [feedback form](examples/starter/FEEDBACK.md).

Acceptance means an outside developer can complete that workflow and correctly distinguish
engine checks from business success. Our own fresh-copy tests are regression evidence,
not independent user feedback. Prioritize the next SDK change from observed integration
friction; keep the frozen core unchanged unless a real boundary gap requires it.
