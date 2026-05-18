import os
import json
import tempfile

import pytest
from mempalace.config import (
    MempalaceConfig,
    normalize_wing_name,
    sanitize_iso_date,
    sanitize_iso_temporal,
    sanitize_kg_value,
    sanitize_name,
)


def test_default_config():
    cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
    assert "palace" in cfg.palace_path
    assert cfg.collection_name == "mempalace_drawers"


def test_config_from_file():
    tmpdir = tempfile.mkdtemp()
    with open(os.path.join(tmpdir, "config.json"), "w") as f:
        json.dump({"palace_path": "/custom/palace"}, f)
    cfg = MempalaceConfig(config_dir=tmpdir)
    assert cfg.palace_path == "/custom/palace"


def test_embedding_device_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("MEMPALACE_EMBEDDING_DEVICE", raising=False)
    cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
    assert cfg.embedding_device == "auto"


def test_embedding_device_from_config_is_normalized(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPALACE_EMBEDDING_DEVICE", raising=False)
    with open(tmp_path / "config.json", "w") as f:
        json.dump({"embedding_device": "  CUDA  "}, f)

    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.embedding_device == "cuda"


def test_embedding_device_env_overrides_config(tmp_path, monkeypatch):
    with open(tmp_path / "config.json", "w") as f:
        json.dump({"embedding_device": "cpu"}, f)
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "  CoreML  ")

    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.embedding_device == "coreml"


def test_env_override():
    raw = "/env/palace"
    os.environ["MEMPALACE_PALACE_PATH"] = raw
    try:
        cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
        # palace_path normalizes with abspath + expanduser to match the
        # --palace CLI code path. On Unix that's a no-op for "/env/palace";
        # on Windows abspath prepends the current drive letter.
        assert cfg.palace_path == os.path.abspath(os.path.expanduser(raw))
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]


def test_env_path_expanduser():
    # Tilde must be expanded to match the --palace CLI code path. We don't
    # assert "~" is absent from the final string because Windows 8.3 short
    # paths (e.g. C:\Users\RUNNER~1\...) legitimately contain tildes — the
    # equality check is authoritative.
    raw = os.path.join("~", "mempalace-test")
    os.environ["MEMPALACE_PALACE_PATH"] = raw
    try:
        cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
        assert cfg.palace_path == os.path.abspath(os.path.expanduser(raw))
        assert cfg.palace_path.endswith("mempalace-test")
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]


def test_env_path_abspath_collapses_traversal():
    # Build a raw path with a .. segment using the platform separator so
    # the assertion is portable (Windows uses \, POSIX uses /).
    raw = os.path.join(tempfile.gettempdir(), "palace", "..", "mempalace-test")
    expected = os.path.abspath(os.path.expanduser(raw))
    os.environ["MEMPALACE_PALACE_PATH"] = raw
    try:
        cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
        # .. segments must be collapsed, not preserved literally.
        assert ".." not in cfg.palace_path
        assert cfg.palace_path == expected
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]


def test_env_path_legacy_alias_normalized():
    # Legacy MEMPAL_PALACE_PATH gets the same normalization treatment as
    # MEMPALACE_PALACE_PATH. We don't assert "~" is absent from the final
    # string because Windows 8.3 short paths (e.g. C:\Users\RUNNER~1\...)
    # legitimately contain tildes — the equality check below is authoritative.
    os.environ.pop("MEMPALACE_PALACE_PATH", None)
    raw = os.path.join("~", "legacy-alias", "..", "mempalace-test")
    os.environ["MEMPAL_PALACE_PATH"] = raw
    try:
        cfg = MempalaceConfig(config_dir=tempfile.mkdtemp())
        assert ".." not in cfg.palace_path
        assert cfg.palace_path == os.path.abspath(os.path.expanduser(raw))
    finally:
        del os.environ["MEMPAL_PALACE_PATH"]


def test_init():
    tmpdir = tempfile.mkdtemp()
    cfg = MempalaceConfig(config_dir=tmpdir)
    cfg.init()
    assert os.path.exists(os.path.join(tmpdir, "config.json"))


# --- normalize_wing_name ---


