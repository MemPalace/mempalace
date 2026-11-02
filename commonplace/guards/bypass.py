"""Detect shell commands that skip or disable git hooks.

A pure library: no I/O, no installers. A PreToolUse hook calls
``match_hook_bypass`` on a shell command and blocks (or warns on) the first hit.

Command-local hook-disabling environment assignments count on recognized git
writes. Persistent disabling exports of HUSKY, LEFTHOOK, HUSKY_SKIP_HOOKS and
SKIP_SIMPLE_GIT_HOOKS count independently; SKIP, LEFTHOOK_EXCLUDE and
PRE_COMMIT_ALLOW_NO_CONFIG exports require a git write. Thus ``HUSKY=0 npm ci``
is allowed, while ``export HUSKY=0`` is reported.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

_MAX_DEPTH = 8
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = ";&|()\n"
_RESERVED = frozenset({"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"})
_REDIR_OPS = frozenset({"<", ">", ">>", ">|", "<>", "&>", "&>>", ">&", "<&", "<<<", "<<", "<<-"})
_WRITE_OPS = frozenset({">", ">|", "&>", ">&"})
_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nohup", "time", "nice", "timeout"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_LEADING_RESERVED = re.compile(r"^(?:(?:\{|!|if|then|else|elif|do|while|until)\s+)+")
_HEREDOC_WORD = re.compile(r"(-?)[ \t]*(['\"]?)([A-Za-z_]\w*)\2")

_WRITE_SUBCOMMANDS = frozenset({"commit", "push", "merge", "am", "rebase"})
_DELETE_CMDS = frozenset({"rm", "unlink", "mv", "shred", "truncate"})
_CHMOD_FLAGS = frozenset(
    {
        "-R",
        "-r",
        "-v",
        "-c",
        "-f",
        "-H",
        "-L",
        "-P",
        "--recursive",
        "--verbose",
        "--changes",
        "--silent",
        "--quiet",
        "--",
    }
)
_CONFIG_FILES = frozenset(
    {
        "lefthook.yml",
        ".lefthook.yml",
        "lefthook-local.yml",
        ".lefthook-local.yml",
        ".pre-commit-config.yaml",
    }
)
_HOOK_DIR = re.compile(r"(^|/)(\.git/hooks|\.husky)(/|$)")
_CHMOD_OCTAL = re.compile(r"[0-7]{3,4}")
_CHMOD_SYMBOLIC = re.compile(r"[ugoa]*[-+=][rwxXst]*(,[ugoa]*[-+=][rwxXst]*)*")

_HOOK_OFF_VALUES = {
    "HUSKY": frozenset({"0"}),
    "LEFTHOOK": frozenset({"0"}),
    "HUSKY_SKIP_HOOKS": frozenset({"1"}),
    "SKIP_SIMPLE_GIT_HOOKS": frozenset({"1"}),
    "PRE_COMMIT_ALLOW_NO_CONFIG": frozenset({"1"}),
}
_HOOK_OFF_NONEMPTY = frozenset({"SKIP", "LEFTHOOK_EXCLUDE"})
# These exports count only when a git write is also present in the command.
_HOOK_OFF_CONDITIONAL = frozenset({"SKIP", "LEFTHOOK_EXCLUDE", "PRE_COMMIT_ALLOW_NO_CONFIG"})

_GUIDANCE = "Fix the failing check, or report the broken hook on your task; do not bypass hooks."


@dataclass(frozen=True)
class Finding:
    """One hook-bypass hit in a shell command."""

    rule: str  # "no-verify" | "hooks-path" | "hook-env" | "hook-files"
    segment: str  # the offending simple command, as text
    reason: str  # one line
    blocked: bool  # True in enforce mode, False in advisory (warn-only) mode

    @property
    def message(self) -> str:
        """Reason plus guidance, prefixed by mode."""
        prefix = "blocked:" if self.blocked else "warning:"
        return f"{prefix} {self.reason}. {_GUIDANCE}"


class _Spec(NamedTuple):
    short_values: str  # short letters that take a value
    attached: str  # short letters whose value is only attached (optional values)
    longs: frozenset[str]  # long options that take a separate value
    reason: str


_NO_VERIFY_SPECS = {
    "commit": _Spec(
        "mFCct",
        "Su",
        frozenset(
            {
                "message",
                "file",
                "author",
                "date",
                "template",
                "reuse-message",
                "reedit-message",
                "fixup",
                "squash",
                "cleanup",
                "trailer",
                "pathspec-from-file",
            }
        ),
        "git commit --no-verify skips the pre-commit and commit-msg hooks",
    ),
    "push": _Spec(
        "o",
        "",
        frozenset({"push-option", "repo", "receive-pack", "exec"}),
        "git push --no-verify skips the pre-push hook",
    ),
    "merge": _Spec(
        "msXF",
        "S",
        frozenset({"message", "strategy", "strategy-option", "file", "into-name", "cleanup"}),
        "git merge --no-verify skips the pre-merge-commit and commit-msg hooks",
    ),
    "am": _Spec(
        "",
        "",
        frozenset({"directory", "exclude", "include", "patch-format", "whitespace", "resolvemsg"}),
        "git am --no-verify skips the applypatch-msg and pre-applypatch hooks",
    ),
    "rebase": _Spec(
        "sXx",
        "C",
        frozenset({"onto", "strategy", "strategy-option", "exec", "empty"}),
        "git rebase --no-verify skips the pre-rebase hook",
    ),
}

_HOOKS_PATH_REASON = (
    "changing core.hooksPath points git at other hooks, so the repo hooks stop running"
)
_HOOK_ENV_REASON = "{} disables the git hook runner"
_HOOK_FILES_REASON = "changing a git hook file or hook config disables that hook"


class _Simple(NamedTuple):
    """One simple command after splitting, redirect removal and wrapper stripping."""

    text: str  # display text, substitutions restored
    argv: tuple[str, ...]  # effective command words, placeholders kept
    env: dict[str, str]  # prefix assignments, including those inherited from a wrapper
    exports: dict[str, str]  # assignments made by an export statement
    writes: tuple[str, ...]  # targets of > redirects
    subs: tuple[tuple[str, str], ...]  # (raw, inner) of each $(), `` and <() in the scan


def match_hook_bypass(command: str, *, enforce: bool = True) -> Finding | None:
    """Return the first finding for a command that skips or disables git hooks, else None."""
    if not isinstance(command, str):
        return None
    simples = _collect(command, {}, 0)
    has_git_write = any(_is_git_write(s) for s in simples)
    for simple in simples:
        hit = _first_hit(simple, has_git_write)
        if hit is not None:
            rule, reason = hit
            return Finding(rule=rule, segment=simple.text, reason=reason, blocked=enforce)
    return None


# --- parsing -----------------------------------------------------------------


def _collect(command: str, inherited: dict[str, str], depth: int) -> list[_Simple]:
    """Flatten a command string into simple commands, recursing into nested shells."""
    if depth > _MAX_DEPTH:
        return []
    segments, subs = _scan(_strip_heredocs(command))
    out: list[_Simple] = []
    for segment in segments:
        for index in _PLACEHOLDER.findall(segment):
            out.extend(_collect(subs[int(index)][1], {}, depth + 1))
        simple = _parse_segment(segment, subs, inherited)
        if simple is None:
            continue
        out.append(simple)
        payload = _shell_payload(simple.argv)
        if payload is not None:
            out.extend(_collect(payload, simple.env, depth + 1))
    return out


def _strip_heredocs(text: str) -> str:
    """Drop heredoc bodies (data, not commands); keep the lines that start them."""
    kept: list[str] = []
    pending: tuple[str, bool] | None = None
    for line in text.split("\n"):
        if pending is not None:
            word, strip_tabs = pending
            probe = line.lstrip("\t") if strip_tabs else line
            if probe == word:
                pending = None
            continue
        kept.append(line)
        pending = _heredoc_start(line)
    return "\n".join(kept)


def _heredoc_start(line: str) -> tuple[str, bool] | None:
    """Return (terminator, strip_tabs) for the first unquoted heredoc on a line."""
    quote = ""
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == quote:
                quote = ""
        elif char == "\\":
            i += 1
        elif char in "'\"":
            quote = char
        elif line.startswith("<<<", i):
            i += 2
        elif line.startswith("<<", i):
            match = _HEREDOC_WORD.match(line, i + 2)
            if match:
                return match.group(3), match.group(1) == "-"
        i += 1
    return None


def _scan(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split on unquoted operators; replace $(), backticks and <() with placeholders."""
    text = text.replace("\x00", "")
    segments: list[str] = []
    subs: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = ""
    i = 0
    n = len(text)

    def flush() -> None:
        segment = "".join(buf).strip()
        if segment:
            segments.append(segment)
        buf.clear()

    while i < n:
        char = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        prev = text[i - 1] if i > 0 else " "
        if quote == "'":
            buf.append(char)
            quote = "" if char == "'" else quote
            i += 1
            continue
        if char == "\\" and nxt:
            if nxt != "\n":
                buf.append(text[i : i + 2])
            i += 2
            continue
        if (char == "$" and nxt == "(") or (not quote and char in "<>" and nxt == "("):
            end = _closing_paren(text, i + 1)
            subs.append((text[i : end + 1], text[i + 2 : end]))
            buf.append(f"\x00{len(subs) - 1}\x00")
            i = end + 1
            continue
        if char == "`":
            end = _closing_backtick(text, i + 1)
            subs.append((text[i : end + 1], text[i + 1 : end]))
            buf.append(f"\x00{len(subs) - 1}\x00")
            i = end + 1
            continue
        if char == "'" and not quote:
            quote = "'"
        elif char == '"':
            quote = "" if quote else '"'
        elif not quote and char == "#" and prev in " \t\n;&|()":
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        elif not quote and char in _SEPARATORS and not _is_redirect_amp(text, i):
            flush()
            i += 1
            continue
        buf.append(char)
        i += 1
    flush()
    return segments, subs


