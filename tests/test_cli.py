"""Tests for mempalace.cli — the main CLI dispatcher."""

import argparse
import shlex
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mempalace.cli import (
    cmd_compress,
    cmd_hook,
    cmd_init,
    cmd_instructions,
    cmd_mine,
    cmd_repair,
    cmd_search,
    cmd_split,
    cmd_status,
    cmd_wakeup,
    main,
)


# ── cmd_status ─────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_status_default_palace(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None)
    mock_miner = MagicMock()
    with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
        cmd_status(args)
        mock_miner.status.assert_called_once_with(palace_path="/fake/palace")


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_status_custom_palace(mock_config_cls):
    args = argparse.Namespace(palace="~/my_palace")
    mock_miner = MagicMock()
    with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
        cmd_status(args)
        import os

        expected = os.path.expanduser("~/my_palace")
        mock_miner.status.assert_called_once_with(palace_path=expected)


# ── cmd_search ─────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_search_calls_search(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        palace=None, query="test query", wing="mywing", room="myroom", results=3
    )
    with patch("mempalace.searcher.search") as mock_search:
        cmd_search(args)
        mock_search.assert_called_once_with(
            query="test query",
            palace_path="/fake/palace",
            wing="mywing",
            room="myroom",
            n_results=3,
        )


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_search_error_exits(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, query="q", wing=None, room=None, results=5)
    from mempalace.searcher import SearchError

    with patch("mempalace.searcher.search", side_effect=SearchError("fail")):
        with pytest.raises(SystemExit) as exc_info:
            cmd_search(args)
        assert exc_info.value.code == 1


# ── cmd_instructions ───────────────────────────────────────────────────


def test_cmd_instructions_calls_run_instructions():
    args = argparse.Namespace(name="help")
    with patch("mempalace.instructions_cli.run_instructions") as mock_run:
        cmd_instructions(args)
        mock_run.assert_called_once_with(name="help")


# ── cmd_hook ───────────────────────────────────────────────────────────


def test_cmd_hook_calls_run_hook():
    args = argparse.Namespace(hook="session-start", harness="claude-code")
    with patch("mempalace.hooks_cli.run_hook") as mock_run:
        cmd_hook(args)
        mock_run.assert_called_once_with(hook_name="session-start", harness="claude-code")