def test_normalize_wing_name_hyphen():
    assert normalize_wing_name("mempal-private") == "mempal_private"


def test_normalize_wing_name_space():
    assert normalize_wing_name("My Project") == "my_project"


def test_normalize_wing_name_already_clean():
    assert normalize_wing_name("memorymark") == "memorymark"


def test_normalize_wing_name_mixed():
    assert normalize_wing_name("My-Cool App") == "my_cool_app"


# --- sanitize_name ---


def test_sanitize_name_ascii():
    assert sanitize_name("hello") == "hello"


def test_sanitize_name_latvian():
    assert sanitize_name("Jānis") == "Jānis"


def test_sanitize_name_cjk():
    assert sanitize_name("太郎") == "太郎"


def test_sanitize_name_cyrillic():
    assert sanitize_name("Алексей") == "Алексей"


def test_sanitize_name_rejects_leading_underscore():
    with pytest.raises(ValueError):
        sanitize_name("_foo")


def test_sanitize_name_rejects_path_traversal():
    with pytest.raises(ValueError):
        sanitize_name("../etc/passwd")


def test_sanitize_name_rejects_empty():
    with pytest.raises(ValueError):
        sanitize_name("")


# --- sanitize_kg_value ---


def test_kg_value_accepts_commas():
    assert sanitize_kg_value("Alice, Bob, and Carol") == "Alice, Bob, and Carol"


def test_kg_value_accepts_colons():
    assert sanitize_kg_value("role: engineer") == "role: engineer"


def test_kg_value_accepts_parentheses():
    assert sanitize_kg_value("Python (programming)") == "Python (programming)"


def test_kg_value_accepts_slashes():
    assert sanitize_kg_value("owner/repo") == "owner/repo"


def test_kg_value_accepts_hash():
    assert sanitize_kg_value("issue #123") == "issue #123"


def test_kg_value_accepts_unicode():
    assert sanitize_kg_value("Jānis Bērziņš") == "Jānis Bērziņš"


def test_kg_value_strips_whitespace():
    assert sanitize_kg_value("  hello  ") == "hello"


def test_kg_value_rejects_empty():
    with pytest.raises(ValueError):
        sanitize_kg_value("")


def test_kg_value_rejects_whitespace_only():
    with pytest.raises(ValueError):
        sanitize_kg_value("   ")


def test_kg_value_rejects_null_bytes():
    with pytest.raises(ValueError):
        sanitize_kg_value("hello\x00world")


def test_kg_value_rejects_over_length():
    with pytest.raises(ValueError):
        sanitize_kg_value("a" * 129)


# --- sanitize_iso_date ---


def test_iso_date_rejects_year_only():
    # Partial dates re-introduce silent empty result sets via lexicographic
    # TEXT comparison in KG queries (e.g. "2026-01-01" <= "2026" is False).
    with pytest.raises(ValueError):
        sanitize_iso_date("2026")


def test_iso_date_rejects_year_month():
    with pytest.raises(ValueError):
        sanitize_iso_date("2026-03")


def test_iso_date_accepts_full_date():
    assert sanitize_iso_date("2026-03-15") == "2026-03-15"


def test_iso_date_passes_through_none():
    assert sanitize_iso_date(None) is None


def test_iso_date_passes_through_empty_string():
    assert sanitize_iso_date("") == ""


def test_iso_date_strips_whitespace():
    assert sanitize_iso_date("  2026-03-15  ") == "2026-03-15"


def test_iso_date_rejects_natural_language():
    with pytest.raises(ValueError):
        sanitize_iso_date("March 2026")


def test_iso_date_rejects_abbreviated_month():
    with pytest.raises(ValueError):
        sanitize_iso_date("Jan 2025")


def test_iso_date_rejects_us_format():
    with pytest.raises(ValueError):
        sanitize_iso_date("03/15/2026")


def test_iso_date_rejects_invalid_month():
    with pytest.raises(ValueError):
        sanitize_iso_date("2026-13")


def test_iso_date_rejects_invalid_day():
    with pytest.raises(ValueError):
        sanitize_iso_date("2026-02-32")


