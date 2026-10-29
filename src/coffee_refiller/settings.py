"""The user's standing rules (v1). Money is always integer cents."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MandateSettings:
    per_order_cap_cents: int = 4000
    monthly_budget_cents: int = 8000
    # Ranked best → fallback. Brands outside this list are never bought.
    preferred_brands: tuple[str, ...] = ("Stumptown", "Counter Culture", "Lavazza", "Folgers")
    deadline_days: int = 3
    approval_ttl_seconds: int = 15 * 60
    currency: str = "USD"


SETTINGS = MandateSettings()
