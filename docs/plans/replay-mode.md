# Feature Implementation Plan: Replay Mode (v1)

**Overall Progress:** `85%`

## TLDR
A hosted, clickable version of the dashboard that needs no backend and no API key. We record real Claude runs of the demo scenarios as JSON, and the same `index.html` plays them back when no server is present. Deployed as a static site. Issue: [replay-mode.md](../issues/replay-mode.md).

## Critical Decisions
- **One page, two modes:** `index.html` probes `api/state`. If it gets a response it runs live as today; if not, it switches to replay. The hosted copy needs no build-time flag, and `?replay` forces replay mode locally.
- **Recordings carry state snapshots:** the budget bar, caps and brands come from `/api/state`, not the event stream. At record time we save the state at the start, and again at every event that triggers `refreshState()` (`payment_intent.succeeded`, `ledger.seeded`). Replay applies those snapshots, so there's no state logic duplicated in JS.
- **Recorded by a script, not by clicking:** `scripts/record_replays.py` drives the real agent in-process (same constructor as `app.py`), sends each scenario's message, and auto-decides approvals. The output is reproducible and can be re-recorded after code changes.
- **Approval branches = two live runs spliced at the decision:** each approval scenario is recorded twice, once approving and once denying. Replay plays the approve run up to `approval.requested`, then continues with whichever run the visitor picks. The deny run's suffix gets the approve run's `approval_id`, and its event ids are renumbered. The script **refuses to save** if the two runs' approval cards differ (merchant, item, total, reasons). The policy is deterministic, so they should match.
- **Pacing:** keep the original timing, but cap any gap at 1.5s (Claude's thinking time is dead air), with a 1× / 3× speed toggle.
- **Honest labelling:** a banner reads "Replay of live Claude runs recorded <date> · model <id> · View code".
- **Hosting: GitHub Pages** via an Actions workflow that copies `index.html` and `replays/` into the site. The repo is already public, and this needs no new account. *(Vercel was the alternative.)*
- **Relative paths:** Pages serves under `/coffee-refiller/`, so replay files load with relative URLs.

## Recordings (7 live runs → 4 files)
| File | Scenario message | Runs |
|---|---|---|
| `replays/happy-path.json` | "Ugh, we're out of coffee again" | 1 |
| `replays/over-cap.json` | "We need 4 bags for the offsite next week" | approve + deny |
| `replays/over-budget.json` | seed $70 spent, then "out of coffee" | approve + deny |
| `replays/by-tomorrow.json` | "…need coffee by tomorrow" | approve + deny |

File shape: `{ scenario, title, recorded_at, model, initial_state, events: [...], branch?: { at_event, approval_id, approve: [...], deny: [...] } }`. Events that triggered a state refresh carry a `state` field.

## Tasks

- [x] 🟩 **Step 1: Recorder**
  - [x] 🟩 Reuse the `/api/state` handler (`app.state()`) for snapshots; mount `/replays` so `/?replay` works locally
  - [x] 🟩 `scripts/record_replays.py`: per scenario, reset the ledger, bus and agent, optionally seed spend, run `handle_message`, auto-decide approvals after a short "human" pause, and collect `bus.history` plus state snapshots
  - [x] 🟩 Splice the approve/deny runs and check that their approval cards match
  - [x] 🟩 Secret check before writing: fail if any payload contains the API key or a private JWK (`"d":`)

- [x] 🟩 **Step 2: Front-end replay engine** (`static/index.html`)
  - [x] 🟩 Split `refreshState()` into `fetchState()` (live) and `applyState(s)` (shared)
  - [x] 🟩 Mode detection (`api/state` probe or `?replay`)
  - [x] 🟩 Player: queue events with capped delays into the existing `handle()`, and apply `state` snapshots
  - [x] 🟩 In replay, the sample buttons pick a scenario. Add an "Over budget" button, hide "Seed $70", and turn Reset into "Restart"
  - [x] 🟩 Approve/Deny in replay continues the chosen branch instead of calling the API
  - [x] 🟩 Disable the input box (placeholder: "Replay: run locally to chat live"), and add the banner and speed toggle

- [x] 🟩 **Step 3: Record the real runs** (needs the API key in `.env`; 7 short Sonnet runs, roughly cents)

- [x] 🟩 **Step 4: Tests**
  - [x] 🟩 Each replay file parses, events match `Event` fields, ids are increasing, and approval branches share one `approval_id`
  - [x] 🟩 No secrets in any replay file
  - [x] 🟩 Existing 21 tests still pass

- [x] 🟩 **Step 5: Deploy**
  - [x] 🟩 `.github/workflows/pages.yml`: build `_site/` from `static/index.html` + `replays/`, then deploy to Pages
  - [x] 🟩 Enable Pages (source: GitHub Actions) in the repo settings
  - [x] 🟩 README: a "▶ Try the demo" link at the top

- [ ] 🟥 **Step 6: Hero GIF** (after the site is live)
  - [ ] 🟥 Screen-record the happy path in replay at 3× (Cmd-Shift-5), then convert it to an optimized GIF with `ffmpeg` (`brew install ffmpeg`)
  - [ ] 🟥 Add the GIF to the top of the README

## Risks
- **Drift:** recordings go stale when the `Event` shape or UI changes. The Step 4 tests catch shape drift. Re-recording is one command.
- **Splice mismatch:** Claude may phrase the approval message differently between runs. Only the card fields have to match. If they don't, re-run.
- **Live mode must not regress:** all replay code paths are gated on the mode flag, and live mode is checked manually after Step 2.
- **Payload size:** Claude events include tool inputs and outputs. If the files get large (>1 MB), trim long payload strings at record time.
