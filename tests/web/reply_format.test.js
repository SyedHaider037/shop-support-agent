// Pure-function checks for the chat page's reply formatter.
// Run by hand with: node --test tests/web/reply_format.test.js  (Node 18+, no npm packages;
// deliberately not part of CI)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { parseInline, parseReply, pendingApproval } = require("../../public/app.js");

test("bold runs become bold segments", () => {
  assert.deepEqual(parseInline("a **b** c"), [
    { text: "a ", bold: false },
    { text: "b", bold: true },
    { text: " c", bold: false },
  ]);
});

test("unmatched stars stay as plain text", () => {
  assert.deepEqual(parseInline("2 ** 3"), [{ text: "2 ** 3", bold: false }]);
});

test("markup in model text stays text", () => {
  const [block] = parseReply('<img src=x onerror="alert(1)"> <script>alert(1)</script>');
  assert.equal(block.type, "p");
  assert.equal(block.lines[0][0].text, '<img src=x onerror="alert(1)"> <script>alert(1)</script>');
  assert.equal(block.lines[0][0].bold, false);
});

test("lists, paragraphs and line breaks", () => {
  const blocks = parseReply("Line one\nline two\n\n- **A** item\n- B item\n1. first\n2) second");
  assert.deepEqual(
    blocks.map((b) => b.type),
    ["p", "ul", "ol"],
  );
  assert.equal(blocks[0].lines.length, 2);
  assert.deepEqual(blocks[1].items[0][0], { text: "A", bold: true });
  assert.equal(blocks[2].items[1][0].text, "second");
});

test("Windows line endings are handled", () => {
  assert.equal(parseReply("a\r\nb")[0].lines.length, 2);
});

test("pending approval needs an ok request_refund with pending_approval", () => {
  const pending = { tool: "request_refund", result: { ok: true, data: { status: "pending_approval" } } };
  assert.equal(pendingApproval([pending]), true);
  assert.equal(pendingApproval([{ tool: "request_refund", result: { ok: false, error: "not_delivered" } }]), false);
  assert.equal(pendingApproval([{ tool: "search_products", result: { ok: true, data: {} } }]), false);
  assert.equal(pendingApproval(undefined), false);
});
