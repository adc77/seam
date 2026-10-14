"use strict";

function decimal(numerator, denominator, places) {
  let n = BigInt(numerator);
  const d = BigInt(denominator);
  if (d <= 0n) throw new RangeError("Expected a positive denominator");
  const sign = n < 0n ? "−" : "";
  if (n < 0n) n = -n;
  const whole = n / d;
  let remainder = n % d;
  let digits = "";
  for (let i = 0; remainder && i < places; i += 1) {
    remainder *= 10n;
    digits += String(remainder / d);
    remainder %= d;
  }
  digits = digits.replace(/0+$/, "");
  return `${remainder ? "≈ " : ""}${sign}${whole}${digits ? `.${digits}` : ""}`;
}

function metricValue(value, unit) {
  if (value === null || value === undefined) return "—";
  const rational = typeof value === "object";
  const n = rational ? value.numerator : value;
  const d = rational ? BigInt(value.denominator) : 1n;
  return unit === "ns"
    ? `${decimal(n, d * 1000000000n, 9)} s`
    : decimal(n, d, 3);
}

function metricLabel(key) {
  const text = key.replace(/_ns$/, "").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function metricKeys(units) {
  const priority = (key) => (["count", "ns"].includes(units[key]) ? 1 : 0);
  return Object.keys(units).sort(
    (a, b) => priority(a) - priority(b) || a.localeCompare(b),
  );
}

function controllerDescription(provider) {
  if (provider === "scripted") return "Scripted controller — not an LLM";
  if (provider === "fake") return "Fake controller — not an LLM";
  return "External decision provider";
}

function headlineMetricKeys(units) {
  const preferred = [
    "preferred_outcome_achieved",
    "disposition_completed",
    "unfinished_actors",
  ].filter((key) => Object.hasOwn(units, key));
  return [
    ...preferred,
    ...metricKeys(units).filter((key) => !preferred.includes(key)),
  ].slice(0, 3);
}

function friendlyMetricLabel(key) {
  const labels = {
    preferred_outcome_achieved: "Goals met",
    disposition_completed: "Final outcomes reached",
    unfinished_actors: "Personas still waiting",
    released: "Samples released",
    scrapped: "Samples rejected",
    filed_reports: "Reports saved",
    completion_time_ns: "Time to final result",
  };
  return Object.hasOwn(labels, key) ? labels[key] : metricLabel(key);
}

function recordedChoice(decision) {
  const value = JSON.parse(decision.decision_json);
  if (typeof value.choice !== "string")
    return "Recorded action · open technical details for the exact decision";
  if (decision.choices_json) {
    const choice = JSON.parse(decision.choices_json).find(
      (row) => row.id === value.choice,
    );
    if (choice && typeof choice.description === "string")
      return choice.description;
  }
  return "Recorded choice: " + value.choice;
}

function readableObservation(raw) {
  const value = JSON.parse(raw);
  if (value === null || Array.isArray(value) || typeof value !== "object")
    return [];
  return Object.entries(value).map(([key, field]) => [
    metricLabel(key),
    field === null
      ? "Not observed / not set"
      : typeof field === "boolean"
        ? field
          ? "Yes"
          : "No"
        : typeof field === "string"
          ? field
          : typeof field === "number" && Number.isSafeInteger(field)
            ? String(field)
            : "Open technical details for the exact value",
  ]);
}

function personaName(actor) {
  const name = JSON.parse(actor.profile_json).persona?.name;
  return typeof name === "string" ? name : actor.id;
}
