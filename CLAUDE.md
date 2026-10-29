# Autonomous Office Coffee Refiller (Prototype)

Learning prototype: an agent that ingests a user comment telling the agent the office is low on coffee.  The agent then reorders it on its own; built to explore agentic purchasing protocols — **AP2** (Agent Payments Protocol), **UCP** (Universal Commerce Protocol) and **MCP** (Model Context Protocol).

Goal is understanding, not production. Favor clarity, readable logs of each protocol step, and mocked merchants/payments over real money.

## Stack

- Python 3.12, managed with `uv`; single process, all actors in one app
- FastAPI + uvicorn; the dashboard is one static page (`src/coffee_refiller/static/index.html`) fed by server-sent events
- Anthropic SDK for the shopping agent (model set in `agent.py`)
- Protocol SDKs: `mcp`, `ucp-sdk`, and the AP2 SDK (pinned git commit in `pyproject.toml`)
- Mock merchants and a Stripe-style mock processor; signing keys generated in memory at startup
- Tests: `uv run pytest` (no API key needed)

## Docs

Plans live in `docs/plans/`, issues in `docs/issues/`.

## Safety

- Never use real payment credentials. Payments are mocked or use a provider's test mode only.
