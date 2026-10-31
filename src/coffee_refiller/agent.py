"""The shopping agent: Claude in a tool-use loop, with code holding the keys.

Claude decides *what* to do (which merchants to search, which offer to pick,
when to ask the user). Code decides whether it's *allowed*:
- offers and prices come from merchant responses, never from Claude's text
- the policy check runs on the merchant-signed checkout
- Claude never sees or passes mandates/JWTs; it works with short ids
  (checkout ids), and code signs the AP2 mandates
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import anthropic
from mcp import Client

from coffee_refiller.catalog import MERCHANTS, MERCHANTS_BY_ID
from coffee_refiller.events import bus
from coffee_refiller.ledger import Ledger
from coffee_refiller.mandates import (
    AGENT,
    OpenMandates,
    checkout_hash,
    close_checkout_mandate,
    close_payment_mandate,
    issue_one_time_mandates,
    issue_standing_mandates,
)
from coffee_refiller.merchant import FULFILLMENT_EXT, Merchant
from coffee_refiller.policy import Offer, PolicyResult, evaluate
from coffee_refiller.processor import MockStripe, StripeError
from coffee_refiller.settings import MandateSettings

MODEL = "claude-sonnet-5-5"
MAX_TURNS = 15
PAYMENT_METHOD = "pm_card_visa"  # mock Stripe test card 4242
MERCHANT_IDS = [m.id for m in MERCHANTS]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_catalog",
        "description": (
            "Search one merchant's catalog (UCP catalog search over MCP). Returns offers with product_id, brand, "
            "unit price in cents, delivery_days and in_stock. Search all three merchants (in parallel, in one turn) "
            "before choosing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string", "enum": MERCHANT_IDS},
                "query": {"type": "string", "description": "Search text, e.g. 'coffee' or a brand name."},
            },
            "required": ["merchant_id", "query"],
        },
    },
    {
        "name": "create_checkout",
        "description": (
            "Create a UCP checkout for one offer you found with search_catalog. The merchant signs the quote; then the "
            "purchase policy runs on it and returns decision 'ok', 'needs_approval' (with reasons) or 'reject'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string", "enum": MERCHANT_IDS},
                "product_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "deadline_days": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Delivery deadline in days from today: the default unless the user's message sets one.",
                },
            },
            "required": ["merchant_id", "product_id", "quantity", "deadline_days"],
        },
    },
    {
        "name": "request_approval",
        "description": (
            "Ask the user to approve a checkout whose policy decision was 'needs_approval'. Waits for their answer. "
            "If approved, the user signs a one-time AP2 mandate for exactly this checkout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "checkout_id": {"type": "string"},
                "message": {"type": "string", "description": "One or two sentences for the user: what, price, delivery, and why approval is needed."},
            },
            "required": ["checkout_id", "message"],
        },
    },
    {
        "name": "complete_purchase",
        "description": (
            "Pay for and complete a checkout whose policy decision was 'ok', or that the user approved. Signs the AP2 "
            "closed mandates, creates the PaymentIntent and completes the UCP checkout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"checkout_id": {"type": "string"}},
            "required": ["checkout_id"],
        },
    },
]


@dataclass
class CheckoutRecord:
    merchant_id: str
    offer: Offer
    quantity: int
    total_cents: int
    delivery_days: int
    jwt: str
    policy: PolicyResult
    approval: OpenMandates | None = None
    status: str = "ready"  # ready → completed | denied


@dataclass
class Approval:
    id: str
    checkout_id: str
    future: asyncio.Future[bool] = field(repr=False)


def _redact(value: Any, limit: int = 120) -> Any:
    """Shorten long tokens (JWTs, SD-JWTs) for the timeline."""
    if isinstance(value, str) and len(value) > limit:
        return f"{value[:24]}…({len(value)} chars)"
    if isinstance(value, dict):
        return {k: _redact(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, limit) for v in value]
    return value


def _usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


class ShoppingAgent:
    def __init__(self, settings: MandateSettings, ledger: Ledger, processor: MockStripe) -> None:
        self.settings = settings
        self.ledger = ledger
        self.processor = processor
        self.merchants = {m.id: Merchant(m, processor) for m in MERCHANTS}
        self.standing: OpenMandates = issue_standing_mandates(settings)
        self.client = anthropic.AsyncAnthropic()
        self.offers: dict[tuple[str, str], Offer] = {}
        self.checkouts: dict[str, CheckoutRecord] = {}
        self.approvals: dict[str, Approval] = {}
        self.busy = False

    # --- chat entry point -------------------------------------------------

    async def handle_message(self, text: str) -> None:
        bus.emit("user", "chat", "message.user", text, {"text": text})
        self.busy = True
        try:
            reply = await self._run(text)
        except anthropic.APIError as exc:
            reply = f"Sorry, I couldn't reach Claude ({type(exc).__name__}). Nothing was bought."
        except Exception as exc:  # surface any bug in chat instead of failing silently
            bus.emit(AGENT, "claude", "run.error", f"Agent run failed: {exc!r}", {"error": repr(exc)})
            reply = "Something went wrong on my side, so I stopped. See the timeline for the error."
        finally:
            self.busy = False
        bus.emit(AGENT, "chat", "message.agent", reply, {"text": reply})

    def decide(self, approval_id: str, approved: bool) -> bool:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.future.done():
            return False
        approval.future.set_result(approved)
        return True

    # --- the loop -----------------------------------------------------------

    def _system_prompt(self) -> str:
        s = self.settings
        spent = self.ledger.month_spend_cents()
        brands = " > ".join(s.preferred_brands)
        return f"""You are the purchasing agent for a small office's coffee supply. People post in the #office-coffee chat; you reorder coffee for them.

