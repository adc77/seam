# Connect a product to Seam

This is a copyable integration example, not an SDK extension or production service.
It imports the published Seam dependency, not Glass, Reservations, or SDK proof products.
All inputs are synthetic. No credentials, GPU, API keys, or live data are needed.

## Run the untouched starter

Python 3.11+ and Git are required on macOS/Linux. Installation needs internet; execution
is offline. Start in an empty working folder outside the system temporary directory;
all folder names below must be new:

```sh
git clone https://github.com/adc77/seam.git seam-source
cp -R seam-source/examples/starter my-product
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./my-product
mkdir runs
starter-demo --scripted --out runs/first
python -m seam_viewer --experiment "Orders=runs/first/experiment" --check
```

Keep running from this working folder, not inside `.venv`. The source checkout is only
used to copy the example; installation fetches the exact published SDK pin in
`pyproject.toml`. Do not add the Seam checkout to `PYTHONPATH` or install it editable.
The copied product works independently of that checkout. Results stay in `runs`.
Choose a new output such as `runs/second` for each run; the parent must exist.

The controller explicitly selects the first legal choice. **It is scripted, not an LLM.**
Zero token/cost counters are synthetic placeholders, not a token measurement. The demo
uses the SDK's durable external-decision protocol so later providers use the same boundary.
It is an integration proof, not a reasoning benchmark or forecast of human behavior.

## What should happen

| Condition, per seed | Fulfilled orders | Rejected orders | Allocated items |
| --- | ---: | ---: | ---: |
| Healthy: two items | 2 | 0 | 2 |
| Scarce: one item, first acknowledgement lost | 1 | 1 | 1 |

The two seeds are 7 and 19. The demo runs four isolated cells, checks product-defined
outcomes and viewer evidence, and replays each recorded decision tape twice with no
provider calls. Every replay pair must be byte-identical. Progress and a readable outcome
summary appear on stderr; stdout remains JSON. Failure exits nonzero and keeps evidence.

Reservation and fulfillment are distinct. A timer fulfills a confirmed order only after
two virtual seconds. A lost acknowledgement retries after one second using the same order
key; it does not consume another item. Two reservation timeouts leave an explicit `unknown`
outcome, not an invented success or rejection. A rejected buyer finishes decisions but
does not achieve their goal. Both scenarios still pass the software checks.

The same registered product handlers run with an explicit adapter in live-mode tests and
an isolated inventory world in simulation. No production endpoint is contacted. Stable
buyer ordering determines the scarce-stock winner; paired seeds do not add behavioral
variation or statistical confidence to this deterministic fixture.

Copy the fully quoted viewer command printed by the demo to run it from any folder,
without activating the environment. Open its private loopback URL; stop with Ctrl-C.
Do not share raw evidence containing private product data or credentials.

## Read and change the product

| File | Your integration responsibility |
| --- | --- |
| `sim_starter/product.py` | Existing handlers, business state, real port wiring, retry limits, timers |
| `sim_starter/inventory.py` | Isolated dependency snapshot, side effects, idempotency, restore validation |
| `sim_starter/actors.py` | Persona assumptions, allowed observations, legal choices, action translation |
| `sim_starter/simulation.py` | Simulation entry point that installs actors on the ordinary product runtime |
| `sim_starter/experiments.py` | Cutoff/configuration, hashed datasets, variants, metric units and outcome meanings |
| `sim_starter/demo.py` | Explicit controller and end-to-end verification, not business logic |

Start with one scenario change: the included `scenario-empty.json` changes the scarce
capacity from `1` to `0`, keeping the other parameters identical. Both buyers should be
rejected. The demo intentionally verifies the untouched fixture, so use the general runner
for altered scenarios instead of weakening the proof's assertions. From your working folder:

```sh
python -m seam.experiments --plan my-product/scenario-empty.json --out runs/changed
python -m seam_viewer --experiment "Orders=runs/changed" --check
```

This runner uses the rule policy in `actors.py` and no external provider. Inspect the
`fulfilled`, `rejected`, `pending`, `ack_unknown`, and `unfinished_actors` metrics;
`status: passed` only says the declared engine/assertion checks passed. A shorter horizon
can leave allocated orders pending with unfinished actors; allocation is not fulfillment.
After any source edit, reinstall the product and start a new output. Preserve the old
installed environment if you need its old journals; this example has no migration layer.

For your real product, follow the [integration guide](https://github.com/adc77/seam/blob/main/ADOPTING.md).
Rename the Python package and update `MODULE`, the adapter path, script entry point,
capture module, test imports, and namespace together. Do not put organization code or
credentials into this public example. Prefer a tiny synthetic workflow you own.

## Test your copy

With the environment still active, run from `my-product`:

```sh
cd my-product
python -m unittest discover -s tests -t . -v
```

Tests execute actual product handlers and supervised simulations, including live/sim
agreement, finite stock, duplicate orders, lost/invalid acknowledgements, unknown outcomes,
observation privacy, forbidden/early actions, invalid snapshots, pending work at cutoff,
checkpoint resume, demo replay, and the viewer command from another folder with spaces.
Tests use the installed pinned SDK; no sibling source override is needed.

## Limits

This is an in-memory synthetic order product, not a web API, crash-durable order service,
inventory allocator for production, or security sandbox. Live storage transactions,
authentication, reconciliation of uncertain reservations, notification delivery, release of
stock, and production capture export are product-specific work not implemented here.
Only two buyers and 0–2 items are supported. Internal `reserve`/`fulfill` handlers must
not be exposed as customer actions. SDK replay correctness does not certify live-adapter
semantics, realistic agents, statistical significance, or cross-version journal compatibility.
