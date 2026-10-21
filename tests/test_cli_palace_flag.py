"""Global ``--palace`` must survive subcommand parsing.

``init`` already uses ``argparse.SUPPRESS`` so a parent ``--palace`` is not
replaced by the subcommand default. ``serve`` had ``default=None``, which
argparse writes onto the shared dest even when the flag is only given
globally (``mempalace --palace X serve`` served the default palace).
"""

from __future__ import annotations

import argparse
import os

import pytest

from mempalace.cli import main


def _iter_subparsers(parser: argparse.ArgumentParser, prefix: str = ""):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                yield path, sub
                yield from _iter_subparsers(sub, path)


def _palace_actions(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if "--palace" in getattr(action, "option_strings", ()):
            yield action


def _capture_parser(monkeypatch) -> dict:
    captured: dict = {}
    original = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        captured["parser"] = self
        captured["serve_global"] = original(self, ["--palace", "/tmp/palace-global", "serve"])
        captured["serve_local"] = original(self, ["serve", "--palace", "/tmp/palace-local"])
        captured["serve_both"] = original(
            self, ["--palace", "/tmp/palace-global", "serve", "--palace", "/tmp/palace-local"]
        )
        captured["search_global"] = original(
            self, ["--palace", "/tmp/palace-global", "search", "q"]
        )
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
    saved_pythonpath = os.environ.get("PYTHONPATH")
    try:
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code in (0, None)
    finally:
        if saved_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = saved_pythonpath
    return captured


def test_serve_keeps_global_palace_and_still_accepts_local(monkeypatch):
    captured = _capture_parser(monkeypatch)
    assert captured["serve_global"].palace == "/tmp/palace-global"
    assert captured["serve_local"].palace == "/tmp/palace-local"
    assert captured["serve_both"].palace == "/tmp/palace-local"
    assert captured["search_global"].palace == "/tmp/palace-global"


def test_no_subcommand_palace_default_clobbers_global(monkeypatch):
    captured = _capture_parser(monkeypatch)
    offenders = []
    for path, sub in _iter_subparsers(captured["parser"]):
        for action in _palace_actions(sub):
            if action.default is None:
                offenders.append(path)
            assert action.default is argparse.SUPPRESS, (
                f"{path} --palace default {action.default!r} overwrites the global flag; "
                "use argparse.SUPPRESS"
            )
    assert offenders == []
