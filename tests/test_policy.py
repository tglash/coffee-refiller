from dataclasses import replace

from coffee_refiller.policy import Offer, evaluate
from coffee_refiller.settings import MandateSettings

S = MandateSettings()
STUMPTOWN = Offer("beanco", "bc-stump-hb", "Stumptown", "Stumptown Hair Bender", 1800, delivery_days=2)


def test_ok_within_all_limits():
    result = evaluate(STUMPTOWN, quantity=1, month_spend_cents=0, deadline_days=3, settings=S)
    assert result.decision == "ok"
    assert result.reasons == ()
    assert result.total_cents == 1800


def test_reject_brand_not_preferred():
    offer = replace(STUMPTOWN, brand="Starbucks")
    assert evaluate(offer, 1, 0, 3, S).decision == "reject"


def test_reject_out_of_stock():
    offer = replace(STUMPTOWN, in_stock=False)
    assert evaluate(offer, 1, 0, 3, S).decision == "reject"


def test_reject_unknown_merchant():
    offer = replace(STUMPTOWN, merchant_id="shadyshop")
    assert evaluate(offer, 1, 0, 3, S).decision == "reject"


def test_reject_beats_approval():
    # Off-list brand that is also over the cap: rejected, never offered for approval.
    offer = replace(STUMPTOWN, brand="Starbucks")
    assert evaluate(offer, 5, 0, 3, S).decision == "reject"


def test_approval_over_per_order_cap():
    result = evaluate(STUMPTOWN, quantity=3, month_spend_cents=0, deadline_days=3, settings=S)  # $54
    assert result.decision == "needs_approval"
    assert "per-order cap" in result.reasons[0]


def test_exactly_at_cap_is_ok():
    offer = replace(STUMPTOWN, unit_price_cents=2000)
    assert evaluate(offer, 2, 0, 3, S).decision == "ok"  # $40.00


def test_approval_over_monthly_budget():
    result = evaluate(STUMPTOWN, quantity=1, month_spend_cents=6500, deadline_days=3, settings=S)  # $83
    assert result.decision == "needs_approval"
    assert "monthly budget" in result.reasons[0]


def test_approval_past_deadline():
    result = evaluate(STUMPTOWN, quantity=1, month_spend_cents=0, deadline_days=1, settings=S)
    assert result.decision == "needs_approval"
    assert "deadline" in result.reasons[0]


def test_multiple_approval_reasons_are_all_reported():
    result = evaluate(STUMPTOWN, quantity=3, month_spend_cents=5000, deadline_days=1, settings=S)
    assert result.decision == "needs_approval"
    assert len(result.reasons) == 3
