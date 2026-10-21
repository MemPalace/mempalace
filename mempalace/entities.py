"""No-LLM structural entity extraction for the associative graph.

Pulls deterministic, *structural* tokens from text — author-quoted code spans, URLs,
file paths, qualified identifiers, and CamelCase symbols — to populate the ``entities``
drawer-metadata field that hallways/tunnels consume. Structural-only by design: no
wordlists, no NLP models, no domain vocabulary, so it stays language-neutral and
predictable, and biases to precision (only tokens that are unambiguously "a thing being
referred to") over recall.

The output format matches what ``hallways._parse_entities`` expects: a ``;``-joined string.
"""

import re

# Author-quoted code spans are the highest-signal structural marker: `foo`, `obj.method()`.
_BACKTICK = re.compile(r"`([^`\n]{2,64})`")
# URLs.
_URL = re.compile(r"https?://[^\s)>\]}\"']+")
# Paths with a separator and a short extension: rag/foo.py, a/b/c.tsx.
_PATH = re.compile(r"\b[\w.-]+/[\w./-]*\.[A-Za-z][A-Za-z0-9]{0,4}\b")
# Qualified dotted identifiers; each segment starts with a letter and is >=2 chars, so
# "1.2.3", "e.g", and "i.e" are excluded: module.func, pkg.Class.method.
_QUALIFIED = re.compile(r"\b[A-Za-z][A-Za-z0-9_]+(?:\.[A-Za-z][A-Za-z0-9_]+)+\b")
# CamelCase with >=2 humps — strongly code-specific: ChromaBackend, MemoryStack.
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
# snake_case (must contain an underscore, so it can't match plain English): do_thing,
# _extract_authored_at. The optional leading/trailing `_?` matches dunder-style names
# whose underscore would otherwise fall outside the `\b` boundary (`_` is a word char).
_SNAKE = re.compile(r"\b_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+_?\b")

_MAX_ENTITIES = 24
_MIN_LEN = 2
_MAX_LEN = 64


def _clean(token):
    # `;` is the entities-metadata separator, so it must never survive inside an entity
    # (e.g. a URL query string or a backtick span) or it would split the field.
    return token.replace(";", " ").strip().strip("`.,:()[]{}<>\"'").strip()


def extract_structural_entities(text, max_entities=_MAX_ENTITIES):
    """Return up to ``max_entities`` structural entities from ``text``.

    Deterministic and order-stable: entities are ranked by occurrence count (ties broken
    by first appearance), deduplicated case-insensitively, preserving the first-seen
    surface form.
    """
    if not text:
        return []
    counts = {}
    order = {}
    seq = 0
    for pattern in (_BACKTICK, _URL, _PATH, _QUALIFIED, _CAMEL, _SNAKE):
        for match in pattern.finditer(text):
            token = _clean(match.group(1) if pattern is _BACKTICK else match.group(0))
            if not (_MIN_LEN <= len(token) <= _MAX_LEN):
                continue
            key = token.lower()
            if key not in counts:
                counts[key] = 0
                order[key] = (seq, token)
                seq += 1
            counts[key] += 1
    ranked = sorted(order, key=lambda k: (-counts[k], order[k][0]))
    return [order[k][1] for k in ranked[:max_entities]]


def entities_metadata(text, max_entities=_MAX_ENTITIES):
    """``;``-joined entity string for drawer metadata, or ``""`` when none are found."""
    return ";".join(extract_structural_entities(text, max_entities=max_entities))
