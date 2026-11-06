"""Record live Claude runs of the demo scenarios as replay files.

    uv run python scripts/record_replays.py              # all scenarios
    uv run python scripts/record_replays.py over-cap     # just one

Each scenario runs the real agent in-process (real Claude, mock merchants and
payments) and saves the event history to src/coffee_refiller/static/replays/.
The dashboard plays these back when there's no server (e.g. GitHub Pages).

Approval scenarios are recorded twice, once approving and once denying, and
spliced at the approval card so a replay viewer can pick either branch.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from coffee_refiller import app as server  # loads .env, builds the agent
from coffee_refiller.agent import MODEL
from coffee_refiller.events import bus

OUT = Path(server.STATIC) / "replays"
DECISION_PAUSE = 1.5  # seconds a "human" takes to click Approve/Deny
STATE_EVENTS = {"payment_intent.succeeded", "ledger.seeded"}  # the events the dashboard refreshes state on
CARD_FIELDS = ("merchant", "item", "total", "delivery_days", "reasons")

SCENARIOS: dict[str, dict[str, Any]] = {
    "happy-path": {"title": "Out of coffee", "text": "Ugh, we're out of coffee again", "approval": False},
    "over-cap": {"title": "4 bags (over $40)", "text": "We need 4 bags for the offsite next week", "approval": True},
    "over-budget": {"title": "Over budget", "text": "Ugh, we're out of coffee again", "seed_cents": 7000, "approval": True},
    "by-tomorrow": {
        "title": "By tomorrow",
        "text": "Completely out and clients are in tomorrow morning, need coffee by tomorrow",
        "approval": True,
    },
}


async def run_once(scenario: dict[str, Any], approve: bool | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One live run from a fresh month. Returns (initial state, events with state snapshots)."""
    server.reset()
    initial = server.state()
    queue = bus.subscribe()
    events: list[dict[str, Any]] = []
    try:
        if "seed_cents" in scenario:
            server.seed_spend(server.SpendIn(cents=scenario["seed_cents"]))
        task = asyncio.create_task(server.agent.handle_message(scenario["text"]))
        while not (task.done() and queue.empty()):
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.2)
            except TimeoutError:
                continue
            row = event.to_dict()
            if event.kind in STATE_EVENTS:
                row["state"] = server.state()  # the ledger is updated before these are emitted
            events.append(row)
            if event.kind == "approval.requested":
                if approve is None:
                    raise RuntimeError("Unexpected approval request in a no-approval scenario")
                await asyncio.sleep(DECISION_PAUSE)
                server.agent.decide(event.payload["approval_id"], approve)
        await task
    finally:
        bus.unsubscribe(queue)
    return initial, json.loads(json.dumps(events, default=str))


def _approval_index(events: list[dict[str, Any]]) -> int:
    hits = [i for i, e in enumerate(events) if e["kind"] == "approval.requested"]
    if len(hits) != 1:
        raise RuntimeError(f"Expected exactly one approval request, got {len(hits)}")
    return hits[0]


def splice(approve_run: list[dict[str, Any]], deny_run: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Shared prefix from the approve run; both suffixes continue from its approval card."""
    ia, idn = _approval_index(approve_run), _approval_index(deny_run)
    card_a, card_d = approve_run[ia]["payload"], deny_run[idn]["payload"]
    diffs = [f for f in CARD_FIELDS if card_a[f] != card_d[f]]
    if diffs:
        raise RuntimeError(f"Approve and deny runs asked about different checkouts ({', '.join(diffs)}); re-record")

    # The deny run has its own random ids; point them at the approve run's card.
    text = json.dumps(deny_run[idn + 1 :])
    for key in ("approval_id", "checkout_id"):
        text = text.replace(card_d[key], card_a[key])
    deny = json.loads(text)
    anchor = approve_run[ia]
    for n, e in enumerate(deny, start=anchor["id"] + 1):
        e["ts"] = anchor["ts"] + (e["ts"] - deny_run[idn]["ts"])
        e["id"] = n

    prefix = approve_run[: ia + 1]
    branch = {"approval_id": card_a["approval_id"], "approve": approve_run[ia + 1 :], "deny": deny}
    return prefix, branch


def check_secrets(text: str) -> None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key and key in text:
        raise RuntimeError("API key found in a recording; not saving")
    if re.search(r'"d":\s*"', text):
        raise RuntimeError("A private JWK ('d') found in a recording; not saving")


async def record(name: str) -> Path:
    scenario = SCENARIOS[name]
    print(f"● {name}: recording…", flush=True)
    if scenario["approval"]:
        initial, approve_run = await run_once(scenario, approve=True)
        print(f"  approve run: {len(approve_run)} events", flush=True)
        _, deny_run = await run_once(scenario, approve=False)
        print(f"  deny run: {len(deny_run)} events", flush=True)
        events, branch = splice(approve_run, deny_run)
    else:
        initial, events = await run_once(scenario, approve=None)
        branch = None
        print(f"  {len(events)} events", flush=True)

    doc = {
        "scenario": name,
        "title": scenario["title"],
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "model": MODEL,
        "initial_state": initial,
        "events": events,
        "branch": branch,
    }
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    check_secrets(text)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(text + "\n")
    print(f"  saved {path.relative_to(Path.cwd())} ({len(text) // 1024} KB)", flush=True)
    return path


async def main(names: list[str]) -> None:
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        sys.exit(f"Unknown scenario(s): {', '.join(unknown)}. Choose from: {', '.join(SCENARIOS)}")
    for name in names or list(SCENARIOS):
        await record(name)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
