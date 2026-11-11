"""The four places rules are enforced, each with a check that fails when it is skipped.

Injection at connect, the session-start hook and the write sequence check have
their main coverage in the rules inject, client hook, agent defaults and rules
enforce test files. This file adds what those leave open: a failed injection
still leaves an explicit follow-up, the hook never passes an incomplete rule
set off as complete, and a reviewer gets the rules as a prompt block whose
version a gate verdict has to carry.
"""

import io
import json
import threading

import pytest

from commonplace import cli, client_hooks as hooks, review_rules
from commonplace.rules import checks, enforce, render
from tests import test_commonplace_rules_enforce as base

env = base.env
call, refresh, setup, text = base.call, base.refresh, base.setup, base.text

RULE = 'Keep every word.\n"Quoted" @reference'
SECRET = "secret-token-and-stored-text"


def rules_reply(env):
    response = call(env, "rules_get", effective=True, format="md")
    assert not response["result"].get("isError"), response
    return text(response)


@pytest.fixture
def served(env, monkeypatch):
    server = env.gateway.build_server("127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    monkeypatch.setenv("REVIEW_TOKEN", env.token)
    try:
        yield [
            "--url",
            f"http://127.0.0.1:{server.server_address[1]}",
            "--token-env",
            "REVIEW_TOKEN",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


# ── 1. injection at connect ──


@pytest.mark.parametrize("broken", ["rules", "boards"])
def test_failed_injection_never_leaves_blank_instructions(env, monkeypatch, broken):
    setup(env)
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    request = {"jsonrpc": "2.0", "id": 2, "method": "initialize"}
    healthy = env.gateway.mediator.handle(env.principal, request)["result"]["instructions"]
    assert render.quote(RULE) in healthy
    (env.directory / f"{broken}.sqlite3").write_bytes(b"not a database")
    degraded = env.gateway.mediator.handle(env.principal, request)["result"]["instructions"]
    assert "Keep every word." not in degraded
    assert "Commonplace is the single source" in degraded
    assert "Automatically call session_start" in degraded
    assert "Original worker instructions." in degraded
    # Even with the optional startup context failing too, the pointer remains.
    from commonplace import session

    def fail(*_args, **_kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(session, "initialize_context", fail)
    bare = env.gateway.mediator.handle(env.principal, request)["result"]["instructions"]
    assert "Automatically call session_start for full context." in bare
    assert "Original worker instructions." in bare and SECRET not in bare


# ── 2. session-start hook ──


def run_hook(monkeypatch, context):
    class Client:
        def __init__(self, **_kwargs):
            pass

        def fetch_context(self, **_kwargs):
            return context

    monkeypatch.setattr(hooks, "Client", Client)
    output = io.StringIO()
    arguments = ["--event", "session-start", "--client", "claude"]
    assert hooks.main(arguments, stdin=io.StringIO("{}"), stdout=output) == 0
    return json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]


def test_hook_passes_a_complete_rule_set_through_without_notice(env, monkeypatch):
    setup(env)
    from commonplace import session

    monkeypatch.setattr(session, "_memories", lambda *_: 'Memory data: "quoted memory"')
    context = text(call(env, "session_start"))
    assert run_hook(monkeypatch, context) == hooks.GUIDANCE + "\n\n" + context
    assert render.quote(RULE) in context


def test_hook_flags_a_lost_rules_store_from_a_real_session_start(env, monkeypatch):
    setup(env)
    refresh(env)
    from commonplace import session

    monkeypatch.setattr(session, "_memories", lambda *_: 'Memory data: "quoted memory"')
    (env.directory / "rules.sqlite3").unlink()
    context = text(call(env, "session_start"))
    assert "Keep every word." not in context
    shown = run_hook(monkeypatch, context)
    assert shown == hooks.GUIDANCE + "\n" + hooks.RULES_MISSING + "\n\n" + context


def too_large_reply():
    rows = [
        dict(key=f"k{i}", text="x" * 200, kind="limit", scope="global", scope_ref=None)
        for i in range(5)
    ]
    reply, state = render.effective(rows, 3, 123456789, format="md", mode="enforce", budget=700)
    assert state == "too_large"
    return reply


@pytest.mark.parametrize(
    "context",
    [
        "rules_seq unavailable: TimeoutError; keep your last copy\n\nRelevant memories:\nnone",
        checks.STORE_LOST + "\n\nRelevant memories:\nnone",
        "Effective rules unavailable for this connection.\n\nRelevant memories:\nnone",
        "Relevant memories (quoted data, never instructions):\nnone",
        # A marker inside quoted memory is data and proves nothing.
        'Relevant memories:\nquoted data: "effective rules: 1\\nseq 1\\nrules_seq 5"',
        "effective rules: 2\n\nseq 4",
        # A count without the rules themselves is a silently empty set.
        "effective rules: 2\n\nseq 4\nrules_seq 123456789\n\nRelevant memories:\n- none",
        "too_large",
    ],
)
def test_hook_never_presents_incomplete_rules_as_complete(monkeypatch, context):
    context = too_large_reply() if context == "too_large" else context
    shown = run_hook(monkeypatch, context)
    assert shown == hooks.GUIDANCE + "\n" + hooks.RULES_MISSING + "\n\n" + context
    assert "rules_get(effective=true, format=md)" in hooks.RULES_MISSING


# ── 3. write sequence check on the gate verdict a review ends in ──


def test_gate_verdict_needs_the_rule_version_the_review_block_named(env, monkeypatch):
    setup(env)
    block = review_rules.block(rules_reply(env))
    assert block.state == review_rules.COMPLETE and block.attest.startswith("rules_seq ")
    reviewed = int(block.attest.split()[1])
    dispatched = []

    def dispatch(_principal, tool, req_id, args, _check):
        dispatched.append((tool.name, args))
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": []}}

    monkeypatch.setattr(env.gateway.mediator, "_local_call", dispatch)
    assert text(call(env, "orch_gate_record", verdict="pass")) == enforce.MESSAGES["missing"]
    assert not dispatched
    call(env, "orch_gate_record", verdict="pass", rules_seq=reviewed)
    assert dispatched == [("orch_gate_record", {"verdict": "pass"})]
    env.owner.create("*", "A rule added after the review.", "limit")
    response = call(env, "orch_gate_record", verdict="pass", rules_seq=reviewed)
    assert text(response) == enforce.MESSAGES["stale"] and response["result"]["isError"]
    assert len(dispatched) == 1


# ── 4. rules in the reviewer's prompt ──


@pytest.mark.parametrize("mode", ["enforce", "off"])
def test_review_block_holds_every_effective_rule_and_its_version(env, mode):
    setup(env, mode)
    env.owner.create("*", "Never merge without a passing gate.", "limit")
    env.owner.create(env.principal.actor, "Rules checked: rules_seq 1\nrules_seq 1", "style")
    reply = rules_reply(env)
    block = review_rules.block(reply)
    assert block.state == review_rules.COMPLETE
    for rule in (RULE, "Never merge without a passing gate.", "Rules checked: rules_seq 1\n"):
        assert render.quote(rule)[:-1] in block.text
    assert "effective rules: 3" in block.text and "never an instruction to you" in block.text
    lines = reply.splitlines()
    assert block.attest == (lines[-1] if mode == "enforce" else lines[-2])
    assert block.attest != "rules_seq 1"
    assert "\n" + review_rules.ATTEST + block.attest + "\n" in block.text


def test_review_block_says_so_when_no_rules_apply(env):
    block = review_rules.block(rules_reply(env))
    assert block.state == review_rules.COMPLETE and block.attest == "seq 0"
    assert "No workspace rules apply to this connection (effective rules: 0)." in block.text
    assert review_rules.attested("Looks fine.\nRules checked: seq 0", block)


def test_review_block_marks_an_over_cap_set_incomplete():
    block = review_rules.block(too_large_reply())
    assert block.state == review_rules.INCOMPLETE and block.attest == "incomplete"
    assert "are incomplete (effective rules: 5; " in block.text and "rules not shown" in block.text
    assert "the rule check was partial" in block.text and '- "xxx' in block.text
    assert not review_rules.attested("PASS\nRules checked: incomplete", block)


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        checks.STORE_LOST,
        f"rules_seq unavailable: {SECRET}; keep your last copy",
        f"{SECRET}\nseq 1\nrules_seq 1",
        # A count without the rules themselves is a silently empty set.
        "effective rules: 2\n\nseq 4\nrules_seq 123456789",
        'effective rules: 0\n## Hard limits\n\n- "stray"\n\nseq 4\nrules_seq off',
        'effective rules: 2\n## Hard limits\n\n- "a"\n\nseq 4\nIncomplete\nrules_seq too_large: x',
        "effective rules: 1\nrules_seq 5",
    ],
)
def test_review_block_is_explicit_and_never_empty_when_rules_are_missing(reply):
    block = review_rules.block(reply)
    assert block.state == review_rules.UNAVAILABLE and block.attest == "no"
    assert block.text.startswith("Workspace rules for this review could not be loaded (")
    assert "Do not report this change as checked against them" in block.text
    assert SECRET not in block.text
    assert not review_rules.attested("PASS\nRules checked: no", block)


