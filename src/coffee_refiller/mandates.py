"""AP2 V2 mandates: who signs what.

- The **user** signs *open* mandates: the standing rules, or a one-time
  approval for one exact checkout. Their `cnf` names the agent's key, which
  delegates authority to the agent.
- The **agent** signs *closed* mandates on top: one specific checkout and
  one specific payment. It can only close what an open mandate allows.
- **Verifiers** (merchant, processor) trust only the user's public key and
  walk the chain down to the agent's signature.

All signing goes through the real AP2 SDK (SD-JWT chains, ES256).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import (
    AllowedMerchants,
    Item,
    LineItemRequirements,
    LineItems,
    OpenCheckoutMandate,
)
from ap2.sdk.generated.open_payment_mandate import (
    AgentRecurrence,
    AllowedPayees,
    AmountRange,
    Budget,
    Frequency,
    OpenPaymentMandate,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.utils import compute_sha256_b64url

from coffee_refiller.catalog import MERCHANTS, MerchantProfile
from coffee_refiller.events import bus
from coffee_refiller.keys import key_for, public_jwk, public_key
from coffee_refiller.settings import MandateSettings

USER, AGENT = "user", "agent"
STANDING_TTL_SECONDS = 31 * 24 * 3600

_client = MandateClient()


class _JsonSafeOpenPaymentMandate(OpenPaymentMandate):
    """Works around an AP2 SDK bug (commit e1ea56d): `delegate_claims_from_model`
    dumps payloads without `mode="json"`, so the `Frequency` enum inside
    `AgentRecurrence` can't be signed. Defaulting to JSON mode fixes it;
    verifiers still parse the plain `OpenPaymentMandate`."""

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("mode", "json")
        return super().model_dump(**kwargs)


def ap2_merchant(profile: MerchantProfile) -> Merchant:
    return Merchant(id=profile.id, name=profile.name, website=profile.website)


@dataclass(frozen=True)
class OpenMandates:
    """A user-signed pair: what may be bought, and how it may be paid for."""

    kind: Literal["standing", "one_time"]
    checkout: str  # SD-JWT: OpenCheckoutMandate
    payment: str  # SD-JWT: OpenPaymentMandate
    expires_at: int


def _issue(
    kind: Literal["standing", "one_time"],
    checkout: OpenCheckoutMandate,
    payment: OpenPaymentMandate,
    expires_at: int,
) -> OpenMandates:
    user_key = key_for(USER)
    mandates = OpenMandates(
        kind=kind,
        checkout=_client.create(payloads=[checkout], issuer_key=user_key),
        payment=_client.create(payloads=[payment], issuer_key=user_key),
        expires_at=expires_at,
    )
    bus.emit(
        USER,
        "ap2",
        f"mandate.open.{kind}",
        f"User signed {kind.replace('_', '-')} open checkout + payment mandates",
        {
            "open_checkout_mandate": checkout.model_dump(mode="json", exclude={"cnf"}),
            "open_payment_mandate": payment.model_dump(mode="json", exclude={"cnf"}),
            "delegated_to": AGENT,
        },
    )
    return mandates


def issue_standing_mandates(settings: MandateSettings) -> OpenMandates:
    """The user's standing rules: the agent may buy within these unattended."""
    now = int(time.time())
    cnf = {"jwk": public_jwk(AGENT)}
    merchants = [ap2_merchant(m) for m in MERCHANTS]
    checkout = OpenCheckoutMandate(
        constraints=[AllowedMerchants(allowed=merchants)],
        cnf=cnf,
        iat=now,
        exp=now + STANDING_TTL_SECONDS,
    )
    payment = _JsonSafeOpenPaymentMandate(
        constraints=[
            AmountRange(currency=settings.currency, max=settings.per_order_cap_cents),
            # AP2 Budget.max is in major units (dollars); amounts are cents.
            Budget(max=settings.monthly_budget_cents / 100, currency=settings.currency),
            AgentRecurrence(frequency=Frequency.MONTHLY),
            AllowedPayees(allowed=merchants),
        ],
        cnf=cnf,
        iat=now,
        exp=now + STANDING_TTL_SECONDS,
    )
    return _issue("standing", checkout, payment, now + STANDING_TTL_SECONDS)


