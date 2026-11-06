# Replay mode: clickable demo with no API key

**Type:** feature · **Priority:** high · **Effort:** medium

## TL;DR
Record real Claude runs of the 4 demo scenarios as JSON, and let the dashboard play them back from those files. Host it statically (e.g. Vercel) so anyone can click through a genuine run without a backend or API key.

## Current state
- The dashboard only works live: it needs the Python server, the `/api/events` stream and `ANTHROPIC_API_KEY`.
- So it can't be shared as a link. Hosting it live would expose API spend to abuse.

## Expected outcome
- **Record:** a way to run a scenario live and save `bus.history` to `replays/<scenario>.json`.
- **Play back:** the dashboard can read events from a replay file instead of the live stream, keeping the original timing (with a speed-up toggle).
- **Branching approvals:** scenarios 2 and 3 record both the Approve and Deny paths, and playback continues down whichever one the visitor clicks.
- **Everything still works:** expandable rows, protocol filters, the budget bar and the chat all behave the same as live.
- **Honest banner:** "Recorded from a live Claude run on <date> · View code" with a link to the repo.
- **Static deploy:** the HTML and JSON files deploy to Vercel with no server.

## Relevant files
- [src/coffee_refiller/events.py](../../src/coffee_refiller/events.py): `Event` / `EventBus`, which is the source of the recordings.
- [src/coffee_refiller/static/index.html](../../src/coffee_refiller/static/index.html): `EventSource("/api/events")` (~line 263) needs a pluggable event source, and the scenario buttons and approval card need replay hooks.
- [src/coffee_refiller/app.py](../../src/coffee_refiller/app.py): the record/export hook, which could be an endpoint or a CLI flag.

## Risks / notes
- **Drift:** recordings go stale if the `Event` format changes. Add a test that loads each replay file and checks it against the current `Event` fields.
- **Secrets:** check that recorded payloads contain no API keys or real private keys before committing them (the mock keys are fine).
- **Refactor scope:** the frontend currently assumes the live server for `fetch()` calls (chat, approve, reset). In replay mode those calls must be stubbed out. This is likely the bulk of the work.
- **No free text:** visitors can't type new messages in replay. Make that obvious in the UI rather than leaving a dead input box.
- **Unlocks:** a shareable demo link and GIFs for the README.