# ── cmd_init ───────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_init_no_entities(mock_config_cls, tmp_path):
    args = argparse.Namespace(dir=str(tmp_path), yes=True)
    with (
        patch("mempalace.entity_detector.scan_for_detection", return_value=[]),
        patch("mempalace.room_detector_local.detect_rooms_local") as mock_rooms,
        patch("mempalace.cli._maybe_run_mine_after_init"),
    ):
        cmd_init(args)
        mock_rooms.assert_called_once_with(project_dir=str(tmp_path), yes=True)
        mock_config_cls.return_value.init.assert_called_once()


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_init_with_entities(mock_config_cls, tmp_path):
    fake_files = [tmp_path / "a.txt"]
    detected = {"people": [{"name": "Alice"}], "projects": [], "uncertain": []}
    confirmed = {"people": ["Alice"], "projects": []}
    args = argparse.Namespace(dir=str(tmp_path), yes=True)
    with (
        patch("mempalace.entity_detector.scan_for_detection", return_value=fake_files),
        patch("mempalace.entity_detector.detect_entities", return_value=detected),
        patch("mempalace.entity_detector.confirm_entities", return_value=confirmed),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        # Pass 0 (corpus_origin) needs real file IO; this test mocks
        # builtins.open globally for the entities.json write, which would
        # break Pass 0's file-reading path. Patch Pass 0 out — a separate
        # suite (tests/test_corpus_origin_integration.py) covers it directly.
        patch("mempalace.cli._run_pass_zero", return_value=None),
        patch("builtins.open", MagicMock()),
        patch("mempalace.cli._maybe_run_mine_after_init"),
    ):
        cmd_init(args)


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_init_normalizes_wing_name_for_topics_registry(mock_config_cls, tmp_path):
    """Regression for #1194: hyphenated dir names must be normalized to the
    same slug ``mempalace.yaml`` uses, otherwise ``topics_by_wing`` keys
    miss the miner's lookup at mine time and tunnels are silently dropped.
    """
    project = tmp_path / "my-cool-app"
    project.mkdir()
    fake_files = [project / "a.txt"]
    detected = {
        "people": [{"name": "Alice"}],
        "projects": [],
        "topics": [{"name": "Bun"}],
        "uncertain": [],
    }
    confirmed = {"people": ["Alice"], "projects": [], "topics": ["Bun"]}
    args = argparse.Namespace(dir=str(project), yes=True)
    with (
        patch("mempalace.entity_detector.scan_for_detection", return_value=fake_files),
        patch("mempalace.entity_detector.detect_entities", return_value=detected),
        patch("mempalace.entity_detector.confirm_entities", return_value=confirmed),
        patch("mempalace.miner.add_to_known_entities") as mock_register,
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.open", MagicMock()),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        # Pass-zero corpus-origin detection runs unconditionally inside
        # cmd_init now (#1221 / #1223). It accesses MempalaceConfig fields
        # that don't survive MagicMock stringification, so stub it out —
        # this test only cares about the wing-slug write to the registry.
        patch("mempalace.cli._run_pass_zero", return_value=None),
    ):
        mock_register.return_value = "/tmp/known_entities.json"
        cmd_init(args)
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["wing"] == "my_cool_app"


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_init_with_entities_zero_total(mock_config_cls, tmp_path, capsys):
    """When entities detected but total is 0, prints 'No entities' message."""
    fake_files = [tmp_path / "a.txt"]
    detected = {"people": [], "projects": [], "uncertain": []}
    args = argparse.Namespace(dir=str(tmp_path), yes=False)
    with (
        patch("mempalace.entity_detector.scan_for_detection", return_value=fake_files),
        patch("mempalace.entity_detector.detect_entities", return_value=detected),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("mempalace.cli._maybe_run_mine_after_init"),
    ):
        cmd_init(args)
    out = capsys.readouterr().out
    assert "No entities detected" in out


# ── _maybe_run_mine_after_init (init → mine prompt, #1181) ─────────────


def _init_args(tmp_path, *, yes=False, auto_mine=False):
    return argparse.Namespace(dir=str(tmp_path), yes=yes, auto_mine=auto_mine)


def _fake_cfg(tmp_path):
    cfg = MagicMock()
    cfg.palace_path = str(tmp_path / "palace")
    return cfg


def _fake_scanned(tmp_path, n=3):
    """Build n real Path objects with stat()-able sizes for the scan estimate."""
    paths = []
    for i in range(n):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x" * 1024)  # 1 KB each
        paths.append(p)
    return paths


def test_maybe_run_mine_prompt_accepted_runs_mine(tmp_path):
    """Empty / 'y' / 'yes' on the prompt triggers mine() in-process."""
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=False, auto_mine=False)
    cfg = _fake_cfg(tmp_path)
    scanned = _fake_scanned(tmp_path, n=3)
    with (
        patch("mempalace.miner.mine") as mock_mine,
        patch("mempalace.miner.scan_project", return_value=scanned),
        patch("builtins.input", return_value=""),
    ):
        _maybe_run_mine_after_init(args, cfg)
        mock_mine.assert_called_once_with(
            project_dir=str(tmp_path),
            palace_path=cfg.palace_path,
            files=scanned,
        )


def test_maybe_run_mine_prompt_yes_accepted_runs_mine(tmp_path):
    """Explicit 'y' answer also runs mine()."""
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=False, auto_mine=False)
    cfg = _fake_cfg(tmp_path)
    with (
        patch("mempalace.miner.mine") as mock_mine,
        patch("mempalace.miner.scan_project", return_value=[]),
        patch("builtins.input", return_value="Y"),
    ):
        _maybe_run_mine_after_init(args, cfg)
        mock_mine.assert_called_once()


