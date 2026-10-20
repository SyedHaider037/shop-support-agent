/* Chat page logic for shop-support-agent.
 *
 * Everything the model writes is untrusted. It is never parsed as HTML: replies are split
 * into plain data (paragraphs, list items, bold runs) and turned into DOM nodes whose text
 * is set with textContent. The page keeps the conversation in memory only and sends the
 * whole history on every request, because the server keeps no memory.
 */
"use strict";

const MAX_MESSAGES = 20; // the API's limit for one conversation
const MAX_CHARS = 1000;
const TIMEOUT_MS = 45000;

/* ---------- safe reply formatting (pure, no DOM) -------------------------------------- */

/** Split "**bold**" runs into [{text, bold}] segments. Unmatched ** stays as text. */
function parseInline(text) {
  const segments = [];
  const pattern = /\*\*(.+?)\*\*/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) segments.push({ text: text.slice(last, match.index), bold: false });
    segments.push({ text: match[1], bold: true });
    last = pattern.lastIndex;
  }
  if (last < text.length) segments.push({ text: text.slice(last), bold: false });
  return segments;
}

/**
 * Turn reply text into blocks: {type: "p", lines: [segments]} or
 * {type: "ul"|"ol", items: [segments]}. Only "- " / "* " and "1. " lists are recognised.
 */
function parseReply(text) {
  const blocks = [];
  let paragraph = null;
  let list = null;
  for (const raw of String(text).replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trim();
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    const numbered = /^\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      const type = bullet ? "ul" : "ol";
      if (!list || list.type !== type) {
        list = { type, items: [] };
        blocks.push(list);
      }
      list.items.push(parseInline((bullet || numbered)[1]));
      paragraph = null;
    } else if (line === "") {
      paragraph = null;
      list = null;
    } else {
      if (!paragraph) {
        paragraph = { type: "p", lines: [] };
        blocks.push(paragraph);
      }
      paragraph.lines.push(parseInline(line));
      list = null;
    }
  }
  return blocks;
}

/* ---------- DOM helpers: every piece of text goes through textContent -------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function appendSegments(parent, segments) {
  for (const segment of segments) {
    if (segment.bold) parent.appendChild(el("strong", null, segment.text));
    else parent.appendChild(document.createTextNode(segment.text));
  }
}

function renderReply(text) {
  const fragment = document.createDocumentFragment();
  for (const block of parseReply(text)) {
    if (block.type === "p") {
      const p = el("p");
      block.lines.forEach((segments, index) => {
        if (index > 0) p.appendChild(document.createElement("br"));
        appendSegments(p, segments);
      });
      fragment.appendChild(p);
    } else {
      const listNode = el(block.type);
      for (const item of block.items) {
        const li = el("li");
        appendSegments(li, item);
        listNode.appendChild(li);
      }
      fragment.appendChild(listNode);
    }
  }
  return fragment;
}

/** A refund request went to a human: the only state the agent can reach for a refund. */
function pendingApproval(steps) {
  return (steps || []).some(
    (step) =>
      step &&
      step.tool === "request_refund" &&
      step.result &&
      step.result.ok === true &&
      step.result.data &&
      step.result.data.status === "pending_approval",
  );
}

/* ---------- page ----------------------------------------------------------------- */

