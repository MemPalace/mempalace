# MemPalace — Antigravity plugin

In-repo packaging for the MemPalace integration with Google's [Antigravity IDE](https://antigravity.google/).

This directory is the source of truth for what gets installed at
`~/.gemini/config/plugins/mempalace/` when the user runs the installer.

## Layout

```
.antigravity-plugin/
├── plugin.json            # marker manifest (verified minimal schema)
├── mcp_config.json        # auto-registers the mempalace-mcp stdio server
├── hooks.json.tmpl        # template — installer renders to hooks.json
├── skills/
│   ├── mempalace/
│   │   └── SKILL.md       # ops skill: setup, mine, status, CLI delegation
│   └── mempalace-recall/
│       └── SKILL.md       # recall-only skill: search-before-answer protocol
├── rules/
│   └── mempalace-recall.md # optional recall rule (complements the skill)
└── README.md              # this file
```

The hook scripts themselves live at `hooks/antigravity/`. The installer
copies them into `<install-dir>/hooks/` and renders `hooks.json.tmpl`
into a `hooks.json` whose `command` paths point at the absolute install
location.

## Three recall layers

MemPalace can store everything, but it only helps if the agent actually
*reads* the palace before answering. Three layers wire that in, from
eager to on-demand:

1. **Wake hook** (`hooks/antigravity/mempal_wake_hook_antigravity.sh`,
   `PreInvocation` event, gated to `invocationNum == 1`). On the first
   model call of a conversation it runs `mempalace wake-up` and injects
   the **actual palace content verbatim** via Antigravity's
   `injectSteps[].ephemeralMessage` output. This is Antigravity's native
   equivalent of Cursor's `sessionStart` `additional_context`, except it
   delivers the memory itself rather than a directive to go fetch it.
2. **Recall skill** (`skills/mempalace-recall/SKILL.md`). The
   search-before-answer protocol the agent follows when a turn is
   recall-relevant — tool selection, unhappy paths, anti-patterns. It
   covers recall only; the `mempalace` skill covers setup / mine /
   status.
3. **Optional recall rule** (`rules/mempalace-recall.md`). A lightweight
   markdown rule that nudges the agent to search before answering when
   Antigravity's matcher decides the turn is recall-relevant. It is
   deliberately recall-scoped (not an always-on global rule) so it never
   adds latency to greenfield work, honouring MemPalace's "memory should
   feel instant" budget.

All three point to the single canonical protocol in
[`integrations/shared/recall-protocol.md`](../integrations/shared/recall-protocol.md)
so the skill and rule never drift.

## Install

```bash
bash hooks/antigravity/install.sh
```

The installer is idempotent and the uninstaller matches by basename, so
re-runs and partial installs are safe.

See [website/guide/antigravity.md](../website/guide/antigravity.md) for
the full user-facing guide and [hooks/antigravity/README.md](../hooks/antigravity/README.md)
for the hooks-specific documentation.

## Verified surfaces

Every file in this directory maps to a surface verified against
[Google's Antigravity docs](https://antigravity.google/docs/). See
[hooks/antigravity/INVESTIGATION.md](../hooks/antigravity/INVESTIGATION.md)
for the full audit, including the surfaces deliberately not shipped.
