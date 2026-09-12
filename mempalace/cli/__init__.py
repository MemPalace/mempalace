#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Three ways to ingest:
  Projects:      mempalace mine ~/projects/my_app                  (code, docs, notes)
  Conversations: mempalace mine <convo-dir> --mode convos          (Claude Code, Claude.ai, ChatGPT, Slack exports)
  Documents:     mempalace mine <docs-dir> --mode extract          (PDF, DOCX, PPTX, XLSX, RTF, EPUB — requires mempalace[extract])
  Adapters:      mempalace mine <source> --source <adapter-name>  (registered source adapters)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace mine <dir> --mode extract   Mine binary office documents (PDF/DOCX/etc.)
    mempalace mine <source> --source NAME Mine through a registered source adapter
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace task create ...             Create a complete agent handoff
    mempalace task launch ...             Run a stored task headlessly
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

# The public import path remains ``mempalace.cli``. Implementation is split
# across this package so PRs can target a command file instead of one
# 4 000-line module. Fragment files are executed into this package's globals
# (the same namespace as the historical module), which preserves
# ``from mempalace.cli import main, cmd_mine, ...`` and test monkeypatches.

import argparse
import contextlib
import os
import shlex
import sys
import warnings
from pathlib import Path

from ..config import MempalaceConfig
from ..corpus_origin import detect_origin_heuristic, detect_origin_llm
from ..llm_client import LLMError, get_provider
from ..version import __version__


_MEMPALACE_PROJECT_FILES = ("mempalace.yaml", "entities.json")

# Pass 0 corpus-origin sampling caps. Tier 1 reads FULL file content (no
# front-bias sampling) but bounds total memory on enormous corpora. Tier 2
# trims to a smaller view because LLM context windows are finite.
_PASS_ZERO_MAX_FILES = 30
_PASS_ZERO_PER_FILE_CAP = 100_000  # 100KB per file is generous for prose
_PASS_ZERO_TOTAL_CAP = 5_000_000  # 5MB total ceiling — bounds memory
_PASS_ZERO_LLM_PER_SAMPLE = 2_000  # for Tier 2 LLM call only
_PASS_ZERO_LLM_MAX_SAMPLES = 20  # caps the LLM-tier sample count
_EXPLICIT_BACKEND_ENV = "MEMPALACE_BACKEND_EXPLICIT"

# Keep parser construction lightweight for --version and hook commands.
# This mirrors miner.MAX_CHUNKS_PER_FILE without importing miner here;
# importing miner pulls in Chroma dependencies before argparse can handle
# lightweight exits such as --version.
_CLI_MAX_CHUNKS_PER_FILE_DEFAULT = 50_000

_FRAGMENT_DIR = Path(__file__).resolve().parent
# Load order is a dependency sequence, not a catalog: later files use names
# defined earlier (_common before commands, commands before parser). Do not
# reorder this tuple.
_FRAGMENTS = (
    "_common.py",
    "cmd_init.py",
    "_hub.py",
    "cmd_mine.py",
    "cmd_sync.py",
    "cmd_query.py",
    "cmd_update.py",
    "cmd_coord.py",
    "cmd_repair.py",
    "cmd_serve.py",
    "parser.py",
)


def _exec_fragment(filename: str) -> None:
    path = _FRAGMENT_DIR / filename
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), globals())


for _fragment in _FRAGMENTS:
    _exec_fragment(_fragment)
del _fragment
