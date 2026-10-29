"""Seed data for the three mock merchants.

The merchants share one implementation and differ only by this data. Prices,
stock and delivery days are chosen so every demo scenario can happen:
- happy path: Stumptown at BeanCo, $18, 2 days
- over $40/order: a multi-bag request
- over $80/month: any order once the ledger is near the budget
- nothing within the deadline: "need it tomorrow" (fastest is 2 days)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    brand: str
    title: str
    price_cents: int
    delivery_days: int
    in_stock: bool = True


@dataclass(frozen=True)
class MerchantProfile:
    id: str
    name: str
    website: str
    products: tuple[Product, ...]

    @property
    def actor(self) -> str:
        return f"merchant:{self.id}"


MERCHANTS: tuple[MerchantProfile, ...] = (
    MerchantProfile(
        id="beanco",
        name="BeanCo Roasters",
        website="beanco.example",
        products=(
            Product("bc-stump-hb", "Stumptown", "Stumptown Hair Bender, whole bean, 12 oz", 1800, 2),
            Product("bc-cc-bt", "Counter Culture", "Counter Culture Big Trouble, whole bean, 12 oz", 1600, 2),
        ),
    ),
    MerchantProfile(
        id="officemart",
        name="OfficeMart",
        website="officemart.example",
        products=(
            Product("om-stump-hm", "Stumptown", "Stumptown Holler Mountain, whole bean, 12 oz", 2100, 4),
            Product("om-lav-sc", "Lavazza", "Lavazza Super Crema, whole bean, 2.2 lb", 2700, 2),
            Product("om-folg-cl", "Folgers", "Folgers Classic Roast, ground, 30.5 oz", 1400, 2),
        ),
    ),
    MerchantProfile(
        id="grocergo",
        name="GrocerGo",
        website="grocergo.example",
        products=(
            Product("gg-stump-hb", "Stumptown", "Stumptown Hair Bender, whole bean, 12 oz", 1700, 3, in_stock=False),
            Product("gg-cc-holo", "Counter Culture", "Counter Culture Hologram, whole bean, 12 oz", 1500, 5),
            Product("gg-folg-cl", "Folgers", "Folgers Classic Roast, ground, 30.5 oz", 1200, 3),
            # Not on the preferred list: tempting, but policy rejects it.
            Product("gg-sb-pp", "Starbucks", "Starbucks Pike Place, ground, 28 oz", 1300, 2),
        ),
    ),
)

MERCHANTS_BY_ID = {m.id: m for m in MERCHANTS}
