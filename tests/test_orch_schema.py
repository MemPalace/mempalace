"""Orchestrator config format: parse, validate, render, diff, experiment rule.

R13 (inline secrets and the approved sha256) and R14 (planner without
design_before_code) live here, plus the schema-side checks other units rely on.
"""

import copy
import hashlib

import pytest

from commonplace.orch import schema
from commonplace.orch.schema import ConfigError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

EXAMPLE = """\
schema = "commonplace.orchestrator/1"
project = "acme-shop"
[limits]
max_agents = 3
[roles.main]
kind = "orchestrator"
harness = "t3"
topic = "orchestrate"
model_role = "main"
workflow = "card-pipeline"
prompt = '''Coordinate acme-shop. {{include:resume_protocol}}'''
skills = [{ id = "skl_example", version = 3 }]
mcps = ["hub"]
[roles.planner]
kind = "planner"
parent = "main"
topic = "plan"
connections = ["planner-acme"]
prompt = '''{{include:design_before_code}}'''
[roles.builder]
kind = "builder"
parent = "main"
harness = "t3"
topic = "build"
model_role = "worker"
workflow = "build-unit"
prompt = '''Implement the card's plan unit; run fast checks.'''
[workflows.card-pipeline]
steps = ["planner", "builder"]
[workflows.build-unit]
steps = ["read plan", "write RED test", "implement", "fast checks", "report"]
[mcps.hub]
url = "https://hub.example.invalid/mcp"
auth = { secret_ref = "env:ACME_HUB_TOKEN" }
[runner]
role = "builder"
machine = "mach_0123456789abcdef"
project = "acme"
runtime_mode = "approval-required"
[[triggers]]
on = "card.todo"
"""

# Synthetic credential shapes, assembled so no literal token sits in the file.
FAKE_ANTHROPIC = "sk-ant-api03-" + "Zq9" * 14
FAKE_GITHUB = "ghp_" + "Ab1" * 12


def data():
    return tomllib.loads(EXAMPLE)


def refused(doc, code):
    with pytest.raises(ConfigError) as info:
        schema.validate(doc)
    assert info.value.code == code, str(info.value)
    return info.value


def assert_not_echoed(error, secret):
    for text in (str(error), repr(error), error.path, error.message, *map(str, error.args)):
        assert secret not in text
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


# ---------------------------------------------------------------- example


def test_example_parses_with_sha256():
    config = schema.parse(EXAMPLE)
    assert config.sha256 == hashlib.sha256(EXAMPLE.encode()).hexdigest()
    assert config.has_runner
    assert config.runner_role.name == "builder"
    assert config.runner_role.model_role == "worker"
    assert config.max_agents == 3
    assert config.planner_connections == ("planner-acme",)
    assert config.mcps["hub"].secret_ref == "env:ACME_HUB_TOKEN"
    assert config.triggers == (schema.Trigger("card.todo"),)
    assert config.roles["main"].skills == (schema.Skill("skl_example", 3),)
    assert list(config.roles) == ["main", "planner", "builder"]


def test_to_dict_round_trips():
    config = schema.parse(EXAMPLE)
    again = schema.validate(config.to_dict())
    assert again == schema.validate(data())
    assert again.sha256 is None
    assert schema.diff(config, again) == []


def test_bytes_input_and_codes_are_known():
    assert schema.parse(EXAMPLE.encode()).sha256 == schema.sha256_text(EXAMPLE)
    for name in (
        "inline_secret",
        "sha256_mismatch",
        "planner_missing_design_before_code",
        "experiment_not_allowed",
    ):
        assert name in schema.CODES


def test_unconfigured_board_config_needs_no_runner_or_limits():
    doc = data()
    del doc["runner"], doc["triggers"], doc["limits"]
    config = schema.validate(doc)
    assert not config.has_runner
    assert config.runner_role is None
    assert config.max_agents is None


# ---------------------------------------------------------------- R13 secrets + sha256


