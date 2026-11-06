"""Every gateway creator of files under the data directory passes the admission gate.

Pure AST over the gateway's creator modules: each call that creates, deletes or
renames a file or directory, or opens a database that may not exist yet, must
sit lexically inside ``with GATE.creating(...)`` or inside a function on the
allowlist (worker launch runs under the backup pause; ``ControlDB.__init__`` runs
at startup). This file imports nothing from the code under test.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "commonplace"

SITE_MODULES = (
    "gateway.py",
    "workers.py",
    "control_db.py",
    "workspace_store.py",
    "handoff/wiring.py",
    "handoff/launchers.py",
    "handoff/manual.py",
    "console/store.py",
    "chat/store.py",
    "boards/store.py",
    "rules/store.py",
    "runner/jobs.py",
    "skills/store.py",
)

# By function (qualified name); nested functions inherit the exemption.
ALLOWLIST = frozenset(
    {
        "SubprocessLauncher.start",  # worker launch: under the backup pause
        "_share_model_cache",  # worker launch: under the backup pause
        "worker_config",  # worker launch: under the backup pause
        "ControlDB.__init__",  # gateway startup, before any backup can run
    }
)

ALWAYS = frozenset(
    {
        "mkdir",
        "makedirs",
        "rmdir",
        "removedirs",
        "unlink",
        "rmtree",
        "mkstemp",
        "mkdtemp",
        "symlink_to",
        "hardlink_to",
        "symlink",
        "link",
        "touch",
        "write_text",
        "write_bytes",
        "renames",
        # Known helpers that create files (handoff.hook, mempalace.fsio).
        "_atomic_text",
        "_write_json",
        "_private_dir",
        "atomic_write_json",
    }
)
SHUTIL_ONLY = frozenset({"move", "copy", "copy2", "copyfile", "copytree"})
NOT_RECEIVERS = frozenset({"dataclasses", "copy"})


def _receiver(func):
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _literal_mode(node, position):
    for keyword in node.keywords:
        if keyword.arg == "mode":
            value = keyword.value
            return value.value if isinstance(value, ast.Constant) else None
    if len(node.args) > position:
        value = node.args[position]
        return value.value if isinstance(value, ast.Constant) else None
    return None


def is_primitive(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return False
    receiver = _receiver(func)
    if receiver in NOT_RECEIVERS:
        return False
    if name in ALWAYS:
        return True
    if name in SHUTIL_ONLY:
        return receiver == "shutil" or isinstance(func, ast.Name)
    if name in ("replace", "rename"):
        # os.replace(a, b) / Path.replace(b); not str.replace(a, b) or datetime.replace(k=v).
        if receiver == "os":
            return True
        return isinstance(func, ast.Attribute) and len(node.args) == 1 and not node.keywords
    if name == "remove":
        return receiver == "os"
    if name == "connect":
        # sqlite3.connect(path) or an imported connect(path); not socket .connect().
        return bool(node.args) or any(k.arg == "database" for k in node.keywords)
    if name == "open":
        if receiver == "os":
            return "O_CREAT" in ast.unparse(node)
        if isinstance(func, ast.Name):
            mode = _literal_mode(node, 1)
        else:
            mode = _literal_mode(node, 0)
        return isinstance(mode, str) and any(flag in mode for flag in "wax+")
    return False


def _is_gate(item: ast.withitem) -> bool:
    call = item.context_expr
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return False
    if call.func.attr != "creating":
        return False
    owner = call.func.value
    return (isinstance(owner, ast.Name) and owner.id == "GATE") or (
        isinstance(owner, ast.Attribute) and owner.attr == "GATE"
    )


def _imports_gate(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("admission"):
            if any(alias.name == "GATE" for alias in node.names):
                return True
    return False


def unwrapped_creators(relative: str) -> list:
    source = (ROOT / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = []
    uses_gate = False

    def visit(node, scope, gated):
        nonlocal uses_gate
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = scope + (node.name,)
            # A nested definition does not run under the enclosing with-block.
            gated = False
        elif isinstance(node, ast.Lambda):
            gated = False
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            if any(_is_gate(item) for item in node.items):
                uses_gate = True
                for item in node.items:
                    visit(item, scope, gated)
                for child in node.body:
                    visit(child, scope, True)
                return
        if isinstance(node, ast.Call) and is_primitive(node) and not gated:
            qualified = ".".join(name for name in scope)
            allowed = any(".".join(scope[: index + 1]) in ALLOWLIST for index in range(len(scope)))
            if not allowed:
                found.append(f"{relative}:{node.lineno} {qualified or '<module>'}")
        for child in ast.iter_child_nodes(node):
            visit(child, scope, gated)

    visit(tree, (), False)
    if uses_gate and not _imports_gate(tree):
        found.append(f"{relative}: GATE is not imported from commonplace.admission")
    return found


def test_creator_registry():
    missing = [name for name in SITE_MODULES if not (ROOT / name).is_file()]
    assert not missing, f"SETUP: site modules missing: {missing}"
    unwrapped = [entry for name in SITE_MODULES for entry in unwrapped_creators(name)]
    assert not unwrapped, f"RED238: unwrapped creators: {unwrapped}"


def test_primitive_detection_is_not_vacuous():
    """The detector sees each primitive kind and ignores look-alikes."""
    hits = """
import os, shutil, sqlite3, tempfile
from pathlib import Path
def f(p, conn):
    p.mkdir(); os.makedirs(p); os.replace(a, b); p.replace(b); p.rename(b); p.unlink()
    os.remove(p); shutil.rmtree(p); shutil.move(a, b); shutil.copy2(a, b); tempfile.mkstemp()
    p.symlink_to(q); p.touch(); p.write_text("x"); p.write_bytes(b"x"); open(p, "w")
    p.open("xb"); os.open(p, os.O_CREAT | os.O_RDWR); sqlite3.connect(p); connect(p); os.rmdir(p)
"""
    misses = """
import copy, dataclasses, os
def f(p, items, request):
    "a".replace("a", "b"); now.replace(microsecond=0); dataclasses.replace(x, y=1)
    items.remove(1); os.environ.copy(); copy.copy(x); super().connect(); opener.open(request)
    open(p); open(p, "rb"); p.open(encoding="utf-8"); os.open(p, os.O_RDONLY); os.fdopen(d, "wb")
"""
    count = lambda text: sum(  # noqa: E731
        is_primitive(node) for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Call)
    )
    assert count(hits) == 21
    assert count(misses) == 0
