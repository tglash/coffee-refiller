# SDK Notes

Findings from Phase 1 discovery (2026-10-29). Read the SDK source; don't trust old blog posts. AP2 changed a lot in V2.

## Versions

| Package | Version | Source |
|---|---|---|
| `ap2` | 0.2.0 ("V2", Apr 2026) | GitHub `google-agentic-commerce/AP2`, pinned to commit `e1ea56d`. The `ap2` package on PyPI is a third-party mirror; don't use it. |
| `ucp-sdk` | 0.5.0 | PyPI (official) |
| `mcp` | latest | PyPI |
| Python | 3.12 | `.python-version` |

## AP2 V2: what changed vs. what we planned

The "Intent / Cart / Payment Mandate" terms come from v0.1. V2 replaces them with **open** mandates (the user's standing rules) and **closed** mandates (one specific purchase):

| Our plan said | V2 reality | Who signs |
|---|---|---|
| Intent Mandate | **Open Checkout Mandate** (what can be bought, where) + **Open Payment Mandate** (how much, how often) | User (root issuer) |
| Cart Mandate | Merchant-signed **checkout JWT** → wrapped in a closed **Checkout Mandate** | Merchant signs the checkout; the agent signs the mandate on top |
| Payment Mandate | Closed **Payment Mandate** | Agent |
| (n/a) | **Payment / Checkout Receipts**, bound to the closed mandate by hash | Processor / merchant |

**Signing is real, not mocked.** V2 uses SD-JWT delegation chains (ES256 keys). The user's key signs the open mandate, which names the agent's key (`cnf`); the agent signs the closed mandate on top. Verifiers trust only the user's key. We generate local keys, so there are still no real credentials. A smoke test passed: a $24 order verifies, and a $52 order fails with `Amount 5200 exceeds maximum 4000`.

### Our rules map onto native AP2 constraints

| Our rule | AP2 constraint | Notes |
|---|---|---|
| $40 / order | `AmountRange(currency="USD", max=4000)` | **cents** |
| $80 / month | `Budget(max=80.0, currency="USD")` + `AgentRecurrence(frequency=MONTHLY)` | Budget `max` is in **dollars (float)**, amounts are cents. Past spend comes from a `MandateContext` that **we** supply (our ledger); `calculate_period_start(MONTHLY)` gives the month boundary. |
| Only our 3 merchants | `AllowedPayees` (payment) + `AllowedMerchants` (checkout) | |
| Coffee only | `LineItems` with `acceptable_items` | Optional for v1 |
| Brand ranking, 3-day deadline | **No AP2 constraint**; this is our policy code | AP2 handles *authority*, not *preference* |

### ⚠️ Design impact: approvals

AP2 constraints are **hard**: a closed mandate over the cap fails verification, full stop. The agent *cannot* approve its own over-budget purchase. So the "ask me to approve" flow becomes:

1. Policy finds the order is over a limit → the agent asks in chat (UCP checkout status `requires_escalation` fits this).
2. You approve → **your** key signs a one-time open mandate for that exact checkout (the "human-present" path).
3. The agent closes the mandate against that one-time mandate instead of the standing one.

This is closer to how AP2 is meant to work, and it's the most instructive part of the build.

### Roles in the samples that we're folding

The AP2 samples use a separate **Credentials Provider** (it issues payment tokens) and talk over **A2A**. For v1 we fold the credentials provider into the mock Stripe-style processor and keep everything in one process, as planned.

Useful reference: `code/samples/python/src/roles/merchant_agent_mcp/server.py` is a FastMCP merchant with tools `search_inventory`, `assemble_cart`, `create_checkout`, `complete_checkout`. It's close to our step 4.

## UCP

- `ucp-sdk` is **Pydantic models only**: no server, no client. We build the merchant servers ourselves.
- Shopping capabilities used: `catalog_search`, `checkout`, `fulfillment`, `order`.
- **MCP transport** (`transports/mcp_tool_call`): UCP rides on MCP `tools/call`, with a `meta` holding `ucp-agent` (the agent's profile) and **`idempotency-key`**, so UCP's MCP binding has idempotency built in.
- **AP2 extension** (`common/payment_ap2_mandate`): a UCP Checkout carries `ap2.merchant_authorization` and `ap2.checkout_mandate`. This is the official bridge between the two protocols.
- **Fulfillment** gives our delivery deadline a place in the protocol (delivery options and availability per product).
- Checkout lifecycle: `incomplete → requires_escalation → ready_for_complete → complete_in_progress → completed | canceled`.

### ⚠️ Two "Checkout" models

- `ucp_sdk...shopping.checkout.Checkout`: the full UCP model (requires `ucp` metadata, `links`, and typed totals). Used on the wire, over MCP.
- `ap2.sdk.generated.types.checkout.Checkout`: AP2's simpler, vendored copy. **AP2's checkout constraint evaluators require this one.**
- Plan: merchants speak full UCP over MCP; we convert to the AP2 shape just before signing and verifying. Watch for schema drift between the two.

## Gotchas

- **AP2 SDK bug (e1ea56d):** `MandateClient.create` can't sign an `AgentRecurrence` constraint. `delegate_claims_from_model` calls `model_dump()` without `mode="json"`, so the `Frequency` enum isn't JSON-serializable. Worked around in `mandates.py` with `_JsonSafeOpenPaymentMandate`. Worth reporting upstream.
- **AP2 checkout verification doesn't check the merchant's signature** on `checkout_jwt`; it only decodes it. The merchant verifies its own JWT in `complete_checkout`.
- **UCP checkout → AP2 Checkout works directly:** a UCP checkout plus a `merchant` field validates as AP2's Checkout (it allows extra fields), so no conversion is needed.
- **MCP is 2.x:** `FastMCP` is now `mcp.server.mcpserver.MCPServer`; `mcp.Client(server)` connects in-process. Most examples online are 1.x.
- **UCP `merchant_authorization`** is a detached JWS (`header..signature`). We sign with `json.dumps`, not strict JCS canonicalization; fine in-process, but not spec-exact.

- AP2 `pyproject` pins `pytest==9.0.2` and pulls in `py` as runtime deps. It's harmless, but don't fight the pins.
- AP2 is pre-1.0 and changes fast. Re-check the source before relying on any API.
