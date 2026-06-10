# Spec 04 — Dashboard + Integration + Demo (Eng 4)

**Owner:** Eng 4 — also the **"does it actually run end-to-end" owner**. This role is what saves Day 2.
**Stack:** your fastest option — Streamlit (read-only-friendly) or Next.js — reading the backend `/stats`.
**Depends on:** Backend `/stats` (Eng 1). Scaffold against the response shape (PRD §6) before real data lands.

> Read `00-hivemind-overview-prd.md` §8 (caveats), §9 (plan), §10 (demo).

## Goal

Two jobs: a dashboard backed by **real** metadata, and ownership of the integration glue + demo rehearsal so the whole thing actually runs when it matters.

## What you build

### Dashboard
Backed by `GET /stats` (real, not mocked):
- **Top contributors** — `by_author`, ranked. (This is requirement (a): who's contributed most.)
- **Per-service coverage** — `by_service` count + `last_updated`. (This is requirement (b), reframed.)

**Framing matters.** Label the per-service number **coverage**, not "health." "More memory = healthier" rewards spam and ignores whether memories get used (PRD §8). Mock only the fancier "efficiency/onboarding-speed" claims if you want them on screen — make clear in the demo which numbers are real (contributors, coverage) and which are illustrative.

If time (could): a **retrieval hit rate** panel — how often a search returns something used. Far more defensible than raw counts, but optional.

### Integration + glue
You own the end-to-end path. Make sure: backend up → seed loaded → MCP server registered → hook firing → dashboard reading live data. When a seam breaks, you're the one chasing it down. Keep a one-command (or one-checklist) way to bring the whole stack up for the demo.

### Demo rehearsal (own this)
Two scripted runs (PRD §10):
1. **Cold onboarding** — fresh session in a seeded service the "developer" has never touched; normal question; hook silently injects seeded memories; Claude answers with context it otherwise wouldn't have. *Eng 3's hook is the star here.*
2. **Cross-service feature** — task spanning two services; memories from both pulled; close on the dashboard's contributor/coverage view.

Rehearse both at least twice. Leave a buffer block for breakage. Decide the fallback if live retrieval misfires (e.g. a known-good prompt that reliably hits seeded memories).

## Acceptance criteria
- [ ] Contributors and coverage views render from **real** `/stats` data.
- [ ] Coverage is labeled as coverage, not health; mocked numbers are visibly distinguished.
- [ ] The full stack comes up from a known checklist/command.
- [ ] Both demo scripts run start-to-finish without manual data fudging.
- [ ] A tested fallback exists if live retrieval misfires on stage.

## Coordinate with
- **Eng 1** on `/stats` shape and on seed content (you know what the demo needs to surface).
- **Eng 3** on the cold-onboarding moment.
- **Everyone** on the bring-up checklist.