def test_r13_inline_secret_in_auth_refused_not_echoed():
    doc = data()
    doc["mcps"]["hub"]["auth"] = {"token": FAKE_GITHUB}
    error = refused(doc, "inline_secret")
    assert error.path == "mcps.hub.auth.token"
    assert_not_echoed(error, FAKE_GITHUB)


@pytest.mark.parametrize(
    "ref", [FAKE_GITHUB, "vault:acme/hub", "env:acme_token", "env:A", "env:" + "A" * 65]
)
def test_r13_secret_ref_must_be_env_name(ref):
    doc = data()
    doc["mcps"]["hub"]["auth"]["secret_ref"] = ref
    error = refused(doc, "inline_secret")
    assert error.path in ("mcps.hub.auth.secret_ref",)
    assert_not_echoed(error, ref)


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("prompt", f"Use {FAKE_ANTHROPIC} for the hub."),
        ("prompt", "line one\nauthorization: Bearer " + "x" * 30 + "\nline three"),
        ("step", f"call the api with {FAKE_GITHUB}"),
        ("url", "https://hub.example.invalid/mcp?token=" + "q" * 24),
        ("url", "https://user:" + "p" * 12 + "@hub.example.invalid/mcp"),
    ],
)
def test_r13_secret_shaped_value_anywhere_refused_not_echoed(where, value):
    doc = data()
    if where == "prompt":
        doc["roles"]["builder"]["prompt"] = value
    elif where == "step":
        doc["workflows"]["build-unit"]["steps"][0] = value
    else:
        doc["mcps"]["hub"]["url"] = value
    error = refused(doc, "inline_secret")
    assert_not_echoed(error, value)
    assert_not_echoed(error, value.split()[-1])


def test_r13_secret_in_long_prompt_found_past_redact_window():
    doc = data()
    doc["roles"]["builder"]["prompt"] = "a" * 20_000 + " " + FAKE_ANTHROPIC
    assert_not_echoed(refused(doc, "inline_secret"), FAKE_ANTHROPIC)


def test_r13_secret_named_key_or_secret_shaped_key_refused():
    doc = data()
    doc["roles"]["main"]["api_key"] = "plain-value-here"
    error = refused(doc, "inline_secret")
    assert error.path == "roles.main.api_key"
    assert_not_echoed(error, "plain-value-here")
    doc = data()
    doc["roles"]["main"][FAKE_GITHUB] = 1
    error = refused(doc, "inline_secret")
    assert error.path == "roles.main.<key>"
    assert_not_echoed(error, FAKE_GITHUB)


def test_r13_wrong_sha256_refused_before_parsing_and_not_echoed():
    text = EXAMPLE.replace("ACME_HUB_TOKEN", "OTHER_TOKEN")
    approved = schema.sha256_text(EXAMPLE)
    with pytest.raises(ConfigError) as info:
        schema.parse(text, expected_sha256=approved)
    assert info.value.code == "sha256_mismatch"
    assert approved not in str(info.value)
    # The sha check runs before TOML: a broken file carrying a secret is not parsed or echoed.
    broken = f"token = {FAKE_GITHUB}\n[[["
    with pytest.raises(ConfigError) as info:
        schema.parse(broken, expected_sha256=approved)
    assert info.value.code == "sha256_mismatch"
    assert_not_echoed(info.value, FAKE_GITHUB)


@pytest.mark.parametrize("claim", ["", "ABC", "A" * 64, "g" * 64, None, 5])
def test_r13_malformed_sha256_claim_refused(claim):
    with pytest.raises(ConfigError) as info:
        schema.verify_sha256(EXAMPLE, claim)
    assert info.value.code == "sha256_invalid"


def test_r13_matching_sha256_accepted():
    sha = schema.sha256_text(EXAMPLE)
    assert schema.verify_sha256(EXAMPLE, sha) == sha
    assert schema.parse(EXAMPLE, expected_sha256=sha).sha256 == sha


