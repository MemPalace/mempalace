"""Screenshots of the layout scenarios and a side-by-side compare.html.

Both stay outside the repository: set CP_E2E_SHOTS to the output folder and, for the
compare page, CP_E2E_DESIGN_SHOTS to the folder with the design's PNGs. Without
CP_E2E_SHOTS nothing is written.
"""

import html
import os
from pathlib import Path

from tests.workbench_e2e.scenarios import SCENARIOS, SHOT_WIDTHS


def target():
    root = os.environ.get("CP_E2E_SHOTS")
    if not root:
        return None
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def wanted(width):
    return target() is not None and width in SHOT_WIDTHS


def capture(page, name, width):
    folder = target()
    if folder is None or width not in SHOT_WIDTHS:
        return None
    path = folder / f"{name}@{width}.png"
    page.screenshot(path=str(path), full_page=False)
    return path


def design_for(name, width, design_dir):
    """The design PNG drawn for this scenario at this width, if any."""
    screen = next((s[4] for s in SCENARIOS if s[0] == name), None)
    if not screen or design_dir is None:
        return None
    candidates = [f"{screen}.png"] if width == 1440 else []
    stem = screen.split("-", 1)[0]
    candidates += [
        f"r-{screen}-{width}.png",
        f"r2-{screen}-{width}.png",
        f"r1-{screen}-{width}.png",
        f"r-{stem}-chat-{width}.png",
    ]
    for candidate in candidates:
        if (design_dir / candidate).exists():
            return design_dir / candidate
    return None


def write_compare():
    folder = target()
    if folder is None:
        return None
    design = os.environ.get("CP_E2E_DESIGN_SHOTS")
    design_dir = Path(design) if design else None
    rows = []
    for name, hash_, _kind, _setup, screen in SCENARIOS:
        for width in SHOT_WIDTHS:
            shot = folder / f"{name}@{width}.png"
            if not shot.exists():
                continue
            drawn = design_for(name, width, design_dir)
            left = (
                f'<img src="{html.escape(drawn.as_uri())}" alt="design {html.escape(drawn.name)}">'
                if drawn
                else '<p class="none">No drawn screen at this width</p>'
            )
            rows.append(
                f"<section><h2>{html.escape(name)} · {width} px <small>{html.escape(hash_)}"
                f"{' · design ' + html.escape(screen) if screen else ''}</small></h2>"
                f'<div class="pair"><figure>{left}<figcaption>Design</figcaption></figure>'
                f'<figure><img src="{html.escape(shot.name)}" alt="app {html.escape(shot.name)}">'
                f"<figcaption>App</figcaption></figure></div></section>"
            )
    page = (
        '<!doctype html><meta charset="utf-8"><title>Workbench: design vs app</title>'
        "<style>body{font:14px system-ui;margin:24px;background:#F3F3EF;color:#17181C}"
        "section{margin:0 0 40px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}"
        "figure{margin:0;background:#FEFEFC;padding:8px;border:1px solid #D5D5CC}"
        "img{max-width:100%;display:block}small{color:#5A5B60;font-weight:400}"
        ".none{color:#5A5B60;padding:40px;text-align:center}</style>"
        "<h1>Workbench: design vs app</h1>" + "".join(rows)
    )
    out = folder / "compare.html"
    out.write_text(page, encoding="utf-8")
    return out
