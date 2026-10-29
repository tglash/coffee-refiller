"""Monthly spend ledger.

AP2's `Budget` constraint can't count past spend by itself. The verifier
supplies a `MandateContext` with the spend so far, and the ledger is that
source of truth. Approved (one-time) orders are recorded too, so they count
toward the monthly budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ap2.sdk.constraints import MandateContext, calculate_period_start
from ap2.sdk.generated.open_payment_mandate import Frequency


@dataclass(frozen=True)
class Entry:
    ts: float
    amount_cents: int
    reference: str


class Ledger:
    def __init__(self) -> None:
        self.entries: list[Entry] = []

    def record(self, amount_cents: int, reference: str, ts: float | None = None) -> None:
        self.entries.append(Entry(ts if ts is not None else time.time(), amount_cents, reference))

    def _this_month(self, now: float | None = None) -> list[Entry]:
        start = calculate_period_start(now if now is not None else time.time(), Frequency.MONTHLY)
        return [e for e in self.entries if e.ts >= start]

    def month_spend_cents(self, now: float | None = None) -> int:
        return sum(e.amount_cents for e in self._this_month(now))

    def mandate_context(self, now: float | None = None) -> MandateContext:
        month = self._this_month(now)
        return MandateContext(total_amount=sum(e.amount_cents for e in month), total_uses=len(month))

    def reset(self) -> None:
        self.entries.clear()


ledger = Ledger()
