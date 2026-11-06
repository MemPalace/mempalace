"""Legacy screens inside the workbench shell: phone Decide height and English copy."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path("commonplace/web/static")


def phone_block(css):
    start = css.index("@media (max-width: 699px)")
    depth, i = 0, css.index("{", start)
    for j in range(i, len(css)):
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        if depth == 0:
            return css[i : j + 1]
    raise AssertionError("unclosed phone block")


def test_phone_decide_shell_outranks_the_shell_height_reset():
    # The shell's phone reset (`.shell.two { height: auto }`) loads after decide.css, so an equally
    # specific decide rule loses and the queue card collapses to nothing.
    shell = phone_block((STATIC / "workbench.css").read_text())
    assert re.search(r"\.shell\.two\b[^{]*\{[^}]*height:\s*auto", shell), (
        "shell phone height reset moved"
    )
    decide = phone_block((STATIC / "decide.css").read_text())
    rule = re.search(r'([^{}]*\.shell\.two\[data-screen="decide"\][^{]*)\{([^}]*)\}', decide)
    assert rule, "decide.css has no phone height rule as specific as the shell reset"
    assert "100dvh" in rule.group(2)


def test_agent_used_copy_reads_as_english():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is required to check the agent copy")
    agents = (STATIC / "agents.js").read_text()
    assert "export function usedText" in agents, "agents.js has no usedText helper"
    api = (STATIC / "api.js").read_text()
    elapsed = api[api.index("export function elapsed") : api.index("export function safeLink")]
    used = agents[agents.index("export function usedText") : agents.index("function agentRows")]
    script = (
        (elapsed + used).replace("export function", "function")
        + "\nconst ago = (m) => new Date(Date.now() - m * 60000).toISOString();"
        + "\nprocess.stdout.write(JSON.stringify([usedText(ago(0)), usedText(ago(5)), usedText(ago(180))]));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert json.loads(result.stdout) == ["Used just now", "Used 5m ago", "Used 3h ago"]
    assert "`Used ${elapsed(" not in agents


def test_child_progress_is_english():
    source = (STATIC / "children.js").read_text()
    assert "klaar" not in source
    assert "/${e(card.children.total)} done</span>" in source
