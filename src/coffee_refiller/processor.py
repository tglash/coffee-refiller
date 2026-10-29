"""Mock payment processor, modeled on Stripe's PaymentIntents API.

Flow (as in Stripe): the agent *creates* a PaymentIntent; the merchant
*confirms* it while completing checkout. What AP2 adds: every intent must
carry a closed AP2 payment mandate whose signature chain leads back to the
user's key and whose constraints (cap, budget, payee) are satisfied.

Also imitates:
- test payment methods (`pm_card_visa` = 4242 succeeds, `pm_card_chargeDeclined` declines)
- idempotency keys: a repeated request returns the first result, and the same
  key with different parameters is an `idempotency_error`
- webhook-style events (`payment_intent.succeeded`, ...) on the event bus

It also stands in for AP2's separate Credentials Provider role.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ap2.sdk.jwt_helper import create_jwt
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.receipt_wrapper import ReceiptClient
from ap2.sdk.utils import compute_sha256_b64url

from coffee_refiller.events import bus
from coffee_refiller.keys import key_for
from coffee_refiller.ledger import Ledger
from coffee_refiller.mandates import verify_payment_chain

ACTOR = "processor"
TEST_PAYMENT_METHODS = {
    "pm_card_visa": {"brand": "visa", "last4": "4242", "declines": False},
    "pm_card_chargeDeclined": {"brand": "visa", "last4": "0002", "declines": True},
}


class StripeError(Exception):
    def __init__(self, type_: str, code: str, message: str) -> None:
        super().__init__(message)
        self.type, self.code, self.message = type_, code, message

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"type": self.type, "code": self.code, "message": self.message}}


@dataclass
class PaymentIntent:
    id: str
    amount: int
    currency: str
    payee: str
    payment_method: str
    transaction_id: str
    status: str = "requires_confirmation"
    created: int = field(default_factory=lambda: int(time.time()))
    last_payment_error: dict[str, Any] | None = None
    ap2_payment_receipt: str | None = None  # processor-signed JWT, set on success
    _chain: PaymentMandateChain | None = field(default=None, repr=False)
    _mandate_token: str = field(default="", repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        return {"object": "payment_intent", **d}


class MockStripe:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.intents: dict[str, PaymentIntent] = {}
        self._idempotency: dict[str, tuple[str, dict[str, Any] | StripeError]] = {}

    # --- idempotency -----------------------------------------------------

    def _idempotent(self, key: str, params: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run `fn` once per key; replay the saved result (or error) afterwards."""
        fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        if key in self._idempotency:
            saved_fingerprint, saved = self._idempotency[key]
            if saved_fingerprint != fingerprint:
                raise StripeError(
                    "idempotency_error", "idempotency_key_in_use", "Keys for idempotent requests can only be used with the same parameters they were first used with."
                )
            bus.emit(ACTOR, "stripe", "idempotent_replay", f"Idempotency key {key[:12]}… replayed; no new charge", {"idempotency_key": key})
            if isinstance(saved, StripeError):
                raise saved
            return saved
        try:
            result: dict[str, Any] | StripeError = fn()
        except StripeError as err:
            result = err
        self._idempotency[key] = (fingerprint, result)
        if isinstance(result, StripeError):
            raise result
        return result

    # --- API ---------------------------------------------------------------

    def create_payment_intent(
        self,
        *,
        amount: int,
        currency: str,
        payee: str,
        payment_method: str,
        transaction_id: str,
        ap2_payment_mandate: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        params = {"amount": amount, "currency": currency, "payee": payee, "payment_method": payment_method, "transaction_id": transaction_id}
        bus.emit(ACTOR, "stripe", "POST /v1/payment_intents", f"Create PaymentIntent ${amount / 100:.2f} → {payee}", params)
        return self._idempotent(
            idempotency_key, params, lambda: self._create(params, ap2_payment_mandate)
        )

    def confirm_payment_intent(self, intent_id: str, *, confirmed_by: str, idempotency_key: str) -> dict[str, Any]:
        params = {"intent": intent_id, "confirmed_by": confirmed_by}
        bus.emit(ACTOR, "stripe", f"POST /v1/payment_intents/{intent_id}/confirm", f"{confirmed_by} confirms {intent_id}", params)
        return self._idempotent(idempotency_key, params, lambda: self._confirm(intent_id, confirmed_by))

    # --- internals ---------------------------------------------------------

    def _check_mandate(self, chain: PaymentMandateChain, transaction_id: str) -> None:
        """AP2 constraint check against fresh ledger state (cap, budget, payee, presets)."""
        violations = chain.verify(expected_transaction_id=transaction_id, mandate_context=self.ledger.mandate_context())
        if violations:
            bus.emit(ACTOR, "ap2", "mandate.rejected", "AP2 payment mandate violates constraints", {"violations": violations})
            raise StripeError("invalid_request_error", "ap2_mandate_violation", "; ".join(violations))

    def _create(self, params: dict[str, Any], mandate_token: str) -> dict[str, Any]:
        if params["payment_method"] not in TEST_PAYMENT_METHODS:
            raise StripeError("invalid_request_error", "resource_missing", f"No such PaymentMethod: '{params['payment_method']}'")
        try:
            chain = verify_payment_chain(mandate_token, nonce=params["transaction_id"])
        except Exception as exc:  # signature, audience, nonce or expiry failure
            bus.emit(ACTOR, "ap2", "mandate.invalid", "AP2 payment mandate signature chain failed", {"error": str(exc)})
            raise StripeError("invalid_request_error", "ap2_mandate_invalid", f"Mandate chain failed verification: {exc}") from exc

        closed = chain.closed_mandate
        mismatches = [
            name
            for name, ok in (
                ("amount", closed.payment_amount.amount == params["amount"]),
                ("currency", closed.payment_amount.currency == params["currency"]),
                ("payee", closed.payee.id == params["payee"]),
                ("payment_method", closed.payment_instrument.id == params["payment_method"]),
            )
            if not ok
        ]
        if mismatches:
            raise StripeError("invalid_request_error", "ap2_mandate_mismatch", f"Request does not match signed mandate: {', '.join(mismatches)}")
        self._check_mandate(chain, params["transaction_id"])
        bus.emit(ACTOR, "ap2", "mandate.verified", "AP2 payment mandate chain verified back to the user's key", {"transaction_id": params["transaction_id"]})

        intent = PaymentIntent(id=f"pi_{secrets.token_hex(12)}", **params, _chain=chain, _mandate_token=mandate_token)
        self.intents[intent.id] = intent
        bus.emit(ACTOR, "stripe", "payment_intent.created", f"{intent.id} requires_confirmation", intent.to_dict())
        return intent.to_dict()

    def _confirm(self, intent_id: str, confirmed_by: str) -> dict[str, Any]:
        intent = self.intents.get(intent_id)
        if intent is None:
            raise StripeError("invalid_request_error", "resource_missing", f"No such payment_intent: '{intent_id}'")
        if confirmed_by != f"merchant:{intent.payee}":
            raise StripeError("invalid_request_error", "payee_mismatch", "Only the payee merchant can confirm this PaymentIntent")
        if intent.status != "requires_confirmation":
            raise StripeError("invalid_request_error", "payment_intent_unexpected_state", f"PaymentIntent is {intent.status}")
        assert intent._chain is not None
        # Re-check the budget now: another order may have landed since creation.
        self._check_mandate(intent._chain, intent.transaction_id)

        if TEST_PAYMENT_METHODS[intent.payment_method]["declines"]:
            intent.status = "requires_payment_method"
            intent.last_payment_error = {"code": "card_declined", "decline_code": "generic_decline"}
            bus.emit(ACTOR, "stripe", "payment_intent.payment_failed", f"webhook: {intent.id} card_declined", intent.to_dict())
            return intent.to_dict()

        intent.status = "succeeded"
        self.ledger.record(intent.amount, intent.id)
        intent.ap2_payment_receipt = self._sign_receipt(intent)
        bus.emit(ACTOR, "stripe", "payment_intent.succeeded", f"webhook: {intent.id} succeeded (${intent.amount / 100:.2f})", intent.to_dict())
        return intent.to_dict()

    def _sign_receipt(self, intent: PaymentIntent) -> str:
        """AP2 payment receipt, bound to the closed mandate by hash."""
        assert intent._chain is not None
        reference = compute_sha256_b64url(MandateClient().get_closed_mandate_jwt(intent._mandate_token))
        receipt = ReceiptClient().create_payment_receipt(intent._chain.closed_mandate, reference=reference)
        payload = receipt.model_dump(mode="json", exclude_none=True)
        bus.emit(ACTOR, "ap2", "receipt.payment", "Processor signed AP2 payment receipt", payload)
        return create_jwt(header={"alg": "ES256", "typ": "JWT", "kid": ACTOR}, payload=payload, private_key=key_for(ACTOR))
