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
        const data = await response.json();
        if (typeof data.reply !== "string") throw new Error("bad response");
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

function renderTrace(container, reply) {
  const trace = reply.trace || {};
  const facts = el("dl", "facts");
  const add = (label, value) => {
    facts.appendChild(el("dt", null, label));
    facts.appendChild(el("dd", null, value));
  };
  add("Latency", `${trace.total_latency_ms ?? "?"} ms`);
  add("Tokens", String((trace.tokens && trace.tokens.total) ?? "?"));
  container.appendChild(facts);
}

/* ---------- start ------------------------------------------------------------------ */

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startPage);
  else startPage();
}

// Lets `node --test` check the pure formatter without a browser; ignored by the page.
if (typeof module !== "undefined") module.exports = { parseInline, parseReply };