def _is_redirect_amp(text: str, i: int) -> bool:
    """True for the & in &> and >& (a redirect, not a separator)."""
    return text[i] == "&" and (text[i + 1 : i + 2] == ">" or text[i - 1 : i] == ">")


def _closing_paren(text: str, open_index: int) -> int:
    """Index of the ')' that closes the '(' at open_index, or len(text) if unclosed."""
    depth = 0
    quote = ""
    j = open_index
    while j < len(text):
        char = text[j]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            j += 1
        elif char == "'":
            quote = "'"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(text)


def _closing_backtick(text: str, start: int) -> int:
    """Index of the next unescaped backtick at or after start, or len(text)."""
    j = start
    while j < len(text) and text[j] != "`":
        j += 2 if text[j] == "\\" else 1
    return min(j, len(text))


def _words(segment: str) -> list[str]:
    """Shell words of one segment; punctuation (>, >>, >&) comes out as its own token."""
    lexer = shlex.shlex(segment, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to a plain split
        return [word.strip("'\"") for word in segment.split()]


def _parse_segment(
    segment: str, subs: list[tuple[str, str]], inherited: dict[str, str]
) -> _Simple | None:
    """Build a _Simple from one segment: drop redirects, apply wrappers, collect env."""
    words = _words(segment)
    while words and words[0] in _RESERVED:
        words = words[1:]
    while words and words[-1] == "}":
        words = words[:-1]
    if not words:
        return None
    argv, writes = _split_redirects(words)
    env = dict(inherited)
    while argv:
        if _ASSIGN.match(argv[0]):
            name, _, value = argv[0].partition("=")
            env[name] = value
            argv = argv[1:]
        elif _base(argv[0]) in _WRAPPERS:
            argv = _unwrap(_base(argv[0]), argv[1:])
        else:
            break
    exports = _export_assignments(argv)
    display = _PLACEHOLDER.sub(lambda m: subs[int(m.group(1))][0], segment).strip()
    display = _LEADING_RESERVED.sub("", display)
    return _Simple(
        text=display,
        argv=tuple(argv),
        env=env,
        exports=exports,
        writes=tuple(writes),
        subs=tuple(subs),
    )


def _split_redirects(words: list[str]) -> tuple[list[str], list[str]]:
    """Remove redirections from words; return (argv, targets of write redirects)."""
    argv: list[str] = []
    writes: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in _REDIR_OPS:
            target = words[i + 1] if i + 1 < len(words) else ""
            if word in _WRITE_OPS and target and not (target.isdigit() or target == "-"):
                writes.append(target)
            i += 2
            continue
        if word.isdigit() and i + 1 < len(words) and words[i + 1] in _REDIR_OPS:
            i += 1  # file descriptor number before a redirect
            continue
        argv.append(word)
        i += 1
    return argv, writes


def _skip_options(args: list[str], value_opts: Iterable[str] = ()) -> list[str]:
    """Skip leading options of a wrapper; options in value_opts also take the next word."""
    values = frozenset(value_opts)
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        if args[i] == "--":
            i += 1
            break
        i += 2 if args[i] in values else 1
    return args[i:]


def _unwrap(name: str, args: list[str]) -> list[str]:
    """Strip one wrapper's own options; env assignments are handled by the caller."""
    if name == "sudo":
        return _skip_options(args, ("-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U"))
    if name == "env":
        return _skip_options(args, ("-u", "-C", "--unset", "--chdir"))
    if name == "exec":
        return _skip_options(args, ("-a",))
    if name == "time":
        return _skip_options(args, ("-f", "-o"))
    if name == "nice":
        return _skip_options(args, ("-n", "--adjustment"))
    if name == "timeout":
        rest = _skip_options(args, ("-s", "-k", "--signal", "--kill-after"))
        return rest[1:]  # the DURATION operand
    return _skip_options(args)


def _export_assignments(argv: list[str]) -> dict[str, str]:
    if not argv or argv[0] != "export":
        return {}
    out: dict[str, str] = {}
    for word in argv[1:]:
        if _ASSIGN.match(word):
            name, _, value = word.partition("=")
            out[name] = value
    return out


def _shell_payload(argv: tuple[str, ...]) -> str | None:
    """The command string run by `bash -c ...` or `eval ...`, if any."""
    if not argv:
        return None
    name = _base(argv[0])
    if name == "eval":
        return " ".join(argv[1:]) or None
    if name not in _SHELLS:
        return None
    args = list(argv[1:])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            i += 1
        elif arg in ("-o", "+o", "-O", "+O"):
            i += 2
        elif arg[:1] in ("-", "+") and len(arg) > 1:
            if "c" in arg[1:]:
                return args[i + 1] if i + 1 < len(args) else None
            i += 1
        else:
            return None
    return None


def _base(word: str) -> str:
    """Lower-case basename without a .exe suffix."""
    return re.split(r"[\\/]", word)[-1].lower().removesuffix(".exe")


# --- git commands ------------------------------------------------------------


class _GitCall(NamedTuple):
    config_keys: list[str]  # keys set with -c or --config-env, lower-cased
    sub: str | None  # subcommand
    args: list[str]  # words after the subcommand


_GIT_VALUE_OPTS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"})


def _git(argv: tuple[str, ...]) -> _GitCall | None:
    """Parse a git invocation: global options, then subcommand and its arguments."""
    if not argv or _base(argv[0]) != "git":
        return None
    keys: list[str] = []
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        arg = argv[i]
        if arg in _GIT_VALUE_OPTS:
            i += 2
            continue
        if arg in ("-c", "--config-env"):
            if i + 1 < len(argv):
                keys.append(_config_key(argv[i + 1]))
            i += 2
            continue
        if arg.startswith("--config-env="):
            keys.append(_config_key(arg.split("=", 1)[1]))
        elif arg.startswith("-c") and not arg.startswith("--"):
            keys.append(_config_key(arg[2:]))
        i += 1
    sub = argv[i] if i < len(argv) else None
    return _GitCall(keys, sub, list(argv[i + 1 :]))


def _config_key(spec: str) -> str:
    return spec.split("=", 1)[0].strip().lower()


def _is_git_write(simple: _Simple) -> bool:
    call = _git(simple.argv)
    return call is not None and call.sub in _WRITE_SUBCOMMANDS


def _has_no_verify(sub: str, args: list[str]) -> bool:
    """True if the git subcommand's arguments include --no-verify (or -n for commit)."""
    spec = _NO_VERIFY_SPECS[sub]
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        if arg.startswith("--"):
            name, eq, _ = arg[2:].partition("=")
            if eq:
                continue
            if _is_no_verify(name):
                return True
            if _long_takes_value(name, spec.longs):
                i += 1
        elif arg.startswith("-") and len(arg) > 1:
            if _short_cluster_hit(arg, sub, spec):
                return True
            if _cluster_takes_next(arg, spec):
                i += 1
    return False


def _is_no_verify(name: str) -> bool:
    """--no-verify and the abbreviations git accepts (down to --no-ver)."""
    return len(name) >= 6 and "no-verify".startswith(name)


def _long_takes_value(name: str, longs: frozenset[str]) -> bool:
    """A long option takes a separate value; unique prefixes count (git accepts them)."""
    if name in longs:
        return True
    if len(name) < 3:
        return False
    matches = [option for option in longs if option.startswith(name)]
    return len(matches) == 1


def _short_cluster_hit(arg: str, sub: str, spec: _Spec) -> bool:
    """True if a short-option cluster has -n before any value-taking letter (commit only)."""
    if sub != "commit":
        return False
    for letter in arg[1:]:
        if letter in spec.short_values or letter in spec.attached:
            return False
        if letter == "n":
            return True
    return False


def _cluster_takes_next(arg: str, spec: _Spec) -> bool:
    """True if the short cluster ends in a value letter, so the next word is its value."""
    for position, letter in enumerate(arg[1:], start=1):
        if letter in spec.short_values:
            return position == len(arg) - 1
        if letter in spec.attached:
            return False
    return False


def _config_sets_hookspath(args: list[str]) -> bool:
    """True if `git config ...` writes core.hooksPath (reads are allowed)."""
    flags, positionals = _config_parts(args)
    key: str | None
    if any(flag in _CONFIG_WRITE_FLAGS for flag in flags):
        key = positionals[0] if positionals else None
    elif any(flag in _CONFIG_READ_FLAGS for flag in flags):
        return False
    elif positionals and positionals[0] in _CONFIG_READ_VERBS:
        return False
    elif positionals and positionals[0] in _CONFIG_WRITE_VERBS:
        key = positionals[1] if len(positionals) > 1 else None
    elif len(positionals) >= 2:
        key = positionals[0]  # legacy `git config <key> <value>`
    else:
        return False
    return key is not None and key.lower() == "core.hookspath"


_CONFIG_VALUE_OPTS = frozenset({"-f", "--file", "--blob", "--type", "--default", "--comment"})
_CONFIG_READ_FLAGS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "-l",
        "--get-color",
        "--get-colorbool",
    }
)
_CONFIG_WRITE_FLAGS = frozenset(
    {"--unset", "--unset-all", "--replace-all", "--add", "--rename-section", "--remove-section"}
)
_CONFIG_READ_VERBS = frozenset({"get", "get-all", "get-regexp", "list"})
_CONFIG_WRITE_VERBS = frozenset({"set", "unset", "unset-all", "add", "replace-all"})