function startPage() {
  const $ = (id) => document.getElementById(id);
  const ui = {
    messages: $("messages"),
    typing: $("typing"),
    status: $("status"),
    form: $("chat-form"),
    input: $("message"),
    counter: $("counter"),
    send: $("send"),
    newChat: $("new-chat"),
    traceBody: $("trace-body"),
    traceDetails: $("trace-details"),
  };

  const state = {
    history: [], // exactly what the API receives: [{role, content}]
    replies: [], // per assistant reply: {node, trace, steps, events}
    selected: -1,
    busy: false,
    controller: null,
    waitUntil: 0, // Date.now() until which Send stays disabled (429 / busy)
    countdown: null,
  };

  // On a phone the trace starts folded so the chat gets the screen.
  if (window.matchMedia && window.matchMedia("(max-width: 899px)").matches) {
    ui.traceDetails.open = false;
  }

  function scrollToEnd() {
    ui.messages.scrollTop = ui.messages.scrollHeight;
  }

  function updateCounter() {
    const length = ui.input.value.length;
    ui.counter.textContent = `${length.toLocaleString("en")} / ${MAX_CHARS.toLocaleString("en")}`;
    ui.counter.classList.toggle("is-over", length > MAX_CHARS);
  }

  function updateSend() {
    ui.send.disabled = state.busy || Date.now() < state.waitUntil;
  }

  function showStatus(text) {
    ui.status.textContent = text;
    ui.status.hidden = !text;
  }

  function addNotice(text) {
    const li = el("li", "msg msg--notice", text);
    ui.messages.appendChild(li);
    scrollToEnd();
    return li;
  }

  function addUserMessage(text) {
    const li = el("li", "msg msg--user", text); // textContent: user text is never HTML
    ui.messages.appendChild(li);
    scrollToEnd();
    return li;
  }

  /** Disable Send for `seconds` and show a live countdown built from `message`. */
  function waitFor(seconds, message) {
    clearInterval(state.countdown);
    const wait = Math.max(1, Math.ceil(Number(seconds) || 30));
    state.waitUntil = Date.now() + wait * 1000;
    const tick = () => {
      const left = Math.ceil((state.waitUntil - Date.now()) / 1000);
      if (left <= 0) {
        clearInterval(state.countdown);
        state.countdown = null;
        state.waitUntil = 0;
        showStatus("");
        updateSend();
        return;
      }
      showStatus(`${message} You can send again in ${left} s.`);
      updateSend();
    };
    tick();
    state.countdown = setInterval(tick, 1000);
  }

  function setBusy(busy) {
    state.busy = busy;
    ui.typing.hidden = !busy;
    ui.form.setAttribute("aria-busy", String(busy));
    updateSend();
  }

  /* --- replies and their traces --- */

  function addAssistantReply(data) {
    const index = state.replies.length;
    const li = el("li", "msg msg--assistant is-clickable");
    li.tabIndex = 0;
    li.setAttribute("aria-label", "Assistant reply. Press Enter to show its trace.");
    li.appendChild(renderReply(data.reply));

    const events = Array.isArray(data.guardrail_events) ? data.guardrail_events : [];
    const badges = el("div", "msg__badges");
    if (events.some((e) => e.layer === "input")) {
      badges.appendChild(el("span", "badge badge--guard", "Blocked by input guardrail"));
    }
    if (events.some((e) => e.layer === "output" && e.action === "replaced")) {
      badges.appendChild(el("span", "badge badge--guard", "Replaced by output guardrail"));
    }
    if (pendingApproval(data.steps)) {
      badges.appendChild(
        el("span", "badge badge--pending", "Pending human approval: not approved yet"),
      );
    }
    if (badges.childElementCount) li.appendChild(badges);

    const select = () => selectReply(index);
    li.addEventListener("click", select);
    li.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });

    ui.messages.appendChild(li);
    state.replies.push({ node: li, trace: data.trace || {}, steps: data.steps || [], events });
    selectReply(index);
    scrollToEnd();
  }

  function selectReply(index) {
    state.replies.forEach((reply, i) => reply.node.classList.toggle("is-selected", i === index));
    state.selected = index;
    const reply = state.replies[index];
    ui.traceBody.replaceChildren();
    if (reply) renderTrace(ui.traceBody, reply);
  }

  /* --- sending --- */

  function failed(userNode, text, message) {
    // The message never reached the agent: say so, and give the text back for a retry.
    userNode.classList.add("is-unsent");
    userNode.appendChild(el("span", "visually-hidden", " (not sent)"));
    addNotice(message);
    if (!ui.input.value) {
      ui.input.value = text;
      updateCounter();
    }
  }

  async function send() {
    if (state.busy || Date.now() < state.waitUntil) return; // no double sends
    const text = ui.input.value.trim();
    if (!text) {
      showStatus("Type a message first.");
      ui.input.focus();
      return;
    }
    if (text.length > MAX_CHARS) {
      showStatus(`Messages can be at most ${MAX_CHARS.toLocaleString("en")} characters.`);
      return;
    }
    if (state.history.length + 1 > MAX_MESSAGES) {
      addNotice(`This chat has reached the ${MAX_MESSAGES}-message limit. Start a new chat to continue.`);
      return;
    }

    showStatus("");
    ui.input.value = "";
    updateCounter();
    const userNode = addUserMessage(text);
    const messages = [...state.history, { role: "user", content: text }];

    setBusy(true);
    const controller = new AbortController();
    state.controller = controller;
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const response = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
        signal: controller.signal,
      });
      const retryAfter = Number(response.headers.get("Retry-After"));
      if (response.ok) {
        // A broken body means the server answered badly, not that it was unreachable.
        const data = await response.json().catch(() => null);
        if (!data || typeof data.reply !== "string") {
          failed(userNode, text, "Something went wrong on the server. Please try again.");
          return;
        }
        state.history = [...messages, { role: "assistant", content: data.reply }];
        addAssistantReply(data);
        if (state.history.length >= MAX_MESSAGES) {
          addNotice(`This chat has reached the ${MAX_MESSAGES}-message limit. Start a new chat to continue.`);
        }
      } else if (response.status === 429) {
        failed(userNode, text, "You are sending messages too quickly.");
        waitFor(retryAfter || 30, "Too many messages for now.");
      } else if (response.status === 503 && retryAfter) {
        failed(userNode, text, "The AI model is busy right now (its free usage limit was reached).");
        waitFor(retryAfter, "The model is busy.");
      } else if (response.status === 503) {
        failed(userNode, text, "The assistant is temporarily unavailable. Please try again later.");
      } else if (response.status === 422) {
        failed(
          userNode,
          text,
          "That message could not be sent: it may be empty or too long, or the chat is too long. Edit it or start a new chat.",
        );
      } else {
        failed(userNode, text, "Something went wrong on the server. Please try again.");
      }
    } catch (error) {
      if (controller.signal.aborted && state.controller === controller) {
        failed(userNode, text, "The assistant took too long to answer. Please try again.");
      } else if (state.controller === controller) {
        failed(userNode, text, "Could not reach the server. Check your connection and try again.");
      }
    } finally {
      clearTimeout(timer);
      if (state.controller === controller) {
        state.controller = null;
        setBusy(false);
      }
    }
  }

  function newChat() {
    if (state.controller) state.controller.abort();
    state.controller = null;
    state.history = [];
    state.replies = [];
    state.selected = -1;
    setBusy(false);
    const intro = ui.messages.querySelector(".msg--intro");
    ui.messages.replaceChildren();
    if (intro) ui.messages.appendChild(intro);
    ui.traceBody.replaceChildren(el("p", "trace__empty", "Send a message to see its trace."));
    if (!state.countdown) showStatus("");
    ui.input.value = "";
    updateCounter();
    ui.input.focus();
  }

  /* --- wiring --- */

  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    send();
  });
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  ui.input.addEventListener("input", updateCounter);
  ui.newChat.addEventListener("click", newChat);
  for (const chip of document.querySelectorAll(".chip[data-prompt]")) {
    chip.addEventListener("click", () => {
      ui.input.value = chip.dataset.prompt; // fills the box; sending stays the user's choice
      updateCounter();
      ui.input.focus();
    });
  }
  updateCounter();
  updateSend();
}

