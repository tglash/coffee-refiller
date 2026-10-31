"""Dashboard backend: chat in, live protocol timeline out (server-sent events)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

STATIC = Path(__file__).parent / "static"
load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # explicit path: find_dotenv is unreliable here

from coffee_refiller.agent import ShoppingAgent  # noqa: E402  (needs the API key loaded first)
from coffee_refiller.catalog import MERCHANTS  # noqa: E402
from coffee_refiller.events import bus  # noqa: E402
from coffee_refiller.ledger import ledger  # noqa: E402
from coffee_refiller.processor import MockStripe  # noqa: E402
from coffee_refiller.settings import SETTINGS  # noqa: E402

app = FastAPI(title="Coffee Refiller")
_tasks: set[asyncio.Task[None]] = set()  # keep background runs referenced until done


def _new_agent() -> ShoppingAgent:
    return ShoppingAgent(SETTINGS, ledger, MockStripe(ledger))


agent = _new_agent()


class ChatIn(BaseModel):
    text: str


class DecisionIn(BaseModel):
    approved: bool


class SpendIn(BaseModel):
    cents: int


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state() -> dict[str, Any]:
    return {
        "per_order_cap_cents": SETTINGS.per_order_cap_cents,
        "monthly_budget_cents": SETTINGS.monthly_budget_cents,
        "month_spend_cents": ledger.month_spend_cents(),
        "preferred_brands": list(SETTINGS.preferred_brands),
        "deadline_days": SETTINGS.deadline_days,
        "merchants": [m.name for m in MERCHANTS],
        "busy": agent.busy,
    }


@app.post("/api/chat", status_code=202)
async def chat(body: ChatIn) -> dict[str, str]:
    if agent.busy:
        raise HTTPException(409, "The agent is still working on the last message.")
    if not body.text.strip():
        raise HTTPException(400, "Empty message.")
    agent.busy = True  # claim before the task starts, so double-submits are refused
    task = asyncio.create_task(agent.handle_message(body.text.strip()))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"status": "accepted"}


@app.post("/api/approvals/{approval_id}")
def decide(approval_id: str, body: DecisionIn) -> dict[str, bool]:
    if not agent.decide(approval_id, body.approved):
        raise HTTPException(404, "No pending approval with that id.")
    return {"ok": True}


@app.post("/api/demo/reset")
def reset() -> dict[str, bool]:
    """Fresh month, fresh timeline, freshly signed standing mandates."""
    global agent
    if agent.busy:
        raise HTTPException(409, "Wait for the agent to finish first.")
    ledger.reset()
    bus.clear()
    agent = _new_agent()
    return {"ok": True}


@app.post("/api/demo/spend")
def seed_spend(body: SpendIn) -> dict[str, int]:
    """Pretend earlier orders happened this month (for the monthly-budget scenario)."""
    ledger.record(body.cents, "demo:earlier-orders")
    bus.emit("demo", "policy", "ledger.seeded", f"Demo: recorded ${body.cents / 100:.2f} of earlier orders this month", {"cents": body.cents})
    return {"month_spend_cents": ledger.month_spend_cents()}


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        queue = bus.subscribe()  # subscribe before replaying so nothing is missed; client dedupes by id
        try:
            for event in list(bus.history):
                yield f"data: {json.dumps(event.to_dict(), default=str)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event.to_dict(), default=str)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
