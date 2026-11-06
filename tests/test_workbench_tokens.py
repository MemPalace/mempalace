"""Shared Workbench styling contract, before component wiring lands."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"


def stylesheet():
    path = STATIC / "workbench.css"
    assert path.is_file(), "Workbench stylesheet missing"
    return path.read_text()


def test_r01_stylesheet_follows_chat():
    html = (STATIC / "index.html").read_text()
    assert "/app/workbench.css" in html
    assert html.index("/app/chat.css") < html.index("/app/workbench.css")
    assert html.index("/app/workbench.css") < html.index("/app/app.js")


def test_r02_light_scheme():
    html = (STATIC / "index.html").read_text()
    assert 'name="color-scheme" content="light"' in html
    assert "color-scheme: light" in stylesheet()


def test_r03_design_tokens_and_legacy_aliases():
    css = stylesheet()
    tokens = dict(re.findall(r"--([\w-]+):\s*([^;]+);", css))
    expected = {
        "bg": "#F3F3EF",
        "pane": "#FAFAF8",
        "surface": "#FEFEFC",
        "well": "#EDEDE7",
        "line": "#DEDED6",
        "line-strong": "#7E7E76",
        "text": "#17181C",
        "text-2": "#4A4C55",
        "text-3": "#62646C",
        "accent": "#2E43C0",
        "accent-hover": "#24369E",
        "accent-weak": "#ECEEFB",
        "wait": "#7A4300",
        "wait-bg": "#FFF1D6",
        "wait-line": "#D99A3D",
        "block": "#A3231A",
        "block-bg": "#FCE9E7",
        "block-line": "#D9675C",
        "ok": "#1B6136",
        "ok-bg": "#E4F3E9",
        "agent": "#5A3EA8",
        "agent-bg": "#EFEAFA",
        "r": "6px",
    }
    for name, value in expected.items():
        assert name in tokens
        assert tokens[name] == value
    legacy = (STATIC / "app.css").read_text().split("::selection", 1)[0]
    for name in re.findall(r"--([\w-]+):", legacy):
        if name != "gap":
            assert name in tokens, f"Legacy token {name} needs a light override"
    assert tokens["ink"] == "var(--text)"
    assert tokens["accent-ink"] == "var(--surface)"
    assert '"Ubuntu Sans", "Inter", "Segoe UI"' in tokens["font"]
    assert tokens["mono"].startswith('"Geist Mono"')
    assert "body { font: 14px/20px var(--font)" in css


def test_r04_shell_focus_and_responsive_contract():
    css = stylesheet()
    for selector in (".shell", ".shell.two", ".rail", ".center", ".context"):
        assert selector + " {" in css
    assert "236px minmax(0, 1fr) 396px" in css
    assert "188px minmax(0, 1fr) 304px" in css
    assert "220px minmax(0, 1fr) 340px" in css
    for boundary in ("1399px", "1100px", "1099px", "1024px", "1023px", "700px", "699px"):
        assert boundary in css
    assert ":focus-visible, .is-focus" in css
    assert "outline: 3px solid var(--focus, var(--accent))" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "animation: none !important; transition: none !important" in css
    assert "[hidden] { display: none !important; }" in css


def test_g01_existing_entrypoint_and_styles_survive():
    html = (STATIC / "index.html").read_text()
    for name in (
        "app",
        "screens",
        "map",
        "mix",
        "ui",
        "decide",
        "rules",
        "console",
        "chat",
    ):
        assert f'href="/app/{name}.css"' in html
    assert '<script type="module" src="/app/app.js">' in html
    assert '<div id="root">' in html


def test_g02_bundled_mono_font_exists():
    assert (STATIC / "fonts" / "geist-mono-latin.woff2").is_file()
