# Loaded into mempalace.palace via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.palace":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.palace")


def clear_validated_embedder_identity(palace_path: Optional[str] = None) -> None:
    """Drop cached embedder-identity verdicts so the next open re-checks.

    Read-only opens of an empty collection can mark a key as validated without
    recording identity on disk (``create=False``). When MCP later promotes that
    reader to a writable owner, the writable open must re-run enforcement so
    the first drawers still get labelled with the active model.
    """
    if palace_path is None:
        _VALIDATED_IDENTITY.clear()
        return
    palace_key = str(palace_path)
    stale = [key for key in _VALIDATED_IDENTITY if key and key[0] == palace_key]
    for key in stale:
        _VALIDATED_IDENTITY.discard(key)


def _enforce_embedder_identity(
    collection,
    palace_path,
    collection_name,
    *,
    create,
    repeat_unknown_warning=False,
) -> None:
    """Check (and, for a brand-new collection, record) embedder identity (RFC 001).

    Check at open so a model swap fails fast — before any query silently
    returns degraded results. Record only when the collection is brand-new and
    empty: recording the *current* model on a legacy palace that already holds
    vectors from an unknown model would mislabel it, so populated-but-unrecorded
    collections warn instead and are resolved with
    ``mempalace palace set-embedder``.

    ``repeat_unknown_warning`` bypasses the process cache so a long-lived Hub
    can reproduce the warning a standalone CLI process emits on every search.

    Bookkeeping must never break memory operations: only the deliberate
    identity/dimension mismatch propagates; every other error is swallowed.
    """
    import warnings

    from ..backends.base import (
        DimensionMismatchError,
        EmbedderIdentity,
        EmbedderIdentityMismatchError,
        EmbedderIdentityUnknownWarning,
        check_embedder_identity,
    )
    from ..embedding import current_model_name

    # A server_embedder backend embeds with its own model and ignores the
    # injected/core embedder, so its effective identity — not the configured
    # model — is what must be checked and recorded. Fall back to the configured
    # model name for the normal (core-embedder) case.
    current: Optional[EmbedderIdentity] = None
    try:
        effective = collection.effective_embedder_identity()
    except Exception:
        effective = None
    if effective is not None and getattr(effective, "model_name", ""):
        current = effective
    else:
        try:
            model_name = current_model_name()
        except Exception:
            return
        if not model_name:
            return  # nameless embedder — cannot enforce identity
        current = EmbedderIdentity(model_name=model_name, dimension=0)

    model_name = current.model_name
    key = (str(palace_path), str(collection_name), model_name)
    if key in _VALIDATED_IDENTITY and not repeat_unknown_warning:
        return

    try:
        stored = collection.get_stored_embedder_identity()
    except Exception:
        logger.debug("embedder-identity read failed for %s", collection_name, exc_info=True)
        return
    try:
        state = check_embedder_identity(stored, current)
    except (EmbedderIdentityMismatchError, DimensionMismatchError):
        raise  # deliberate, user-facing — the whole point of the contract
    except Exception:
        return

    if state == "unknown" and stored is None:
        # Preflight HNSW divergence before touching count(): this is the
        # universal chokepoint every tool passes through via
        # get_collection(), and count() on a diverged segment can raise
        # chromadb's rust-level pyo3_runtime.PanicException or hard-segfault
        # (#1222) -- neither of which the except Exception below can catch,
        # since a native crash takes the whole process down regardless of
        # any Python try/except. A diverged palace must never reach count()
        # here; this bookkeeping-only identity check simply skips itself
        # (count treated as unknown, matching the except-Exception fallback
        # already below) rather than risk the read.
        try:
            from ..backends.chroma import hnsw_capacity_status

            diverged = hnsw_capacity_status(str(palace_path), str(collection_name)).get("diverged")
        except Exception:
            diverged = False
        if diverged:
            count = None
        else:
            try:
                count = collection.count()
            except Exception:
                count = None
        if count == 0:
            if create:
                try:
                    collection.set_embedder_identity(current)
                except Exception:
                    logger.debug("embedder-identity record failed", exc_info=True)
        elif count:
            warnings.warn(
                f"palace collection {collection_name!r} has no recorded embedder "
                f"identity; assuming the current model {model_name!r}. Run "
                "`mempalace palace set-embedder --model <name>` to record it.",
                EmbedderIdentityUnknownWarning,
                stacklevel=2,
            )

    _VALIDATED_IDENTITY.add(key)


# The closets collection name is fixed (not user-configurable) — it is the
# searchable index layer and MemPalace never opens a differently-named closets
# store. Mirrored independently in repair.py as ``CLOSETS_COLLECTION_NAME``.
CLOSETS_COLLECTION_NAME = "mempalace_closets"