def _config_parts(args: list[str]) -> tuple[list[str], list[str]]:
    """Split git config arguments into option names and positional operands."""
    flags: list[str] = []
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals.extend(args[i + 1 :])
            break
        if arg.startswith("-") and len(arg) > 1:
            name = arg.split("=", 1)[0]
            flags.append(name)
            if name in _CONFIG_VALUE_OPTS and "=" not in arg:
                i += 1
        else:
            positionals.append(arg)
        i += 1
    return flags, positionals


def _env_sets_hookspath(env: dict[str, str]) -> bool:
    for name, value in env.items():
        if name == "GIT_CONFIG_PARAMETERS" and "hookspath" in value.lower():
            return True
        if re.fullmatch(r"GIT_CONFIG_KEY_\d+", name) and value.strip("'\"").lower() == (
            "core.hookspath"
        ):
            return True
    return False


# --- hook-disabling environment ----------------------------------------------


def _disables_hooks(name: str, value: str) -> bool:
    if name in _HOOK_OFF_VALUES:
        return value in _HOOK_OFF_VALUES[name]
    return name in _HOOK_OFF_NONEMPTY and value != ""


# --- hook files --------------------------------------------------------------


def _is_hook_path(path: str, subs: tuple[tuple[str, str], ...]) -> bool:
    """True for .git/hooks, .husky, the lefthook and pre-commit config files (not *.sample)."""
    expanded = _PLACEHOLDER.sub(lambda m: _subst_text(subs[int(m.group(1))][1]), path)
    normalized = re.sub(r"^(\./)+", "", expanded.replace("\\", "/")).rstrip("/")
    if normalized.endswith(".sample"):
        return False
    if normalized.rsplit("/", 1)[-1] in _CONFIG_FILES:
        return True
    return _HOOK_DIR.search(normalized) is not None


def _subst_text(inner: str) -> str:
    """Stand-in text for a $(git rev-parse ...) substitution that names the hooks dir."""
    if "rev-parse" in inner:
        if "hooks" in inner:
            return ".git/hooks"
        if "git-dir" in inner or "git-common-dir" in inner:
            return ".git"
    return "\x01"  # unknown output: matches nothing


def _operands(args: list[str]) -> list[str]:
    out: list[str] = []
    ends_options = False
    for arg in args:
        if not ends_options and arg == "--":
            ends_options = True
        elif not ends_options and arg.startswith("-") and len(arg) > 1:
            continue
        else:
            out.append(arg)
    return out


def _chmod_parts(args: list[str]) -> tuple[str | None, list[str]]:
    rest = [arg for arg in args if arg not in _CHMOD_FLAGS]
    if rest and (_CHMOD_OCTAL.fullmatch(rest[0]) or _CHMOD_SYMBOLIC.fullmatch(rest[0])):
        return rest[0], rest[1:]
    return None, []


def _mode_drops_exec(mode: str) -> bool:
    """True if a chmod mode removes the execute bit (and never adds it)."""
    if _CHMOD_OCTAL.fullmatch(mode):
        return int(mode, 8) & 0o111 == 0
    drops = False
    for clause in mode.split(","):
        match = re.fullmatch(r"[ugoa]*([-+=])([rwxXst]*)", clause)
        if not match:
            continue
        op, perms = match.groups()
        if op == "+" and "x" in perms:
            return False
        if (op == "-" and "x" in perms) or (op == "=" and "x" not in perms):
            drops = True
    return drops