def test_maybe_run_mine_prompt_declined_prints_hint(tmp_path, capsys):
    """'n' answer skips mine() and prints the resume hint."""
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=False, auto_mine=False)
    cfg = _fake_cfg(tmp_path)
    with (
        patch("mempalace.miner.mine") as mock_mine,
        patch("mempalace.miner.scan_project", return_value=[]),
        patch("builtins.input", return_value="n"),
    ):
        _maybe_run_mine_after_init(args, cfg)
        mock_mine.assert_not_called()
    out = capsys.readouterr().out
    # shlex.quote is a no-op on POSIX-safe paths but wraps Windows paths
    # (which contain backslashes) in single quotes, so the assertion has
    # to mirror what the production code actually emits.
    assert f"mempalace mine {shlex.quote(str(tmp_path))}" in out
    assert "Skipped" in out


def test_maybe_run_mine_yes_alone_still_prompts(tmp_path):
    """`--yes` is scoped to entity auto-accept and MUST still prompt for mine.

    Regression guard for the flag-overload review feedback on #1183: extending
    `--yes` to also auto-mine would silently change behaviour for scripted
    callers and turn a fast command into a minutes-long ChromaDB write.
    """
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=True, auto_mine=False)
    cfg = _fake_cfg(tmp_path)
    with (
        patch("mempalace.miner.mine") as mock_mine,
        patch("mempalace.miner.scan_project", return_value=[]),
        patch("builtins.input", return_value="n") as mock_input,
    ):
        _maybe_run_mine_after_init(args, cfg)
        mock_input.assert_called_once()  # the prompt MUST fire
        mock_mine.assert_not_called()


def test_maybe_run_mine_auto_mine_skips_prompt(tmp_path):
    """`--auto-mine` runs mine() automatically without calling input()."""
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=False, auto_mine=True)
    cfg = _fake_cfg(tmp_path)
    scanned = _fake_scanned(tmp_path, n=2)
    with (
        patch("mempalace.miner.mine") as mock_mine,
        patch("mempalace.miner.scan_project", return_value=scanned),
        patch("builtins.input", side_effect=AssertionError("input() must not be called")),
    ):
        _maybe_run_mine_after_init(args, cfg)
        mock_mine.assert_called_once_with(
            project_dir=str(tmp_path),
            palace_path=cfg.palace_path,
            files=scanned,
        )


def test_maybe_run_mine_yes_and_auto_mine_fully_noninteractive(tmp_path):
    """`--yes --auto-mine` together: never call input(), always mine."""
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=True, auto_mine=True)
    cfg = _fake_cfg(tmp_path)
    with (
        patch("mempalace.miner.mine") as mock_mine,
        patch("mempalace.miner.scan_project", return_value=[]),
        patch("builtins.input", side_effect=AssertionError("input() must not be called")),
    ):
        _maybe_run_mine_after_init(args, cfg)
        mock_mine.assert_called_once()


def test_maybe_run_mine_decline_quotes_path_with_spaces(tmp_path, capsys):
    """The resume hint must shell-quote the project dir so paths with
    spaces / metacharacters produce a copy-paste-safe command."""
    from mempalace.cli import _maybe_run_mine_after_init

    spaced_dir = tmp_path / "my project dir"
    spaced_dir.mkdir()
    args = argparse.Namespace(dir=str(spaced_dir), yes=False, auto_mine=False)
    cfg = _fake_cfg(tmp_path)
    with (
        patch("mempalace.miner.mine"),
        patch("mempalace.miner.scan_project", return_value=[]),
        patch("builtins.input", return_value="n"),
    ):
        _maybe_run_mine_after_init(args, cfg)
    out = capsys.readouterr().out
    # shlex.quote wraps paths with spaces (and Windows backslashes) in
    # single quotes — the assertion must use the same shlex form so the
    # test passes on every platform's tmp_path layout.
    assert f"mempalace mine {shlex.quote(str(spaced_dir))}" in out
    # Bare unquoted form must NOT appear — that's the bug we're guarding.
    assert f"mempalace mine {spaced_dir} " not in out
    assert f"mempalace mine {spaced_dir}`" not in out


