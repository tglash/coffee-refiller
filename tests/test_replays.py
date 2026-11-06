"""Replay recordings stay loadable and consistent (no API key needed).

Re-record with `uv run python scripts/record_replays.py` if these fail after
an Event or UI change.
"""

import dataclasses
import json
import re
from pathlib import Path

import pytest

from coffee_refiller.events import Event

REPLAYS = Path(__file__).resolve().parents[1] / "src" / "coffee_refiller" / "static" / "replays"
SCENARIOS = ["happy-path", "over-cap", "over-budget", "by-tomorrow"]
EVENT_FIELDS = {f.name for f in dataclasses.fields(Event)}
STATE_EVENTS = {"payment_intent.succeeded", "ledger.seeded"}


def load(name: str) -> dict:
    return json.loads((REPLAYS / f"{name}.json").read_text())


def timelines(doc: dict) -> list[list[dict]]:
    """Every path a viewer can watch: the prefix, plus each approval branch."""
    if doc["branch"] is None:
        return [doc["events"]]
    return [doc["events"] + doc["branch"]["approve"], doc["events"] + doc["branch"]["deny"]]


@pytest.mark.parametrize("name", SCENARIOS)
def test_replay_shape(name):
    doc = load(name)
    assert doc["scenario"] == name
    assert {"per_order_cap_cents", "monthly_budget_cents", "month_spend_cents"} <= doc["initial_state"].keys()
    for events in timelines(doc):
        ids = [e["id"] for e in events]
        assert ids == sorted(set(ids)), "event ids must be unique and increasing"
        for e in events:
            assert EVENT_FIELDS <= e.keys()
            if e["kind"] in STATE_EVENTS:
                assert "state" in e, f"{e['kind']} needs a state snapshot for the budget bar"
        assert events[-1]["kind"] == "message.agent", "each path ends with the agent's reply"


@pytest.mark.parametrize("name", [n for n in SCENARIOS if n != "happy-path"])
def test_approval_branches(name):
    doc = load(name)
    branch = doc["branch"]
    card = doc["events"][-1]
    assert card["kind"] == "approval.requested"
    assert card["payload"]["approval_id"] == branch["approval_id"]
    for choice, approved in (("approve", True), ("deny", False)):
        decided = branch[choice][0]
        assert decided["kind"] == "approval.decided"
        assert decided["payload"] == {"approval_id": branch["approval_id"], "approved": approved}
    assert any(e["kind"] == "payment_intent.succeeded" for e in branch["approve"])
    assert not any(e["kind"] == "payment_intent.succeeded" for e in branch["deny"])


def test_happy_path_buys_without_asking():
    doc = load("happy-path")
    kinds = [e["kind"] for e in doc["events"]]
    assert "approval.requested" not in kinds
    assert "payment_intent.succeeded" in kinds


@pytest.mark.parametrize("name", SCENARIOS)
def test_no_secrets(name):
    text = (REPLAYS / f"{name}.json").read_text()
    assert "sk-ant-" not in text
    assert not re.search(r'"d":\s*"', text), "private JWK material in a recording"
