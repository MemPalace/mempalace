#!/usr/bin/env python3
"""Check release-version copies without importing or modifying MemPalace.

Run ``python scripts/check_versions.py`` from any directory. Python 3.11+
uses the standard-library TOML parser; older supported Python versions use
MemPalace's existing ``tomli`` dependency. Optional GitHub report paths are
appended to only; version sources are never rewritten.
"""

import argparse
import ast
import html
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


_CARGO_PACKAGES = ("mempalace-cli", "mempalace-core", "mempalace-py")


def _field(data, *keys):
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            raise ValueError(f"missing field {'.'.join(keys)}")
        data = data[key]
    return data


def _version(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("version must be a nonempty, trimmed string")
    if len(value.splitlines()) != 1 or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("version must be a single line without control characters")
    return value


def _python_version(text):
    tree = ast.parse(text)
    stores = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "__version__"
        and isinstance(node.ctx, ast.Store)
    ]
    assignments = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if len(targets) == 1 and isinstance(targets[0], ast.Name):
            if targets[0].id == "__version__":
                assignments.append(node.value)
    if len(stores) != 1 or len(assignments) != 1:
        raise ValueError("expected exactly one literal __version__ assignment")
    value = assignments[0]
    if not isinstance(value, ast.Constant):
        raise ValueError("__version__ must be a literal string, not an expression")
    return {"__version__": value.value}


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def _json(text):
    return json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_json_constant)


def _json_version(text):
    return {"version": _field(_json(text), "version")}


def _marketplace_version(text):
    data = _json(text)
    plugins = _field(data, "plugins")
    if not isinstance(plugins, list) or any(not isinstance(item, dict) for item in plugins):
        raise ValueError("plugins must be a list of objects")
    matches = [item for item in plugins if item.get("name") == "mempalace"]
    if len(matches) != 1:
        raise ValueError("expected exactly one plugin named mempalace")
    return {"plugins[mempalace].version": _field(matches[0], "version")}


def _pyproject_version(text):
    return {"project.version": _field(tomllib.loads(text), "project", "version")}


def _cargo_version(text):
    return {
        "workspace.package.version": _field(tomllib.loads(text), "workspace", "package", "version")
    }


def _packages(text):
    packages = _field(tomllib.loads(text), "package")
    if not isinstance(packages, list) or any(not isinstance(item, dict) for item in packages):
        raise ValueError("package must be an array of tables")
    return packages


def _uv_version(text):
    matches = [
        item
        for item in _packages(text)
        if item.get("name") == "mempalace" and item.get("source") == {"editable": "."}
    ]
    if len(matches) != 1:
        raise ValueError("expected exactly one mempalace package with source editable '.'")
    return {"mempalace (editable '.')": _field(matches[0], "version")}


def _cargo_lock_versions(text):
    packages = _packages(text)
    versions = {}
    for name in _CARGO_PACKAGES:
        matches = [item for item in packages if item.get("name") == name and "source" not in item]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one source-less workspace package named {name}")
        versions[name] = _field(matches[0], "version")
    return versions


