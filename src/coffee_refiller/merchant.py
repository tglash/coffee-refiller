"""Mock merchant: an MCP server exposing UCP shopping tools.

This is where MCP and UCP connect: MCP is the transport (`tools/call`), and
UCP defines what the tools mean and what their payloads look like (catalog
search, checkout, order). AP2 shows up at checkout: the merchant signs the
checkout, and later verifies the agent's closed checkout mandate before it
lets the payment be confirmed.

One implementation serves all three merchants; only the data differs.
"""

from __future__ import annotations

import secrets
from typing import Any

from ap2.sdk.constraints import check_checkout_constraints
from ap2.sdk.generated.open_checkout_mandate import OpenCheckoutMandate
from ap2.sdk.generated.types.checkout import Checkout as AP2Checkout
from ap2.sdk.jwt_helper import create_jwt, verify_jwt
from ap2.sdk.mandate import MandateClient
from ap2.sdk.receipt_wrapper import ReceiptClient
from ap2.sdk.utils import compute_sha256_b64url
from mcp.server.mcpserver import MCPServer
from ucp_sdk.models.schemas.shopping.catalog_search import SearchResponse
from ucp_sdk.models.schemas.shopping.checkout import Checkout as UCPCheckout

from coffee_refiller.catalog import MerchantProfile, Product
from coffee_refiller.events import bus
from coffee_refiller.keys import key_for, public_key
from coffee_refiller.mandates import USER, checkout_hash, verify_checkout_chain
from coffee_refiller.processor import MockStripe, StripeError

UCP_VERSION = "2026-01-11"
# Not in UCP 0.5's fulfillment option schema (it has no delivery-days field),
# so delivery estimates ride along as a clearly-named extension field.
FULFILLMENT_EXT = "fulfillment_estimate"


def _error(code: str, message: str) -> dict[str, Any]:
    """UCP-style error message list."""
    return {"messages": [{"type": "error", "code": code, "content": message}]}


