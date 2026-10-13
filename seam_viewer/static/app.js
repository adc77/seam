"use strict";

const byId = (id) => document.getElementById(id);
const model = {
  experiments: [],
  experiment: null,
  run: null,
  runIndex: 0,
  tab: "personas",
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
  navigation();
  clear(byId("overview"));
  byId("run-section").hidden = true;
  if (!experiment.available) {
    notice(`Cannot inspect ${experiment.label}: ${experiment.error}. ${experiment.error_hint}`, true);
    return;
  }
  notice("");
  renderOverview(experiment);
  const select = byId("run-select");
  clear(select);
  experiment.runs.forEach((row, index) => {
    const option = node(
      "option",
      `${row.variant} · seed ${row.seed} · engine checks ${row.status}`,
    );
    option.value = String(index);
    select.append(option);
  });
  byId("run-section").hidden = false;
  selectRun(0);
}

function renderOverview(experiment) {
  const panel = node("section", undefined, "panel");
  const heading = node("div", undefined, "panel-heading");
  const title = node("div");
  title.append(
    node("div", "PAIRED SCENARIO COMPARISON", "eyebrow"),
    node("h2", experiment.label),
  );
  heading.append(title, pill(experiment.status, `Engine checks ${experiment.status}`));
  panel.append(heading);
  const counts = node("div", undefined, "summary-grid");
  const passed = experiment.runs.filter(
    (row) => row.status === "passed",
  ).length;
  [
    [experiment.plan.variants.length, "Variants"],
    [experiment.plan.seeds.length, "Paired seeds"],
    [passed, "Engine checks passed"],
    [experiment.runs.length - passed, "Engine checks failed"],
  ].forEach(([value, label]) => {
    const card = node("div", undefined, "summary-card");
    card.append(node("strong", value), node("span", label));
    counts.append(card);
  });
  panel.append(counts);
  if (experiment.inference) {
    const usage = experiment.inference;
    panel.append(
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
  panel.append(
    metadata,
    node(
      "p",
      "Engine checks do not establish business success or persona goal completion. Differences are variant − baseline for the same seed; failed pairs are excluded. Approximate decimals are marked ≈; exact values are retained below. These are observations, not statistical significance.",
      "comparison-intro",
    ),
  );
  panel.append(node("h3", "Product-defined outcomes and run metrics"));
  const variants = experiment.plan.variants.map((variant) => variant.name);
  const keys = metricKeys(experiment.units);
  const totals = table(["Metric / mean", ...variants]);
  keys.forEach((key) => {
    const unit = experiment.units[key];
    const row = fieldRow(totals.body, [
      `${metricLabel(key)} (${unit === "ns" ? "seconds" : unit})`,
      ...variants.map((name) => metricValue(experiment.aggregates[name].metrics[key].mean, unit)),
    ]);
    row.title = JSON.stringify(Object.fromEntries(variants.map((name) => [name, experiment.aggregates[name].metrics[key].mean])));
  });
  panel.append(totals.wrap);
  disclosure(panel, "Exact aggregate statistics (original units)", JSON.stringify(experiment.aggregates, null, 2));
  Object.entries(experiment.comparisons).forEach(([name, comparison]) => {
    const label = node("div", undefined, "comparison-heading");
    label.append(
      node("h3", `${name} versus ${comparison.baseline}`),
      pill(comparison.status, `Pairing ${comparison.status}`),
    );
    panel.append(label);
    panel.append(
      node(
        "p",
        `Included seeds: ${comparison.paired_seeds.join(", ") || "none"} · Excluded seeds: ${comparison.excluded_seeds.join(", ") || "none"}`,
        "comparison-intro",
      ),
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
      row.firstChild.append(node("span", unit === "ns" ? "seconds" : unit, "metric-unit"));
    });
    panel.append(deltas.wrap);
    disclosure(panel, "Exact difference statistics (original units)", JSON.stringify(comparison.delta, null, 2));
    disclosure(
      panel,
      "Per-seed paired differences",
      experiment.pairs_json[name],
    );
  });
  disclosure(
    panel,
    "Declared scenario parameters and seeds",
    experiment.plan_json,
  );
  disclosure(
    panel,
    "Pinned source, SDK, interpreter, and environment identity",
    experiment.identity_json,
  );
  byId("overview").append(panel);
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
    renderRun(run);
    activateTab(model.tab);
  } catch (error) {
    if (generation !== model.request) return;
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
  byId("run-title").textContent = `${row.variant} · seed ${row.seed}`;
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
  byId("run-status").append(status);
  if (row.metrics) {
    byId("run-status").append(node("h3", "Product-defined outcomes and run metrics"));
    const outcomes = table(["Metric", "Observed value"]);
    metricKeys(model.experiment.units).forEach((key) => {
      const unit = model.experiment.units[key];
      const item = fieldRow(outcomes.body, [
        `${metricLabel(key)} (${unit === "ns" ? "seconds" : unit})`,
        metricValue(row.metrics[key], unit),
      ]);
      item.title = `${key}: ${row.metrics[key]} ${unit}`;
    });
    byId("run-status").append(outcomes.wrap);
  }
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
  jsonBlock(state, "Final product state", run.artifact.final_state_json);
  jsonBlock(state, "Assertions", run.artifact.assertions_json);
  jsonBlock(state, "Faults and grader failures", run.artifact.failure_json);
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
      node("strong", actor.id),
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
      node("h2", actor.id),
      pill("neutral", actor.next_ns === null ? "Decisions finished" : "Decision pending"),
    );
    inspector.append(header);
    inspector.append(
      node(
        "p",
        `${actor.policy} · ${actor.mode} policy · start ${ns(actor.start_ns)} · next wake ${ns(actor.next_ns)}`,
      ),
    );
    inspector.append(
      node(
        "p",
        `Actor world ${actor.sealed ? "sealed" : "not sealed"}. Sealing ends the observation window; it does not mean every persona completed its goal.`,
        "comparison-intro",
      ),
    );
    const profile = node("div", undefined, "evidence-grid");
    jsonBlock(profile, "Profile / behavioral assumptions", actor.profile_json);
    jsonBlock(profile, "Authorized actions", actor.actions_json);
    inspector.append(profile);
    disclosure(inspector, "Initial private memory", actor.initial_memory_json);
    disclosure(inspector, "Current private memory", actor.current_memory_json);
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
        node(
          "p",
          `Actor RNG counter: ${decision.rng_before} → ${decision.rng_after}. Recorded evidence, not an inferred rationale.`,
          "comparison-intro",
        ),
      );
      jsonBlock(
        detail,
        "What this persona observed",
        decision.observation_json,
      );
      const memory = node("div", undefined, "evidence-grid");
      jsonBlock(memory, "Memory before", decision.memory_before_json);
      jsonBlock(memory, "Memory after", decision.memory_after_json);
      detail.append(memory);
      jsonBlock(
        detail,
        "Accepted selection",
        decision.decision_json,
      );
      if (decision.choices_json)
        disclosure(detail, "Product-authorized choices offered", decision.choices_json);
      jsonBlock(detail, "Resolved action / memory / next wake", decision.resolution_json);
      jsonBlock(detail, "Translated product delivery", decision.delivery_json);
      disclosure(detail, "Exact policy request", decision.request_json);
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