def test_maybe_run_mine_eof_on_stdin_treated_as_decline(tmp_path, capsys):
    """Piped / non-interactive stdin (EOFError) declines without crashing."""
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=False, auto_mine=False)
    cfg = _fake_cfg(tmp_path)
    with (
        patch("mempalace.miner.mine") as mock_mine,
        patch("mempalace.miner.scan_project", return_value=[]),
        patch("builtins.input", side_effect=EOFError),
    ):
        _maybe_run_mine_after_init(args, cfg)
        mock_mine.assert_not_called()
    assert "Skipped" in capsys.readouterr().out


def test_maybe_run_mine_failure_surfaces_via_exit(tmp_path, capsys):
    """Mine errors are not swallowed — they exit non-zero with an error line."""
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=False, auto_mine=True)
    cfg = _fake_cfg(tmp_path)
    with (
        patch("mempalace.miner.mine", side_effect=RuntimeError("boom")),
        patch("mempalace.miner.scan_project", return_value=[]),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _maybe_run_mine_after_init(args, cfg)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "boom" in err


def test_maybe_run_mine_estimate_appears_before_prompt(tmp_path, capsys):
    """The file-count + size estimate line MUST render BEFORE the prompt.

    Required by the spec: hitting Enter on a default-Y prompt with no size
    info is a footgun on a real corpus where mine takes minutes. The user
    must see scope before being asked to confirm.
    """
    from mempalace.cli import _maybe_run_mine_after_init

    args = _init_args(tmp_path, yes=False, auto_mine=False)
    cfg = _fake_cfg(tmp_path)
    scanned = _fake_scanned(tmp_path, n=4)  # 4 files * 1 KB each
    captured_when_prompted = {}

    def fake_input(prompt):
        # Snapshot what stdout looked like at the moment the prompt fires.
        captured_when_prompted["stdout"] = capsys.readouterr().out
        return "n"

    with (
        patch("mempalace.miner.mine"),
        patch("mempalace.miner.scan_project", return_value=scanned),
        patch("builtins.input", side_effect=fake_input),
    ):
        _maybe_run_mine_after_init(args, cfg)

    pre_prompt = captured_when_prompted["stdout"]
    assert "4 files" in pre_prompt, f"file count missing from pre-prompt output: {pre_prompt!r}"
    assert "MB" in pre_prompt, f"size estimate missing from pre-prompt output: {pre_prompt!r}"
    assert "would be mined" in pre_prompt


# ── cmd_mine ───────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_mine_projects_mode(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        dir="/src",
        palace=None,
        mode="projects",
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        no_gitignore=False,
        include_ignored=[],
        extract="exchange",
    )
    with patch("mempalace.miner.mine") as mock_mine:
        cmd_mine(args)
        mock_mine.assert_called_once_with(
            project_dir="/src",
            palace_path="/fake/palace",
            wing_override=None,
            agent="mempalace",
            limit=0,
            dry_run=False,
            respect_gitignore=True,
            include_ignored=[],
        )


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_mine_convos_mode(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        dir="/chats",
        palace=None,
        mode="convos",
        wing="mywing",
        agent="me",
        limit=10,
        dry_run=True,
        no_gitignore=False,
        include_ignored=[],
        extract="general",
    )
    with patch("mempalace.convo_miner.mine_convos") as mock_mine:
        cmd_mine(args)
        mock_mine.assert_called_once_with(
            convo_dir="/chats",
            palace_path="/fake/palace",
            wing="mywing",
            agent="me",
            limit=10,
            dry_run=True,
            extract_mode="general",
        )


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_mine_include_ignored_comma_split(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        dir="/src",
        palace=None,
        mode="projects",
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        no_gitignore=False,
        include_ignored=["a.txt,b.txt", "c.txt"],
        extract="exchange",
    )
    with patch("mempalace.miner.mine") as mock_mine:
        cmd_mine(args)
        mock_mine.assert_called_once()
        call_kwargs = mock_mine.call_args[1]
        assert call_kwargs["include_ignored"] == ["a.txt", "b.txt", "c.txt"]


