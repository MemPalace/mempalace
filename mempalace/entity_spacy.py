"""Opt-in spaCy NER augmentation for entity detection.

This module is the integration surface for the ``mempalace[nlp]`` extra.
When spaCy is installed, it provides statistical named-entity recognition
(PERSON, ORG, GPE, PRODUCT, WORK_OF_ART, EVENT) that augments — never
replaces — the existing regex + COCA-filter + known-systems pipeline.

When spaCy is NOT installed, every public function gracefully no-ops:
``extract_spacy_entities`` returns ``{}`` and the rest of the entity
pipeline runs unchanged. spaCy is therefore strictly opt-in; uninstalling
it (or never installing ``[nlp]``) is the "disable" mechanism.

Model selection
---------------
The model defaults to ``en_core_web_sm`` (~12 MB, fast, CPU-only).
Users may override via ``MEMPALACE_SPACY_MODEL=en_core_web_md|lg|trf``
for higher accuracy at the cost of size and speed.

The model itself is NOT installed by the extra. On first use,
``_get_spacy_nlp`` attempts ``spacy.load(name)``; on ``OSError`` (model
not present), it calls ``spacy.cli.download(name)`` once and retries.
Mirrors the lazy first-use download pattern used by the embeddinggemma
ONNX model (#1483) so installs stay slim and offline-friendly.
"""

from __future__ import annotations

import functools
import logging
import os

logger = logging.getLogger("mempalace.entity_spacy")

# spaCy NER labels we consider entity-quality. Excludes DATE, CARDINAL,
# ORDINAL, PERCENT, MONEY, TIME, QUANTITY, LANGUAGE, NORP, FAC, LAW, LOC —
# either too noisy or already covered by other tiers. The kept set
# corresponds to "proper nouns a user would search for by name."
_KEEP_LABELS: frozenset[str] = frozenset(
    {"PERSON", "ORG", "GPE", "PRODUCT", "WORK_OF_ART", "EVENT"}
)

_DEFAULT_MODEL = "en_core_web_sm"
_ENV_VAR = "MEMPALACE_SPACY_MODEL"


def _spacy_available() -> bool:
    """Return True if spaCy can be imported in the current environment.

    First call pays the spaCy import cost (Python caches the module in
    ``sys.modules`` so subsequent calls are a dict lookup). spaCy itself
    pulls in numpy + several Cython extensions, so the cold import is on
    the order of hundreds of milliseconds — but it only happens once per
    process, and only when something actually calls into this module.
    The base mempalace install never reaches here.
    """
    try:
        import spacy  # noqa: F401  (probe; cached in sys.modules thereafter)
    except (ImportError, ModuleNotFoundError):
        return False
    except Exception:
        # Any other import-time failure (e.g. broken install, missing
        # transitive dep, ABI mismatch) is treated as "spaCy not usable"
        # rather than propagated up — the rest of the pipeline must keep
        # working even when spaCy itself is broken.
        logger.debug("spaCy probe raised non-ImportError; treating as unavailable")
        return False
    return True


@functools.lru_cache(maxsize=1)
def _resolve_model_name() -> str:
    """Return the effective spaCy model name from env, or an empty string.

    Returns the trimmed value of ``MEMPALACE_SPACY_MODEL`` if set, or
    ``""`` if the env var is missing, empty, or whitespace-only. The
    caller is responsible for layering the default on top via
    ``_resolve_model_name() or _DEFAULT_MODEL`` — keeping the resolver
    silent about defaults makes "user did not configure" distinguishable
    from "user explicitly set the default," per project convention.

    Cached at ``maxsize=1`` because env vars don't change mid-process
    under normal operation; tests that need to flip the value must call
    ``_resolve_model_name.cache_clear()`` between assertions.
    """
    raw = os.environ.get(_ENV_VAR, "")
    return raw.strip()


@functools.lru_cache(maxsize=4)
def _get_spacy_nlp(model_name: str):
    """Load (and cache) a spaCy nlp pipeline by model name.

    Returns the loaded pipeline object on success, or ``None`` on any
    failure (spaCy missing, model missing AND download failed, etc.).
    The lru_cache ensures ``spacy.load`` is called at most once per
    distinct model name — repeated entity-extraction calls hit the
    cached pipeline directly.

    The cache is keyed by model name so a user can switch via the env
    var without restarting the process, and multiple models can be
    held in parallel (capacity 4 covers sm/md/lg/trf simultaneously).
    """
    if not _spacy_available():
        return None

    import spacy

    # Outer broad-Exception guard — this module's design contract is
    # "strictly opt-in, never load-bearing." A ValueError on an invalid
    # model name, an ImportError from a broken model package, or any
    # other spaCy-internal failure must NOT crash the caller; the rest
    # of the entity pipeline must keep running. The inner OSError-only
    # path handles the common "model not installed → try to download"
    # case; anything else falls through to the outer except.
    try:
        try:
            return spacy.load(model_name)
        except OSError:
            # Model not installed — attempt one-time lazy download.
            logger.info("spaCy model %r not present locally; downloading", model_name)
            try:
                spacy.cli.download(model_name)
            except Exception as exc:  # network down, disk full, permissions, etc.
                logger.warning(
                    "spaCy model download failed for %r (%s); spaCy NER "
                    "augmentation will be skipped for this session",
                    model_name,
                    exc,
                )
                return None
            try:
                return spacy.load(model_name)
            except Exception as exc:
                logger.warning(
                    "spaCy model %r still unloadable after download (%s); "
                    "spaCy NER augmentation will be skipped",
                    model_name,
                    exc,
                )
                return None
    except Exception as exc:
        logger.warning(
            "spaCy model %r failed to load (%s); spaCy NER augmentation "
            "will be skipped for this session",
            model_name,
            exc,
        )
        return None


def extract_spacy_entities(text: str) -> dict[str, int]:
    """Return ``{entity_text: occurrence_count}`` for kept NER labels.

    Filters spaCy's emitted spans down to ``_KEEP_LABELS`` and dedupes
    by surface form (case-sensitive — "Aya" and "aya" are separate
    surface forms here; case-folding is the caller's responsibility if
    desired, to avoid clobbering legitimately distinct strings).

    Whitespace surrounding the entity text is stripped; empty/whitespace-
    only ents are dropped entirely. spaCy occasionally emits these on
    malformed inputs.

    Returns an empty dict if:
      - ``text`` is empty/whitespace-only,
      - spaCy is not installed,
      - the configured model could not be loaded or downloaded,
      - the model raised on processing the text.

    Never raises — the existing entity pipeline must stay running even
    when spaCy misbehaves.
    """
    if not text or not text.strip():
        return {}

    nlp = _get_spacy_nlp(_resolve_model_name() or _DEFAULT_MODEL)
    if nlp is None:
        return {}

    try:
        doc = nlp(text)
    except Exception as exc:
        logger.warning(
            "spaCy processing raised %s on a %d-char input; skipping",
            type(exc).__name__,
            len(text),
        )
        return {}

    counts: dict[str, int] = {}
    for ent in doc.ents:
        if ent.label_ not in _KEEP_LABELS:
            continue
        name = ent.text.strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts
