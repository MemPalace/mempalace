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
│   └── mempalace/
│       └── SKILL.md       # the in-plugin skill discovered by Antigravity
└── README.md              # this file
```

The hook scripts themselves live at `hooks/antigravity/`. The installer
copies them into `<install-dir>/hooks/` and renders `hooks.json.tmpl`
into a `hooks.json` whose `command` paths point at the absolute install
location.

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
