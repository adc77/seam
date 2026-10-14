"use strict";

const byId = (id) => document.getElementById(id);
const model = {
  experiments: [],
  experiment: null,
  run: null,
  runIndex: 0,
  tab: "personas",
  workspace: "overview",
  request: 0,
};
const pageSize = 50;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function button(text, callback, className) {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", callback);
  return element;
}

function pill(status, label = status) {
  return node("span", label.toUpperCase(), `pill ${status}`);
}
function clear(element) {
  element.replaceChildren();
}
function jsonBlock(parent, title, value) {
  const section = node("section");
  section.append(node("h3", title), node("pre", value));
  parent.append(section);
}
function disclosure(parent, title, value) {
  const details = node("details");
  details.append(node("summary", title), node("pre", value));
  parent.append(details);
}
function ns(value) {
  return value === null ? "none" : metricValue(value, "ns");
}
function notice(text, error = false) {
  clear(byId("notice"));
  if (text)
    byId("notice").append(node("div", text, error ? "notice error" : "notice"));
}
async function api(route) {
  const response = await fetch(`api/${route}`, {
    cache: "no-store",
    credentials: "omit",
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}
function table(headers) {
  const wrap = node("div", undefined, "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "Scrollable evidence table");
  const element = node("table");
  const head = node("thead");
  const row = node("tr");
  headers.forEach((title) => {
    const cell = node("th", title);
    cell.scope = "col";
    row.append(cell);
  });
  head.append(row);
  const body = node("tbody");
  element.append(head, body);
  wrap.append(element);
  return { wrap, body };
}
function fieldRow(body, values) {
  const row = node("tr");
  values.forEach((value, index) =>
    row.append(node("td", value, index ? "numeric" : undefined)),
  );
  body.append(row);
  return row;
}

async function refresh() {
  const generation = ++model.request;
  notice("Verifying selected evidence…");
  byId("evidence-health").textContent = "VERIFYING EVIDENCE";
  byId("evidence-health").className = "pill neutral";
  try {
    const response = await api("experiments");
    if (generation !== model.request) return;
    model.experiments = response.experiments;
    notice("");
    const previous = model.experiment && model.experiment.id;
    selectExperiment(
      response.experiments.find((item) => item.id === previous) ||
        response.experiments[0],
    );
  } catch (error) {
    if (generation !== model.request) return;
    model.run = null;
    byId("run-section").hidden = true;
    clear(byId("overview"));
    byId("evidence-health").textContent = "EVIDENCE UNAVAILABLE";
    byId("evidence-health").className = "pill failed";
    notice(`Evidence unavailable: ${error.message}`, true);
  }
}

function navigation() {
  const container = byId("experiments");
  clear(container);
  model.experiments.forEach((item) => {
    const control = button(
      "",
      () => selectExperiment(item),
      "experiment-button",
    );
    control.setAttribute(
      "aria-current",
      String(item.id === model.experiment.id),
    );
    const label = node("div", item.label);
    label.append(
      node(
        "small",
        item.available
          ? `${item.runs.length} isolated runs`
          : "Evidence unavailable",
      ),
    );
    control.append(
      label,
      node("span", item.available && item.status === "passed" ? "OK" : "!"),
    );
    container.append(control);
  });
}

function selectExperiment(experiment) {
  ++model.request;
  model.experiment = experiment;
  model.run = null;
  model.runIndex = 0;
  if (!experiment) {
    clear(byId("experiments"));
    clear(byId("overview"));
    byId("run-section").hidden = true;
    notice("No selected experiment evidence is available.", true);
    byId("evidence-health").textContent = "NO EVIDENCE";
    byId("evidence-health").className = "pill neutral";
    return;
  }
  navigation();
  clear(byId("overview"));
  byId("run-section").hidden = true;
  if (!experiment.available) {
    byId("evidence-health").textContent = "EVIDENCE UNAVAILABLE";
    byId("evidence-health").className = "pill failed";
    notice(
      `Cannot inspect ${experiment.label}: ${experiment.error}. ${experiment.error_hint}`,
      true,
    );
    return;
  }
  notice("");
  byId("evidence-health").textContent = "SUMMARY VERIFIED";
  byId("evidence-health").className = "pill passed";
  renderOverview(experiment);
  const select = byId("run-select");
  clear(select);
  experiment.runs.forEach((row, index) => {
    const option = node(
      "option",
      `${row.variant} · repeat ${row.seed} · run checks ${row.status}`,
    );
    option.value = String(index);
    select.append(option);
  });
  activateWorkspace(model.workspace);
  if (!experiment.runs.length) {
    byId("run-status").textContent = "No runs recorded in this experiment.";
    return;
  }
  selectRun(0);
}

function renderOverview(experiment) {
  const panel = node("section", undefined, "panel");
  const heading = node("div", undefined, "panel-heading");
  const title = node("div");
  title.append(
    node("div", "SELECTED EXPERIMENT", "eyebrow"),
    node("h2", experiment.label),
  );
  heading.append(
    title,
    pill(experiment.status, `Run checks ${experiment.status}`),
  );
  panel.append(heading);
  const counts = node("div", undefined, "summary-grid");
  const passed = experiment.runs.filter(
    (row) => row.status === "passed",
  ).length;
  [
    [experiment.plan.variants.length, "Scenarios"],
    [experiment.plan.seeds.length, "Input repeats"],
    [passed, "Run checks passed"],
    [experiment.runs.length - passed, "Run checks failed"],
  ].forEach(([value, label]) => {
    const card = node("div", undefined, "summary-card");
    card.append(node("strong", value), node("span", label));
    counts.append(card);
  });
  renderScenarioResults(panel, experiment);
  panel.append(
    node(
      "p",
      experiment.inference
        ? controllerDescription(experiment.inference.provider)
        : "Recorded policy decisions · this viewer makes no model calls",
      "controller-label",
    ),
  );
  renderRunList(panel, experiment);
  const advanced = node("details", undefined, "advanced-report");
  advanced.append(
    node(
      "summary",
      "Advanced report · provider usage, metrics, and reproducibility",
    ),
  );
  advanced.append(counts);
  if (experiment.inference) {
    const usage = experiment.inference;
    advanced.append(
      node("h3", controllerDescription(usage.provider)),
      node(
        "p",
        `${usage.provider} · ${usage.model} · ${usage.reserved_calls} / ${usage.budget.max_calls} call reservations`,
      ),
      node(
        "p",
        `Accepted cost: ${usage.accepted_cost_microusd} micro-USD · Held for unresolved/rejected calls: ${usage.held_cost_microusd} micro-USD · Limit: ${usage.budget.max_cost_microusd} micro-USD`,
      ),
      node(
        "p",
        "Reservations are conservative dispatch allowances, not invoices. Matching seeds pair scenario inputs, not independent model outputs. Replay uses recorded decisions without inference.",
        "comparison-intro",
      ),
    );
  }
  const metadata = node("div", undefined, "metadata");
  metadata.append(
    node("span", `Adapter: ${experiment.plan.adapter}`),
    node("span", `Baseline: ${experiment.plan.baseline}`),
    node("span", `Verified summary: ${experiment.summary_sha256.slice(0, 12)}`),
  );
  advanced.append(
    metadata,
    node(
      "p",
      "Engine checks do not establish business success or persona goal completion. Differences are variant − baseline for the same seed; failed pairs are excluded. Approximate decimals are marked ≈; exact values are retained below. These are observations, not statistical significance.",
      "comparison-intro",
    ),
  );
  const metrics = node("details");
  metrics.append(node("summary", "Scenario outcome metrics · variant means"));
  const variants = experiment.plan.variants.map((variant) => variant.name);
  const keys = metricKeys(experiment.units);
  const totals = table(["Metric / mean", ...variants]);
  keys.forEach((key) => {
    const unit = experiment.units[key];
    const row = fieldRow(totals.body, [
      `${metricLabel(key)} (${unit === "ns" ? "seconds" : unit})`,
      ...variants.map((name) =>
        metricValue(experiment.aggregates[name].metrics[key].mean, unit),
      ),
    ]);
    row.title = JSON.stringify(
      Object.fromEntries(
        variants.map((name) => [
          name,
          experiment.aggregates[name].metrics[key].mean,
        ]),
      ),
    );
  });
  metrics.append(totals.wrap);
  disclosure(
    metrics,
    "Exact aggregate statistics (original units)",
    JSON.stringify(experiment.aggregates, null, 2),
  );
  advanced.append(metrics);
  Object.entries(experiment.comparisons).forEach(([name, comparison]) => {
    const label = node("div", undefined, "comparison-heading");
    label.append(
      node("h3", `${name} versus ${comparison.baseline}`),
      pill(comparison.status, `Pairing ${comparison.status}`),
    );
    advanced.append(label);
    advanced.append(
      node(
        "p",
        `Included seeds: ${comparison.paired_seeds.join(", ") || "none"} · Excluded seeds: ${comparison.excluded_seeds.join(", ") || "none"}`,
        "comparison-intro",
      ),
    );
    const details = node("details");
    details.append(
      node("summary", "Paired metric differences · variant minus baseline"),
    );
    const deltas = table([
      "Metric",
      "Mean difference",
      "Minimum",
      "Maximum",
      "Paired count",
    ]);
    keys.forEach((key) => {
      const stats = comparison.delta[key];
      const unit = experiment.units[key];
      const row = fieldRow(deltas.body, [
        metricLabel(key),
        metricValue(stats.mean, unit),
        metricValue(stats.min, unit),
        metricValue(stats.max, unit),
        stats.count,
      ]);
      row.title = JSON.stringify(stats);
      row.firstChild.append(
        node("span", unit === "ns" ? "seconds" : unit, "metric-unit"),
      );
    });
    details.append(deltas.wrap);
    disclosure(
      details,
      "Exact difference statistics (original units)",
      JSON.stringify(comparison.delta, null, 2),
    );
    advanced.append(details);
    disclosure(
      advanced,
      "Per-seed paired differences",
      experiment.pairs_json[name],
    );
  });
  disclosure(
    advanced,
    "Declared scenario parameters and seeds",
    experiment.plan_json,
  );
  disclosure(
    advanced,
    "Pinned source, SDK, interpreter, and environment identity",
    experiment.identity_json,
  );
  panel.append(advanced);
  byId("overview").append(panel);
}

function renderScenarioResults(parent, experiment) {
  const section = node("section", undefined, "scenario-results");
  section.id = "scenario-results";
  section.append(
    node(
      "h3",
      Object.hasOwn(experiment.units, "preferred_outcome_achieved")
        ? "Did the product achieve its goal?"
        : "What did the product report?",
    ),
  );
  section.append(
    node(
      "p",
      "A passed run check means the recorded run passed its checks. It does not mean the product achieved its goal.",
      "result-explanation",
    ),
  );
  const grid = node("div", undefined, "scenario-grid");
  const keys = headlineMetricKeys(experiment.units);
  for (const variant of experiment.plan.variants) {
    const card = node("article", undefined, "scenario-card");
    card.append(node("h3", variant.name));
    const stats = experiment.aggregates[variant.name];
    card.append(
      node(
        "p",
        `${stats.passed} checked runs · ${stats.failed} failed checks`,
        "scenario-checks",
      ),
    );
    for (const key of keys) {
      const row = node("div", undefined, "result-metric");
      const label = node("span", friendlyMetricLabel(key));
      label.append(
        node(
          "small",
          experiment.units[key] === "ns"
            ? "seconds per run"
            : `${experiment.units[key]} per run`,
          "metric-unit",
        ),
      );
      row.append(
        label,
        node(
          "strong",
          metricValue(stats.metrics[key].mean, experiment.units[key]),
        ),
      );
      card.append(row);
    }
    card.append(
      node(
        "p",
        "Values are averages across recorded runs. Missing outcomes are not counted as zero.",
        "scenario-note",
      ),
    );
    grid.append(card);
  }
  section.append(grid);
  parent.append(section);
}

function renderRunList(parent, experiment) {
  parent.append(node("h3", "Recorded runs"));
  const filters = node("div", undefined, "run-filters");
  const searchLabel = node("label", "Search scenario or repeat ID");
  const search = node("input");
  search.type = "search";
  search.id = "run-search";
  search.placeholder = "Filter recorded runs…";
  searchLabel.append(search);
  const statusLabel = node("label", "Run check status");
  const status = node("select");
  status.id = "run-filter";
  ["all", "passed", "failed"].forEach((value) => {
    const option = node("option", value === "all" ? "All statuses" : value);
    option.value = value;
    status.append(option);
  });
  statusLabel.append(status);
  filters.append(searchLabel, statusLabel);
  parent.append(filters);
  const keys = headlineMetricKeys(experiment.units);
  const runs = table([
    "Scenario / repeat ID",
    "Run checks",
    ...keys.map(friendlyMetricLabel),
    "Explore",
  ]);
  parent.append(runs.wrap);
  const count = node("p", "", "comparison-intro");
  parent.append(count);
  function draw() {
    clear(runs.body);
    let visible = 0;
    experiment.runs.forEach((row, index) => {
      if (
        !`${row.variant} ${row.seed}`
          .toLowerCase()
          .includes(search.value.trim().toLowerCase()) ||
        (status.value !== "all" && row.status !== status.value)
      )
        return;
      visible += 1;
      const item = node("tr");
      item.dataset.runIndex = String(index);
      item.append(node("td", `${row.variant} / ${row.seed}`, "numeric"));
      const check = node("td");
      check.append(pill(row.status, row.status));
      item.append(check);
      keys.forEach((key) =>
        item.append(
          node(
            "td",
            metricValue(row.metrics?.[key], experiment.units[key]),
            "numeric",
          ),
        ),
      );
      const control = node("td");
      const inspect = button(
        "Explore run",
        () => {
          activateWorkspace("inspector");
          selectRun(index);
        },
        "event-button",
      );
      inspect.setAttribute(
        "aria-label",
        `Explore ${row.variant} repeat ${row.seed}`,
      );
      control.append(inspect);
      item.append(control);
      runs.body.append(item);
    });
    count.textContent = `${visible} of ${experiment.runs.length} recorded runs. Engine checks and product outcomes are separate verdicts.`;
  }
  search.addEventListener("input", draw);
  status.addEventListener("change", draw);
  draw();
}

async function selectRun(index) {
  const generation = ++model.request;
  model.runIndex = index;
  model.run = null;
  byId("run-select").value = String(index);
  ["personas", "timeline", "state", "run-status"].forEach((id) =>
    clear(byId(id)),
  );
  byId("run-status").append(node("p", "Verifying run inputs and artifact…"));
  try {
    const run = await api(`experiments/${model.experiment.id}/runs/${index}`);
    if (generation !== model.request) return;
    model.run = run;
    byId("evidence-health").textContent = "RUN VERIFIED";
    byId("evidence-health").className = "pill passed";
    renderRun(run);
    activateTab(model.tab);
  } catch (error) {
    if (generation !== model.request) return;
    byId("evidence-health").textContent = "RUN UNAVAILABLE";
    byId("evidence-health").className = "pill failed";
    notice(
      `Cannot verify this run: ${error.message}. Recheck evidence before relying on the displayed comparison.`,
      true,
    );
    clear(byId("run-status"));
    byId("run-status").append(
      node(
        "div",
        `Run unavailable: ${error.message}. Recheck evidence before trusting this report.`,
        "notice error",
      ),
    );
  }
}

function renderRun(run) {
  const row = run.row;
  byId("run-title").textContent = `${row.variant} · repeat ${row.seed}`;
  clear(byId("run-status"));
  const status = node("div", undefined, "metadata");
  status.append(pill(row.status, `Engine checks ${row.status}`));
  if (run.artifact) {
    status.append(
      node(
        "span",
        `Engine ${run.artifact.status} · stop: ${run.artifact.stop_reason}`,
      ),
      node("span", `Artifact SHA-256: ${row.artifact_sha256.slice(0, 12)}`),
    );
  }
  if (row.error) status.append(node("span", row.error));
  if (row.inference) {
    status.append(
      node(
        "span",
        `${row.inference.reserved_calls} inference call reservations · ${row.inference.accepted_cost_microusd} accepted micro-USD · ${row.inference.held_cost_microusd} held micro-USD`,
      ),
    );
  }
  const provenance = node("details");
  provenance.append(
    node("summary", "Run checks and technical identity"),
    status,
  );
  if (row.metrics) {
    const results = node("div", undefined, "run-result-strip");
    for (const key of headlineMetricKeys(model.experiment.units)) {
      const card = node("div");
      card.append(
        node("span", friendlyMetricLabel(key)),
        node(
          "strong",
          metricValue(row.metrics[key], model.experiment.units[key]),
        ),
      );
      results.append(card);
    }
    byId("run-status").append(
      results,
      node(
        "p",
        "These are the product's recorded outcomes. Checks passing and persona decisions ending do not establish goal success.",
        "result-explanation",
      ),
    );
    const outcomes = table(["Metric", "Observed value"]);
    metricKeys(model.experiment.units).forEach((key) => {
      const unit = model.experiment.units[key];
      const item = fieldRow(outcomes.body, [
        `${metricLabel(key)} (${unit === "ns" ? "seconds" : unit})`,
        metricValue(row.metrics[key], unit),
      ]);
      item.title = `${key}: ${row.metrics[key]} ${unit}`;
    });
    const metrics = node("details");
    metrics.append(node("summary", "All observed run metrics"), outcomes.wrap);
    byId("run-status").append(metrics);
  }
  byId("run-status").append(provenance);
  if (run.inference_json)
    disclosure(
      byId("state"),
      "Decision-provider requests, responses, and reservations (private evidence)",
      run.inference_json,
    );
  if (!run.artifact) {
    ["personas", "timeline"].forEach((id) =>
      byId(id).append(
        node(
          "div",
          "This cell failed before an artifact was available. No persona decisions or product outcomes can be inferred.",
          "empty",
        ),
      ),
    );
    jsonBlock(byId("state"), "Retained failure record", run.row_json);
    return;
  }
  renderPersonas(run.artifact.actors);
  renderTimeline(run.artifact.timeline);
  const state = byId("state");
  state.append(
    node("h3", "Recorded result details"),
    node(
      "p",
      "The outcome metrics above describe what the product reported. Open these exact records when you need to investigate a failure; they are technical evidence, not inferred explanations.",
      "result-explanation",
    ),
  );
  disclosure(state, "Final product state", run.artifact.final_state_json);
  disclosure(state, "Recorded assertions", run.artifact.assertions_json);
  disclosure(state, "Faults and grader failures", run.artifact.failure_json);
  disclosure(state, "Timer outcomes", run.artifact.timers_json);
  disclosure(state, "Artifact provenance", run.artifact.provenance_json);
  disclosure(
    state,
    "Dependency and actor worlds (private evidence)",
    run.artifact.world_states_json,
  );
  disclosure(state, "Retained cell record", run.row_json);
}

function renderPersonas(actors) {
  const container = byId("personas");
  clear(container);
  if (!actors.length) {
    container.append(
      node(
        "div",
        "No actor world is recorded in this artifact. Inspect its product timeline and state instead.",
        "empty",
      ),
    );
    return;
  }
  const grid = node("div", undefined, "inspector-grid");
  const list = node("div", undefined, "actor-list");
  const inspector = node("div");
  grid.append(list, inspector);
  container.append(grid);
  actors.forEach((actor, index) => {
    const control = button("", () => selectActor(index), "actor-button");
    control.append(
      node("strong", personaName(actor)),
      node("small", `${actor.world} · ${actor.step} decisions`),
    );
    list.append(control);
  });
  function selectActor(index) {
    Array.from(list.children).forEach((control, item) =>
      control.setAttribute("aria-pressed", String(index === item)),
    );
    clear(inspector);
    const actor = actors[index];
    const header = node("div", undefined, "actor-header");
    header.append(
      node("h2", personaName(actor)),
      pill(
        "neutral",
        actor.next_ns === null ? "Decisions finished" : "Decision pending",
      ),
    );
    inspector.append(header);
    const persona = JSON.parse(actor.profile_json).persona;
    if (Array.isArray(persona?.goals)) {
      const goals = node("div", undefined, "persona-goals");
      goals.append(node("h3", "This persona's stated goals"));
      for (const goal of persona.goals)
        if (typeof goal === "string") goals.append(node("p", goal));
      inspector.append(goals);
    }
    disclosure(
      inspector,
      "Persona timing and policy details",
      `${actor.policy} · ${actor.mode} policy · start ${ns(actor.start_ns)} · next wake ${ns(actor.next_ns)}`,
    );
    inspector.append(
      node(
        "p",
        "The report window is closed when the actor world is sealed. Decisions ending does not mean the persona achieved its goal.",
        "comparison-intro",
      ),
    );
    const context = node("div", undefined, "evidence-grid");
    disclosure(
      context,
      "Persona profile / behavioral assumptions",
      actor.profile_json,
    );
    disclosure(context, "Authorized actions", actor.actions_json);
    disclosure(context, "Initial private memory", actor.initial_memory_json);
    disclosure(context, "Current private memory", actor.current_memory_json);
    inspector.append(context);
    if (!actor.decisions.length) {
      inspector.append(
        node(
          "div",
          "No recorded decisions. The persona may not have arrived within this window; do not infer a response.",
          "empty",
        ),
      );
      return;
    }
    const decisionGrid = node("div", undefined, "decision-grid");
    const choices = node("div", undefined, "decision-list");
    const detail = node("div");
    decisionGrid.append(choices, detail);
    inspector.append(decisionGrid);
    actor.decisions.forEach((decision, decisionIndex) => {
      const control = button(
        "",
        () => selectDecision(decisionIndex),
        "decision-button",
      );
      control.append(
        node("strong", `Decision ${decision.step}`),
        node("small", ns(decision.at_ns)),
      );
      choices.append(control);
    });
    function selectDecision(decisionIndex) {
      Array.from(choices.children).forEach((control, item) =>
        control.setAttribute("aria-pressed", String(item === decisionIndex)),
      );
      clear(detail);
      const decision = actor.decisions[decisionIndex];
      detail.append(
        node("h3", `Decision ${decision.step} · ${ns(decision.at_ns)}`),
      );
      const choice = node("div", undefined, "recorded-choice");
      choice.append(
        node("span", "Accepted selection", "eyebrow"),
        node("p", recordedChoice(decision)),
      );
      detail.append(choice, node("h3", "What this persona observed"));
      const observation = readableObservation(decision.observation_json);
      if (observation.length) {
        const fields = table(["Observed field", "Recorded value"]);
        observation.forEach((values) => fieldRow(fields.body, values));
        detail.append(fields.wrap);
      } else
        detail.append(
          node(
            "p",
            "Structured observation recorded. Open technical details for its exact contents.",
            "comparison-intro",
          ),
        );
      const technical = node("details", undefined, "decision-technical");
      technical.append(
        node(
          "summary",
          "Technical details · exact observation, action, memory, and policy request",
        ),
      );
      technical.append(
        node(
          "p",
          `Actor RNG counter: ${decision.rng_before} → ${decision.rng_after}. No rationale is inferred from this counter.`,
          "comparison-intro",
        ),
      );
      jsonBlock(technical, "Exact observation", decision.observation_json);
      jsonBlock(technical, "Exact accepted selection", decision.decision_json);
      jsonBlock(technical, "Memory before", decision.memory_before_json);
      jsonBlock(technical, "Memory after", decision.memory_after_json);
      if (decision.choices_json)
        disclosure(
          technical,
          "Product-authorized choices offered",
          decision.choices_json,
        );
      jsonBlock(
        technical,
        "Resolved action / memory / next wake",
        decision.resolution_json,
      );
      jsonBlock(
        technical,
        "Translated product delivery",
        decision.delivery_json,
      );
      disclosure(technical, "Exact policy request", decision.request_json);
      detail.append(technical);
      if (decision.event_i !== null)
        detail.append(
          button(
            `Inspect driver event #${decision.event_i}`,
            () => {
              activateTab("timeline");
              renderTimeline(model.run.artifact.timeline, decision.event_i);
            },
            "action-button",
          ),
        );
    }
    selectDecision(0);
  }
  selectActor(0);
}

function renderTimeline(events, selected = null) {
  const container = byId("timeline");
  clear(container);
  const controls = node("div", undefined, "timeline-controls");
  const kindLabel = node("label", "Event kind");
  const kind = node("select");
  kind.id = "event-kind";
  ["all", ...new Set(events.map((event) => event.kind))].forEach((value) => {
    const option = node("option", value);
    option.value = value;
    kind.append(option);
  });
  kindLabel.append(kind);
  const searchLabel = node(
    "label",
    "Search handler, event number, or recorded details",
  );
  const search = node("input");
  search.type = "search";
  search.id = "event-search";
  search.placeholder = "For example: receive, payment, actors_step";
  searchLabel.append(search);
  controls.append(kindLabel, searchLabel);
  container.append(controls);
  container.append(
    node(
      "p",
      "Events retain the artifact's recorded order. Port calls are linked by their exact driver event; no causal link is invented between equal-time events.",
      "comparison-intro",
    ),
  );
  const listing = table(["Event", "Virtual time", "Kind", "Handler", "Status"]);
  container.append(listing.wrap);
  const pagination = node("div", undefined, "pagination");
  const count = node("span");
  const buttons = node("div");
  const previous = button(
    "Previous",
    () => {
      page -= 1;
      draw();
    },
    "quiet-button",
  );
  const next = button(
    "Next",
    () => {
      page += 1;
      draw();
    },
    "quiet-button",
  );
  buttons.append(previous, next);
  pagination.append(count, buttons);
  container.append(pagination);
  const detail = node("div", undefined, "evidence-grid");
  container.append(detail);
  let page =
    selected === null
      ? 0
      : Math.floor(
          Math.max(
            0,
            events.findIndex((event) => event.i === selected),
          ) / pageSize,
        );
  function inspect(event) {
    selected = event.i;
    clear(detail);
    jsonBlock(detail, `Recorded event #${event.i}`, event.detail_json);
    jsonBlock(detail, "Port calls during this event", event.port_calls_json);
    draw();
  }
  function draw() {
    const query = search.value.trim().toLowerCase();
    const filtered = events.filter(
      (event) =>
        (kind.value === "all" || event.kind === kind.value) &&
        event.detail_json.toLowerCase().includes(query),
    );
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    page = Math.min(page, pages - 1);
    clear(listing.body);
    filtered.slice(page * pageSize, (page + 1) * pageSize).forEach((event) => {
      const row = fieldRow(listing.body, [
        "",
        ns(event.at_ns),
        event.kind,
        event.handler,
        event.status,
      ]);
      row.firstChild.append(
        button(`#${event.i}`, () => inspect(event), "event-button"),
      );
      if (event.i === selected) row.className = "selected-event";
    });
    count.textContent = `${filtered.length} events · page ${page + 1} of ${pages}`;
    previous.disabled = page === 0;
    next.disabled = page + 1 >= pages;
  }
  kind.addEventListener("change", () => {
    page = 0;
    draw();
  });
  search.addEventListener("input", () => {
    page = 0;
    draw();
  });
  draw();
  if (selected !== null) {
    const event = events.find((item) => item.i === selected);
    if (event) inspect(event);
  }
}

function activateTab(tab) {
  model.tab = tab;
  ["personas", "timeline", "state"].forEach((id) => {
    const selected = id === tab;
    byId(id).hidden = !selected;
    const control = byId(`tab-${id}`);
    control.setAttribute("aria-selected", String(selected));
    control.tabIndex = selected ? 0 : -1;
  });
}
function activateWorkspace(view) {
  const changed = model.workspace !== view;
  model.workspace = view;
  byId("overview").hidden = view !== "overview";
  byId("run-section").hidden =
    view !== "inspector" || !model.experiment?.available;
  for (const name of ["overview", "inspector"]) {
    byId("view-" + name).setAttribute("aria-selected", String(view === name));
    byId("view-" + name).tabIndex = view === name ? 0 : -1;
  }
  if (changed) window.scrollTo(0, 0);
}
for (const name of ["overview", "inspector"]) {
  byId("view-" + name).addEventListener("click", () => activateWorkspace(name));
  byId("view-" + name).addEventListener("keydown", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const target =
        event.key === "Home"
          ? "overview"
          : event.key === "End"
            ? "inspector"
            : name === "overview"
              ? "inspector"
              : "overview";
      activateWorkspace(target);
      byId("view-" + target).focus();
    }
  });
}
["personas", "timeline", "state"].forEach((tab, index, tabs) => {
  byId(`tab-${tab}`).addEventListener("click", () => activateTab(tab));
  byId(`tab-${tab}`).addEventListener("keydown", (event) => {
    const target =
      event.key === "ArrowRight"
        ? tabs[(index + 1) % tabs.length]
        : event.key === "ArrowLeft"
          ? tabs[(index + tabs.length - 1) % tabs.length]
          : event.key === "Home"
            ? tabs[0]
            : event.key === "End"
              ? tabs[tabs.length - 1]
              : null;
    if (target) {
      event.preventDefault();
      activateTab(target);
      byId(`tab-${target}`).focus();
    }
  });
});
byId("run-select").addEventListener("change", (event) =>
  selectRun(Number(event.target.value)),
);
byId("refresh").addEventListener("click", refresh);
refresh();
