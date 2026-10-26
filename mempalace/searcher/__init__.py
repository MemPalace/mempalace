#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Hybrid search: BM25 keyword matching + vector semantic similarity. The
drawer query is the floor — always runs — and closet hits add a rank-based
boost when they agree. Closets are a ranking *signal*, never a gate, so
weak closets (regex extraction on narrative content) can only help, never
hide drawers the direct path would have found.
"""

# The public import path remains ``mempalace.searcher``. Implementation is
# split across this package so PRs can target ranking, sqlite BM25, the
# hybrid pipeline, or CLI output without colliding. Fragments exec into
# this package's globals (the same namespace as the historical module).

import functools
import logging
import math
import os
import re
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path
from typing import Optional

from ..backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    PalaceNotFoundError,
    UnsupportedCapabilityError,
)
from ..backends._inproc_sqlite import open_reader as open_palace_reader
from ..backends.chroma_sqlite import (
    _decoded_rows,
    _filtered_candidate_rows,
    _fts_candidate_rows,
    _match_sql,
    _newest_row_ids,
)
from ..config import MempalaceConfig, wing_aliases
from ..date_window import filed_at_in_window, parse_window
from ..embedding import UnknownEmbeddingModelError, _model_error_class, model_error_result
from ..i18n import _canonical_lang, get_stopwords
from ..lexical import bm25_scores, tokenize
from ..metadata_types import is_bookkeeping
from ..palace import (
    _open_collection_or_explain,
    get_closets_collection,
    get_collection,
    is_aaak_row,
    resolve_backend_name,
)
from ..search_util import (
    _boosted_cosine_distance,
    _cosine_scale_distance,
    _distance_to_similarity,
    _first_or_empty,
    _metric_for_collection,
    _metric_scale_distance,
    build_where_filter,
    chroma_known_wings,
    palace_wing_aliases,
)
from ..stored_text import decode_record, encode_filter
from ..timeutil import localize

logger = logging.getLogger(__name__)


class SearchError(Exception):
    """Raised when search cannot proceed (e.g. no palace found)."""


_FRAGMENT_DIR = Path(__file__).resolve().parent
# Load order is a dependency sequence, not a catalog: later files use names
# defined earlier (ranking before sqlite_bm25, candidates before CLI search).
# Do not reorder this tuple.
_FRAGMENTS = (
    "ranking.py",
    "filters.py",
    "sqlite_bm25.py",
    "candidates.py",
    "query.py",
    "cli_search.py",
    "render.py",
)


def _exec_fragment(filename: str) -> None:
    path = _FRAGMENT_DIR / filename
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), globals())


for _fragment in _FRAGMENTS:
    _exec_fragment(_fragment)
del _fragment
