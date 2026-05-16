#!/usr/bin/env bash
set -euo pipefail

EXTENSION_NAME="pg_sorted_heap"
REPO_URL=""
BUILD_DIR=""
PG_CONFIG_BIN="${PG_CONFIG:-}"
PSQL_BIN="${PSQL:-}"
DSN="${MEMPALACE_POSTGRES_DSN:-}"
SOURCE_DIR=""
USE_SUDO=0
FORCE_BUILD=0
CREATE_EXTENSION=1

usage() {
  cat <<'EOF'
Install a PostgreSQL extension for the MemPalace PostgreSQL backend.

Usage:
  scripts/install_pg_backend.sh [options]

Options:
  --extension NAME      Extension to install: pg_sorted_heap or vector. Default: pg_sorted_heap.
  --dsn DSN             PostgreSQL DSN where CREATE EXTENSION should run.
  --pg-config PATH      pg_config for the PostgreSQL version to target.
  --psql PATH           psql binary to use for CREATE EXTENSION.
  --source DIR          Build from an existing extension source checkout.
  --repo URL            Git repo to clone when --source is not supplied.
  --build-dir DIR       Clone/build directory. Default: $TMPDIR/mempalace-<extension>-build.
  --sudo                Run make install through sudo.
  --no-create-extension Install files only; do not run CREATE EXTENSION.
  --force-build         Build/install even if the extension already appears installed.
  -h, --help            Show this help.

Environment:
  PG_CONFIG                  Alternative way to set --pg-config.
  PSQL                       Alternative way to set --psql.
  MEMPALACE_POSTGRES_DSN     Alternative way to set --dsn.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --extension)
      EXTENSION_NAME="${2:?--extension requires a value}"
      shift 2
      ;;
    --dsn)
      DSN="${2:?--dsn requires a value}"
      shift 2
      ;;
    --pg-config)
      PG_CONFIG_BIN="${2:?--pg-config requires a value}"
      shift 2
      ;;
    --psql)
      PSQL_BIN="${2:?--psql requires a value}"
      shift 2
      ;;
    --source)
      SOURCE_DIR="${2:?--source requires a value}"
      shift 2
      ;;
    --repo)
      REPO_URL="${2:?--repo requires a value}"
      shift 2
      ;;
    --build-dir)
      BUILD_DIR="${2:?--build-dir requires a value}"
      shift 2
      ;;
    --sudo)
      USE_SUDO=1
      shift
      ;;
    --no-create-extension)
      CREATE_EXTENSION=0
      shift
      ;;
    --force-build)
      FORCE_BUILD=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$EXTENSION_NAME" in
  pg_sorted_heap)
    DEFAULT_REPO_URL="https://github.com/skuznetsov/pg_sorted_heap.git"
    ;;
  vector)
    DEFAULT_REPO_URL="https://github.com/pgvector/pgvector.git"
    ;;
  *)
    echo "Unsupported extension: $EXTENSION_NAME" >&2
    echo "Expected one of: pg_sorted_heap, vector" >&2
    exit 2
    ;;
esac

if [[ -z "$REPO_URL" ]]; then
  REPO_URL="$DEFAULT_REPO_URL"
fi
if [[ -z "$BUILD_DIR" ]]; then
  BUILD_DIR="${TMPDIR:-/tmp}/mempalace-${EXTENSION_NAME}-build"
fi

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

if [[ -z "$PG_CONFIG_BIN" ]]; then
  PG_CONFIG_BIN="$(command -v pg_config || true)"
fi
if [[ -z "$PG_CONFIG_BIN" || ! -x "$PG_CONFIG_BIN" ]]; then
  echo "Could not find pg_config. Pass --pg-config /path/to/pg_config." >&2
  exit 1
fi

if [[ -z "$PSQL_BIN" ]]; then
  PSQL_BIN="$(command -v psql || true)"
fi

