# Feature Implementation Plan: Agentic Coffee Reorder (v1)

**Overall Progress:** `100%`

## TLDR
A weekend learning prototype. A Slack-style chat message ("we're out of coffee") triggers a Claude-powered shopping agent. It finds coffee across 3 mock merchants over **MCP**, checks out with **UCP**, proves authority with **AP2** mandates, and pays through a mock Stripe-style processor. A small web dashboard shows the chat and a live timeline of every protocol message so each step can be studied.

## Critical Decisions
- **Real SDKs where they exist:** AP2 Python SDK (**V2 / 0.2.0**, from the `google-agentic-commerce/AP2` GitHub repo, pinned to commit `e1ea56d`, not the PyPI mirror) and `ucp-sdk` 0.5.0 for UCP models. Small fixes against a changing SDK are accepted. See `docs/sdk-notes.md`.
- **Python + uv, single process:** all actors run in one app; the merchants differ only by data (prices, stock, delivery days). This keeps it to a weekend.
- **Merchants are MCP servers that expose UCP tools:** this one step shows how MCP and UCP connect.
- **Claude Sonnet 5.5 via the Anthropic API:** a real agent rather than a script. The API key goes in a gitignored `.env`.
- **Policy enforced in code, not left to the LLM:** budget, deadline and approval checks run as plain functions the agent must pass. Claude chooses; code decides whether the choice is allowed.
- **Real AP2 signing with local keys:** V2 signs mandates as SD-JWT chains (ES256). The user's key is the root and the agent's key signs on top. Keys are generated locally; no real credentials, ever. *(Changed from "mock signing" in discovery: the SDK does real signing for free.)*
- **Approvals = a human-signed one-time mandate:** AP2 constraints are hard, so the agent can't sign past a limit. On approval, the user's key signs a one-time mandate for that exact checkout. *(Added in discovery.)* Approved orders **count toward the $80** (the ledger records real spend). One-time mandates **expire after 15 minutes**. Caveat: in v1 the user's key and the agent's key live in one process; this simulates the trust boundary rather than enforcing it.
- **Credentials Provider folded into the processor:** the AP2 samples have a separate role for it; one process keeps v1 small.
- **Mock processor imitates Stripe:** PaymentIntents create/confirm, test card `4242…`, webhook-style events.
- **Dashboard:** FastAPI + plain HTML/JS with server-sent events (SSE) for the live timeline, with no front-end framework.

## Open Mandates (v1 settings): AP2 V2's replacement for the "Intent Mandate"
- Per-order cap: **$40** → `AmountRange(max=4000)`
- Monthly budget: **$80** → `Budget(max=80)` + `AgentRecurrence(MONTHLY)`
- Preferred brands, ranked: **Stumptown → Counter Culture → Lavazza → Folgers** (fallback)
- Delivery deadline: **3 days** by default; the chat message can override it
- Selection: drop offers that miss the deadline → choose the highest-ranked brand within budget (not the cheapest)
- Approval required when: an order is over $40, OR it would push the month past $80, OR nothing arrives in time (relax the deadline, then ask)
- Out of scope for v1: unit/quantity limits, real Slack, real payments

## Tasks

- [x] 🟩 **Step 1: Project setup**
  - [x] 🟩 `uv` project, `pyproject.toml`, dependencies (AP2 from git, `ucp-sdk`, `mcp`, `anthropic`, `fastapi`)
  - [x] 🟩 `.gitignore` (incl. `.env`), `.env.example`, `git init`

- [x] 🟩 **Step 2: SDK discovery**
  - [x] 🟩 Inspect the AP2 SDK models: open/closed Checkout and Payment Mandates, receipts, plus the samples
  - [x] 🟩 Inspect the UCP SDK models: catalog, checkout, fulfillment, the AP2 extension, and the MCP binding
  - [x] 🟩 Write short notes in `docs/sdk-notes.md` (what we use, any gaps we mock)

- [x] 🟩 **Step 3: Event log**
  - [x] 🟩 In-process event bus; each protocol message is recorded with its actor, protocol (MCP/UCP/AP2/Stripe) and payload

- [x] 🟩 **Step 4: Mock merchants (MCP + UCP)**
  - [x] 🟩 Seed data for BeanCo, OfficeMart, GrocerGo (brands, prices, stock, delivery days), chosen so that every demo scenario can happen
  - [x] 🟩 MCP server exposing UCP tools: catalog search, create checkout, complete checkout
  - [x] 🟩 Merchant signs the checkout JWT (UCP checkout with the AP2 extension)

- [x] 🟩 **Step 5: Mock payment processor (Stripe-style)**
  - [x] 🟩 PaymentIntent create/confirm that accepts an AP2 Payment Mandate
  - [x] 🟩 Test card `4242…` succeeds; webhook-style event to the timeline
  - [x] 🟩 Idempotency keys (Stripe-style): repeating a request with the same key returns the original result and never charges twice

- [x] 🟩 **Step 6: Mandate + policy**
  - [x] 🟩 Open Checkout + Open Payment Mandates from the v1 settings, signed by the user's key
  - [x] 🟩 Monthly spend ledger
  - [x] 🟩 Policy check → `ok` / `needs_approval(reason)` / `reject`
  - [x] 🟩 Unit tests covering each policy branch (+ end-to-end protocol tests without Claude)

- [x] 🟩 **Step 7: Shopping agent (Claude)**
  - [x] 🟩 Tool-use loop with Sonnet 5.5 over the merchant MCP tools
  - [x] 🟩 Agent's MCP client logs every `tools/call` to the timeline; long tokens (mandates, JWTs) are held by code and given to Claude as short handles
  - [x] 🟩 Run parallel tool calls at the same time (Claude searches all 3 merchants in one turn)
  - [x] 🟩 Flow: parse request (incl. a deadline override) → gather offers → filter by deadline → rank by brand within budget → policy check → closed Checkout Mandate → closed Payment Mandate → processor → receipt
  - [x] 🟩 Over a limit → ask in chat → on approval, the user's key signs a one-time mandate for that checkout
  - [x] 🟩 Nothing arrives in time → relax the deadline to the earliest available → ask for approval
  - [x] 🟩 Log each Claude decision and its reasoning to the timeline

- [x] 🟩 **Step 8: Dashboard**
  - [x] 🟩 Slack-style chat panel (user messages, agent replies, approve/deny buttons)
  - [x] 🟩 Live protocol timeline via SSE, color-coded by protocol, with expandable payloads
  - [x] 🟩 Mandate panel: limits, spend this month, brand ranking

- [x] 🟩 **Step 9: Demo scenarios + README**
  - [x] 🟩 Happy path: preferred brand, under both caps, on time
  - [x] 🟩 Over $40 per order → approval
  - [x] 🟩 Under $40 but over the $80 month → approval
  - [x] 🟩 Nothing within 3 days → relaxed deadline → approval
  - [x] 🟩 README: setup, run, and a walkthrough of what to watch in each scenario

## Verification (2026-10-29)
- 21 automated tests pass (policy + end-to-end protocol flows, no Claude)
- Live with Claude Sonnet 5.5: scenarios 1 and 4 from the CLI; scenarios 2 (Deny) and 3 (Approve) in the dashboard
- Dashboard checked at desktop and 375px phone width

## Follow-ups noticed
- Claude occasionally misstates budget arithmetic in its chat reply (said the budget was "used up" at $70/$80); the decision was right. Candidate for an eval.
- Claude handles thinking blocks: they're echoed back with signatures and shown on the timeline.