def issue_one_time_mandates(
    merchant: MerchantProfile,
    product_id: str,
    product_title: str,
    quantity: int,
    amount_cents: int,
    settings: MandateSettings,
) -> OpenMandates:
    """A human-present approval for exactly one checkout.

    Pre-setting `payee` and `payment_amount` means the agent's closed payment
    mandate must match them exactly; `LineItems` pins the cart contents.
    """
    now = int(time.time())
    exp = now + settings.approval_ttl_seconds
    cnf = {"jwk": public_jwk(AGENT)}
    payee = ap2_merchant(merchant)
    checkout = OpenCheckoutMandate(
        constraints=[
            AllowedMerchants(allowed=[payee]),
            LineItems(
                items=[
                    LineItemRequirements(
                        id="approved",
                        acceptable_items=[Item(id=product_id, title=product_title)],
                        quantity=quantity,
                    )
                ]
            ),
        ],
        cnf=cnf,
        iat=now,
        exp=exp,
    )
    payment = OpenPaymentMandate(
        constraints=[
            AmountRange(currency=settings.currency, max=amount_cents),
            AllowedPayees(allowed=[payee]),
        ],
        payee=payee,
        payment_amount=Amount(amount=amount_cents, currency=settings.currency),
        cnf=cnf,
        iat=now,
        exp=exp,
    )
    return _issue("one_time", checkout, payment, exp)


def checkout_hash(checkout_jwt: str) -> str:
    """AP2's transaction id: the SHA-256 of the merchant-signed checkout JWT."""
    return compute_sha256_b64url(checkout_jwt)


def close_checkout_mandate(open_checkout: str, checkout_jwt: str, merchant_id: str, nonce: str) -> str:
    """Agent signs: "I commit to this exact merchant-signed checkout"."""
    now = int(time.time())
    closed = CheckoutMandate(checkout_jwt=checkout_jwt, checkout_hash=checkout_hash(checkout_jwt), iat=now, exp=now + 900)
    chain = _client.present(
        holder_key=key_for(AGENT),
        mandate_token=open_checkout,
        payloads=[closed],
        nonce=nonce,
        aud=f"merchant:{merchant_id}",
    )
    bus.emit(
        AGENT,
        "ap2",
        "mandate.checkout.closed",
        f"Agent signed closed checkout mandate for {merchant_id}",
        {"checkout_hash": closed.checkout_hash, "aud": f"merchant:{merchant_id}", "nonce": nonce},
    )
    return chain


def close_payment_mandate(
    open_payment: str,
    transaction_id: str,
    merchant: MerchantProfile,
    amount_cents: int,
    payment_method: str,
    currency: str = "USD",
) -> str:
    """Agent signs: "pay this merchant exactly this much for this transaction"."""
    now = int(time.time())
    closed = PaymentMandate(
        transaction_id=transaction_id,
        payee=ap2_merchant(merchant),
        payment_amount=Amount(amount=amount_cents, currency=currency),
        payment_instrument=PaymentInstrument(type="card", id=payment_method, description="Mock Stripe test card"),
        iat=now,
        exp=now + 900,
    )
    chain = _client.present(
        holder_key=key_for(AGENT),
        mandate_token=open_payment,
        payloads=[closed],
        nonce=transaction_id,
        aud="processor",
    )
    bus.emit(
        AGENT,
        "ap2",
        "mandate.payment.closed",
        f"Agent signed closed payment mandate: ${amount_cents / 100:.2f} to {merchant.name}",
        closed.model_dump(mode="json"),
    )
    return chain


def verify_checkout_chain(token: str, aud: str, nonce: str) -> CheckoutMandateChain:
    """Check signatures back to the user's key. Raises on a bad chain."""
    payloads = _client.verify(
        token=token, key_or_provider=lambda _t: public_key(USER), expected_aud=aud, expected_nonce=nonce
    )
    return CheckoutMandateChain.parse(payloads)


def verify_payment_chain(token: str, nonce: str) -> PaymentMandateChain:
    """Check signatures back to the user's key. Raises on a bad chain."""
    payloads = _client.verify(
        token=token, key_or_provider=lambda _t: public_key(USER), expected_aud="processor", expected_nonce=nonce
    )
    return PaymentMandateChain.parse(payloads)