SHARE_DIR="$("$PG_CONFIG_BIN" --sharedir)"
PKG_LIB_DIR="$("$PG_CONFIG_BIN" --pkglibdir)"
CONTROL_FILE="$SHARE_DIR/extension/$EXTENSION_NAME.control"
case "$(uname -s)" in
  Darwin) LIB_SUFFIX=".dylib" ;;
  *) LIB_SUFFIX=".so" ;;
esac
LIB_FILE="$PKG_LIB_DIR/$EXTENSION_NAME$LIB_SUFFIX"

echo "extension: $EXTENSION_NAME"
echo "Target PostgreSQL: $("$PG_CONFIG_BIN" --version)"
echo "pg_config: $PG_CONFIG_BIN"
echo "extension dir: $SHARE_DIR/extension"
echo "library dir: $PKG_LIB_DIR"

if [[ "$FORCE_BUILD" -eq 0 && -f "$CONTROL_FILE" && -e "$LIB_FILE" ]]; then
  echo "$EXTENSION_NAME already appears installed for this PostgreSQL."
else
  need_cmd make
  if [[ -n "$SOURCE_DIR" ]]; then
    if [[ ! -f "$SOURCE_DIR/Makefile" ]]; then
      echo "--source does not look like a PostgreSQL extension checkout: $SOURCE_DIR" >&2
      exit 1
    fi
    BUILD_PATH="$SOURCE_DIR"
  else
    need_cmd git
    BUILD_PATH="$BUILD_DIR"
    if [[ -d "$BUILD_PATH/.git" ]]; then
      echo "Updating existing checkout: $BUILD_PATH"
      git -C "$BUILD_PATH" pull --ff-only
    elif [[ -e "$BUILD_PATH" ]]; then
      echo "Build directory exists but is not a git checkout: $BUILD_PATH" >&2
      echo "Pass --build-dir to use another directory, or remove it manually." >&2
      exit 1
    else
      echo "Cloning $REPO_URL into $BUILD_PATH"
      git clone --depth 1 "$REPO_URL" "$BUILD_PATH"
    fi
  fi

  echo "Building $EXTENSION_NAME"
  make -C "$BUILD_PATH" PG_CONFIG="$PG_CONFIG_BIN"

  echo "Installing $EXTENSION_NAME"
  if [[ "$USE_SUDO" -eq 1 ]]; then
    sudo make -C "$BUILD_PATH" install PG_CONFIG="$PG_CONFIG_BIN"
  else
    make -C "$BUILD_PATH" install PG_CONFIG="$PG_CONFIG_BIN"
  fi

  if [[ ! -f "$CONTROL_FILE" || ! -e "$LIB_FILE" ]]; then
    echo "Install finished, but expected files were not found." >&2
    echo "Missing? control=$CONTROL_FILE lib=$LIB_FILE" >&2
    exit 1
  fi
fi

if [[ "$CREATE_EXTENSION" -eq 1 ]]; then
  if [[ -z "$DSN" ]]; then
    echo "No DSN supplied; skipping CREATE EXTENSION."
    echo "Run later: psql <dsn> -c 'CREATE EXTENSION IF NOT EXISTS $EXTENSION_NAME;'"
  else
    if [[ -z "$PSQL_BIN" || ! -x "$PSQL_BIN" ]]; then
      echo "Could not find psql. Pass --psql /path/to/psql or use --no-create-extension." >&2
      exit 1
    fi
    echo "Creating extension in target database"
    "$PSQL_BIN" "$DSN" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS $EXTENSION_NAME;"
    "$PSQL_BIN" "$DSN" -v ON_ERROR_STOP=1 -Atc \
      "SELECT extname FROM pg_extension WHERE extname = '$EXTENSION_NAME';"
  fi
fi

cat <<'EOF'

MemPalace PostgreSQL backend environment:
  export MEMPALACE_BACKEND=postgres
  export MEMPALACE_POSTGRES_DSN="<your postgres dsn>"
EOF