def test_iso_date_rejects_non_string():
    with pytest.raises(ValueError):
        sanitize_iso_date(20260315)


def test_iso_date_error_names_field():
    with pytest.raises(ValueError, match="valid_from"):
        sanitize_iso_date("yesterday", "valid_from")


def test_iso_temporal_accepts_full_date():
    assert sanitize_iso_temporal("2026-05-06") == "2026-05-06"


def test_iso_temporal_accepts_canonical_utc_datetime():
    assert sanitize_iso_temporal("2026-05-06T14:23:00Z") == "2026-05-06T14:23:00Z"


def test_iso_temporal_strips_datetime_whitespace():
    assert sanitize_iso_temporal(" 2026-05-06T14:23:00Z ") == "2026-05-06T14:23:00Z"


def test_iso_date_backward_compatible_wrapper_accepts_datetime():
    assert sanitize_iso_date("2026-05-06T14:23:00Z") == "2026-05-06T14:23:00Z"


def test_iso_temporal_rejects_datetime_without_seconds():
    with pytest.raises(ValueError):
        sanitize_iso_temporal("2026-05-06T14:23")


def test_iso_temporal_rejects_naive_datetime():
    with pytest.raises(ValueError):
        sanitize_iso_temporal("2026-05-06T14:23:00")


def test_iso_temporal_rejects_fractional_seconds():
    with pytest.raises(ValueError):
        sanitize_iso_temporal("2026-05-06T14:23:00.123Z")


def test_iso_temporal_rejects_timezone_offset():
    with pytest.raises(ValueError):
        sanitize_iso_temporal("2026-05-06T14:23:00+02:00")


def test_iso_temporal_rejects_space_separator():
    with pytest.raises(ValueError):
        sanitize_iso_temporal("2026-05-06 14:23:00")


def test_iso_temporal_rejects_invalid_datetime_hour():
    with pytest.raises(ValueError):
        sanitize_iso_temporal("2026-05-06T24:00:00Z")


def test_iso_temporal_rejects_invalid_calendar_date():
    with pytest.raises(ValueError):
        sanitize_iso_temporal("2026-02-31")


def test_iso_temporal_error_names_field():
    with pytest.raises(ValueError, match="as_of"):
        sanitize_iso_temporal("2026-05-06T14:23", "as_of")


def test_iso_temporal_normalizes_plus_zero_offset_to_z():
    assert sanitize_iso_temporal("2026-05-06T14:23:00+00:00") == "2026-05-06T14:23:00Z"


# ── Chunk-config validation ────────────────────────────────────────────
# Backs the validated chunk_* properties added in #1024. Every property
# resolves through ``_validated_chunk_config`` which (a) coerces to int
# (or falls back to the documented default), (b) enforces the invariants
# ``chunk_text()`` needs (chunk_size >= 1, chunk_overlap < chunk_size,
# min_chunk_size <= chunk_size). A bad config.json must NEVER hang
# ingest — repair, don't raise.


def _write_config(tmp_path, **values):
    """Helper: drop a config.json with the given keys into tmp_path."""
    with open(tmp_path / "config.json", "w") as f:
        json.dump(values, f)
    return MempalaceConfig(config_dir=str(tmp_path))


def test_chunk_config_defaults_when_unset(tmp_path):
    """No config.json → documented defaults."""
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.chunk_size == 800
    assert cfg.chunk_overlap == 100
    assert cfg.min_chunk_size == 50


def test_chunk_config_user_overrides_honored(tmp_path):
    """Valid file values pass through unchanged."""
    cfg = _write_config(tmp_path, chunk_size=1200, chunk_overlap=200, min_chunk_size=80)
    assert cfg.chunk_size == 1200
    assert cfg.chunk_overlap == 200
    assert cfg.min_chunk_size == 80


def test_chunk_config_string_coerced_to_int(tmp_path):
    """Hand-edited config can drop quotes around numbers — accept ``"1500"``."""
    cfg = _write_config(tmp_path, chunk_size="1500", chunk_overlap="50")
    assert cfg.chunk_size == 1500
    assert cfg.chunk_overlap == 50