def test_r13_toml_error_does_not_echo_the_line():
    text = f'schema = "commonplace.orchestrator/1"\nsecret = {FAKE_GITHUB} {FAKE_GITHUB}\n'
    with pytest.raises(ConfigError) as info:
        schema.parse(text)
    assert info.value.code == "toml_invalid"
    assert "line 2" in str(info.value)
    assert_not_echoed(info.value, FAKE_GITHUB)


# ---------------------------------------------------------------- R14 design_before_code


@pytest.mark.parametrize(
    "prompt",
    [
        None,
        "Plan the card carefully.",
        "{{include:resume_protocol}}",
        "{{ include:design_before_code }}",
    ],
)
def test_r14_planner_without_design_before_code_refused(prompt):
    doc = data()
    if prompt is None:
        del doc["roles"]["planner"]["prompt"]
    else:
        doc["roles"]["planner"]["prompt"] = prompt
    error = refused(doc, "planner_missing_design_before_code")
    assert error.path == "roles.planner.prompt"


def test_r14_planner_with_design_before_code_accepted_and_expanded():
    doc = data()
    doc["roles"]["planner"]["prompt"] = "Plan first.\n{{include:design_before_code}}\nThen stop."
    config = schema.validate(doc)
    text = schema.role_instructions(config, "planner")
    assert "{{include" not in text
    assert schema.template("design_before_code").strip() in text
    assert "Dependency | Timeout | Bounded retry | Idempotency key | User sees | RED" in text


def test_r14_rule_applies_to_every_planner_role():
    doc = data()
    doc["roles"]["planner-2"] = {"kind": "planner", "prompt": "No template here."}
    assert refused(doc, "planner_missing_design_before_code").path == "roles.planner-2.prompt"


def test_non_planner_needs_no_design_template():
    doc = data()
    doc["roles"]["review"] = {"kind": "reviewer", "prompt": "Review the diff."}
    assert "review" in schema.validate(doc).roles


# ---------------------------------------------------------------- structure


