# Offline experiments

Seam 0.7 adds an optional `seam.experiments` controller. It runs the product's existing simulation entry point once per variant/seed, with fresh isolated inputs and a supervised subprocess. It has no model client, paid calls, domain branches, checkpoint forks, or mandatory dependencies.

## Try the installed proof

From a neutral directory with an installed SDK and an existing output parent:

```sh
python -m seam.proof.experiments --out /path/to/new-experiment --installed
```

This compares two synthetic counter goals over three seeds. Six actual product runs produce a manifest, per-run evidence, and a checked summary. Repeating the same inputs in a different new directory produces the same results under the pinned execution contract.

## Declare an experiment

```python
from seam.experiments import plan, read_experiment, run_experiment

spec = plan(
    "myproduct.experiments",
    [
        {"name": "baseline", "parameters": {"hold_ns": 10}},
        {"name": "long_hold", "parameters": {"hold_ns": 30}},
    ],
    [7, 19, 31],
    baseline="baseline",
    timeout_s=30,
)
summary = run_experiment("/path/to/new-experiment", spec)
verified = read_experiment("/path/to/new-experiment")
assert verified == summary
```

The closed plan format is `seam-experiment`, version 1, with exactly `format`, `version`, `adapter`, `variants`, `seeds`, `baseline`, and `timeout_s`. Parameters are canonical Seam JSON without floats. Variant names are unique lowercase identifiers; baseline names an existing variant. Seeds are distinct integers in `[0, 2**63-1]`, excluding booleans. At most 32 variants and 1,000 total runs are accepted. Each subprocess timeout is an integer from 1 to 300 seconds. Runs are sequential; worst-case runtime scales with the declared run count and timeout. Plans are limited to 1 MiB.

The CLI accepts the same JSON plan:

```sh
python -m seam.experiments --plan experiment-plan.json --out /path/to/new-experiment
```

Exit 0 means every cell passed; exit 1 means a completed experiment contains failed cells; exit 2 means invalid input or an interrupted execution contract. Existing output directories are refused rather than overwritten. There is no automatic resume or retry.

## Product adapter

The configured module is application-owned trusted Python. It exports:

```python
MODULE = "myproduct.simulation"
ACTOR_WORLDS = ("actors",)
METRICS = {"confirmed": "reservations", "held_ns": "capacity_ns"}

def prepare(directory, parameters, seed):
    # Validate parameters, create the requested directory, and return its case path.
    ...

def measure(artifact):
    # Return exactly the declared scalar metrics from a copied passed artifact.
    ...
```

Both functions must be synchronous. Preparation must create a new case and all referenced datasets beneath the supplied cell directory. Cases cannot resume, pause at checkpoints, or read external tapes. Every case uses the supplied product seed. Actor datasets must use rule mode and the same supplied actor seed. Ordinary dataset hashes and historical cutoffs still apply. Floats, booleans, collections, missing metrics, and extra metrics are refused: each metric is a signed 64-bit integer with an explicit unit. Rates and distributions should be declared as count/sum metrics or explicitly scaled integers, not silently converted to floats.

`ACTOR_WORLDS` lists the worlds that must be sealed before metrics can be accepted. It must match worlds using actor datasets; sealing cannot be omitted. Use an empty tuple for non-actor products. Preparation, parameter semantics, snapshot selection, ownership visibility, and faithful metric definitions belong to the adapter. Adapters must not connect to real services or reuse a mutable live database. The controller hashes declared files and supervises product execution; preparation and measurement themselves are trusted code, not sandboxed or separately timed out.

Matching seeds do not by themselves establish matching populations or causal isolation. Adapters should use the same generation recipe and draw ordering for paired variants, changing only declared parameters. Compare stored inputs to confirm this. Glass and the independent local Reservations proof demonstrate that contract without domain-specific branches in Seam.

Only `PYTHONPATH` may be explicitly supplied via `env`. The product subprocess receives the runner's minimal environment, not provider credentials. The parent must already be able to import the adapter. The manifest records product/adapter/SDK Python-source hashes, interpreter, environment hash, units, required actor worlds, and the complete plan. It does not pin native dependencies or authenticate trusted code.

## Results and comparisons

Each variant/seed has its own input directory, artifact, and result record. A failed cell retains an explicit status and error code; product failures also retain the supervised artifact and return code. No metrics are extracted from faulted, refused, paused, or unsealed runs. SDK/source/environment changes abort the experiment without a completed summary.

`summary.json` contains:

- Per-cell statuses, metrics, relative artifact paths, behavioral digests, and artifact hashes.
- Per-variant successful/failed counts and successful-cell metric count, sum, minimum, maximum, and exact mean.
- Paired `variant - baseline` deltas for the same seed, with explicit included and excluded seed lists.
- An `incomplete` comparison status if either side of any pair failed.
- File-evidence hashes and a summary checksum.

Means are exact `{ "numerator": sum, "denominator": count }` objects; an empty mean is null. Aggregate sums and differences can exceed signed 64-bit even though individual metrics cannot. No confidence intervals, significance tests, best-variant ranking, or prediction guarantees are implied. Successful-cell aggregates and partial paired differences are descriptive and may be biased by failures; an incomplete comparison must not be presented as a conclusive result.

`read_experiment` checks the report checksum, retained evidence, per-cell records, and recomputed statistics without importing the adapter or launching a product. Controller reports are limited to 16 MiB. Hashes detect accidental changes, not deliberate forgery by the directory owner. Evidence directories are mode 0700; controller metadata is 0600. The product artifact retains the normal runner's permissions within that private directory.

The summary is written only after all cells are processed and retained evidence is rechecked. A killed controller leaves its manifest and any completed cell records; a missing, partial, or invalid summary is incomplete, never a success. There is no journal migration, checkpoint-based fork, concurrent scheduling, disk quota, or incremental resume in this v1. Keep old source environments for existing service journals and captures; this SDK upgrade does not relax their identity checks.

These experiments explore synthetic assumptions. Predictive claims require separate behavioral calibration and holdout validation. The optional [viewer](VIEWER.md) reads completed evidence; the separate [model-mode controller](INFERENCE_EXPERIMENTS.md) adds version 2 inference ensembles without changing this version 1 contract. Real-provider testing and checkpoint forks remain separate work.