def test_chunk_config_garbage_string_falls_back_to_default(tmp_path):
    cfg = _write_config(tmp_path, chunk_size="not a number")
    assert cfg.chunk_size == 800  # default, not a crash


def test_chunk_config_bool_falls_back_to_default(tmp_path):
    """``bool`` is a subclass of ``int`` in Python — a JSON ``true``
    would otherwise coerce to 1 and quietly break ingest. Treat as bad
    input."""
    cfg = _write_config(tmp_path, chunk_size=True)
    assert cfg.chunk_size == 800


def test_chunk_config_negative_falls_back(tmp_path):
    """Negative chunk_size/min_chunk_size violates ``minimum`` and reverts."""
    cfg = _write_config(tmp_path, chunk_size=-100, min_chunk_size=-5)
    assert cfg.chunk_size == 800
    assert cfg.min_chunk_size == 50


def test_chunk_config_zero_chunk_size_falls_back(tmp_path):
    """``chunk_size=0`` would loop forever — must revert to default."""
    cfg = _write_config(tmp_path, chunk_size=0)
    assert cfg.chunk_size == 800


def test_chunk_config_overlap_at_or_above_size_repaired(tmp_path):
    """``chunk_overlap >= chunk_size`` is the hang condition; repair to
    the documented default when the default fits, otherwise to
    ``chunk_size - 1``."""
    cfg = _write_config(tmp_path, chunk_size=900, chunk_overlap=900)
    assert cfg.chunk_size == 900
    # 100 (default) fits inside 900 → use the default.
    assert cfg.chunk_overlap == 100
    assert cfg.chunk_overlap < cfg.chunk_size


def test_chunk_config_overlap_repair_when_default_doesnt_fit(tmp_path):
    """Tiny chunk_size where the default overlap (100) wouldn't fit:
    repair to ``chunk_size - 1`` instead."""
    cfg = _write_config(tmp_path, chunk_size=50, chunk_overlap=100)
    assert cfg.chunk_size == 50
    assert cfg.chunk_overlap == 49  # max(0, chunk_size - 1)
    assert cfg.chunk_overlap < cfg.chunk_size


def test_chunk_config_min_chunk_size_above_size_repaired(tmp_path):
    """``min_chunk_size > chunk_size`` would silently produce 0 drawers
    on every ingest — repair to default if it fits, else clamp to
    chunk_size."""
    cfg = _write_config(tmp_path, chunk_size=1000, min_chunk_size=2000)
    assert cfg.min_chunk_size == 50  # default fits inside 1000

    cfg2 = _write_config(tmp_path, chunk_size=20, min_chunk_size=200)
    assert cfg2.min_chunk_size == 20  # default (50) > chunk_size, clamp


# ── min_chunk_size_explicit (convo-path validated accessor) ────────────
# Backs the #1024-review fix: convo_miner must distinguish "user tuned
# min_chunk_size" from "untuned" WITHOUT reaching into raw _file_config.
# Untuned/unusable → None (convo keeps its 30 floor). Usable → validated
# int. A bad key must never reach the convo length-gate / chunk_exchanges
# as a non-int and crash ingest.