def mutate(path, value):
    doc = data()
    *parents, last = path
    node = doc
    for key in parents:
        node = node[key]
    if value is KeyError:
        del node[last]
    else:
        node[last] = value
    return doc


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("schema",), "commonplace.orchestrator/2", "schema_unsupported"),
        (("schema",), KeyError, "missing_key"),
        (("extra",), 1, "unknown_key"),
        (("project",), "Acme Shop", "invalid_value"),
        (("limits", "max_agents"), 0, "invalid_value"),
        (("limits", "max_agents"), 21, "invalid_value"),
        (("limits", "max_agents"), True, "wrong_type"),
        (("limits",), KeyError, "limits_required"),
        (("roles",), {}, "invalid_value"),
        (("roles", "main", "kind"), "boss", "invalid_value"),
        (("roles", "main", "kind"), KeyError, "missing_key"),
        (("roles", "main", "colour"), "red", "unknown_key"),
        (("roles", "main", "harness"), "other", "harness_not_allowed"),
        (("roles", "main", "model_role"), "planner", "invalid_value"),
        (("roles", "main", "topic"), "-bad", "invalid_value"),
        (("roles", "main", "workflow"), "missing", "unknown_workflow"),
        (("roles", "main", "mcps"), ["nope"], "unknown_mcp"),
        (("roles", "main", "parent"), "nobody", "unknown_role"),
        (("roles", "main", "parent"), "main", "parent_cycle"),
        (("roles", "main", "skills"), [{"id": "skl_x"}], "missing_key"),
        (("roles", "main", "skills"), [{"id": "skl_x", "version": 0}], "invalid_value"),
        (("roles", "main", "prompt"), "{{include:other}}", "unknown_template"),
        (("roles", "main", "connections"), ["planner-acme"], "connections_not_planner"),
        (("roles", "planner", "connections"), ["conn_0123456789abcdef"], "connection_id_shaped"),
        (("roles", "planner", "connections"), ["bad name"], "invalid_value"),
        (("roles", "builder", "harness"), "codex", "runner_harness_not_t3"),
        (("roles", "builder", "harness"), KeyError, "runner_harness_not_t3"),
        (("roles", "builder", "model_role"), KeyError, "runner_role_incomplete"),
        (("roles", "builder", "topic"), KeyError, "runner_role_incomplete"),
        (("runner", "role"), "ghost", "unknown_role"),
        (("runner", "machine"), "mach_xyz", "invalid_value"),
        (("runner", "runtime_mode"), "yolo", "invalid_value"),
        (("runner", "project"), KeyError, "missing_key"),
        (("triggers",), [{"on": "card.done"}], "invalid_value"),
        (("triggers",), [{"on": "card.todo", "when": "now"}], "unknown_key"),
        (("workflows", "build-unit", "steps"), [], "invalid_value"),
        (("workflows", "build-unit", "steps"), ["x" * 501], "invalid_value"),
        (("workflows", "build-unit", "steps"), ["ok"] * 65, "too_many"),
        (("workflows", "Bad_Name"), {"steps": ["x"]}, "invalid_value"),
        (("mcps", "hub", "url"), "ftp://hub.example.invalid", "invalid_value"),
        (("mcps", "hub", "url"), KeyError, "missing_key"),
        (("mcps", "hub", "auth"), {}, "missing_key"),
        (("mcps", "hub", "url"), "https://deploy@hub.example.invalid/mcp", "inline_secret"),
        (("mcps", "hub", "url"), "http://[::1/mcp", "invalid_value"),
        (("roles", "main"), "orchestrator", "wrong_type"),
        (("roles", "main", "topic"), 5, "wrong_type"),
        (("roles", "main", "mcps"), "hub", "wrong_type"),
    ],
)
def test_structure_refusals(path, value, code):
    refused(mutate(path, value), code)


def test_parent_cycle_over_two_roles():
    doc = data()
    doc["roles"]["main"]["parent"] = "planner"
    assert refused(doc, "parent_cycle").path.startswith("roles.")


def test_triggers_need_runner():
    doc = data()
    del doc["runner"]
    refused(doc, "trigger_without_runner")


def test_too_many_roles():
    doc = data()
    for n in range(14):
        doc["roles"][f"extra-{n}"] = {"kind": "reviewer"}
    refused(doc, "too_many")


