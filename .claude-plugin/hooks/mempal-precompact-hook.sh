#!/bin/bash
# MemPalace PreCompact Hook — thin wrapper calling Python CLI
# All logic lives in mempalace.hooks_cli for cross-harness extensibility
#
# Dispatch guard: a stale `mempalace` binary on PATH (e.g. an old uv/pipx
# tool install) must not shadow the plugin's bundled package. If the PATH
# binary reports a version older than the plugin's pyproject.toml, prefer
# `python -m mempalace` with the plugin root prepended to PYTHONPATH, and
# only fall back to the stale binary (with a warning) when no python
# runner is available. Uses bash builtins only — hook environments may
# carry a minimal PATH.

_plugin_root() {
  # builtins only — no dirname on a minimal hook PATH.
  local src="${BASH_SOURCE[0]}" dir
  case "$src" in
    */*) dir="${src%/*}" ;;
    *) dir="." ;;
  esac
  dir="$(cd "$dir/../.." 2>/dev/null && pwd)" || return 1
  printf '%s' "$dir"
}

_plugin_version() {
  # First `version = "X.Y.Z"` line in pyproject.toml, builtins only.
  local root="$1" line
  [ -f "$root/pyproject.toml" ] || return 1
  while IFS= read -r line; do
    if [[ $line =~ ^[[:space:]]*version[[:space:]]*=[[:space:]]*\"([0-9]+(\.[0-9]+)*)\" ]]; then
      printf '%s' "${BASH_REMATCH[1]}"
      return 0
    fi
  done < "$root/pyproject.toml"
  return 1
}

_binary_version() {
  # Parse "MemPalace X.Y.Z"; fall back to the first dotted-numeric run so
  # an unparsable banner fails open (binary treated as current).
  local out
  out="$(mempalace --version </dev/null 2>/dev/null)" || return 1
  if [[ $out =~ [Mm]em[Pp]alace[[:space:]]+v?([0-9]+(\.[0-9]+)+) ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi
  if [[ $out =~ ([0-9]+(\.[0-9]+)+) ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

_version_lt() {
  # _version_lt A B → exit 0 (true) when dotted version A < B.
  local IFS=.
  local -a av bv
  local i a b
  read -r -a av <<< "$1"
  read -r -a bv <<< "$2"
  for ((i = 0; i < ${#av[@]} || i < ${#bv[@]}; i++)); do
    a=$((10#${av[i]:-0}))
    b=$((10#${bv[i]:-0}))
    ((a < b)) && return 0
    ((a > b)) && return 1
  done
  return 1
}

run_mempalace_hook() {
  local plugin_root expected="" bin_ver="" stale_binary=""
  plugin_root="$(_plugin_root)" || plugin_root=""
  if [ -n "$plugin_root" ]; then
    expected="$(_plugin_version "$plugin_root")" || expected=""
  fi

  if command -v mempalace >/dev/null 2>&1; then
    bin_ver="$(_binary_version)" || bin_ver=""
    if [ -z "$expected" ] || [ -z "$bin_ver" ] || ! _version_lt "$bin_ver" "$expected"; then
      mempalace hook run "$@"
      return $?
    fi
    stale_binary="$bin_ver"
    echo "MemPalace hook warning: PATH mempalace v$bin_ver is older than plugin v$expected; preferring bundled module (fix: uv tool upgrade mempalace)" >&2
  fi

  # Prepend the plugin root so the bundled package wins over any stale
  # site-packages copy.
  local pp="${PYTHONPATH:-}"
  if [ -n "$plugin_root" ] && [ -d "$plugin_root/mempalace" ]; then
    pp="$plugin_root${pp:+:$pp}"
  fi

  # Probe the modules the -m entry actually loads: cli.main() imports
  # miner unconditionally while building its arg parser, so `python -m
  # mempalace hook run` needs the heavy backends importable in-process.
  # A bare `import mempalace` succeeds even when they are missing and the
  # hook run would crash.
  local probe="import mempalace.cli, mempalace.hooks_cli, mempalace.miner"

  if command -v python3 >/dev/null 2>&1 && PYTHONPATH="$pp" python3 -c "$probe" >/dev/null 2>&1; then
    PYTHONPATH="$pp" python3 -m mempalace hook run "$@"
    return $?
  fi

  if command -v python >/dev/null 2>&1 && PYTHONPATH="$pp" python -c "$probe" >/dev/null 2>&1; then
    PYTHONPATH="$pp" python -m mempalace hook run "$@"
    return $?
  fi

  if [ -n "$stale_binary" ]; then
    echo "MemPalace hook warning: no python runner with mempalace available; falling back to stale mempalace v$stale_binary" >&2
    mempalace hook run "$@"
    return $?
  fi

  echo "MemPalace hook error: could not find a runnable mempalace command or module" >&2
  return 1
}

run_mempalace_hook --hook precompact --harness claude-code