def _uninstalls_hooks(argv: tuple[str, ...]) -> bool:
    return any(
        _base(word) in ("pre-commit", "lefthook") and following == "uninstall"
        for word, following in zip(argv, argv[1:])
    )


def _find_deletes(args: list[str]) -> list[str]:
    """Start paths of a ``find`` that deletes what it finds (-delete or -exec rm)."""
    deletes = "-delete" in args or any(
        flag in ("-exec", "-execdir", "-ok", "-okdir") and _base(cmd) in _DELETE_CMDS
        for flag, cmd in zip(args, args[1:])
    )
    if not deletes:
        return []
    paths: list[str] = []
    for arg in args:
        if arg.startswith("-") or arg in ("(", "!"):
            break
        paths.append(arg)
    return paths


def _removes_hook_files(simple: _Simple) -> bool:
    argv = simple.argv
    if _uninstalls_hooks(argv):
        return True
    if not argv:
        return False
    name = _base(argv[0])
    targets: list[str] = []
    if name in _DELETE_CMDS:
        targets = _operands(list(argv[1:]))
    elif name == "find":
        targets = _find_deletes(list(argv[1:]))
    elif name == "chmod":
        mode, paths = _chmod_parts(list(argv[1:]))
        if mode is not None and _mode_drops_exec(mode):
            targets = paths
    else:
        call = _git(argv)
        if call is not None and call.sub == "rm":
            targets = _operands(call.args)
    return any(_is_hook_path(target, simple.subs) for target in targets)


