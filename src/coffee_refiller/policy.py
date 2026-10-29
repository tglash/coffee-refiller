"""Purchase policy: plain code that decides whether the agent's choice is allowed.

Claude proposes an offer; this decides. It covers the rules AP2 can't
express (brand list, delivery deadline) and predicts the AP2 limits (per-order
cap, monthly budget) so the agent asks for approval *before* trying a payment
the processor would refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from coffee_refiller.catalog import MERCHANTS_BY_ID
from coffee_refiller.settings import MandateSettings

Decision = Literal["ok", "needs_approval", "reject"]


@dataclass(frozen=True)
class Offer:
    merchant_id: str
    product_id: str
    brand: str
    title: str
    unit_price_cents: int
    delivery_days: int
    in_stock: bool = True


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reasons: tuple[str, ...]
    total_cents: int


def _usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


def evaluate(
    offer: Offer,
    quantity: int,
    month_spend_cents: int,
    deadline_days: int,
    settings: MandateSettings,
) -> PolicyResult:
    total = offer.unit_price_cents * quantity

    rejections = [
        reason
        for failed, reason in (
            (offer.merchant_id not in MERCHANTS_BY_ID, f"merchant {offer.merchant_id} is not an allowed merchant"),
            (offer.brand not in settings.preferred_brands, f"{offer.brand} is not on the preferred brand list"),
            (not offer.in_stock, f"{offer.title} is out of stock"),
            (quantity < 1, "quantity must be at least 1"),
        )
        if failed
    ]
    if rejections:
        return PolicyResult("reject", tuple(rejections), total)

    approvals = [
        reason
        for failed, reason in (
            (total > settings.per_order_cap_cents, f"order total {_usd(total)} is over the {_usd(settings.per_order_cap_cents)} per-order cap"),
            (
                month_spend_cents + total > settings.monthly_budget_cents,
                f"month would reach {_usd(month_spend_cents + total)}, over the {_usd(settings.monthly_budget_cents)} monthly budget",
            ),
            (offer.delivery_days > deadline_days, f"arrives in {offer.delivery_days} days, past the {deadline_days}-day deadline"),
        )
        if failed
    ]
    if approvals:
        return PolicyResult("needs_approval", tuple(approvals), total)
    return PolicyResult("ok", (), total)
