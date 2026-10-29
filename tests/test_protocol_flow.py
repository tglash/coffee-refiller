"""End-to-end protocol tests, no Claude: MCP + UCP + AP2 + mock Stripe.

These drive the same steps the agent will: create a UCP checkout over MCP,
close the AP2 mandates, create a PaymentIntent, and complete the checkout.
"""

import json
from typing import Any

import pytest
from mcp import Client

from coffee_refiller.catalog import MERCHANTS_BY_ID
from coffee_refiller.events import bus
from coffee_refiller.ledger import ledger
from coffee_refiller.mandates import (
    OpenMandates,
    checkout_hash,
    close_checkout_mandate,
    close_payment_mandate,
    issue_one_time_mandates,
    issue_standing_mandates,
)
from coffee_refiller.merchant import Merchant
from coffee_refiller.processor import MockStripe, StripeError
from coffee_refiller.settings import MandateSettings

pytestmark = pytest.mark.anyio
S = MandateSettings()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset():
    ledger.reset()
    bus.clear()


@pytest.fixture
def stripe() -> MockStripe:
    return MockStripe(ledger)


@pytest.fixture
def beanco(stripe: MockStripe) -> Merchant:
    return Merchant(MERCHANTS_BY_ID["beanco"], stripe)


async def call(merchant: Merchant, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(merchant.server) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def create_checkout(merchant: Merchant, product_id: str, qty: int, mandates: OpenMandates) -> dict[str, Any]:
    return await call(merchant, "create_checkout", {"product_id": product_id, "quantity": qty, "open_checkout_mandate": mandates.checkout})


async def buy(
    merchant: Merchant,
    stripe: MockStripe,
    product_id: str,
    qty: int,
    mandates: OpenMandates,
    payment_method: str = "pm_card_visa",
    pay_amount: int | None = None,
) -> dict[str, Any]:
    created = await create_checkout(merchant, product_id, qty, mandates)
    checkout, jwt = created["checkout"], created["checkout_jwt"]
    amount = checkout["totals"][-1]["amount"]
    tx = checkout_hash(jwt)
    closed_checkout = close_checkout_mandate(mandates.checkout, jwt, merchant.profile.id, nonce=checkout["id"])
    closed_payment = close_payment_mandate(mandates.payment, tx, merchant.profile, pay_amount or amount, payment_method)
    intent = stripe.create_payment_intent(
        amount=pay_amount or amount,
        currency="USD",
        payee=merchant.profile.id,
        payment_method=payment_method,
        transaction_id=tx,
        ap2_payment_mandate=closed_payment,
        idempotency_key=f"pi-{checkout['id']}",
    )
    return await call(
        merchant, "complete_checkout", {"checkout_id": checkout["id"], "checkout_mandate": closed_checkout, "payment_intent_id": intent["id"]}
    )


async def test_mcp_tools_are_listed(beanco: Merchant):
    async with Client(beanco.server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"search_catalog", "create_checkout", "complete_checkout"}


async def test_catalog_search_returns_ucp_products(beanco: Merchant):
    result = await call(beanco, "search_catalog", {"query": "stumptown"})
    assert result["ucp"]["version"]
    [product] = result["products"]
    assert product["brand"] == "Stumptown"
    assert product["fulfillment_estimate"]["delivery_days"] == 2


async def test_happy_path(beanco: Merchant, stripe: MockStripe):
    result = await buy(beanco, stripe, "bc-stump-hb", 1, issue_standing_mandates(S))
    assert result["checkout"]["status"] == "completed"
    assert result["checkout"]["order"]["id"].startswith("ord_")
    assert result["payment_intent"]["status"] == "succeeded"
    assert result["payment_intent"]["ap2_payment_receipt"]
    assert result["ap2_checkout_receipt"]
    assert ledger.month_spend_cents() == 1800


async def test_standing_mandate_refuses_over_cap(beanco: Merchant, stripe: MockStripe):
    with pytest.raises(StripeError) as err:
        await buy(beanco, stripe, "bc-stump-hb", 3, issue_standing_mandates(S))  # $54
    assert err.value.code == "ap2_mandate_violation"
    assert "exceeds maximum 4000" in err.value.message
    assert ledger.month_spend_cents() == 0


async def test_standing_mandate_refuses_over_monthly_budget(beanco: Merchant, stripe: MockStripe):
    ledger.record(7000, "earlier-order")
    with pytest.raises(StripeError) as err:
        await buy(beanco, stripe, "bc-stump-hb", 1, issue_standing_mandates(S))  # $70 + $18
    assert "exceeds budget" in err.value.message


async def test_one_time_approval_allows_over_cap_and_counts_toward_budget(beanco: Merchant, stripe: MockStripe):
    product = MERCHANTS_BY_ID["beanco"].products[0]
    approval = issue_one_time_mandates(beanco.profile, product.id, product.title, 3, 5400, S)
    result = await buy(beanco, stripe, product.id, 3, approval)
    assert result["checkout"]["status"] == "completed"
    assert ledger.month_spend_cents() == 5400


async def test_one_time_approval_pins_the_amount(beanco: Merchant, stripe: MockStripe):
    product = MERCHANTS_BY_ID["beanco"].products[0]
    approval = issue_one_time_mandates(beanco.profile, product.id, product.title, 3, 5400, S)
    with pytest.raises(StripeError) as err:
        await buy(beanco, stripe, product.id, 3, approval, pay_amount=6000)
    assert err.value.code == "ap2_mandate_violation"


async def test_one_time_approval_pins_the_cart(beanco: Merchant):
    stumptown, counter_culture = MERCHANTS_BY_ID["beanco"].products
    approval = issue_one_time_mandates(beanco.profile, stumptown.id, stumptown.title, 1, 1800, S)
    result = await create_checkout(beanco, counter_culture.id, 1, approval)
    assert result["messages"][0]["code"] == "mandate_violation"


async def test_merchant_rejects_mandate_over_a_forged_checkout(beanco: Merchant, stripe: MockStripe):
    mandates = issue_standing_mandates(S)
    created = await create_checkout(beanco, "bc-cc-bt", 1, mandates)
    checkout = created["checkout"]
    # Agent swaps in a JWT it made up (cheaper price, its own signature).
    header, payload, sig = created["checkout_jwt"].split(".")
    forged = f"{header}.{payload[:-4]}AAAA.{sig}"
    closed = close_checkout_mandate(mandates.checkout, forged, "beanco", nonce=checkout["id"])
    result = await call(beanco, "complete_checkout", {"checkout_id": checkout["id"], "checkout_mandate": closed, "payment_intent_id": "pi_x"})
    assert result["messages"][0]["code"] == "mandate_violation"


async def test_declined_card_charges_nothing(beanco: Merchant, stripe: MockStripe):
    result = await buy(beanco, stripe, "bc-stump-hb", 1, issue_standing_mandates(S), payment_method="pm_card_chargeDeclined")
    assert result["messages"][0]["code"] == "payment_failed"
    assert ledger.month_spend_cents() == 0


async def test_idempotency_key_replays_instead_of_double_charging(beanco: Merchant, stripe: MockStripe):
    mandates = issue_standing_mandates(S)
    created = await create_checkout(beanco, "bc-stump-hb", 1, mandates)
    tx = checkout_hash(created["checkout_jwt"])
    closed = close_payment_mandate(mandates.payment, tx, beanco.profile, 1800, "pm_card_visa")
    args = dict(amount=1800, currency="USD", payee="beanco", payment_method="pm_card_visa", transaction_id=tx, ap2_payment_mandate=closed)

    first = stripe.create_payment_intent(**args, idempotency_key="key-1")
    second = stripe.create_payment_intent(**args, idempotency_key="key-1")
    assert first["id"] == second["id"]
    assert len(stripe.intents) == 1

    with pytest.raises(StripeError) as err:
        stripe.create_payment_intent(**{**args, "amount": 1700}, idempotency_key="key-1")
    assert err.value.type == "idempotency_error"
