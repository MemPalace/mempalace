"""
test_output.py — Unit tests for mempalace.output helpers.
"""

import io
import sys

from mempalace.output import safe_separator


class TestSafeSeparator:
    def test_utf8_returns_unicode_char(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="utf-8"))
        assert safe_separator(4) == "────"

    def test_cp1252_returns_hyphens(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="cp1252"))
        assert safe_separator(4) == "----"

    def test_default_width_is_56(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="utf-8"))
        assert len(safe_separator()) == 56

    def test_custom_width(self, monkeypatch):
        buf = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="utf-8"))
        assert len(safe_separator(10)) == 10

    def test_none_encoding_defaults_to_utf8(self, monkeypatch):
        class _NoEncoding:
            encoding = None
        monkeypatch.setattr(sys, "stdout", _NoEncoding())
        assert safe_separator(4) == "────"

    def test_unknown_encoding_returns_hyphens(self, monkeypatch):
        class _BadEncoding:
            encoding = "not-a-real-encoding"
        monkeypatch.setattr(sys, "stdout", _BadEncoding())
        assert safe_separator(4) == "----"
