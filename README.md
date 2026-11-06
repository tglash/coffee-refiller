# ☕ Coffee Refiller

A weekend learning prototype for **agentic purchasing**. Someone posts "we're out of coffee" in a Slack-style chat. A Claude-powered agent shops three mock merchants and buys within rules you signed. Every protocol message shows up on a live timeline:

- **MCP**: how the agent talks to merchants (`tools/call`)
- **UCP**: what the merchant tools mean (catalog search, checkout, order)
- **AP2**: cryptographic proof that *you* allowed this purchase (signed mandates)
- **Stripe-style processor**: PaymentIntents, test cards, idempotency, webhooks

Nothing here is real: the merchants, money and keys are all mocks. Claude is real.

> Independent learning project. Not affiliated with or endorsed by Google, Shopify, Anthropic, Stripe or any coffee brand named here. Brand names appear only as mock catalog data.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (`brew install uv`) and an Anthropic API key.

```bash
uv sync
cp .env.example .env   # then paste your key after ANTHROPIC_API_KEY=
```

## Run

```bash
uv run coffee-refiller
```

Open http://127.0.0.1:8000.

```bash
uv run pytest          # 21 tests: policy + end-to-end protocol flows (no API key needed)
```

## Your standing mandate (v1)

| Rule | Enforced by |
|---|---|
| $40 per order | AP2 `AmountRange`, a signed constraint the processor verifies |
| $80 per month | AP2 `Budget` + `AgentRecurrence(MONTHLY)`, checked against the ledger |
| Only BeanCo, OfficeMart, GrocerGo | AP2 `AllowedMerchants` / `AllowedPayees` |
| Brands: Stumptown → Counter Culture → Lavazza → Folgers | Policy code (AP2 covers *authority*, not *preference*) |
| Delivered within 3 days (a message can override) | Policy code |
| Over a limit → ask the user | Policy code + a **one-time AP2 mandate signed by you** |

## Demo scenarios

Use the sample buttons above the chat. **Reset** starts a fresh month.

**1. Happy path: "Out of coffee"**
Claude searches all 3 merchants *in one turn* (three MCP calls fire together), picks Stumptown from BeanCo ($18, 2 days), and the policy says ok. Watch the AP2 rows: the agent signs closed mandates, and the processor and merchant each verify the chain back to *your* key, then sign receipts.

**2. Over the per-order cap: "4 bags"**
Every 4-bag order is over $40 (even Folgers is $48), so Claude picks the top brand and asks. The approval card explains why. **Deny** → nothing is bought. **Approve** → your key signs a one-time mandate pinned to exactly that merchant, cart and amount (event `mandate.open.one_time`), and the purchase goes through under it.

**3. Over the monthly budget: "Seed $70 spent", then "Out of coffee"**
$18 would bring the month to $88. Approve it and watch the budget bar go red. Approved orders count toward the budget.

**4. Nothing arrives in time: "By tomorrow"**
Claude turns "by tomorrow" into a 1-day deadline. Nothing arrives in under 2 days, so it relaxes the deadline to the soonest option and asks.

**Things to try:** expand any timeline row to see its full payload, and use the protocol filters to follow one protocol at a time.

## How it fits together

```
chat ──► ShoppingAgent (Claude Sonnet 5.5, tool-use loop)            agent.py
           │  search_catalog / create_checkout / complete_purchase
           ├─ MCP tools/call ──► 3 merchants (UCP payloads)           merchant.py, catalog.py
           ├─ policy check on the merchant-signed quote              policy.py
           ├─ request_approval ──► you ──► one-time AP2 mandate        mandates.py
           ├─ AP2 closed checkout + payment mandates (agent's key)    mandates.py
           └─ PaymentIntent ──► mock Stripe (verifies AP2 chain)      processor.py, ledger.py
everything ──► event bus ──► SSE ──► dashboard                         events.py, app.py, static/
```

Design rule: **Claude chooses; code decides.** Prices come from what the merchant signed, never from Claude's text. Claude never handles mandates or JWTs; it passes short checkout ids, and code does the signing.

## Honest caveats

- All actors run in one process, so your key and the agent's key sit side by side. Real AP2 keeps the user's key on the user's device; this *simulates* that boundary.
- UCP's MCP binding isn't fully spec-exact: delivery estimates use an extension field, and the merchant's detached signature isn't JCS-canonicalized.
- AP2 is pre-1.0 and pinned to one commit. See [docs/sdk-notes.md](docs/sdk-notes.md) for versions, findings, and an SDK bug we work around.

## Docs

- [docs/plans/agentic-coffee-reorder.md](docs/plans/agentic-coffee-reorder.md): the build plan and decisions
- [docs/sdk-notes.md](docs/sdk-notes.md): what AP2 V2, UCP and MCP 2.x actually look like

## License

MIT. See [LICENSE](LICENSE).