def _allowed_wrapper_collection_names() -> List[str]:
    """The collection names the ``get_collection`` wrapper routes through.

    Only two stores are first-class to MemPalace: the configured drawers
    collection (default ``mempalace_drawers``, overridable in config) and the
    closets collection. Every other name points at a store the search/CLI/MCP
    layer never reads — the exact silent-miss failure of issue ``#2347``.
    """
    from ..config import get_configured_collection_name

    allowed = [get_configured_collection_name(), CLOSETS_COLLECTION_NAME]
    seen: set[str] = set()
    out: list[str] = []
    for name in allowed:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


class CollectionNameMismatchError(ValueError):
    """Raised when ``get_collection(collection_name=...)`` names a collection the
    palace wrapper does not own.

    The wrapper (``mempalace.palace.get_collection``) front-loads exactly two
    collections: the configured drawers collection (default
    ``mempalace_drawers``, overridable in config) and ``mempalace_closets`` (the
    searchable index layer). Opening any other name would create or touch a
    store the rest of MemPalace never reads — so data upserted through it would
    be invisible to search, MCP, and repair — instead of the caller discovering
    the miss once their reads come back empty.

    Subclass of :class:`ValueError` so callers that already catch the generic
    type for a bad string keep working, while ``except
    CollectionNameMismatchError`` gives the specific signal. This is a distinct
    failure from :class:`mempalace.backends.CollectionNotInitializedError`
    (palace + DB present, collection simply not bootstrapped yet): this one means
    the *name* is not one MemPalace routes reads/writes through.
    """

    def __init__(self, requested: str, allowed: List[str], palace_path: Optional[str] = None):
        self.requested = requested
        self.allowed = list(allowed)
        self.palace_path = palace_path
        where = f" in {palace_path}" if palace_path else ""
        allowed_str = ", ".join(repr(n) for n in allowed)
        super().__init__(
            f"collection {requested!r} is not a MemPalace collection{where}: "
            f"get_collection routes reads and writes through {allowed_str} only. "
            f"Opening {requested!r} would create or touch a store the rest of "
            f"MemPalace never reads, so data upserted through it would be "
            f"invisible to search, MCP, and repair. Use one of the configured "
            f"names — the drawers collection name (default "
            f"'mempalace_drawers', overridable in config; expose it with "
            f"``get_configured_collection_name()``) or the closets collection "
            f"``get_closets_collection()`` — not an ad-hoc string."
        )


def get_collection(
    palace_path: str,
    collection_name: Optional[str] = None,
    create: bool = True,
    backend: Optional[str] = None,
    read_only: bool = False,
    _skip_identity_check: bool = False,
    _skip_name_check: bool = False,
):
    """Get a first-class MemPalace collection (drawers or closets).

    The wrapper front-loads exactly two collections and is the public surface
    MCP, miners, the search layer, and the CLI use to open a palace:

    * the **drawers** collection — the verbatim document store. Its name is
      configurable (default ``mempalace_drawers``); read it via
      :func:`mempalace.config.get_configured_collection_name` rather than
      hard-coding the string.
    * the **closets** collection — the searchable index layer. Always named
      :data:`mempalace.palace.CLOSETS_COLLECTION_NAME`` ("mempalace_closets");
      open it through :func:`mempalace.palace.get_closets_collection`.

    Any other name points to a store the rest of MemPalace never reads, so data
    upserted through it stays invisible to search/CLI/MCP. ``get_collection``
    therefore raises :class:`CollectionNameMismatchError` (a ``ValueError``)
    naming both the offending and allowed strings, instead of silently creating
    that orphan collection (issue ``#2347``). Passing ``collection_name=None``
    resolves to the configured drawers name and always succeeds.

    ``read_only=True`` asks local backends to open storage without schema
    initialization, migrations, or metadata writes. Backends that support a
    genuine read-only mode receive it through the backend ``options`` mapping.

    ``_skip_identity_check`` bypasses the embedder-identity enforcement so the
    ``set-embedder`` override path can open a palace whose recorded model
    differs from the current one (the very state it exists to repair).

    ``_skip_name_check`` is the maintenance escape hatch for tools like
    ``mempalace_repair_encoding --collection NAME`` that must be able to open a
    legacy ad-hoc collection name deliberately created before this check
    existed. Callers on this path take the risk of a mismatched-name orphan;
    they are explicit and self-aware. The common programmatic-API misuse this
    check exists to prevent never uses this flag and still fails loudly.
    """
    if collection_name is None:
        from ..config import get_configured_collection_name

        collection_name = get_configured_collection_name()
    if not _skip_name_check:
        allowed = _allowed_wrapper_collection_names()
        if collection_name not in allowed:
            raise CollectionNameMismatchError(collection_name, allowed, palace_path)
    backend_obj = get_backend_for_palace(palace_path, explicit=backend)
    palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
    backend_options = {"read_only": True} if read_only else None
    preferred_kwargs = {
        "palace": palace_ref,
        "collection_name": collection_name,
        "create": create,
    }
    if backend_options is not None:
        preferred_kwargs["options"] = backend_options
    try:
        collection = backend_obj.get_collection(**preferred_kwargs)
    except TypeError as exc:
        msg = str(exc)
        # Plugin backends may still use the pre-options signature. Drop
        # ``options`` first so read_only degrades gracefully instead of
        # hard-failing TypeError on third-party entry points.
        if backend_options is not None and "options" in msg:
            preferred_kwargs.pop("options", None)
            try:
                collection = backend_obj.get_collection(**preferred_kwargs)
            except TypeError as nested:
                if "unexpected keyword argument 'palace'" not in str(nested):
                    raise
                collection = backend_obj.get_collection(
                    palace_path,
                    collection_name=collection_name,
                    create=create,
                )
        elif "unexpected keyword argument 'palace'" not in msg:
            raise
        else:
            legacy_kwargs = {
                "collection_name": collection_name,
                "create": create,
            }
            if backend_options is not None:
                legacy_kwargs["options"] = backend_options
            try:
                collection = backend_obj.get_collection(palace_path, **legacy_kwargs)
            except TypeError as nested:
                if backend_options is None or "options" not in str(nested):
                    raise
                collection = backend_obj.get_collection(
                    palace_path,
                    collection_name=collection_name,
                    create=create,
                )
    if "requires_explicit_embeddings" in getattr(backend_obj, "capabilities", frozenset()):
        collection = EmbeddingCollection(collection)
    if not _skip_identity_check:
        _enforce_embedder_identity(collection, palace_path, collection_name, create=create)
    return collection