# ── cmd_wakeup ─────────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_wakeup(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, wing=None)
    mock_stack = MagicMock()
    mock_stack.wake_up.return_value = "Hello world context"
    with patch("mempalace.layers.MemoryStack", return_value=mock_stack):
        cmd_wakeup(args)
    out = capsys.readouterr().out
    assert "Hello world context" in out
    assert "tokens" in out


# ── cmd_split ──────────────────────────────────────────────────────────


def test_cmd_split_basic():
    args = argparse.Namespace(dir="/chats", output_dir=None, dry_run=False, min_sessions=2)
    with patch("mempalace.split_mega_files.main") as mock_main:
        cmd_split(args)
        mock_main.assert_called_once()


def test_cmd_split_all_options():
    args = argparse.Namespace(dir="/chats", output_dir="/out", dry_run=True, min_sessions=5)
    with patch("mempalace.split_mega_files.main") as mock_main:
        cmd_split(args)
        mock_main.assert_called_once()
    # sys.argv should be restored
    assert sys.argv[0] != "mempalace split"


# ── main() argparse dispatch ──────────────────────────────────────────


def test_main_no_args_prints_help(capsys):
    with patch("sys.argv", ["mempalace"]):
        main()
    out = capsys.readouterr().out
    assert "MemPalace" in out