def test_prompt_and_config_size_limits():
    doc = data()
    doc["roles"]["main"]["prompt"] = "é" * (schema.MAX_PROMPT_BYTES // 2 + 1)
    refused(doc, "prompt_too_large")
    with pytest.raises(ConfigError) as info:
        schema.parse(EXAMPLE + "#" * schema.MAX_CONFIG_BYTES)
    assert info.value.code == "config_too_large"


def test_launch_text_limit_counts_expanded_includes():
    doc = data()
    doc["roles"]["builder"]["prompt"] = "{{include:resume_protocol}}" * 1000
    refused(doc, "launch_too_large")


def test_not_utf8_and_non_table_input():
    with pytest.raises(ConfigError) as info:
        schema.parse(b"\xff\xfe")
    assert info.value.code == "not_utf8"
    with pytest.raises(ConfigError) as info:
        schema.parse("\ud800")
    assert info.value.code == "not_utf8"
    refused([], "wrong_type")


def test_check_connections_against_workspace_names():
    config = schema.parse(EXAMPLE)
    schema.check_connections(config, ["planner-acme", "other"])
    with pytest.raises(ConfigError) as info:
        schema.check_connections(config, ["other"])
    assert info.value.code == "connection_unknown"
    assert info.value.path == "roles.planner.connections[0]"


def test_template_names_are_closed():
    with pytest.raises(ConfigError):
        schema.template("../schema")
    for name in schema.TEMPLATES:
        assert schema.template(name).strip()


# ---------------------------------------------------------------- render


def test_role_instructions_and_render():
    config = schema.parse(EXAMPLE)
    text = schema.role_instructions(config, "builder")
    assert text.startswith("Implement the card's plan unit")
    assert "Workflow build-unit:\n1. read plan\n2. write RED test" in text
    main = schema.role_instructions(config, "main")
    assert schema.template("resume_protocol").strip() in main
    out = schema.render(config)
    assert "# Orchestrator config `acme-shop`" in out
    assert f"sha256: `{config.sha256}`" in out
    assert "runner: role `builder` on `mach_0123456789abcdef`" in out
    assert "`hub`: https://hub.example.invalid/mcp (auth: `env:ACME_HUB_TOKEN`)" in out
    assert "connections: `planner-acme`" in out
    assert "skills: `skl_example@3`" in out
    assert "{{include" not in out
    with pytest.raises(ConfigError):
        schema.role_instructions(config, "ghost")


# ---------------------------------------------------------------- diff + experiments


def variant(**changes):
    doc = copy.deepcopy(data())
    for dotted, value in changes.items():
        *parents, last = dotted.split("__")
        node = doc
        for key in parents:
            node = node[key]
        node[last] = value
    return schema.validate(doc)


def test_diff_and_format():
    live = schema.validate(data())
    exp = variant(
        roles__builder__prompt="Line one.\nLine two.",
        roles__builder__topic="build-exp",
        roles__main__skills=[{"id": "skl_example", "version": 4}],
    )
    changes = schema.diff(live, exp)
    assert [c.path for c in changes] == [
        "roles.builder.prompt",
        "roles.builder.topic",
        "roles.main.skills",
    ]
    assert all(c.op == "change" for c in changes)
    text = schema.format_diff(changes)
    assert '~ roles.builder.topic: "build" -> "build-exp"' in text
    assert "~ roles.builder.prompt" in text and "+Line two." in text
    assert schema.format_diff([]) == "no changes\n"
    doc = data()
    doc["roles"]["review"] = {"kind": "reviewer"}
    del doc["mcps"]["hub"]["auth"]
    added = schema.diff(live, schema.validate(doc))
    assert ("mcps.hub.auth", "remove") in [(c.path, c.op) for c in added]
    assert ("roles.review", "add") in [(c.path, c.op) for c in added]
    assert '+ roles.review = {"kind": "reviewer"}' in schema.format_diff(added)


def test_experiment_may_change_allowed_fields():
    live = schema.validate(data())
    exp = variant(
        roles__builder__prompt="Try a stricter prompt.",
        roles__builder__workflow="card-pipeline",
        roles__builder__topic="build-exp",
        roles__builder__model_role="main",
        roles__main__skills=[],
        **{"workflows__card-pipeline__steps": ["planner", "review", "builder"]},
    )
    assert schema.experiment_violations(live, exp) == []
    schema.check_experiment(live, exp)


@pytest.mark.parametrize(
    "changes",
    [
        {"runner__machine": "mach_fedcba9876543210"},
        {"runner__runtime_mode": "full-access"},
        {"mcps__hub__url": "https://other.example.invalid/mcp"},
        {"mcps__hub__auth": {"secret_ref": "env:OTHER_TOKEN"}},
        {"limits__max_agents": 20},
        {"roles__main__harness": "codex"},
        {"roles__main__mcps": []},
        {"roles__planner__connections": ["planner-other"]},
        {"roles__planner__parent": "builder"},
        {"roles__review": {"kind": "reviewer"}},
        {"project": "acme-other"},
        {"triggers": []},
    ],
)
def test_experiment_disallowed_diff_refused(changes):
    live = schema.validate(data())
    exp = variant(**changes)
    assert schema.experiment_violations(live, exp)
    with pytest.raises(ConfigError) as info:
        schema.check_experiment(live, exp)
    assert info.value.code == "experiment_not_allowed"
