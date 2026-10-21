// Pure-function checks for the chat page's helpers (scrolling, trace wording, totals).
// Run by hand with: node --test "tests/web/*.test.js"  (Node 21+, no npm packages; not part of CI)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { isNearEnd, traceChecks, codeWords, riskPill, resultPill } = require("../../public/app.js");

test("a reader at the end of the list is following along", () => {
  assert.equal(isNearEnd(1000, 600, 400), true);
  assert.equal(isNearEnd(1000, 560, 400), true); // 40 px short still counts
});

test("a reader who scrolled up is left alone", () => {
  assert.equal(isNearEnd(1000, 300, 400), false);
  assert.equal(isNearEnd(1000, 540, 400, 48), false);
});

test("a quiet trace folds its checks into one line", () => {
  const checks = traceChecks({ guardrail_events: [], retries: [], error: null });
  assert.equal(checks.allQuiet, true);
  assert.equal(checks.line, "Checks: no guardrails fired, no retries, no errors.");
  assert.equal(traceChecks(undefined).line, "Checks: no guardrails fired, no retries, no errors.");
});

test("checks that happened are listed; only the quiet ones stay in the line", () => {
  const checks = traceChecks({
    guardrail_events: [{ layer: "output", rule: "stray_email", action: "masked" }],
    retries: [],
    recovered_error: "tool_use_failed",
    error: null,
  });
  assert.equal(checks.allQuiet, false);
  assert.deepEqual(checks.guardrails, ["output · stray_email · masked"]);
  assert.deepEqual(checks.retries, ["recovered from: tool_use_failed"]);
  assert.equal(checks.line, "Checks: no errors.");
});

test("when every check has something, there is no summary line", () => {
  const checks = traceChecks({
    guardrail_events: [{ layer: "input", rule: "ignore_instructions", action: "refused" }],
    retries: [{ reason: "rate_limited", wait_s: 2 }],
    error: "input_refused",
  });
  assert.equal(checks.line, null);
  assert.equal(checks.error, "input_refused");
  assert.deepEqual(checks.retries, ["rate_limited, waited 2 s"]);
});

test("result codes read as plain words", () => {
  assert.equal(codeWords("no_matching_order"), "no matching order");
  assert.equal(codeWords("NOT_DELIVERED"), "not delivered");
  assert.equal(codeWords(""), "unknown");
  assert.equal(codeWords(undefined), "unknown");
});

test("pills carry their meaning in words", () => {
  assert.deepEqual(riskPill("high"), { text: "Risk: high", tone: "high" });
  assert.deepEqual(riskPill("low"), { text: "Risk: low", tone: "low" });
  assert.deepEqual(riskPill("surprise"), { text: "Risk: unknown", tone: "unknown" });
  assert.deepEqual(resultPill("ok"), { text: "Result: ok", tone: "ok" });
  assert.deepEqual(resultPill("no_matching_order"), { text: "Result: no matching order", tone: "error" });
});