class Merchant:
    def __init__(self, profile: MerchantProfile, processor: MockStripe) -> None:
        self.profile = profile
        self.processor = processor
        self.checkouts: dict[str, dict[str, Any]] = {}  # id -> {"ucp": ..., "jwt": ..., "hash": ...}
        self.server = self._build_server()

    @property
    def actor(self) -> str:
        return self.profile.actor

    # --- UCP payload builders --------------------------------------------

    def _ucp_product(self, p: Product) -> dict[str, Any]:
        price = {"amount": p.price_cents, "currency": "USD"}
        return {
            "id": p.id,
            "title": p.title,
            "description": {"plain": f"{p.brand} coffee from {self.profile.name}"},
            "price_range": {"min": price, "max": price},
            "variants": [{"id": p.id, "title": p.title, "description": {"plain": p.brand}, "price": price}],
            "brand": p.brand,
            "in_stock": p.in_stock,
            FULFILLMENT_EXT: {"method": "shipping", "delivery_days": p.delivery_days},
        }

    def _ucp_checkout(self, checkout_id: str, p: Product, quantity: int) -> dict[str, Any]:
        subtotal = p.price_cents * quantity
        totals = [{"type": "subtotal", "amount": subtotal}, {"type": "total", "amount": subtotal}]
        checkout = UCPCheckout.model_validate(
            {
                "ucp": {"version": UCP_VERSION},
                "id": checkout_id,
                "line_items": [
                    {"id": "li_0", "item": {"id": p.id, "title": p.title, "price": p.price_cents}, "quantity": quantity, "totals": totals}
                ],
                "status": "ready_for_complete",
                "currency": "USD",
                "totals": totals,
                "links": [
                    {"type": "terms_of_service", "url": f"https://{self.profile.website}/tos"},
                    {"type": "privacy_policy", "url": f"https://{self.profile.website}/privacy"},
                ],
            }
        ).model_dump(mode="json", exclude_none=True)
        # AP2 checks need to know who the merchant is; UCP allows extra fields.
        checkout["merchant"] = {"id": self.profile.id, "name": self.profile.name, "website": self.profile.website}
        checkout[FULFILLMENT_EXT] = {"method": "shipping", "delivery_days": p.delivery_days}
        return checkout

    def _sign(self, checkout: dict[str, Any]) -> tuple[str, str]:
        """Return (compact checkout JWT for AP2, detached JWS for UCP `ap2.merchant_authorization`)."""
        jwt = create_jwt(header={"alg": "ES256", "typ": "JWT", "kid": self.actor}, payload=checkout, private_key=key_for(self.actor))
        header, _payload, signature = jwt.split(".")
        return jwt, f"{header}..{signature}"

    # --- tool implementations --------------------------------------------

    def search_catalog(self, query: str = "") -> dict[str, Any]:
        q = query.lower().strip()
        products = [p for p in self.profile.products if q in f"{p.brand} {p.title} coffee".lower()]
        response = SearchResponse.model_validate(
            {"ucp": {"version": UCP_VERSION}, "products": [self._ucp_product(p) for p in products]}
        ).model_dump(mode="json", exclude_none=True)
        response["merchant"] = {"id": self.profile.id, "name": self.profile.name}
        return response

    def create_checkout(self, product_id: str, quantity: int, open_checkout_mandate: str) -> dict[str, Any]:
        product = next((p for p in self.profile.products if p.id == product_id), None)
        if product is None:
            return _error("not_found", f"No product {product_id}")
        if not product.in_stock:
            return _error("out_of_stock", f"{product.title} is out of stock")
        if quantity < 1:
            return _error("invalid", "quantity must be at least 1")

        # Before quoting, check the agent's authority: the open mandate must be
        # user-signed and must allow this checkout.
        try:
            verified = MandateClient().verify(token=open_checkout_mandate, key_or_provider=public_key(USER), payload_type=OpenCheckoutMandate)
        except Exception as exc:
            bus.emit(self.actor, "ap2", "mandate.invalid", "Open checkout mandate failed verification", {"error": str(exc)})
            return _error("mandate_invalid", f"Open checkout mandate failed verification: {exc}")

        checkout_id = f"chk_{secrets.token_hex(8)}"
        checkout = self._ucp_checkout(checkout_id, product, quantity)
        violations = check_checkout_constraints(verified.mandate_payload, AP2Checkout.model_validate(checkout))
        if violations:
            bus.emit(self.actor, "ap2", "mandate.rejected", "Checkout violates open checkout mandate", {"violations": violations})
            return _error("mandate_violation", "; ".join(violations))

        jwt, detached = self._sign(checkout)
        self.checkouts[checkout_id] = {"ucp": checkout, "jwt": jwt, "hash": checkout_hash(jwt)}
        bus.emit(
            self.actor,
            "ucp",
            "checkout.created",
            f"{self.profile.name} signed checkout {checkout_id}: {quantity}× {product.brand}, ${checkout['totals'][-1]['amount'] / 100:.2f}",
            checkout,
        )
        return {"checkout": {**checkout, "ap2": {"merchant_authorization": detached}}, "checkout_jwt": jwt}

    def complete_checkout(self, checkout_id: str, checkout_mandate: str, payment_intent_id: str) -> dict[str, Any]:
        record = self.checkouts.get(checkout_id)
        if record is None:
            return _error("not_found", f"No checkout {checkout_id}")
        checkout = record["ucp"]
        if checkout["status"] != "ready_for_complete":
            return _error("invalid_state", f"Checkout is {checkout['status']}")

        # 1. The agent's closed checkout mandate: chain back to the user, bound to
        #    this merchant (aud) and this checkout (nonce), and to our exact JWT.
        try:
            chain = verify_checkout_chain(checkout_mandate, aud=self.actor, nonce=checkout_id)
            verify_jwt(chain.closed_mandate.checkout_jwt, public_key(self.actor))  # our own signature, untampered
            violations = chain.verify(expected_checkout_hash=record["hash"], checkout_jwt=chain.closed_mandate.checkout_jwt)
        except Exception as exc:
            violations = [str(exc)]
        if violations:
            bus.emit(self.actor, "ap2", "mandate.rejected", "Closed checkout mandate rejected", {"violations": violations})
            return _error("mandate_violation", "; ".join(violations))
        bus.emit(self.actor, "ap2", "mandate.verified", f"Closed checkout mandate verified for {checkout_id}", {"checkout_hash": record["hash"]})

        # 2. Confirm the agent's PaymentIntent with the processor.
        checkout["status"] = "complete_in_progress"
        try:
            intent = self.processor.confirm_payment_intent(payment_intent_id, confirmed_by=self.actor, idempotency_key=f"confirm-{checkout_id}")
        except StripeError as err:
            checkout["status"] = "ready_for_complete"
            return _error(err.code, err.message)
        if intent["status"] != "succeeded" or intent["transaction_id"] != record["hash"]:
            checkout["status"] = "ready_for_complete"
            return _error("payment_failed", f"PaymentIntent {intent['id']} is {intent['status']}")

        # 3. Order + AP2 checkout receipt bound to the closed mandate.
        order_id = f"ord_{secrets.token_hex(6)}"
        checkout["status"] = "completed"
        checkout["order"] = {"id": order_id, "permalink_url": f"https://{self.profile.website}/orders/{order_id}"}
        reference = compute_sha256_b64url(MandateClient().get_closed_mandate_jwt(checkout_mandate))
        receipt = ReceiptClient().create_checkout_receipt(merchant=self.profile.id, reference=reference, order_id=order_id)
        receipt_payload = receipt.model_dump(mode="json", exclude_none=True)
        bus.emit(self.actor, "ap2", "receipt.checkout", "Merchant signed AP2 checkout receipt", receipt_payload)
        bus.emit(self.actor, "ucp", "checkout.completed", f"Order {order_id} placed with {self.profile.name}", checkout)
        receipt_jwt = create_jwt(header={"alg": "ES256", "typ": "JWT", "kid": self.actor}, payload=receipt_payload, private_key=key_for(self.actor))
        return {"checkout": checkout, "ap2_checkout_receipt": receipt_jwt, "payment_intent": intent}

    # --- MCP wiring ---------------------------------------------------------

    def _build_server(self) -> MCPServer:
        server = MCPServer(self.profile.name, instructions=f"UCP shopping tools for {self.profile.name}.")

        @server.tool()
        def search_catalog(query: str = "") -> dict[str, Any]:
            """UCP catalog search. Returns products with price (cents), brand, stock, and a delivery estimate in days."""
            return self.search_catalog(query)

        @server.tool()
        def create_checkout(product_id: str, quantity: int, open_checkout_mandate: str) -> dict[str, Any]:
            """Create a UCP checkout for one product. Requires the user's AP2 open checkout mandate. Returns the merchant-signed checkout and its JWT."""
            return self.create_checkout(product_id, quantity, open_checkout_mandate)

        @server.tool()
        def complete_checkout(checkout_id: str, checkout_mandate: str, payment_intent_id: str) -> dict[str, Any]:
            """Complete a UCP checkout. Requires the agent's closed AP2 checkout mandate and a created PaymentIntent, which this merchant confirms."""
            return self.complete_checkout(checkout_id, checkout_mandate, payment_intent_id)

        return server
