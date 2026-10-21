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

/**
 * "You" / "Assistant" at the top of a message. A screen reader hears it once, as
 * "Assistant: ..." (the colon is for speech only), and it is plain text, so the live
 * region announces each new message once, with no extra hint attached.
 */
function roleLabel(role) {
  const label = el("span", "msg__role", role);
  label.appendChild(el("span", "visually-hidden", ":"));
  return label;
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

/** True when a scroll box is within `slack` px of its end: the reader is following along. */
function isNearEnd(scrollHeight, scrollTop, clientHeight, slack = 48) {
  return scrollHeight - (scrollTop + clientHeight) <= slack;
}

/** Requests, tokens and estimated cost summed over the traces of one chat. */
function sessionTotals(traces) {
  const totals = { requests: 0, tokens: 0, cost: null };
  for (const trace of traces || []) {
    totals.requests += 1;
    const tokens = Number(trace && trace.tokens && trace.tokens.total);
    if (Number.isFinite(tokens)) totals.tokens += tokens;
    const cost = trace && trace.estimated_cost_usd;
    if (typeof cost === "number" && Number.isFinite(cost)) totals.cost = (totals.cost || 0) + cost;
  }
  return totals;
}

/** The running line under the chat, or "" while the chat is empty. */
function formatTotals(totals) {
  if (!totals || !totals.requests) return "";
  const requests = `${totals.requests} ${totals.requests === 1 ? "request" : "requests"}`;
  const tokens = `${totals.tokens.toLocaleString("en")} tokens`;
  const cost =
    totals.cost === null
      ? "cost not available for this model"
      : `about $${totals.cost.toFixed(totals.cost < 0.01 ? 5 : 4)} (estimated at list prices)`;
  return `This chat: ${requests}, ${tokens}, ${cost}`;
}

/** What a sample-order button puts in the message box. */
function orderPrompt(orderId, email) {
  return `Where is my order ${orderId}? My email is ${email}.`;
}

/* ---------- page ----------------------------------------------------------------- */

function startPage() {
  const $ = (id) => document.getElementById(id);
  const ui = {
    messages: $("messages"),
    typing: $("typing"),
    status: $("status"),
    totals: $("totals"),
    samples: $("samples"),
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

  // From 900 px the list scrolls on its own; on a phone the page is the only scroller.
  const listScrolls = () => getComputedStyle(ui.messages).overflowY !== "visible";

  /** Is the reader at the newest message, or did they scroll up to read older ones? */
  function isFollowing() {
    if (listScrolls()) {
      return isNearEnd(ui.messages.scrollHeight, ui.messages.scrollTop, ui.messages.clientHeight);
    }
    // On a phone: is the latest message (usually the reader's own) still on screen?
    const last = ui.messages.lastElementChild;
    if (!last) return true;
    const box = last.getBoundingClientRect();
    return box.top < window.innerHeight && box.bottom > 0;
  }

  /**
   * Show a new message. Measure `follow` before adding it: a reader who scrolled up keeps
   * their place. The jump is instant (no smooth scroll), so it is the same with reduced motion.
   */
  function reveal(node, follow) {
    if (!follow) return;
    if (listScrolls()) ui.messages.scrollTop = ui.messages.scrollHeight;
    node.scrollIntoView({ block: "nearest" });
  }

  function updateCounter() {
    const length = ui.input.value.length;
    ui.counter.textContent = `${length.toLocaleString("en")} / ${MAX_CHARS.toLocaleString("en")}`;
    ui.counter.classList.toggle("is-over", length > MAX_CHARS);
  }

  function updateSend() {
    ui.send.disabled = state.busy || Date.now() < state.waitUntil;
  }

  /**
   * An empty chat leads with the examples, above the message box; once a message is
   * sent they move below it. The DOM node moves (not just CSS order), so Tab follows
   * what the eye sees.
   */
  function placeSamples(hasMessages) {
    if (hasMessages) {
      if (ui.samples.previousElementSibling !== ui.form) ui.form.after(ui.samples);
    } else if (ui.samples.nextElementSibling !== ui.form) {
      ui.form.before(ui.samples);
    }
  }

  function updateTotals() {
    const text = formatTotals(sessionTotals(state.replies.map((reply) => reply.trace)));
    ui.totals.textContent = text;
    ui.totals.hidden = !text;
  }

  function showStatus(text) {
    ui.status.textContent = text;
    ui.status.hidden = !text;
  }

  function addNotice(text) {
    const follow = isFollowing();
    const li = el("li", "msg msg--notice", text);
    ui.messages.appendChild(li);
    reveal(li, follow);
    return li;
  }

  function addUserMessage(text) {
    const li = el("li", "msg msg--user");
    li.appendChild(roleLabel("You"));
    li.appendChild(el("span", "msg__text", text)); // textContent: user text is never HTML
    ui.messages.appendChild(li);
    reveal(li, true); // the reader just sent it
    return li;
  }

  /** Disable Send for `seconds` and show a live countdown. The notice in the chat says why. */
  function waitFor(seconds) {
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
      showStatus(`You can send again in ${left} s.`);
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
    const follow = isFollowing();
    const index = state.replies.length;
    const li = el("li", "msg msg--assistant is-clickable");
    li.tabIndex = 0;
    // The key hint is a description, read on focus; an aria-label here would replace the
    // reply text for screen readers, and inside the live region it would be announced too.
    li.setAttribute("aria-describedby", "reply-hint");
    li.appendChild(roleLabel("Assistant"));
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
    updateTotals();
    selectReply(index);
    reveal(li, follow);
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
    userNode.appendChild(el("span", "msg__unsent", "Not sent"));
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
    placeSamples(true);
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
        waitFor(retryAfter || 30);
      } else if (response.status === 503 && retryAfter) {
        failed(userNode, text, "The AI model is busy right now (its free usage limit was reached).");
        waitFor(retryAfter);
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
    updateTotals();
    placeSamples(false);
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
  const fillBox = (text) => {
    ui.input.value = text; // fills the box; sending stays the user's choice
    updateCounter();
    ui.input.focus();
  };
  for (const chip of document.querySelectorAll(".chip[data-prompt]")) {
    chip.addEventListener("click", () => fillBox(chip.dataset.prompt));
  }
  for (const pick of document.querySelectorAll(".order-pick[data-order][data-email]")) {
    pick.addEventListener("click", () => fillBox(orderPrompt(pick.dataset.order, pick.dataset.email)));
  }
  updateCounter();
  updateSend();
}

/* ---------- trace panel ------------------------------------------------------------ */

/**
 * What the guardrails, retries and error code say for one trace, as plain strings.
 * `quiet` lists the checks with nothing to report; `line` sums them up in one sentence.
 */
function traceChecks(trace) {
  const t = trace || {};
  const guardrails = (Array.isArray(t.guardrail_events) ? t.guardrail_events : []).map(
    (e) => `${e.layer} · ${e.rule} · ${e.action}`,
  );
  const retries = (Array.isArray(t.retries) ? t.retries : []).map((r) => `${r.reason}, waited ${r.wait_s} s`);
  if (t.forced_retry_reason) retries.push(`forced a tool call: ${t.forced_retry_reason}`);
  if (t.recovered_error) retries.push(`recovered from: ${t.recovered_error}`);
  const error = t.error || null;

  const quiet = [];
  if (!guardrails.length) quiet.push("no guardrails fired");
  if (!retries.length) quiet.push("no retries");
  if (!error) quiet.push("no errors");
  return {
    guardrails,
    retries,
    error,
    allQuiet: quiet.length === 3,
    line: quiet.length ? `Checks: ${quiet.join(", ")}.` : null,
  };
}

/** A result or error code in plain words: "no_matching_order" -> "no matching order". */
function codeWords(code) {
  if (code === undefined || code === null || code === "") return "unknown";
  return String(code).replace(/[_-]+/g, " ").trim().toLowerCase() || "unknown";
}

/** Labelled pills: the words carry the meaning, the colour is extra. */
function riskPill(risk) {
  const level = ["low", "medium", "high"].includes(risk) ? risk : "unknown";
  return { text: `Risk: ${level}`, tone: level };
}

function resultPill(code) {
  const ok = code === "ok";
  return { text: `Result: ${ok ? "ok" : codeWords(code)}`, tone: ok ? "ok" : "error" };
}

function pill(info) {
  return el("span", `badge badge--label badge--${info.tone}`, info.text);
}

/*
 * One plain sentence per tool result, from the codes in app/tools (handlers.py,
 * refund_rules.py, dispatcher.py). Rule based: no model is involved, and no sentence
 * repeats customer data, so nothing here can show more than the (masked) trace already does.
 */
const TOOL_SENTENCES = {
  search_products: {
    ok: "Searched the product catalogue.",
  },
  get_order_status: {
    ok: "Looked up the order and found it.",
    no_matching_order: "Looked up the order but found no match, so it asked the customer to double-check.",
  },
  request_refund: {
    ok: "Sent the refund request to a human for approval; nothing is approved yet.",
    no_matching_order: "Tried to request a refund, but the order number and email did not match.",
    not_delivered: "Tried to request a refund, but the order has not been delivered yet.",
    outside_window: "Tried to request a refund, but the order was delivered more than 30 days ago.",
    already_requested: "Tried to request a refund, but one was already requested for this order.",
    reason_required: "Tried to request a refund, but no reason was given.",
  },
  capture_lead: {
    ok: "Saved the customer's contact details for the support team.",
  },
};

// Codes the dispatcher can return for any tool.
const ANY_TOOL_SENTENCES = {
  unknown_tool: (tool) => `Asked for a tool called ${tool} that does not exist, so the call was rejected.`,
  invalid_json: (tool) => `Called ${tool} with arguments that were not valid JSON, so the call was rejected.`,
  invalid_arguments: (tool) => `Called ${tool} with arguments that failed validation, so the call was rejected.`,
  tool_failed: (tool) => `Called ${tool}, but the tool failed with an internal error.`,
  consent_required: (tool) =>
    `Called ${tool}, but the customer had not agreed to have their details saved, so nothing was saved.`,
};

function callSentence(tool, code) {
  const name = tool || "an unnamed tool";
  const known = TOOL_SENTENCES[name] && TOOL_SENTENCES[name][code];
  if (known) return known;
  if (ANY_TOOL_SENTENCES[code]) return ANY_TOOL_SENTENCES[code](name);
  if (code === "ok") return `Called ${name}.`;
  return `Called ${name}, which returned "${codeWords(code)}".`;
}

/** One line for a model step: what its tool calls did, or that it wrote the reply. */
function stepSentence(step) {
  const calls = step && Array.isArray(step.tool_calls) ? step.tool_calls : [];
  if (!calls.length) return "Wrote the reply.";
  return calls.map((call) => callSentence(call && call.name, call && call.result)).join(" ");
}

/**
 * Each step's share of the model time (the sum of step latencies): `percent` for the text,
 * `width` for the bar, never under 3 so a fast step still shows.
 */
function stepShares(latencies) {
  const values = (latencies || []).map((ms) => (Number.isFinite(Number(ms)) && Number(ms) > 0 ? Number(ms) : 0));
  const total = values.reduce((sum, ms) => sum + ms, 0);
  return values.map((ms) => {
    const share = total > 0 ? (ms / total) * 100 : 100 / values.length;
    return { percent: Math.round(share), width: Math.max(3, Math.round(share * 10) / 10) };
  });
}

function section(container, title) {
  const wrap = el("section", "trace__section");
  wrap.appendChild(el("h3", null, title));
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
  head.appendChild(pill(riskPill(call.risk)));
  head.appendChild(pill(resultPill(call.result)));
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

  section(container, "Summary").appendChild(
    factList([
      ["Model", formatValue(trace.model)],
      ["Reasoning effort", formatValue(trace.reasoning_effort)],
      ["Total latency", `${trace.total_latency_ms ?? "?"} ms`],
      ["Tokens", `${tokens.total ?? 0} (${tokens.prompt ?? 0} in, ${tokens.completion ?? 0} out)`],
      ["Cost", formatCost(trace.estimated_cost_usd)],
      ["Model steps", String(steps.length)],
    ]),
  );

  const stepsBox = section(container, "Steps");
  if (!steps.length) {
    const blocked = (trace.guardrail_events || []).some((e) => e.layer === "input");
    stepsBox.appendChild(
      el("p", "trace__empty", blocked ? "No model call: the input guardrail answered." : "No model call was made."),
    );
  }
  const shares = stepShares(steps.map((step) => step.latency_ms));
  steps.forEach((step, index) => {
    const box = el("div", "step");
    box.appendChild(
      el(
        "p",
        "step__title",
        `Step ${step.step}: ${step.latency_ms} ms, ${step.prompt_tokens} in / ${step.completion_tokens} out tokens`,
      ),
    );
    // The bar is decoration; the same fact is in the words next to it.
    const share = el("div", "step__share");
    const bar = el("span", "step__bar");
    bar.setAttribute("aria-hidden", "true");
    const fill = el("span", "step__fill");
    fill.style.width = `${shares[index].width}%`; // CSSOM, allowed by the CSP (no style attribute)
    bar.appendChild(fill);
    share.appendChild(bar);
    share.appendChild(el("span", "step__share-text", `${shares[index].percent}% of model time`));
    box.appendChild(share);
    box.appendChild(el("p", "step__summary", stepSentence(step)));
    const calls = Array.isArray(step.tool_calls) ? step.tool_calls : [];
    for (const call of calls) box.appendChild(renderToolCall(call));
    stepsBox.appendChild(box);
  });

  // Only what happened gets a section; everything quiet is said in one line.
  const checks = traceChecks(trace);
  if (checks.guardrails.length) section(container, "Guardrails").appendChild(plainList(checks.guardrails));
  if (checks.retries.length) section(container, "Retries and waits").appendChild(plainList(checks.retries));
  if (checks.error) {
    section(container, "Error code").appendChild(
      el("p", "badge badge--label badge--error", `Error: ${codeWords(checks.error)}`),
    );
  }
  if (checks.line) container.appendChild(el("p", "trace__checks", checks.line));
}

/* ---------- start ------------------------------------------------------------------ */

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startPage);
  else startPage();
}

// Lets `node --test` check the pure helpers without a browser; ignored by the page.
if (typeof module !== "undefined") {
  module.exports = {
    parseInline,
    parseReply,
    pendingApproval,
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
    orderPrompt,
  };
}