def set_palace_embedder_identity(
    palace_path: str,
    model: Optional[str] = None,
    *,
    force: bool = False,
    backend: Optional[str] = None,
    collection_name: Optional[str] = None,
):
    """Record (or force-override) a palace collection's embedder identity (RFC 001).

    Backs ``mempalace palace set-embedder``. Returns ``(old, new)`` identities.
    Without ``force``, refuses to overwrite an existing identity that names a
    different model (the user must confirm they know the vectors are
    compatible). Opens with the identity check skipped so a mismatched palace —
    the exact state being repaired — can be opened at all.
    """
    from ..backends.base import EmbedderIdentity, EmbedderIdentityMismatchError
    from ..config import MempalaceConfig
    from ..embedding import get_embedder_identity

    configured = MempalaceConfig().embedding_model
    target = (model or configured or "").strip().lower()
    if not target:
        # No model given and none configured — there is nothing to record, and
        # recording a nameless identity is a silent no-op in every backend.
        raise ValueError(
            "no embedder model to record: pass --model NAME or configure MEMPALACE_EMBEDDING_MODEL"
        )
    if target == (configured or "").strip().lower():
        # Recording the in-use model — probe its dimension (already loaded).
        new = get_embedder_identity()
    else:
        # Explicit override of a non-configured model: record the name only,
        # never load a foreign model (which can be a large download) just to
        # probe a dimension. The model-name check is the actual protection.
        new = EmbedderIdentity(model_name=target, dimension=0)
    collection = get_collection(
        palace_path,
        collection_name=collection_name,
        create=True,
        backend=backend,
        _skip_identity_check=True,
    )
    try:
        old = collection.get_stored_embedder_identity()
    except Exception:
        old = None
    if old is not None and old.model_name != new.model_name and not force:
        raise EmbedderIdentityMismatchError(
            f"palace already records embedder {old.model_name!r}; pass --force to "
            f"overwrite it with {new.model_name!r} (only if the vectors are compatible)"
        )
    collection.set_embedder_identity(new)
    # Reset the per-process validation cache so a re-open re-checks against the
    # newly recorded identity rather than a stale verdict.
    _VALIDATED_IDENTITY.clear()
    return old, new


def get_closets_collection(
    palace_path: str,
    create: bool = True,
    backend: Optional[str] = None,
    *,
    read_only: bool = False,
):
    """Get the closets collection — the searchable index layer."""
    return get_collection(
        palace_path,
        collection_name="mempalace_closets",
        create=create,
        backend=backend,
        **({"read_only": True} if read_only else {}),
    )