Today is {dt.date.today():%A %B %d, %Y}.

The user's standing rules (enforced by signed AP2 mandates and policy code; you can't override them):
- Per-order cap: {_usd(s.per_order_cap_cents)}. Monthly budget: {_usd(s.monthly_budget_cents)}. Spent this month so far: {_usd(spent)}.
- Preferred brands, best first: {brands}. Never buy other brands.
- Delivery deadline: {s.deadline_days} days, unless the message sets one (e.g. "by tomorrow" = 1). Turn dates into days from today.
- Default quantity: 1 bag, unless the message asks for more.

How to choose:
1. Search all three merchants ({', '.join(MERCHANT_IDS)}) in one turn.
2. Consider in-stock offers from preferred brands that arrive within the deadline.
3. Pick the highest-ranked brand whose order fits the per-order cap and the remaining monthly budget. If the best brand doesn't fit but a lower-ranked one does, take the lower-ranked one. Price only matters for fitting the limits; on a tie in brand, prefer faster delivery, then lower price.
4. If nothing fits the limits, pick the highest-ranked on-time offer and ask for approval.
5. If nothing arrives within the deadline, relax it: pick from the offers that arrive soonest (best brand among them) and ask for approval.
6. create_checkout, then follow the policy decision: 'ok' → complete_purchase; 'needs_approval' → request_approval, then complete_purchase only if approved; 'reject' → choose differently.

