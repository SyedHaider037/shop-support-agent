// Pure-function checks for the chat page's helpers (scrolling, trace wording, totals).
// Run by hand with: node --test tests/web  (Node 18+, no npm packages; not part of CI)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { isNearEnd } = require("../../public/app.js");

test("a reader at the end of the list is following along", () => {
  assert.equal(isNearEnd(1000, 600, 400), true);
  assert.equal(isNearEnd(1000, 560, 400), true); // 40 px short still counts
});

test("a reader who scrolled up is left alone", () => {
  assert.equal(isNearEnd(1000, 300, 400), false);
  assert.equal(isNearEnd(1000, 540, 400, 48), false);
});