def _frontmatter_version(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening YAML frontmatter")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError("unterminated YAML frontmatter")
    matches = [re.fullmatch(r"version\s*:\s*(.*)", line) for line in lines[1:end]]
    values = [match.group(1) for match in matches if match]
    if len(values) != 1:
        raise ValueError("expected exactly one top-level frontmatter version")
    raw = values[0].strip()
    if raw.startswith('"'):
        try:
            value, offset = json.JSONDecoder().raw_decode(raw)
        except ValueError as exc:
            raise ValueError("malformed quoted frontmatter version") from exc
        remainder = raw[offset:].strip()
        if remainder and not remainder.startswith("#"):
            raise ValueError("unexpected text after frontmatter version")
    elif raw.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", raw)
        if not match:
            raise ValueError("malformed quoted frontmatter version")
        value = match.group(1).replace("''", "'")
    else:
        value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        if value in ("null", "~", "true", "false", "|", ">") or value.startswith(("[", "{")):
            raise ValueError("frontmatter version must be a scalar string")
    return {"frontmatter.version": value}


def _readme_version(text):
    definitions = re.findall(
        r"^ {0,3}\[version-shield\]:[ \t]*(<[^>\r\n]+>|\S+)(?:[ \t]+.*)?$",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if len(definitions) != 1:
        raise ValueError("expected exactly one version-shield reference definition")
    url = urlsplit(definitions[0].strip("<>"))
    prefix = "/badge/version-"
    if url.scheme != "https" or url.netloc != "img.shields.io" or not url.path.startswith(prefix):
        raise ValueError("version-shield must use the Shields version badge URL")
    badge = url.path[len(prefix) :]
    # Shields escapes literal hyphens as '--'; only a single hyphen separates
    # the version message from the color. Decode percent escapes after splitting.
    separators = []
    i = 0
    while i < len(badge):
        if badge[i : i + 2] == "--":
            i += 2
        elif badge[i] == "-":
            separators.append(i)
            i += 1
        else:
            i += 1
    if len(separators) != 1:
        raise ValueError("malformed version-shield badge message/color")
    offset = separators[0]
    if not badge[offset + 1 :] or "/" in badge:
        raise ValueError("malformed version-shield badge color")
    return {"version-shield": unquote(badge[:offset].replace("--", "-"))}


_SOURCES = (
    ("mempalace/version.py", ("__version__",), _python_version),
    ("pyproject.toml", ("project.version",), _pyproject_version),
    (".claude-plugin/marketplace.json", ("plugins[mempalace].version",), _marketplace_version),
    (".claude-plugin/plugin.json", ("version",), _json_version),
    (".codex-plugin/plugin.json", ("version",), _json_version),
    (".dsh-plugin/package.json", ("version",), _json_version),
    ("integrations/openclaw/SKILL.md", ("frontmatter.version",), _frontmatter_version),
    ("README.md", ("version-shield",), _readme_version),
    ("uv.lock", ("mempalace (editable '.')",), _uv_version),
    ("Cargo.toml", ("workspace.package.version",), _cargo_version),
    ("Cargo.lock", _CARGO_PACKAGES, _cargo_lock_versions),
)


def collect_versions(root):
    """Return the 13 expected version values and any per-source diagnostics."""
    versions = {}
    errors = []
    for path, fields, reader in _SOURCES:
        for field in fields:
            versions[(path, field)] = None
        try:
            values = reader((root / path).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        for field in fields:
            try:
                versions[(path, field)] = _version(_field(values, field))
            except ValueError as exc:
                errors.append(f"{path} ({field}): {exc}")
    return versions, errors


def _code(text):
    if not any(char in text for char in "&<>`|"):
        return "`" + text + "`"
    return "<code>" + html.escape(text, quote=False).replace("|", "&#124;") + "</code>"


def _summary(versions, errors):
    lines = ["## Detected versions", "", "| Source | Version |", "| --- | --- |"]
    for (path, field), value in versions.items():
        lines.append(f"| {_code(f'{path} ({field})')} | {_code(value or '<invalid>')} |")
    if errors:
        lines.extend(("", "## Version guard failures", ""))
        lines.extend(f"- {_code(error)}" for error in errors)
    else:
        lines.extend(("", "All 13 version values agree."))
    return "\n".join(lines) + "\n"


def main(argv=None):
    """Check all version copies, optionally append GitHub reports, and return an exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    args = parser.parse_args(argv)
    versions, errors = collect_versions(args.root)
    reference = versions[("mempalace/version.py", "__version__")]
    if reference is not None:
        for (path, field), value in versions.items():
            if value is not None and value != reference:
                errors.append(f"{path} ({field}): expected {reference!r}, got {value!r}")
    # Keep every diagnostic on one line, including syntax errors from parsers.
    errors = [error.replace("\r", "\\r").replace("\n", "\\n") for error in errors]
    summary = _summary(versions, errors)
    print(summary, end="")
    protected = {(args.root / path).resolve() for path, _, _ in _SOURCES}
    for path, is_output in ((args.github_summary, False), (args.github_output, True)):
        content = f"py_version={reference}\n" if is_output else summary
        if is_output and errors:
            continue
        if path is None or content is None:
            continue
        try:
            if path.resolve() in protected:
                raise ValueError("report path would modify a version source")
            with path.open("a", encoding="utf-8", newline="") as report:
                report.write(content)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"cannot write report {path}: {exc}")
    for error in errors:
        error = error.replace("\r", "\\r").replace("\n", "\\n")
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
