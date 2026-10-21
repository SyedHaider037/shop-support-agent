// Pure-function checks for the chat page's helpers (scrolling, trace wording, totals).
// Run by hand with: node --test "tests/web/*.test.js"  (Node 21+, no npm packages; not part of CI)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  isNearEnd,
  traceChecks,
  codeWords,
  riskPill,
  resultPill,
  stepSentence,
  TOOL_SENTENCES,
  stepShares,
  sessionTotals,
  formatTotals,
} = require("../../public/app.js");

const TOOLS_DIR = path.join(__dirname, "..", "..", "app", "tools");
const readTool = (name) => fs.readFileSync(path.join(TOOLS_DIR, name), "utf8");

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

test("a step without a tool call wrote the reply", () => {
  assert.equal(stepSentence({ tool_calls: [] }), "Wrote the reply.");
  assert.equal(stepSentence({}), "Wrote the reply.");
});

test("known tool results get their own sentence", () => {
  const step = (name, result) => ({ tool_calls: [{ name, result }] });
  assert.equal(
    stepSentence(step("get_order_status", "no_matching_order")),
    "Looked up the order but found no match, so it asked the customer to double-check.",
  );
  assert.match(stepSentence(step("request_refund", "ok")), /human for approval/);
  assert.match(stepSentence(step("request_refund", "outside_window")), /more than 30 days/);
  assert.match(stepSentence(step("capture_lead", "invalid_arguments")), /capture_lead .*failed validation/);
});

test("several calls in one step become one line", () => {
  const line = stepSentence({
    tool_calls: [
      { name: "get_order_status", result: "ok" },
      { name: "request_refund", result: "not_delivered" },
    ],
  });
  assert.equal(line, "Looked up the order and found it. Tried to request a refund, but the order has not been delivered yet.");
});

test("unknown tools and codes fall back to a generic sentence", () => {
  assert.equal(stepSentence({ tool_calls: [{ name: "new_tool", result: "ok" }] }), "Called new_tool.");
  assert.equal(
    stepSentence({ tool_calls: [{ name: "get_order_status", result: "brand_new_code" }] }),
    'Called get_order_status, which returned "brand new code".',
  );
  assert.equal(stepSentence({ tool_calls: [{}] }), 'Called an unnamed tool, which returned "unknown".');
});

test("every tool and result code in app/tools has its own sentence", () => {
  const dispatcher = readTool("dispatcher.py");
  const tools = [...dispatcher.matchAll(/"([a-z_]+)": handlers\./g)].map((m) => m[1]);
  assert.deepEqual(tools.sort(), Object.keys(TOOL_SENTENCES).sort());

  const generic = [...dispatcher.matchAll(/_error\("([a-z_]+)"/g)].map((m) => m[1]);
  const refundCodes = [...readTool("refund_rules.py").matchAll(/return "([a-z_]+)"/g)].map((m) => m[1]);
  const lookupCodes = [...readTool("handlers.py").matchAll(/ToolError\("([a-z_]+)"\)/g)].map((m) => m[1]);
  assert.ok(generic.length >= 4 && refundCodes.length >= 4 && lookupCodes.length >= 1);

  const genericFallback = (tool, code) => `Called ${tool}, which returned "${codeWords(code)}".`;
  for (const tool of tools) {
    for (const code of ["ok", ...generic]) {
      const line = stepSentence({ tool_calls: [{ name: tool, result: code }] });
      assert.notEqual(line, genericFallback(tool, code), `${tool} ${code}`);
      assert.notEqual(line, `Called ${tool}.`, `${tool} ${code}`);
    }
  }
  for (const code of [...lookupCodes, ...refundCodes]) {
    const line = stepSentence({ tool_calls: [{ name: "request_refund", result: code }] });
    assert.notEqual(line, genericFallback("request_refund", code), code);
  }
  for (const code of lookupCodes) {
    const line = stepSentence({ tool_calls: [{ name: "get_order_status", result: code }] });
    assert.notEqual(line, genericFallback("get_order_status", code), code);
  }
});

test("bars show each step's share of model time, never under 3 percent", () => {
  assert.deepEqual(stepShares([600, 400]), [
    { percent: 60, width: 60 },
    { percent: 40, width: 40 },
  ]);
  const [fast, slow] = stepShares([10, 990]);
  assert.equal(fast.percent, 1);
  assert.equal(fast.width, 3);
  assert.equal(slow.percent, 99);
  assert.deepEqual(stepShares([1200]), [{ percent: 100, width: 100 }]);
});

test("missing or zero latencies split the bar evenly instead of dividing by zero", () => {
  assert.deepEqual(stepShares([0, 0]), [
    { percent: 50, width: 50 },
    { percent: 50, width: 50 },
  ]);
  assert.deepEqual(stepShares([undefined, 300])[0], { percent: 0, width: 3 });
  assert.deepEqual(stepShares([]), []);
});

test("session totals add up requests, tokens and cost", () => {
  const totals = sessionTotals([
    { tokens: { total: 3010 }, estimated_cost_usd: 0.000496 },
    { tokens: { total: 2789 }, estimated_cost_usd: 0.000471 },
    { tokens: { total: 0 }, estimated_cost_usd: null }, // refused before any model call
  ]);
  assert.equal(totals.requests, 3);
  assert.equal(totals.tokens, 5799);
  assert.ok(Math.abs(totals.cost - 0.000967) < 1e-12);
  assert.equal(
    formatTotals(totals),
    "This chat: 3 requests, 5,799 tokens, about $0.00097 (estimated at list prices)",
  );
});

test("the totals line is empty for a new chat and singular for one request", () => {
  assert.equal(formatTotals(sessionTotals([])), "");
  assert.equal(
    formatTotals(sessionTotals([{ tokens: { total: 1200 }, estimated_cost_usd: 0.0123 }])),
    "This chat: 1 request, 1,200 tokens, about $0.0123 (estimated at list prices)",
  );
  assert.equal(
    formatTotals(sessionTotals([{ tokens: { total: 10 } }])),
    "This chat: 1 request, 10 tokens, cost not available for this model",
  );
});
