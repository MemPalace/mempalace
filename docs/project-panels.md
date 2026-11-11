# Project panels

A project can declare a small panel as data: a few highlight lines and action
links. The app shows it at the top of the project's board. The panel is plain
text from a repo, an agent or the owner, so it is checked strictly and drawn as
text, never as markup.

## File format `commonplace.panel/1`

Keep it in the repo, for example as `.commonplace/panel.json`:

```json
{
  "format": "commonplace.panel/1",
  "title": "Atlas",
  "highlights": ["Beta ships Friday", "3 open risks"],
  "links": [{ "label": "Runbook", "url": "https://example.com/runbook" }]
}
```

| Field | Rule |
| --- | --- |
| `format` | Required, exactly `commonplace.panel/1`. |
| `title` | Optional text, at most 60 characters. |
| `highlights` | At most 6 lines of text, each at most 200 characters. |
| `links` | At most 6 objects with exactly `label` (at most 40 characters) and `url`. |

- Any other field is refused. A panel needs at least one highlight or link.
- Text is plain: control and invisible-direction characters, and anything that
  looks like an HTML tag (`<b`, `</`, `<!`), are refused. Text is trimmed.
- A `url` must be absolute `http://` or `https://`, at most 500 characters, with
  a host, no spaces, quotes, angle brackets or backslashes, and no user name or
  password. `javascript:`, `data:`, `file:`, `ftp:` and relative links are refused.
- The whole panel is at most 4096 bytes once normalized.
- A new version of the format gets a new `format` value; `/1` files keep working.

## Submit and read

- `PUT /api/projects/<slug>/panel` with `{"panel": <document>}` sets the panel;
  `{"panel": null}` clears it. The answer is `{"panel": ..., "updated_at": ...}`.
- `GET /api/projects/<slug>/panel` answers `{"panel": ...}`, with `null` when the
  project has none. Anyone who can read the project can read it; the project view
  (`GET /api/projects/<slug>`) is not changed.
- Setting needs the project's board-write authority: the owner or a member who
  may change the board, or an agent connection with `memory:write` on the
  project's wing. Read-only and add-cards-only connections get 403. A connection
  uses the same `rules_seq` header as for other writes.
- Refusals say what to change: 400 `invalid_panel`, 413 `panel_too_large`,
  403 `not_allowed`, 404 for an unknown project.

In the app the links open in a new tab with `rel="noopener noreferrer nofollow"`;
a link that is not http(s) is dropped again at draw time.
