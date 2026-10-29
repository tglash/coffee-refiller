"""In-process event bus: every protocol message becomes one timeline row.

Each actor (agent, merchant, processor, user) calls `bus.emit(...)` when it
sends or receives something worth studying. The dashboard subscribes and
streams the rows live; tests read `bus.history`.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Protocol = Literal["chat", "claude", "mcp", "ucp", "ap2", "stripe", "policy"]


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    actor: str  # who emitted it, e.g. "agent", "merchant:beanco", "processor"
    protocol: Protocol
    kind: str  # short machine name, e.g. "tools/call", "mandate.signed"
    summary: str  # one human-readable line for the timeline
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.history: list[Event] = []
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def emit(
        self,
        actor: str,
        protocol: Protocol,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        event = Event(next(self._ids), time.time(), actor, protocol, kind, summary, payload or {})
        self.history.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue[Event]:
        """Return a queue that receives every future event. Pair with `unsubscribe`."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def clear(self) -> None:
        self.history.clear()


bus = EventBus()
