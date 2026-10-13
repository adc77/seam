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
  const priority = (key) => ["count", "ns"].includes(units[key]) ? 1 : 0;
  return Object.keys(units).sort((a, b) => priority(a) - priority(b) || a.localeCompare(b));
}

function controllerDescription(provider) {
  if (provider === "scripted") return "Scripted controller — not an LLM";
  if (provider === "fake") return "Fake controller — not an LLM";
  return "External decision provider";
}
