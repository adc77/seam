# Local simulation evidence viewer

`seam_viewer` is an optional, standard-library companion packaged with Seam. It reads completed experiment evidence without importing product adapters, rerunning simulations, calling providers, or changing experiment evidence. It does not turn synthetic personas into calibrated forecasts.

## Start locally

Install the checkout normally, then select the folders you want to inspect:

```sh
python -m pip install /path/to/seam
python -m seam_viewer \
  --experiment Glass=/path/to/glass-experiment \
  --experiment Reservations=/path/to/reservation-experiment
```

Open the printed `http://localhost:<port>/v/<session-token>/` URL. The default port is assigned automatically; `--port 8767` requests a fixed local port. Stop with Ctrl+C. There is no remote bind option, automatic browser launch, filesystem discovery, upload, or write endpoint. Each startup has a new session URL; do not share it or expose the service through a tunnel/proxy.

The companion lives outside the `seam` runtime package. Adding it does not change the runtime's Python-source hash or require consumer pin updates. Existing SDK/product source identities and checkpoint/capture restrictions remain in force. The viewer can inspect old completed experiment reports without importing their old products or weakening those execution restrictions.

## What you can inspect

- Experiment selection, declared variants/parameters, paired seeds, baseline, and pinned source/interpreter/environment identity.
- Successful/failed run counts, declared metric units, exact rational means, per-seed differences, and excluded pairs. Partial comparisons remain labeled incomplete; failed runs never become zero-valued outcomes.
- Individual personas: policy mode, profile assumptions, allowed actions, initial/current private memory, arrivals, pending wakeups, and recorded decision requests.
- Each decision's exact observation, memory before/after, accepted action, translated product delivery, and RNG counters. The viewer does not invent a human rationale or expose a claimed observation assembled from the full product database.
- Recorded events in artifact order, kind filters, search through recorded event details, pagination, and port calls linked by their exact `during` event. Driver-event links require a unique recorded match; equal timestamps alone do not establish causality.
- Final product state, assertion results, faults/grader failures, timer outcomes, and raw private dependency/actor worlds.

World sealing ends the observation window; it does not mean every persona completed its goal. A persona with a pending wakeup is explicitly unfinished, including one that arrives after the horizon. Non-actor experiments still expose their timeline, state, assertions, and failures.

Raw state/world inspection is private diagnostic evidence, not an actor-visible observation. Selected files can contain sensitive profiles or product data. Exporters and the operator remain responsible for sanitizing them before sharing.

## Integrity and access boundaries

The viewer uses `read_experiment` to validate the completed report, retained files, cell records, and recomputed statistics. Missing or partial summaries are unavailable, not successful. One unavailable experiment remains visible without hiding other valid selections.

An active session pins its summary and manifest. Run requests hash the exact bytes read for the selected cell, artifact, case, and datasets against retained evidence. Modified evidence is refused, never silently reinterpreted as the original run. **Recheck evidence** validates all selected reports/files again. If an experiment was incomplete or invalid at startup, restart the viewer after it becomes complete; it is not a live-run monitor.

HTTP serves only bundled assets and fixed experiment/run APIs behind the random session path. It rejects foreign Host/Origin headers, cross-site fetches, path/query browsing, and write methods. Responses are uncached, forbid framing, carry a strict same-origin content policy, and do not include raw filesystem paths. The UI uses text nodes for evidence; no HTML interpolation, remote fonts/scripts, or browser-side provider keys.

All structured integer values cross the HTTP/JavaScript boundary as decimal strings, including seeds, nanoseconds, and wide aggregate sums. Raw JSON inspector blocks preserve the original numeric types. The UI shows exact fractions rather than rounding away large values or implying statistical significance.

Checksums detect changed evidence; they are not signatures or proof of exporter authenticity. The standard-library server is for one trusted local operator, not multi-user production hosting or hostile-filesystem isolation. Any local user who can access the selected files already has their contents. Do not load untrusted directories or introduce unreviewed provider secrets into evidence.

## Bounds and limitations

- Select 1–16 uniquely labeled experiment directories; labels begin with a letter and contain at most 64 letters, digits, spaces, underscores, or hyphens.
- Summary/metadata reads are bounded by the experiment report limit (16 MiB); individual artifacts/inputs by 32 MiB. Selected-cell reads and encoded HTTP responses each have a 64 MiB limit. Oversized evidence/responses are refused explicitly.
- The single-request local server verifies reports synchronously. Large ensembles may take time to recheck; this is not a concurrent analytics service.
- The viewer reads completed experiment folders only. It does not inspect arbitrary standalone artifacts or active inference journals, resume experiments, edit scenarios, launch jobs, migrate journals, or create checkpoint forks.
- Completed rule-mode and [model-driven](INFERENCE_EXPERIMENTS.md) ensembles are supported. Model-mode views include exact trajectory populations, accepted costs, unresolved/rejected cost holds, and retained request/response/reservation evidence. Live-provider certification remains separate work.

## Verification

Verify evidence without starting HTTP:

```sh
python -m seam_viewer --experiment Glass=/path/to/experiment --check
```

Exit 0 means all selected reports are available; exit 1 means at least one is incomplete/invalid; exit 2 means invalid CLI/configuration. A completed experiment with failed cells can still be valid evidence: check its displayed status and pair exclusions.

Run the actual installed-process proof from outside the checkout:

```sh
env -u PYTHONPATH python -m seam_viewer.proof --out /path/to/new-proof --installed
```

It executes six SDK counter cells, starts the installed viewer in a fresh process, fetches all packaged assets and persona/timeline APIs, checks denied writes/path browsing, and requires unchanged evidence hashes. CI repeats this alongside the full suites, installed SDK experiments, and inference proofs on Linux/macOS with Python 3.11, 3.13, and 3.14. Browser QA should additionally exercise real product reports, selection, decision inspection, event search/pagination, failed/incomplete views, and narrow-screen layouts.
