from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _hook(name):
    return (ROOT / "hooks" / name).read_text(encoding="utf-8")


def test_save_hook_uses_shared_parser_and_utf8_counter():
    body = _hook("mempal_save_hook.sh")

    assert "-m mempalace.hook_shell parse-stop" in body
    assert "-m mempalace.hook_shell count-human-messages" in body
    assert "transcript_path not found after normalization" in body
    assert "safe = lambda" not in body
    assert "with open(sys.argv[1]) as f:" not in body


def test_precompact_hook_uses_shared_parser():
    body = _hook("mempal_precompact_hook.sh")

    assert "-m mempalace.hook_shell parse-precompact" in body
    assert "missing or invalid transcript path after normalization" in body
    assert "safe = lambda" not in body