# --- rules -------------------------------------------------------------------


def _first_hit(simple: _Simple, has_git_write: bool) -> tuple[str, str] | None:
    for hit in (
        _no_verify_hit(simple),
        _hooks_path_hit(simple),
        _hook_env_hit(simple, has_git_write),
    ):
        if hit is not None:
            return hit
    if _removes_hook_files(simple) or any(_is_hook_path(t, simple.subs) for t in simple.writes):
        return "hook-files", _HOOK_FILES_REASON
    return None


def _no_verify_hit(simple: _Simple) -> tuple[str, str] | None:
    call = _git(simple.argv)
    if call is None or call.sub not in _NO_VERIFY_SPECS:
        return None
    if _has_no_verify(call.sub, call.args):
        return "no-verify", _NO_VERIFY_SPECS[call.sub].reason
    return None


def _hooks_path_hit(simple: _Simple) -> tuple[str, str] | None:
    call = _git(simple.argv)
    if call is not None and (
        "core.hookspath" in call.config_keys
        or (call.sub == "config" and _config_sets_hookspath(call.args))
    ):
        return "hooks-path", _HOOKS_PATH_REASON
    if _env_sets_hookspath({**simple.env, **simple.exports}):
        return "hooks-path", _HOOKS_PATH_REASON
    return None


def _hook_env_hit(simple: _Simple, has_git_write: bool) -> tuple[str, str] | None:
    if _is_git_write(simple):
        for name, value in simple.env.items():
            if _disables_hooks(name, value):
                return "hook-env", _HOOK_ENV_REASON.format(name)
    for name, value in simple.exports.items():
        conditional = name in _HOOK_OFF_CONDITIONAL
        if _disables_hooks(name, value) and (has_git_write or not conditional):
            return "hook-env", _HOOK_ENV_REASON.format(f"exported {name}")
    return None