def test_main_status_dispatches():
    with (
        patch("sys.argv", ["mempalace", "status"]),
        patch("mempalace.cli.cmd_status") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_search_dispatches():
    with (
        patch("sys.argv", ["mempalace", "search", "my query"]),
        patch("mempalace.cli.cmd_search") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_init_dispatches():
    with (
        patch("sys.argv", ["mempalace", "init", "/some/dir"]),
        patch("mempalace.cli.cmd_init") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_mine_dispatches():
    with (
        patch("sys.argv", ["mempalace", "mine", "/some/dir"]),
        patch("mempalace.cli.cmd_mine") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_wakeup_dispatches():
    with (
        patch("sys.argv", ["mempalace", "wake-up"]),
        patch("mempalace.cli.cmd_wakeup") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_split_dispatches():
    with (
        patch("sys.argv", ["mempalace", "split", "/chats"]),
        patch("mempalace.cli.cmd_split") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_mcp_command_prints_setup_guidance(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mempalace", "mcp"])

    main()

    captured = capsys.readouterr()
    assert "MemPalace MCP quick setup:" in captured.out
    assert "claude mcp add mempalace -- mempalace-mcp" in captured.out
    assert "\nOptional custom palace:\n" in captured.out
    assert "mempalace-mcp --palace /path/to/palace" in captured.out
    assert "[--palace /path/to/palace]" not in captured.out
    assert captured.err == ""


def test_mcp_command_uses_custom_palace_path_when_provided(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mempalace", "--palace", "~/tmp/my palace", "mcp"])

    main()

    captured = capsys.readouterr()
    expanded = str(Path("~/tmp/my palace").expanduser())

    assert "mempalace-mcp --palace" in captured.out
    assert expanded in captured.out
    assert "Optional custom palace:" not in captured.out
    assert "[--palace /path/to/palace]" not in captured.out
    assert captured.err == ""


def test_main_hook_no_subcommand_prints_help(capsys):
    with patch("sys.argv", ["mempalace", "hook"]):
        main()
    out = capsys.readouterr().out
    assert "hook" in out.lower() or "run" in out.lower()


def test_main_hook_run_dispatches():
    with (
        patch(
            "sys.argv",
            ["mempalace", "hook", "run", "--hook", "session-start", "--harness", "claude-code"],
        ),
        patch("mempalace.cli.cmd_hook") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_instructions_no_subcommand_prints_help(capsys):
    with patch("sys.argv", ["mempalace", "instructions"]):
        main()
    out = capsys.readouterr().out
    assert "instructions" in out.lower() or "init" in out.lower()


def test_main_instructions_dispatches():
    with (
        patch("sys.argv", ["mempalace", "instructions", "help"]),
        patch("mempalace.cli.cmd_instructions") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_repair_dispatches():
    with (
        patch("sys.argv", ["mempalace", "repair"]),
        patch("mempalace.cli.cmd_repair") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


def test_main_compress_dispatches():
    with (
        patch("sys.argv", ["mempalace", "compress"]),
        patch("mempalace.cli.cmd_compress") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()


# ── cmd_repair ─────────────────────────────────────────────────────────


def _mock_backend_for(col=None, new_col=None):
    """Build a mock ChromaBackend whose get_collection/create_collection return *col* / *new_col*."""
    mock_backend = MagicMock()
    if col is not None:
        mock_backend.get_collection.return_value = col
    if new_col is not None:
        mock_backend.create_collection.return_value = new_col
    return mock_backend


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_no_palace(mock_config_cls, tmp_path, capsys):
    mock_config_cls.return_value.palace_path = str(tmp_path / "nonexistent")
    args = argparse.Namespace(palace=None)
    with patch("mempalace.backends.chroma.ChromaBackend"):
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "No palace found" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_requires_palace_database(mock_config_cls, tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    mock_config_cls.return_value.palace_path = str(palace_dir)
    args = argparse.Namespace(palace=None)
    with patch("mempalace.backends.chroma.ChromaBackend"):
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "No palace database found" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_error_reading(mock_config_cls, tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")
    mock_config_cls.return_value.palace_path = str(palace_dir)
    args = argparse.Namespace(palace=None)
    mock_backend = MagicMock()
    mock_backend.get_collection.side_effect = Exception("corrupt db")
    with patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend):
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "Error reading palace" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_zero_drawers(mock_config_cls, tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")
    mock_config_cls.return_value.palace_path = str(palace_dir)
    args = argparse.Namespace(palace=None)
    mock_col = MagicMock()
    mock_col.count.return_value = 0
    mock_backend = _mock_backend_for(col=mock_col)
    with patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend):
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "Nothing to repair" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_success(mock_config_cls, tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")
    mock_config_cls.return_value.palace_path = str(palace_dir)
    args = argparse.Namespace(palace=None, yes=True)
    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
    }
    mock_new_col = MagicMock()
    mock_backend = _mock_backend_for(col=mock_col, new_col=mock_new_col)
    with patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend):
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "Repair complete" in out
    assert "2 drawers rebuilt" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_repair_aborts_without_confirmation(mock_config_cls, tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")
    mock_config_cls.return_value.palace_path = str(palace_dir)
    args = argparse.Namespace(palace=None)
    mock_col = MagicMock()
    mock_col.count.return_value = 1
    mock_backend = _mock_backend_for(col=mock_col)
    with (
        patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend),
        patch("builtins.input", return_value="n"),
    ):
        cmd_repair(args)
    out = capsys.readouterr().out
    assert "Aborted." in out
    mock_backend.create_collection.assert_not_called()


# ── cmd_compress ───────────────────────────────────────────────────────


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_no_palace(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, wing=None, dry_run=False, config=None)
    mock_backend = MagicMock()
    mock_backend.get_collection.side_effect = Exception("no palace")
    with (
        patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend),
        pytest.raises(SystemExit),
    ):
        cmd_compress(args)


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_no_drawers(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, wing="mywing", dry_run=False, config=None)
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": [], "ids": []}
    mock_backend = _mock_backend_for(col=mock_col)
    with patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend):
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "No drawers found" in out


def _make_mock_dialect_module(dialect_instance):
    """Create a mock dialect module with a Dialect class that returns the given instance."""
    mock_mod = MagicMock()
    mock_mod.Dialect.return_value = dialect_instance
    mock_mod.Dialect.from_config.return_value = dialect_instance
    mock_mod.Dialect.count_tokens = MagicMock(side_effect=lambda x: len(x) // 4)
    return mock_mod


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_dry_run(mock_config_cls, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, wing=None, dry_run=True, config=None)
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {
            "documents": ["some long text here for testing"],
            "metadatas": [{"wing": "test", "room": "general", "source_file": "test.txt"}],
            "ids": ["id1"],
        },
        {"documents": [], "metadatas": [], "ids": []},
    ]
    mock_backend = _mock_backend_for(col=mock_col)

    mock_dialect = MagicMock()
    mock_dialect.compress.return_value = "compressed"
    mock_dialect.compression_stats.return_value = {
        "original_chars": 100,
        "summary_chars": 30,
        "original_tokens_est": 25,
        "summary_tokens_est": 8,
        "size_ratio": 3.3,
        "note": "Estimates only.",
    }
    mock_dialect_mod = _make_mock_dialect_module(mock_dialect)

    with (
        patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend),
        patch.dict("sys.modules", {"mempalace.dialect": mock_dialect_mod}),
    ):
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "Compressing" in out
    assert "Total:" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_with_config(mock_config_cls, tmp_path, capsys):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    config_file = tmp_path / "entities.json"
    config_file.write_text('{"people": [], "projects": []}')
    args = argparse.Namespace(palace=None, wing=None, dry_run=True, config=str(config_file))
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": [], "ids": []}
    mock_backend = _mock_backend_for(col=mock_col)

    mock_dialect = MagicMock()
    mock_dialect_mod = _make_mock_dialect_module(mock_dialect)

    with (
        patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend),
        patch.dict("sys.modules", {"mempalace.dialect": mock_dialect_mod}),
    ):
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "Loaded entity config" in out


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_compress_stores_results(mock_config_cls, capsys):
    """Non-dry-run compress stores to mempalace_compressed collection."""
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(palace=None, wing=None, dry_run=False, config=None)
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {
            "documents": ["text"],
            "metadatas": [{"wing": "w", "room": "r", "source_file": "f.txt"}],
            "ids": ["id1"],
        },
        {"documents": [], "metadatas": [], "ids": []},
    ]
    mock_comp_col = MagicMock()
    mock_backend = MagicMock()
    mock_backend.get_collection.return_value = mock_col
    mock_backend.get_or_create_collection.return_value = mock_comp_col

    mock_dialect = MagicMock()
    mock_dialect.compress.return_value = "compressed"
    mock_dialect.compression_stats.return_value = {
        "original_chars": 100,
        "summary_chars": 30,
        "original_tokens_est": 25,
        "summary_tokens_est": 8,
        "size_ratio": 3.3,
        "note": "Estimates only.",
    }
    mock_dialect_mod = _make_mock_dialect_module(mock_dialect)

    with (
        patch("mempalace.backends.chroma.ChromaBackend", return_value=mock_backend),
        patch.dict("sys.modules", {"mempalace.dialect": mock_dialect_mod}),
    ):
        cmd_compress(args)
    out = capsys.readouterr().out
    assert "Stored" in out
    assert "Total:" in out
    mock_comp_col.upsert.assert_called_once()


def test_cmd_repair_trailing_slash_does_not_recurse():
    """Repair with trailing slash should put backup outside palace dir (#395)."""
    import os

    args = argparse.Namespace(palace="/tmp/fake_palace/")
    with patch("mempalace.cli.os.path.isdir", return_value=False):
        cmd_repair(args)
    # Verify the rstrip logic: palace_path should not end with separator
    palace_path = os.path.expanduser(args.palace).rstrip(os.sep)
    backup_path = palace_path + ".backup"
    assert not backup_path.startswith(palace_path + os.sep)
