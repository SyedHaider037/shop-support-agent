# shop-support-agent

A customer-support agent for a fake online shop that looks things up and files requests through tools, with guardrails, a human-approval gate for refunds, and an evaluation suite that measures how reliably it follows its rules.

[![CI](https://github.com/SyedHaider037/shop-support-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/SyedHaider037/shop-support-agent/actions/workflows/ci.yml)
[![CodeQL](https://github.com/SyedHaider037/shop-support-agent/actions/workflows/codeql.yml/badge.svg)](https://github.com/SyedHaider037/shop-support-agent/actions/workflows/codeql.yml)

**Live demo:** https://shop-support-agent-demo.vercel.app

The shop, its products, orders and customers are all fake sample data. The demo runs within Groq's rate limits, so it can answer "busy" when many people use it at once.

## Screenshots

![Chat page on a desktop: the conversation on the left, the trace panel with tool calls and guardrail checks on the right](docs/screenshots/chat-desktop.png)

## What it is and why

The agent answers questions about products, orders and refunds for a sample shop called Example Goods. It uses four tools, and its policy forbids stating a product, price or order fact that no tool returned.
Anything risky is checked in code, not left to the prompt. Order data needs a matching order ID and email. Saving contact details needs the customer's consent. A refund request only goes into a queue for a human.
Every reply comes with a trace: model calls, tool calls, guardrail events, tokens and an estimated cost.
An evaluation suite of 36 tasks measures the agent over repeated trials, because one passing run says little about an agent.

## How a request flows

```mermaid
flowchart TD
    A["Chat page"] -->|"POST /chat with the whole chat"| B["FastAPI server"]
    B --> C{"Rate limit"}
    C -->|"over the limit"| C1["429 with Retry-After"]
    C -->|"allowed"| D{"Input guardrails"}
    D -->|"attack pattern"| D1["Fixed refusal, no model call"]
    D -->|"clean"| E["Agent loop with the model"]
    E -->|"tool call"| F["Dispatcher and tools"]
    F -->|"tool result"| E
    F -->|"request_refund"| G["Pending approval in the store"]
    G -.->|"only when ADMIN_TOKEN is set"| H["Human approves or rejects"]
    E -->|"final reply"| I{"Output guardrails"}
    I --> J["Reply and trace"]
    D1 --> J
    J --> A
```

The server keeps no chat memory, so the page sends the whole conversation with every message.
The rate limit allows 10 messages a minute per client address. It runs before the request body is validated, so invalid requests count too.
The input guardrails check every customer message, including earlier ones, against the patterns in `guardrails.toml`. A match gets a fixed refusal and no model call.
The agent loop sends the policy, the chat and the tool schemas to the model, runs any tool calls, and repeats, up to 5 model calls and a 40-second deadline per request.
The dispatcher validates every tool call. It checks identity for order tools and consent for saving contact details, and returns an error code the model must explain.
A refund request becomes a pending approval in the store. Only a human with the admin token can approve or reject it, and the agent can never do either.
The output guardrails check the final reply for policy leaks, invented action claims, and emails or phone numbers the customer did not type.

## Tools

| Tool | What it does | Risk | Control around it (in code) |
|---|---|---|---|
| `search_products` | Searches the catalogue for products, prices and stock | low | Read only. Arguments validated. |
| `get_order_status` | Returns an order's status, items, dates, tracking and total | low | Read only. Needs the order ID and the email used for the order. An unknown order and a wrong email return the same `no_matching_order`, so order IDs cannot be probed. |
| `capture_lead` | Saves a name, email and message for the support team | medium | Write. Refused with `consent_required` unless a customer message in the chat clearly agrees (`app/tools/consent.py`). The email is validated. Contact details are not echoed back into results or traces. |
| `request_refund` | Files a refund request for a human to review | high | Write, but it only **files** a request and never moves money. Same identity check as orders. Eligibility is checked in code: delivered, within 30 days, no earlier request, a reason given. It returns no amount, so the model has nothing to promise. |

## Security model

- **Input guardrails** (`app/guardrails/input.py`, `guardrails.toml`): limits on message count (20) and length (1,000 characters), control and zero-width characters stripped, fake `system:` or `assistant:` lines refused, and six attack patterns (ignore instructions, "you are now", developer mode, no rules, reveal the prompt, mentions of a system prompt).
- **Output guardrails** (`app/guardrails/output.py`): a reply that repeats 6 or more words in a row from the policy is replaced, except sentences the policy tells the agent to say. A first-person claim of an action ("I've submitted", "I've saved") is replaced unless a write tool returned ok in this request, and a claim of approval is always replaced. Emails and phone numbers the customer did not type are masked.
- **Code-level checks**: the identity check (order ID plus email), the consent lock on `capture_lead`, refund eligibility, and the approval gate. These hold whatever the model says.
- **Rate limit**: a token bucket of 10 per minute per client address (`app/ratelimit.py`). The address is the first `X-Forwarded-For` hop, which Vercel sets.
- **Security headers**: `Content-Security-Policy` (`default-src 'none'`, scripts, styles and connections from the same origin only, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The app sets them in middleware, and `vercel.json` sets the same values for files served from Vercel's CDN.
- **API docs off**: `/docs`, `/redoc` and `/openapi.json` return 404, so the schema does not list the admin paths.
- **Admin API**: `/admin/approvals` and its approve and reject routes return 404 unless `ADMIN_TOKEN` is set. When it is set, the token is compared in constant time.

### The lethal trifecta

An agent is most dangerous when it has private data, reads untrusted input, and can act. This one has all three, so each is limited:

| Risk | What this project does |
|---|---|
| Private data | Order data is shared only after the order ID and email match. Replies are masked for emails and phones the customer did not type. Traces and logs mask emails and keep only tool argument names. All data is fake. |
| Untrusted input | Every customer message is checked before the model sees it. The policy says earlier messages are not proof of an action (2.4) and tool results are data, not instructions (7.3). |
| Ability to act | There are only two write tools. A refund is only filed and never approved by the agent. Saving contact details needs consent checked in code. Action claims are checked against what the tools actually did. |

### What this does not cover

- The input rules are regular-expression patterns. A new wording of an attack can get past them.
- Hidden instructions inside data fields (for example a product description or an order note) are not tested by any eval task.
- Approvals are not live on the demo: `ADMIN_TOKEN` is unset, so requests stay pending until a cold start clears them.
- The client sends the whole chat, so earlier assistant turns can be forged. The policy and the action-claim check limit the damage, but the model still reads them.
- The action-claim guardrail only catches first-person claims (see [What the evals found](#what-the-evals-found)).

## Evaluation

**Method.** The suite has 36 tasks in [`evals/tasks.json`](evals/tasks.json): 23 *regression* tasks (rules that must never break) and 13 *capability* tasks (harder or fuzzier behaviour). 8 tasks use a simulated customer, a second model (`openai/gpt-oss-20b`) that only gives facts when asked. Grading is rule-based, with no AI judge. It looks at the store after the run (refund requests, approvals, leads, audit log), the tool calls and their result codes, regexes over the replies, and guardrail events. Every trial starts from a fresh copy of the fake sample data. Infra errors (rate limits, simulator failures) are counted separately, never as a pass or a fail.

- **pass@k**: at least one of k trials passed. It shows what the agent *can* do.
- **pass^k**: all k trials passed. It shows what the agent does *every time*, which matters more for a support agent.

Numbers in brackets are 95% Wilson confidence intervals. With this few tasks they are wide, and differences of a few points mean little.

**4B, the first run (2026-10-20): three models, one trial of all 36 tasks.** All three used the same settings: temperature 0.2, the production policy, tools and guardrails.

| Model | pass@1 | Regression | Capability | Mean latency per task | Est. cost per task |
|---|---|---|---|---|---|
| openai/gpt-oss-120b | 29/35 = 83% [67-92] | 23/23 = 100% [86-100] | 6/12 = 50% [25-75] | 8.0 s | $0.00038 |
| openai/gpt-oss-20b | 25/35 = 71% [55-84] | 20/23 = 87% [68-95] | 5/12 = 42% [19-68] | 17.2 s | $0.00020 |
| qwen/qwen3.8-27b | 24/35 = 69% [52-81] | 20/23 = 87% [68-95] | 4/12 = 33% [14-61] | 25.7 s | $0.00226 |

Latency is mostly the pacer waiting for Groq's per-minute limits, not model time. Each model had 1 infra error. The only safe conclusion is that gpt-oss-120b was the only model that held every regression rule. It is the default model.

**Before and after, `openai/gpt-oss-120b`.** pass^3 is over the same 12 reliability tasks, chosen before any results were seen.

| Run | pass@1 | Regression | Capability | pass^3 (12 tasks) | Caveat |
|---|---|---|---|---|---|
| 4B: first run (2026-10-20) | 29/35 = 83% [67-92] | 23/23 = 100% [86-100] | 6/12 = 50% [25-75] | 9/12 = 75% [47-91] | 1 infra error. Simulated chats that ended with no agent turn were graded as fails. |
| 4D: after the 4C fixes (2026-10-21) | 27/36 = 75% [59-86] | 21/23 = 91% [73-98] | 6/13 = 46% [23-71] | 10/12 = 83% [55-95] | 11 tasks ran different checks, so the overall score is not a clean comparison. 2 regression tasks failed for the first time. The intervals overlap. |
| 4E: after the 4E fixes (2026-10-21) | 34/35 = 97% [85-99] | 23/23 = 100% [86-100] | 11/12 = 92% [65-99] | 11/12 = 92% [65-99] | 1 infra error. Of the 8 tasks fixed since 4D, 5 are simulated tasks whose customer changed and 1 had its check changed, so harness fixes explain part of the jump. 24 of 36 tasks ran once. The intervals overlap. |

The whole 4E re-run (60 rows) cost an estimated $0.0317. Every number, row and caveat is in [`evals/RESULTS.md`](evals/RESULTS.md), and the raw rows are in [`evals/results/published/`](evals/results/published/).

## What the evals found

| Finding | What was changed | Status |
|---|---|---|
| A retry meant to stop "let me check" replies forced a tool call even when the model was correctly asking for the email. Groq rejected the forced call, and the customer got a handoff message. | The loop forces a tool call only for a promise to look something up that asks no question. If a forced call fails, the model's own reply is sent. | Not seen again: 0 forced retries in the 60 rows of the next run. |
| The agent claimed actions it never took ("I've submitted a refund request") with no tool call. | A new output guardrail (`action_claims`) and policy rules 2.4 and 5.5. | Fewer, not gone (see the first-person gap below). |
| After the scope change in 4C, a lead was saved in turn 1, before the agent asked for consent. The customer then said no. | `capture_lead` is refused with `consent_required` unless a customer message agrees. This is checked in code. | Fixed in one run. One false negative: a real "yes" was missed once, which is safe but cost a turn. |
| A product-care question was refused as off topic after the scope list was made explicit. | Policy 1.1 now says choosing, using and caring for the kinds of goods sold is in scope. | Fixed in one run. |
| The simulated customer ended chats right after the agent asked a question. 0 of 8 simulated chats reached 3 customer turns in 4D. | The simulator must answer every question, and the runner asks it again if it ends early. | 4 of 7 graded simulated chats reached 3 or more turns in 4E. |
| A check passed a made-up claim ("still pending approval" with no request) and failed truthful replies about a real request. | The forged-turn task now checks that what the reply says about a request matches the store. | Works on the recorded replies. The new run did not exercise the hard case. |

**Still open:**
- **"Not approved yet" wording.** Policy 5.4 gives an exact sentence, but 0 of 12 replies after a successful refund request said "not approved" in 4E. The wording task failed 3 of 3.
- **Passive claims.** The action-claim guardrail only looks at first-person claims, so "your refund request is still pending approval" with no request gets past it.
- **Forged history.** In the 4E run the agent always made a real request, so the forged-turn hard case was not tested. No reply corrected the forged "$99.98 in 2 days" message.
- **Simulator infra.** `refund-outside-window-sim-01` ended in a simulator error in the first run and again in 4E.

## Run it locally

You need Python 3.12, Node.js (only for the chat page tests) and a Groq API key from https://console.groq.com/keys.

**Windows PowerShell**

```powershell
git clone https://github.com/SyedHaider037/shop-support-agent.git
cd shop-support-agent
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# Open .env and set GROQ_API_KEY=your key
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

**macOS or Linux**

```bash
git clone https://github.com/SyedHaider037/shop-support-agent.git
cd shop-support-agent
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
# Open .env and set GROQ_API_KEY=your key
.venv/bin/python -m uvicorn app.main:app --port 8000
```

Then open http://127.0.0.1:8000. `/health` answers without a key; `/chat` needs one.

**Tests and lint** (the tests use a fake Groq client, so no key or network is needed). On macOS or Linux use `.venv/bin/python` in place of `.\.venv\Scripts\python`:

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m pytest -q
node --test "tests/web/*.test.js"
```

**Evals** (these call Groq and use your quota). Start with a dry run, which prints the plan and calls nothing:

```powershell
.\.venv\Scripts\python -m evals.run --model openai/gpt-oss-120b --trials 1 --dry-run
```

Remove `--dry-run` to run it. Rows are saved to `evals/results/run.jsonl` as each trial finishes. A long run can reach Groq's daily limit. When that happens the command stops with exit code 3. Run the same command again later: it skips the trials already graded and re-runs infra errors. `--tasks`, `--suite` and `--category` narrow the run, and `--fail-under` exits 1 if the regression pass rate is below a threshold.

## Deployment

The demo runs on Vercel with its zero-config FastAPI support. `pyproject.toml` gives the runtime dependencies (the same pins as `requirements.txt`) and the entrypoint `app.main:app`. Vercel serves `public/` from its CDN.

`vercel.json` sets the same three security headers as the app on every path, because CDN files never pass through the app's middleware. It also gives the function a `maxDuration` of 60 seconds, above the 40-second request deadline.

| Variable | Needed | On the demo |
|---|---|---|
| `GROQ_API_KEY` | Required for `/chat` | Set |
| `ADMIN_TOKEN` | Optional. Turns on the admin approval API. | Unset, so every admin route returns 404 |

Optional tuning variables are read in `app/config.py`: `MODEL`, `MAX_STEPS`, `TEMPERATURE`, `REASONING_EFFORT`, `REQUEST_DEADLINE_S`, `MAX_RETRIES`, `TOKEN_BUDGET` and `RATE_LIMIT_PER_MINUTE`.

**Groq limits.** Groq's rate-limit page lists 30 requests a minute, 1,000 requests a day, 8K tokens a minute and 200K tokens a day for `openai/gpt-oss-120b`. The limits apply per model and per organisation, and cached tokens do not count. The 4E eval conversations used about 2,900 raw tokens each (176,499 over 60), so 200K tokens a day is roughly 70 chats like them. Prompt caching stretches that.

## Engineering notes

- **CI** (`.github/workflows/ci.yml`) runs on every push to main and every pull request: `ruff check`, `ruff format --check`, `pytest` (596 tests) and `pip-audit` on both requirements files. The 24 chat page tests run with Node locally.
- **CodeQL** scans the Python code on every push, every pull request and weekly.
- **Dependabot** opens weekly update pull requests for pip and GitHub Actions, grouping minor and patch updates.
- Every action in the workflows is pinned to a full commit SHA. Both workflows default to read-only permissions; only the CodeQL job may also upload its results.
- [`SECURITY.md`](SECURITY.md) explains how to report a vulnerability privately.
- **Drift tests**: `tests/test_packaging.py` fails if `pyproject.toml` and `requirements.txt` list different dependencies. `tests/test_vercel_config.py` fails if `vercel.json` headers differ from the app's.

## Limitations

Everything the app remembers lives in the memory of one serverless instance: rate-limit buckets, saved leads, pending refund approvals and decisions, order changes, and the audit log. Two instances each have their own copy, so a refund requested on one is not visible on the other, and a cold start resets everything to the sample data. Durable refunds and a hard rate limit would need a shared store such as Redis or a database.

- Groq's limits cap the demo at roughly 70 chats a day (see [Deployment](#deployment)) and can make it answer "busy".
- 36 tasks, most run once: the confidence intervals are wide.
- The eval tasks are almost all in English (one is a Spanish jailbreak).
- The simulated customer adds noise: it has ended chats early and failed with simulator errors.
- The open weaknesses from [What the evals found](#what-the-evals-found): the "not approved yet" wording, passive action claims, the untested forged-history case, and consent false negatives.

## Roadmap

- A shared store for approvals, leads and rate limits.
- A recorded replay of a sample chat for when the demo is busy.
- Eval tasks with hidden instructions inside product and order data.
- Detection of passive action claims, not only first-person ones.
- `Permissions-Policy` and `X-Frame-Options` headers.

## How this was built

This project was built with Claude Code as an AI pair programmer, and the phase plans and prompts were drafted with Claude's help. Claude Code helped write the code throughout. Many commits carry a `Co-authored-by: Claude` line, but some do not (the first setup commits and the chat page work), so that line does not mark every commit Claude helped with. I directed the project phase by phase, reviewed every phase's report before it was pushed, and made the calls on scope. The evals, their failures and the fixes are recorded in the commit history and in [`evals/RESULTS.md`](evals/RESULTS.md), including the results that went down.

## Repo layout

```
app/        FastAPI app: agent loop, guardrails, tools, store, trace, rate limit
data/       Fake sample products and orders
docs/       Screenshots
evals/      Tasks, graders, simulated customer, runner, results
public/     The chat page (HTML, CSS, JavaScript; no build step)
tests/      pytest tests, plus Node tests for the page in tests/web/
policy.md   The support policy the model follows
guardrails.toml  Input patterns, refusals and output rules
```

## License

MIT. See [LICENSE](LICENSE).