Finish with a short chat reply (2-4 sentences): what you bought (or didn't), from whom, price, delivery, and the deadline you used. Plain text, no markdown headers."""

    async def _run(self, text: str) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        for turn in range(1, MAX_TURNS + 1):
            response = await self.client.messages.create(
                model=MODEL, max_tokens=2048, system=self._system_prompt(), tools=TOOLS, messages=messages
            )
            content = [self._block_to_param(b) for b in response.content]
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            texts = " ".join(b.text for b in response.content if b.type == "text").strip()
            thinking = " ".join(b.thinking for b in response.content if b.type == "thinking").strip()
            bus.emit(
                AGENT,
                "claude",
                "messages.create",
                f"Claude turn {turn}: " + (", ".join(f"{b.name}({b.input.get('merchant_id', b.input.get('checkout_id', ''))})" for b in tool_uses) or "final reply"),
                {
                    "stop_reason": response.stop_reason,
                    "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
                    "thinking": thinking or None,
                    "reasoning": texts if tool_uses else None,
                    "tool_calls": [{"name": b.name, "input": b.input} for b in tool_uses],
                },
            )
            if response.stop_reason != "tool_use":
                return texts or "Done."
            # Claude may ask for several tools in one turn; run them concurrently.
            results = await asyncio.gather(*(self._run_tool(b.id, b.name, b.input) for b in tool_uses))
            messages += [{"role": "assistant", "content": content}, {"role": "user", "content": list(results)}]
        return "I hit my step limit before finishing, so nothing more was bought. Check the timeline for where I got stuck."

    @staticmethod
    def _block_to_param(block: Any) -> dict[str, Any]:
        """Echo a response block back as conversation history.

        Thinking blocks must be returned unchanged (with their signature) for
        tool use to continue across turns.
        """
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
        if block.type == "thinking":
            return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
        if block.type == "redacted_thinking":
            return {"type": "redacted_thinking", "data": block.data}
        return {"type": "text", "text": block.text}

    async def _run_tool(self, tool_use_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "search_catalog": self._search_catalog,
            "create_checkout": self._create_checkout,
            "request_approval": self._request_approval,
            "complete_purchase": self._complete_purchase,
        }
        try:
            result = await handlers[name](**args)
        except (KeyError, TypeError) as exc:
            result = {"error": f"bad tool call: {exc}"}
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(result), "is_error": "error" in result}

    # --- MCP --------------------------------------------------------------------

    async def _call_merchant(self, merchant_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """One MCP `tools/call`, logged both ways on the timeline."""
        merchant = self.merchants[merchant_id]
        bus.emit(AGENT, "mcp", "tools/call", f"→ {merchant.profile.name}: {tool}", {"server": merchant.actor, "tool": tool, "arguments": _redact(args)})
        async with Client(merchant.server) as client:
            result = await client.call_tool(tool, args)
        payload = json.loads(result.content[0].text)
        bus.emit(merchant.actor, "mcp", "tools/call.result", f"← {merchant.profile.name}: {tool} result", _redact(payload))
        return payload

    # --- tools --------------------------------------------------------------------

    async def _search_catalog(self, merchant_id: str, query: str) -> dict[str, Any]:
        response = await self._call_merchant(merchant_id, "search_catalog", {"query": query})
        offers = []
        for p in response.get("products", []):
            offer = Offer(
                merchant_id=merchant_id,
                product_id=p["id"],
                brand=p["brand"],
                title=p["title"],
                unit_price_cents=p["price_range"]["min"]["amount"],
                delivery_days=p[FULFILLMENT_EXT]["delivery_days"],
                in_stock=p["in_stock"],
            )
            self.offers[(merchant_id, offer.product_id)] = offer
            offers.append(
                {"product_id": offer.product_id, "brand": offer.brand, "title": offer.title, "unit_price_cents": offer.unit_price_cents, "delivery_days": offer.delivery_days, "in_stock": offer.in_stock}
            )
        return {"merchant_id": merchant_id, "offers": offers}

    async def _create_checkout(self, merchant_id: str, product_id: str, quantity: int, deadline_days: int) -> dict[str, Any]:
        offer = self.offers.get((merchant_id, product_id))
        if offer is None:
            return {"error": f"Unknown offer {merchant_id}/{product_id}. Use search_catalog first."}
        response = await self._call_merchant(
            merchant_id, "create_checkout", {"product_id": product_id, "quantity": quantity, "open_checkout_mandate": self.standing.checkout}
        )
        if "checkout" not in response:
            return {"error": response.get("messages", [{}])[0].get("content", "checkout failed")}

        # Policy runs on what the merchant signed, not on the catalog or Claude's claims.
        checkout = response["checkout"]
        total = checkout["totals"][-1]["amount"]
        delivery = checkout[FULFILLMENT_EXT]["delivery_days"]
        signed_offer = Offer(merchant_id, product_id, offer.brand, offer.title, total // quantity, delivery, offer.in_stock)
        policy = evaluate(signed_offer, quantity, self.ledger.month_spend_cents(), deadline_days, self.settings)
        self.checkouts[checkout["id"]] = CheckoutRecord(merchant_id, signed_offer, quantity, total, delivery, response["checkout_jwt"], policy)
        bus.emit(
            "policy",
            "policy",
            f"policy.{policy.decision}",
            f"Policy: {policy.decision.replace('_', ' ')}" + (f" ({'; '.join(policy.reasons)})" if policy.reasons else ""),
            {"checkout_id": checkout["id"], "decision": policy.decision, "reasons": list(policy.reasons), "deadline_days": deadline_days},
        )
        return {
            "checkout_id": checkout["id"],
            "merchant": MERCHANTS_BY_ID[merchant_id].name,
            "item": f"{quantity}× {offer.title}",
            "total_cents": total,
            "delivery_days": delivery,
            "deadline_days": deadline_days,
            "policy_decision": policy.decision,
            "policy_reasons": list(policy.reasons),
        }

    async def _request_approval(self, checkout_id: str, message: str) -> dict[str, Any]:
        record = self.checkouts.get(checkout_id)
        if record is None:
            return {"error": f"Unknown checkout {checkout_id}"}
        if record.policy.decision != "needs_approval":
            return {"error": f"Checkout policy decision is '{record.policy.decision}'; approval is only for 'needs_approval'."}

        approval = Approval(f"apr_{secrets.token_hex(4)}", checkout_id, asyncio.get_running_loop().create_future())
        self.approvals[approval.id] = approval
        merchant = MERCHANTS_BY_ID[record.merchant_id]
        bus.emit(
            AGENT,
            "chat",
            "approval.requested",
            message,
            {
                "approval_id": approval.id,
                "checkout_id": checkout_id,
                "merchant": merchant.name,
                "item": f"{record.quantity}× {record.offer.title}",
                "total": _usd(record.total_cents),
                "delivery_days": record.delivery_days,
                "reasons": list(record.policy.reasons),
            },
        )
        try:
            approved = await asyncio.wait_for(approval.future, timeout=self.settings.approval_ttl_seconds)
        except TimeoutError:
            approved = False
        bus.emit("user", "chat", "approval.decided", "Approved" if approved else "Denied", {"approval_id": approval.id, "approved": approved})
        if not approved:
            record.status = "denied"
            return {"approved": False}
        # The user's key signs a one-time mandate pinned to this exact checkout.
        record.approval = issue_one_time_mandates(merchant, record.offer.product_id, record.offer.title, record.quantity, record.total_cents, self.settings)
        return {"approved": True, "expires_in_minutes": self.settings.approval_ttl_seconds // 60}

    def _mandates_for(self, record: CheckoutRecord) -> OpenMandates | str:
        """Which user-signed authority covers this checkout, or why none does."""
        if record.status != "ready":
            return f"Checkout is {record.status}."
        if record.policy.decision == "ok":
            return self.standing
        if record.policy.decision == "reject":
            return "Policy rejected this checkout."
        if record.approval is None:
            return "This checkout needs the user's approval first (request_approval)."
        if record.approval.expires_at < dt.datetime.now().timestamp():
            return "The user's approval has expired; ask again."
        return record.approval

    async def _complete_purchase(self, checkout_id: str) -> dict[str, Any]:
        record = self.checkouts.get(checkout_id)
        if record is None:
            return {"error": f"Unknown checkout {checkout_id}"}
        mandates = self._mandates_for(record)
        if isinstance(mandates, str):
            return {"error": mandates}

        merchant = MERCHANTS_BY_ID[record.merchant_id]
        tx = checkout_hash(record.jwt)
        closed_checkout = close_checkout_mandate(mandates.checkout, record.jwt, merchant.id, nonce=checkout_id)
        closed_payment = close_payment_mandate(mandates.payment, tx, merchant, record.total_cents, PAYMENT_METHOD)
        try:
            intent = self.processor.create_payment_intent(
                amount=record.total_cents,
                currency=self.settings.currency,
                payee=merchant.id,
                payment_method=PAYMENT_METHOD,
                transaction_id=tx,
                ap2_payment_mandate=closed_payment,
                idempotency_key=f"pi-{checkout_id}",
            )
        except StripeError as err:
            return {"error": f"Payment refused: {err.message}"}

        result = await self._call_merchant(
            merchant.id, "complete_checkout", {"checkout_id": checkout_id, "checkout_mandate": closed_checkout, "payment_intent_id": intent["id"]}
        )
        if result.get("checkout", {}).get("status") != "completed":
            return {"error": result.get("messages", [{}])[0].get("content", "checkout failed")}
        record.status = "completed"
        return {
            "order_id": result["checkout"]["order"]["id"],
            "merchant": merchant.name,
            "item": f"{record.quantity}× {record.offer.title}",
            "paid_cents": record.total_cents,
            "delivery_days": record.delivery_days,
            "month_spend_cents": self.ledger.month_spend_cents(),
            "authority": "one-time approval" if record.approval else "standing mandate",
        }
