"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const context = vm.createContext({});
vm.runInContext(
  fs.readFileSync(
    path.join(__dirname, "../seam_viewer/static/format.js"),
    "utf8",
  ),
  context,
);
const {
  metricValue,
  metricLabel,
  controllerDescription,
  decimal,
  metricKeys,
  headlineMetricKeys,
  friendlyMetricLabel,
  recordedChoice,
  readableObservation,
  personaName,
} = context;
assert.equal(metricValue("4000000000", "ns"), "4 s");
assert.equal(metricValue("1000000001", "ns"), "1.000000001 s");
assert.equal(
  metricValue("9223372036854775807", "ns"),
  "9223372036.854775807 s",
);
assert.equal(
  metricValue("18446744073709551614", "count"),
  "18446744073709551614",
);
assert.equal(
  metricValue({ numerator: "34", denominator: "2" }, "samples"),
  "17",
);
assert.equal(
  metricValue({ numerator: "1", denominator: "3" }, "samples"),
  "≈ 0.333",
);
assert.equal(metricValue({ numerator: "1", denominator: "3" }, "ns"), "≈ 0 s");
assert.equal(metricValue("-13000000000", "ns"), "−13 s");
assert.equal(metricValue(null, "samples"), "—");
assert.equal(metricValue(undefined, "samples"), "—");
assert.throws(() => decimal("1", "0", 3));
assert.equal(metricLabel("stop_at_ns"), "Stop at");
assert.equal(
  metricLabel("preferred_outcome_achieved"),
  "Preferred outcome achieved",
);
assert.equal(
  controllerDescription("scripted"),
  "Scripted controller — not an LLM",
);
assert.equal(controllerDescription("fake"), "Fake controller — not an LLM");
assert.equal(controllerDescription("unknown"), "External decision provider");
assert.equal(
  Array.from(
    metricKeys({ calls: "count", stop_at_ns: "ns", resolved: "tickets" }),
  ).join(","),
  "resolved,calls,stop_at_ns",
);
assert.equal(
  Array.from(
    headlineMetricKeys({
      calls: "count",
      preferred_outcome_achieved: "samples",
      disposition_completed: "samples",
      unfinished_actors: "actors",
    }),
  ).join(","),
  "preferred_outcome_achieved,disposition_completed,unfinished_actors",
);
assert.equal(
  Array.from(headlineMetricKeys({ calls: "count", resolved: "tickets" })).join(
    ",",
  ),
  "resolved,calls",
);
assert.equal(Array.from(headlineMetricKeys({})).length, 0);
assert.equal(friendlyMetricLabel("preferred_outcome_achieved"), "Goals met");
assert.equal(
  friendlyMetricLabel("custom_product_metric"),
  "Custom product metric",
);
assert.equal(
  recordedChoice({
    decision_json: '{"choice":"submit"}',
    choices_json: '[{"id":"submit","description":"Submit my sample."}]',
  }),
  "Submit my sample.",
);
assert.equal(
  recordedChoice({ decision_json: '{"choice":"unknown"}', choices_json: "[]" }),
  "Recorded choice: unknown",
);
assert.equal(
  recordedChoice({ decision_json: '{"action":{"value":9223372036854775807}}' }),
  "Recorded action · open technical details for the exact decision",
);
assert.equal(
  readableObservation('{"value":9223372036854775807}')[0][1],
  "Open technical details for the exact value",
);
assert.equal(readableObservation('{"value":89}')[0][1], "89");
assert.equal(
  readableObservation('{"status":null}')[0][1],
  "Not observed / not set",
);
assert.equal(readableObservation('{"retested":false}')[0][1], "No");
assert.equal(readableObservation("[1,2]").length, 0);
assert.equal(
  personaName({
    id: "fallback_id",
    profile_json: '{"persona":{"name":"Responsive submitter"}}',
  }),
  "Responsive submitter",
);
assert.equal(
  personaName({ id: "original_id", profile_json: "{}" }),
  "original_id",
);
console.log("Viewer formatting assertions passed");