def test_min_chunk_size_explicit_none_when_unset(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.min_chunk_size_explicit is None


def test_min_chunk_size_explicit_none_when_json_null(tmp_path):
    """Explicit JSON ``null`` is treated as untuned (preserves the prior
    ``_file_config.get(...) is None`` sentinel semantics)."""
    cfg = _write_config(tmp_path, min_chunk_size=None)
    assert cfg.min_chunk_size_explicit is None


def test_min_chunk_size_explicit_returns_validated_value(tmp_path):
    cfg = _write_config(tmp_path, min_chunk_size=80)
    assert cfg.min_chunk_size_explicit == 80


def test_min_chunk_size_explicit_coerces_numeric_string(tmp_path):
    cfg = _write_config(tmp_path, min_chunk_size="42")
    assert cfg.min_chunk_size_explicit == 42


@pytest.mark.parametrize("bad", ["abc", -5, True, "", "  "])
def test_min_chunk_size_explicit_none_on_unusable_value(tmp_path, bad):
    """Garbage / negative / bool / blank → None, NOT a crash and NOT the
    miner.py default. convo_miner then falls back to its own 30 floor.
    This is the exact class of value that used to TypeError the convo
    length-gate or ValueError out of chunk_exchanges."""
    cfg = _write_config(tmp_path, min_chunk_size=bad)
    assert cfg.min_chunk_size_explicit is None


def test_min_chunk_size_explicit_none_when_above_chunk_size(tmp_path):
    """min_chunk_size > chunk_size would zero out ingest — treat as
    unusable so convo falls back to its floor instead."""
    cfg = _write_config(tmp_path, chunk_size=100, min_chunk_size=500)
    assert cfg.min_chunk_size_explicit is None


def test_convo_min_chunk_fallback_is_always_safe_int(tmp_path):
    """Regression for #1024 review: the convo_miner fallback expression
    must yield a usable int for ANY config — never a str/bool/negative
    that would crash the length gate or chunk_exchanges."""
    from mempalace.convo_miner import MIN_CHUNK_SIZE

    for bad in ("not-a-number", -10, True, {}, []):
        cfg = _write_config(tmp_path, min_chunk_size=bad)
        explicit = cfg.min_chunk_size_explicit
        effective = explicit if explicit is not None else MIN_CHUNK_SIZE
        assert isinstance(effective, int) and not isinstance(effective, bool)
        assert effective == MIN_CHUNK_SIZE  # untuned floor, no crash

    cfg = _write_config(tmp_path, min_chunk_size=15)
    explicit = cfg.min_chunk_size_explicit
    assert (explicit if explicit is not None else MIN_CHUNK_SIZE) == 15


def test_min_chunk_size_explicit_handles_json_infinity(tmp_path):
    """JSON ``Infinity`` round-trips to float('inf'); ``int(inf)`` raises
    OverflowError. That is still garbage config, not a crash — must fall
    back to None (untuned), same as any other unusable value."""
    cfg = _write_config(tmp_path, min_chunk_size=float("inf"))
    assert cfg.min_chunk_size_explicit is None
    # chunk_size path coerces the same value → documented default, no crash.
    cfg2 = _write_config(tmp_path, chunk_size=float("inf"))
    assert cfg2.chunk_size == 800


def test_chunk_text_rejects_non_positive_chunk_size():
    """Direct callers (tests, library users) that pass ``chunk_size <= 0``
    must hit a clear ValueError, not loop forever."""
    from mempalace.miner import chunk_text

    with pytest.raises(ValueError, match="chunk_size"):
        chunk_text("some content", "src.txt", chunk_size=0)
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_text("some content", "src.txt", chunk_size=-1)


def test_chunk_text_rejects_overlap_at_or_above_size():
    from mempalace.miner import chunk_text

    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text("some content", "src.txt", chunk_size=100, chunk_overlap=100)
    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text("some content", "src.txt", chunk_size=100, chunk_overlap=200)


def test_chunk_text_rejects_negative_overlap():
    from mempalace.miner import chunk_text

    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text("some content", "src.txt", chunk_overlap=-1)


def test_miner_constants_alias_config_defaults():
    """Single source of truth: the legacy ``CHUNK_SIZE`` / ``CHUNK_OVERLAP``
    / ``MIN_CHUNK_SIZE`` re-exports in ``mempalace.miner`` must equal the
    canonical ``DEFAULT_CHUNK_*`` constants in ``mempalace.config``.
    Pinned by this test so a future drift would surface as a unit failure.
    """
    from mempalace.miner import CHUNK_SIZE, CHUNK_OVERLAP, MIN_CHUNK_SIZE
    from mempalace.config import (
        DEFAULT_CHUNK_SIZE,
        DEFAULT_CHUNK_OVERLAP,
        DEFAULT_MIN_CHUNK_SIZE,
    )

    assert CHUNK_SIZE == DEFAULT_CHUNK_SIZE == 800
    assert CHUNK_OVERLAP == DEFAULT_CHUNK_OVERLAP == 100
    assert MIN_CHUNK_SIZE == DEFAULT_MIN_CHUNK_SIZE == 50
