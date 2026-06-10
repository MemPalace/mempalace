# Spec 03 — Auto-Pull Hook (Eng 3)

**Owner:** Eng 3.
**Stack:** a Claude Code hook (script) + HTTP call to the backend.
**Depends on:** Backend `/search` (Eng 1). Build against the contract early with a fixed service string; swap in real detection on Day 2.

> Read `00-hivemind-overview-prd.md` §4.4–§4.5 and §3 (read path). This hook is the **demo centerpiece** — it's the "magic" the audience sees.

## Goal

On every prompt, before Claude sees it, detect the current service, query the backend for the most relevant memories, and inject them as context. The developer does nothing; relevant service knowledge just appears.

## What you build

### The hook
Use the Claude Code prompt-time hook (likely `UserPromptSubmit` — **verify the exact event name and payload against the current Claude Code docs**, this is the detail most likely to be stale). The hook:
1. Reads the user's prompt text from the hook payload.
2. Determines the current `service` (see below).
3. Calls `GET /search?q=<prompt>&service=<service>&k=5`.
4. Formats the top results and injects them as additional context ahead of the prompt.

Keep it **fast and silent** — this runs on every prompt. Local embeddings (PRD §4.6) keep the search hop quick; don't add anything heavy here. Fail open: if the backend is down or returns nothing, inject nothing and let the prompt through untouched. Never block the developer.

### Service detection
Simplest convention that works: a **`.hivemind` file in the repo root** naming the service:
```
service: notary
```
Read it from the working directory. If absent, either skip injection or fall back to an unscoped search — decide with Eng 2 (who reads the same file in `/hivemind-push`). **Keep this convention identical across the hook and the slash command.**

### Injection formatting
Make injected memories clearly delimited and labeled so they read as reference context, e.g. a short "Relevant Hivemind memories for `<service>`:" header followed by each memory's `title` + `body` (and `refs` if useful). Cap at `k` so you don't bloat the prompt.

## Acceptance criteria
- [ ] A fresh session in a seeded service gets relevant memories injected automatically, no manual step.
- [ ] Injection is scoped to the current service (no cross-service bleed).
- [ ] Backend down / empty result → prompt still goes through cleanly (fail open).
- [ ] Injected block is readable and clearly marked as Hivemind context.

## Gotchas
- Verify the hook event name/payload against live docs before wiring — don't trust memory.
- Don't make the hook write anything. Hooks pull; humans push (PRD §4.4).
- Watch latency — this is in the hot path of every prompt.

## Coordinate with
- **Eng 2** on the `.hivemind` convention (shared).
- **Eng 4** on the demo: the cold-onboarding moment is your hook firing. Rehearse it.