/* ---------- trace panel ------------------------------------------------------------ */

const RISK_HELP = "Risk: low reads data, medium saves contact details, high asks for a refund.";

function section(container, title, explanation) {
  const wrap = el("section", "trace__section");
  wrap.appendChild(el("h3", null, title));
  wrap.appendChild(el("p", "hint", explanation));
  container.appendChild(wrap);
  return wrap;
}

function factList(rows) {
  const facts = el("dl", "facts");
  for (const [label, value] of rows) {
    facts.appendChild(el("dt", null, label));
    facts.appendChild(el("dd", null, value));
  }
  return facts;
}

function plainList(items) {
  const list = el("ul", "plain-list");
  for (const item of items) list.appendChild(el("li", null, item));
  return list;
}

function formatValue(value) {
  if (value === null || value === undefined) return "none";
  if (typeof value === "object") return JSON.stringify(value); // shown as text, never HTML
  return String(value);
}

function formatCost(cost) {
  if (typeof cost !== "number") return "not available for this model";
  return `$${cost.toFixed(6)} (estimated)`;
}

function renderToolCall(call) {
  const box = el("div", "tool");
  const head = el("div", "tool__head");
  head.appendChild(el("strong", null, call.name));
  const risk = ["low", "medium", "high"].includes(call.risk) ? call.risk : "unknown";
  head.appendChild(el("span", `badge badge--${risk}`, `${risk} risk`));
  const ok = call.result === "ok";
  head.appendChild(el("span", `badge ${ok ? "badge--ok" : "badge--error"}`, ok ? "ok" : call.result));
  head.appendChild(el("span", "hint", `${call.latency_ms ?? "?"} ms`));
  box.appendChild(head);

  const args = el("ul", "tool__args");
  const values = call.arguments;
  if (values && typeof values === "object" && !Array.isArray(values)) {
    for (const [key, value] of Object.entries(values)) {
      args.appendChild(el("li", null, `${key}: ${formatValue(value)}`));
    }
  } else {
    args.appendChild(el("li", null, `arguments: ${formatValue(values)}`));
  }
  box.appendChild(args);
  return box;
}

