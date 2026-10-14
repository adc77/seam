# Seam developer companion

This opt-in package generates the tested synthetic order starter, checks its installed
code and staged inputs, runs rule-mode experiments, and opens the existing evidence
viewer. It does not modify the frozen SDK runtime or integrate arbitrary unmodified
products. No browser, LLM, GPU, credentials, external product service, or production data is used
by these commands. Installation needs Python 3.11+, Git, and internet on macOS/Linux.

## First run from a fresh working folder

Keep this working folder and its environment outside the system temporary directory;
the SDK's cold-import guards deliberately exclude temporary files. Use new folder names:

```sh
git clone https://github.com/adc77/seam.git seam-source
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./seam-source/tools/adoption
seam-dev init orders --name orders
python -m pip install ./orders
seam-dev check --project ./orders
mkdir runs
seam-dev run --project ./orders --out runs/first
seam-dev view --experiment runs/first --check
seam-dev view --experiment runs/first
```

Open the private loopback URL and stop the viewer with Ctrl-C. Run from this working
folder, not inside the cloned source or environment. No editable installs or PYTHONPATH.
The wheel includes its template; generation does not need the cloned repository afterward.
Both companion and generated product install the same immutable published Seam 0.10.0 pin.
Companion version 0.1.0 is separate from the unchanged SDK version.

## What is generated

The project contains actual order handlers, a restorable inventory world, two synthetic
buyers, legal place/wait/finish choices, a simulation entry point, an experiment adapter,
metrics, scenarios, and the original starter's product tests. Distribution, package,
console script, simulation namespace, imports, and module declarations are renamed
together. Product names are 1–24 lowercase letters/digits with single hyphens, starting
with a letter. For example, `--name lab-orders` generates `lab_orders_sim` and
`lab-orders-demo`; it never creates a package shadowing `seam` or a standard library name.

`init` requires a new directory with an existing parent and never overwrites anything.
Partial generation failures retain their directory. Select a new path; do not reset it.
The packaged template is kept byte-for-byte in sync with `examples/starter` by tests;
generated adaptations and documentation are separately tested.

The generated `seam-project.json` pins the initial order-template integration contract.
It is not a generic adapter registry, vendor selector, production exporter, or migration
format. To integrate a different workflow, use the [product integration guide](../../ADOPTING.md).
Extend the companion only when real adoption demonstrates the missing contract.

## Preflight versus execution

`check` uses an isolated interpreter in a neutral working directory, without inherited
credentials or PYTHONPATH. It requires an ordinary product installation, the project's
exact SDK Git pin, and identical source bytes in the copied project and installed package.
It refuses stale installs rather than silently running the old product.

Preflight validates a closed version-1 rule plan and adapter declarations. It runs trusted
product imports and `prepare` for every cell in a disposable directory, then verifies case
containment, namespace, dataset hashes, and snapshot cutoffs. The generated adapter itself
validates its parameters. The companion supports at most 32 cells; core limits are unchanged.
This is trusted synthetic setup code, not a security sandbox or a generic production exporter.

Preflight does not execute product handlers or dependency calls. It does not prove all world
schemas or business outcomes; `run` is the additional supervised simulation proof. Custom
product preparation must not contact live systems or do irreversible work. A malicious
import/prepare function can perform ambient I/O; do not run untrusted project code.

`run` repeats preflight, prepares a fresh experiment, and uses the public SDK runner.
The default plan has healthy/scarce variants and paired seeds 7/19. Per seed, healthy
fulfills two orders; scarce fulfills one and rejects one. A rejected buyer is a failed
goal even though the engine checks pass. These fixtures do not establish realistic agents
or statistically meaningful population effects. Every experiment requires a new output.

```sh
seam-dev check --project ./orders --plan scenario-empty.json
seam-dev run --project ./orders --plan scenario-empty.json --out runs/empty
seam-dev view --experiment runs/empty --check
```

Alternative plan files are relative to the project and must stay inside it. Version-2
inference plans are explicitly refused: this CLI cannot silently dispatch a provider.
For the separate scripted external-decision proof, including eight offline recorded replays:

```sh
orders-demo --scripted --out runs/recorded
seam-dev view --experiment runs/recorded/experiment --check
```

This is still scripted, not an LLM, and its zero usage counters are synthetic placeholders.
Actual providers remain explicit through Seam's existing inference API; no vendor is
certified or automatically selected by this companion.

## Edits, tests, and evidence

After editing generated Python code, use a new environment with the companion and the
new product installed. Preserve old environments for old journals. Do not erase evidence,
change SDK pins, or relax hashes to make a changed product replay an old run. Scenario
JSON changes do not require reinstalling unchanged Python code, but do require a new output.

```sh
cd orders
python -m unittest discover -s tests -t . -v
```

Companion contributors run its own unit and ordinary-install integration tests:

```sh
cd seam-source/tools/adoption
python -m unittest discover -s tests -t . -v
```

Tests build and install a companion wheel into a separate environment outside system
temporary storage, generate independently renamed projects, install normally, run actual
simulations and recorded replays, check stale installs/refusals, and verify read-only viewer
inspection. The independent human tryout remains outstanding; author-run tests are not it.
Glass's browser workbench/bridge, goal exploration, larger populations, and production
exporter improvements are later milestones, not features implemented by this release.