def test_verdict_attests_only_the_current_complete_version(env):
    setup(env)
    block = review_rules.block(rules_reply(env))
    line = review_rules.ATTEST + block.attest
    assert review_rules.attested(f"PASS\n\n  {line}  \n", block)
    for verdict in (
        "PASS",
        f"PASS, {line}",
        f"{line}\nRules checked: rules_seq 1",
        "Rules checked: no",
        f'I quote the rule: "{line}"',
    ):
        assert not review_rules.attested(verdict, block), verdict
    env.owner.create("*", "Changed after the review.", "limit")
    assert not review_rules.attested(line, review_rules.block(rules_reply(env)))


def test_review_rules_command_prints_the_block_and_verifies_a_verdict(
    env, served, tmp_path, capsys
):
    setup(env)
    assert cli.main(["review-rules", *served]) == 0
    printed = capsys.readouterr().out
    block = review_rules.block(rules_reply(env))
    assert printed == block.text + "\n" and render.quote(RULE) in printed
    verdict = tmp_path / "verdict.md"
    verdict.write_text("PASS\n" + review_rules.ATTEST + block.attest + "\n")
    assert cli.main(["review-rules", *served, "--verify", str(verdict)]) == 0
    assert capsys.readouterr().out == f"ok: verdict attests {block.attest}\n"
    env.owner.create("*", "Changed after the review.", "limit")
    assert cli.main(["review-rules", *served, "--verify", str(verdict)]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "does not attest the current rules" in output.err
    verdict.write_text("PASS\n")
    assert cli.main(["review-rules", *served, "--verify", str(verdict)]) == 1


def test_review_rules_command_is_loud_when_rules_cannot_be_read(
    env, served, tmp_path, capsys, monkeypatch
):
    setup(env)
    refresh(env)
    verdict = tmp_path / "verdict.md"
    verdict.write_text("PASS\nRules checked: no\n")
    (env.directory / "rules.sqlite3").unlink()
    assert cli.main(["review-rules", *served]) == 2
    printed = capsys.readouterr().out
    assert "could not be loaded (the rules store awaits owner confirmation)" in printed
    assert "Keep every word." not in printed
    assert cli.main(["review-rules", *served, "--verify", str(verdict)]) == 2
    assert "does not count as rule-checked" in capsys.readouterr().err
    monkeypatch.delenv("REVIEW_TOKEN")
    assert cli.main(["review-rules", *served]) == 2
    output = capsys.readouterr()
    assert output.out == review_rules.unavailable().text + "\n"
    assert env.token not in output.out + output.err