/** Fill `container` with the trace of one reply. Every value is set as text. */
function renderTrace(container, reply) {
  const trace = reply.trace || {};
  const steps = Array.isArray(trace.steps) ? trace.steps : [];
  const tokens = trace.tokens || {};

  if (pendingApproval(reply.steps)) {
    container.appendChild(el("p", "badge badge--pending", "Pending human approval: not approved yet"));
  }

  section(
    container,
    "Summary",
    "Latency is the whole request; tokens are what the model read and wrote; cost uses list prices.",
  ).appendChild(
    factList([
      ["Model", formatValue(trace.model)],
      ["Reasoning effort", formatValue(trace.reasoning_effort)],
      ["Total latency", `${trace.total_latency_ms ?? "?"} ms`],
      ["Tokens", `${tokens.total ?? 0} (${tokens.prompt ?? 0} in, ${tokens.completion ?? 0} out)`],
      ["Cost", formatCost(trace.estimated_cost_usd)],
      ["Model steps", String(steps.length)],
    ]),
  );

  const stepsBox = section(
    container,
    "Steps",
    `Each step is one model call; its latency includes any retries. ${RISK_HELP}`,
  );
  if (!steps.length) {
    const blocked = (trace.guardrail_events || []).some((e) => e.layer === "input");
    stepsBox.appendChild(
      el("p", "trace__empty", blocked ? "No model call: the input guardrail answered." : "No model call was made."),
    );
  }
  for (const step of steps) {
    const box = el("div", "step");
    box.appendChild(
      el(
        "p",
        "step__title",
        `Step ${step.step}: ${step.latency_ms} ms, ${step.prompt_tokens} in / ${step.completion_tokens} out tokens`,
      ),
    );
    const calls = Array.isArray(step.tool_calls) ? step.tool_calls : [];
    if (!calls.length) box.appendChild(el("p", "hint", "No tool call: the model answered."));
    for (const call of calls) box.appendChild(renderToolCall(call));
    stepsBox.appendChild(box);
  }

  const events = Array.isArray(trace.guardrail_events) ? trace.guardrail_events : [];
  section(
    container,
    "Guardrails",
    "Input checks run before the model; output checks run on the final reply.",
  ).appendChild(
    events.length
      ? plainList(events.map((e) => `${e.layer} · ${e.rule} · ${e.action}`))
      : el("p", "trace__empty", "None fired."),
  );

  const retryLines = (Array.isArray(trace.retries) ? trace.retries : []).map(
    (r) => `${r.reason}, waited ${r.wait_s} s`,
  );
  if (trace.forced_retry_reason) retryLines.push(`forced a tool call: ${trace.forced_retry_reason}`);
  if (trace.recovered_error) retryLines.push(`recovered from: ${trace.recovered_error}`);
  section(
    container,
    "Retries and waits",
    "Calls repeated after a rate limit, a server error or a malformed tool call.",
  ).appendChild(retryLines.length ? plainList(retryLines) : el("p", "trace__empty", "None."));

  section(
    container,
    "Error code",
    "Set when the agent stopped early, e.g. a step limit or a refused input.",
  ).appendChild(el("p", trace.error ? "badge badge--error" : "trace__empty", trace.error || "None."));
}

/* ---------- start ------------------------------------------------------------------ */

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startPage);
  else startPage();
}

// Lets `node --test` check the pure formatter without a browser; ignored by the page.
if (typeof module !== "undefined") module.exports = { parseInline, parseReply, pendingApproval };
