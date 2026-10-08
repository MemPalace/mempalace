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


def _collection_has_rows(collection, palace_path, collection_name) -> Optional[bool]:
    """Whether ``collection`` holds any drawer; ``None`` when that is unknown.

    For Chroma this reads one row from chroma.sqlite3 instead of calling
    ``count()``: on a freshly built client ``count()`` loads the whole HNSW
    segment while holding the GIL, so on a large palace this bookkeeping check
    stalled every thread in the process, and loaded the full index for a
    wake-up that reads ten drawers. Other backends, and a Chroma database the
    read cannot reach, fall back to ``count()``.
    """
    from ..backends.chroma import ChromaCollection, _sqlite_collection_has_rows

    inner = collection._inner if isinstance(collection, EmbeddingCollection) else collection
    if isinstance(inner, ChromaCollection):
        has_rows = _sqlite_collection_has_rows(str(palace_path), str(collection_name))
        if has_rows is not None:
            return has_rows
    # Preflight HNSW divergence before touching count(): count() on a diverged
    # segment can raise chromadb's rust-level PanicException or hard-segfault
    # (#1222), which no try/except can catch. This bookkeeping-only check
    # skips itself on a diverged palace instead.
    try:
        from ..backends.chroma import hnsw_capacity_status

        if hnsw_capacity_status(str(palace_path), str(collection_name)).get("diverged"):
            return None
    except Exception:
        pass
    try:
        return collection.count() > 0
    except Exception:
        return None


def _server_embedder_identity(collection):
    """The identity a ``server_embedder`` collection reports, or ``None``.

    A server_embedder backend embeds with its own model and ignores the core
    embedder; ``effective_embedder_identity()`` returning a named identity is
    how it says so. ``None`` means the core (configured) embedder does the
    embedding.
    """
    try:
        effective = collection.effective_embedder_identity()
    except Exception:
        return None
    if effective is not None and getattr(effective, "model_name", ""):
        return effective
    return None


def _persists_embedder_identity(collection) -> bool:
    """Whether the collection's backend records an embedder identity at all.

    A plugin backend that keeps :class:`~mempalace.backends.base.BaseCollection`'s
    no-op identity hooks never records one, so "no recorded identity" is its
    permanent state, not a sign of a legacy palace; refusing its writes would
    lock it forever. Such backends keep the old warn-and-write behavior.
    """
    from ..backends.base import BaseCollection

    inner = collection._inner if isinstance(collection, EmbeddingCollection) else collection
    if not isinstance(inner, BaseCollection):
        # Duck-typed collection: it records an identity if it has the hooks.
        return callable(getattr(inner, "get_stored_embedder_identity", None)) and callable(
            getattr(inner, "set_embedder_identity", None)
        )
    cls = type(inner)
    getter = getattr(cls, "get_stored_embedder_identity", None)
    setter = getattr(cls, "set_embedder_identity", None)
    if getter is None or setter is None:
        return False
    return (
        getter is not BaseCollection.get_stored_embedder_identity
        and setter is not BaseCollection.set_embedder_identity
    )


def _set_embedder_model_arg(model_name: str) -> str:
    """The ``--model`` value that makes ``palace set-embedder`` record ``model_name``."""
    if model_name.startswith("embeddinggemma2:"):
        return "embeddinggemma2"
    return model_name


def _confirm_model_hint(palace_path, model_name: str) -> str:
    """How to confirm the model behind a collection's vectors (shared by every refusal)."""
    import shlex

    command = f"mempalace --palace {shlex.quote(str(palace_path))} palace set-embedder --model"
    return (
        "Confirm the model the palace was built with, then record it with "
        f"`{command} <model>` (if that is the configured model: "
        f"`{command} {shlex.quote(_set_embedder_model_arg(model_name))}`). "
        "Reads and search keep working meanwhile."
    )


def _normalize_legacy_identity(collection, stored, *, create):
    """Read a core embedder's legacy recorded name as the model behind it.

    Older builds recorded the raw configured name while embedding anything
    unrecognized with MiniLM, so a recorded ``"all-minilm-l6-v2"`` or
    ``"none"`` is a MiniLM palace (see ``_normalize_stored_model_name``). A
    write open (``create``) records the normalized name; a read open only
    compares with it.
    """
    from ..backends.base import EmbedderIdentity
    from ..embedding import _normalize_stored_model_name

    normalized = _normalize_stored_model_name(stored.model_name)
    if normalized == stored.model_name:
        return stored
    stored = EmbedderIdentity(model_name=normalized, dimension=stored.dimension)
    if create:
        try:
            collection.set_embedder_identity(stored)
        except Exception as exc:
            # Not fatal: the palace stays protected, the legacy name keeps
            # reading as the normalized one. Say so rather than hide it.
            logger.warning("could not rewrite the legacy embedder identity: %s", exc)
    return stored


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
    returns degraded results. ``create`` marks a write open.

    When the identity cannot be checked, reads go on with a warning and
    writes refuse with
    :class:`~mempalace.backends.base.EmbedderIdentityUnconfirmedError` until
    ``mempalace palace set-embedder`` records the model:

    * no recorded identity: a write records the current model only while the
      collection is empty. Recording it over vectors from an unknown model
      would mislabel them, and writing without a record would let a later
      same-dimension model swap through, so a collection that holds vectors
      (or whose row count cannot be read) refuses the write;
    * an unreadable record (a truncated ``mempalace_embedder.json``, a
      malformed entry, a failed read): something was recorded and MemPalace
      cannot tell what, so a write refuses whatever the row count.

    A backend that never records identities (``BaseCollection``'s no-op hooks)
    keeps the warn-and-write behavior: it has nothing to confirm against.

    ``repeat_unknown_warning`` bypasses the process cache so a long-lived Hub
    can reproduce the warning a standalone CLI process emits on every search.

    Errors that propagate: the identity/dimension mismatches,
    :class:`~mempalace.backends.base.EmbedderIdentityUnconfirmedError` above,
    and :class:`~mempalace.backends.base.EmbedderIdentityRecordError` when a
    write open cannot record a brand-new collection's identity.
    """
    import warnings

    from ..backends.base import (
        DimensionMismatchError,
        EmbedderIdentity,
        EmbedderIdentityMismatchError,
        EmbedderIdentityRecordError,
        EmbedderIdentityUnconfirmedError,
        EmbedderIdentityUnknownWarning,
        EmbedderIdentityUnreadableError,
        EmbedderIdentityUnreadableWarning,
        check_embedder_identity,
    )
    from ..embedding import current_model_name

    # A server_embedder backend embeds with its own model and ignores the
    # injected/core embedder, so its effective identity — not the configured
    # model — is what must be checked and recorded. Fall back to the configured
    # model name for the normal (core-embedder) case.
    current: Optional[EmbedderIdentity] = None
    effective = _server_embedder_identity(collection)
    core_embedder = effective is None
    if not core_embedder:
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
    # A verdict that allows writes ("rw") also covers reads; a read-only
    # verdict ("r": the identity could not be confirmed) never covers a write.
    write_key, read_key = key + ("rw",), key + ("r",)
    if not repeat_unknown_warning and (
        write_key in _VALIDATED_IDENTITY or (not create and read_key in _VALIDATED_IDENTITY)
    ):
        return

    persists = _persists_embedder_identity(collection)
    try:
        stored = collection.get_stored_embedder_identity()
    except Exception as exc:
        logger.debug("embedder-identity read failed for %s", collection_name, exc_info=True)
        if not persists:
            return
        if isinstance(exc, EmbedderIdentityUnreadableError):
            problem = str(exc)
        else:
            problem = (
                f"the embedder identity of collection {collection_name!r} in {palace_path} "
                f"could not be read ({type(exc).__name__}: {exc})"
            )
        hint = _confirm_model_hint(palace_path, model_name)
        if create:
            raise EmbedderIdentityUnconfirmedError(
                f"{problem}, so MemPalace cannot tell which model embedded the collection's "
                f"vectors; writing with the current model {model_name!r} could mix two models "
                f"in one palace. {hint}"
            ) from exc
        warnings.warn(
            f"{problem}; reading with the current model {model_name!r}. Writes are refused "
            f"until the record is repaired. {hint}",
            EmbedderIdentityUnreadableWarning,
            stacklevel=2,
        )
        _VALIDATED_IDENTITY.add(read_key)
        return
    unrecorded = False
    write_ok = True
    if core_embedder and stored is not None and getattr(stored, "model_name", ""):
        normalized = _normalize_legacy_identity(collection, stored, create=create)
        # A read open compares with the normalized name but does not write it;
        # stay out of the cache so the next write open records it.
        unrecorded = normalized is not stored and not create
        stored = normalized
    try:
        state = check_embedder_identity(stored, current)
    except (EmbedderIdentityMismatchError, DimensionMismatchError):
        raise  # deliberate, user-facing — the whole point of the contract
    except Exception:
        return

    if state == "unknown" and stored is None:
        has_rows = _collection_has_rows(collection, palace_path, collection_name)
        if has_rows is False:
            # A read open of an empty, unrecorded collection records nothing;
            # stay out of the cache so the next write open in this process
            # still records it.
            unrecorded = not create
            if create:
                try:
                    collection.set_embedder_identity(current)
                except EmbedderIdentityRecordError:
                    raise
                except Exception as exc:
                    # Writing on would leave the collection unrecorded, and
                    # its next write open would refuse; say so now instead.
                    raise EmbedderIdentityRecordError(
                        f"could not record the embedder identity of collection "
                        f"{collection_name!r} in {palace_path}: {type(exc).__name__}: {exc}"
                    ) from exc
        else:
            if has_rows and model_name.startswith("embeddinggemma2:"):
                raise EmbedderIdentityMismatchError(
                    f"collection {collection_name!r} has vectors but no recorded embedding identity; "
                    "the vectors may come from a different model or modality configuration. "
                    "Rebuild the index before using it with EmbeddingGemma 2."
                )
            if not persists:
                if has_rows:
                    warnings.warn(
                        f"palace collection {collection_name!r} has no recorded embedder "
                        f"identity; assuming the current model {model_name!r}.",
                        EmbedderIdentityUnknownWarning,
                        stacklevel=2,
                    )
            else:
                held = (
                    "holds vectors"
                    if has_rows
                    else "may hold vectors (its row count could not be read)"
                )
                problem = (
                    f"collection {collection_name!r} in {palace_path} {held} but has no "
                    "recorded embedder identity, so MemPalace cannot tell which model "
                    "embedded them"
                )
                hint = _confirm_model_hint(palace_path, model_name)
                if create:
                    raise EmbedderIdentityUnconfirmedError(
                        f"{problem}; writing with the current model {model_name!r} could mix "
                        f"two models in one palace. {hint}"
                    )
                if has_rows:
                    warnings.warn(
                        f"palace {problem}; reading with the current model {model_name!r}. "
                        f"Writes are refused until the model is recorded. {hint}",
                        EmbedderIdentityUnknownWarning,
                        stacklevel=2,
                    )
                write_ok = False

    if not unrecorded:
        _VALIDATED_IDENTITY.add(write_key if write_ok else read_key)


# The closets collection name is fixed (not user-configurable) — it is the
# searchable index layer and MemPalace never opens a differently-named closets
# store. Mirrored independently in repair.py as ``CLOSETS_COLLECTION_NAME``.
CLOSETS_COLLECTION_NAME = "mempalace_closets"
ASSETS_COLLECTION_NAME = "mempalace_assets"


def _allowed_wrapper_collection_names() -> List[str]:
    """The collection names the ``get_collection`` wrapper routes through.

    The configured drawers collection, closets collection, and fixed media
    assets collection are first-class to MemPalace. Every other name points at a store the search/CLI/MCP
    layer never reads — the exact silent-miss failure of issue ``#2347``.
    """
    from ..config import get_configured_collection_name

    allowed = [get_configured_collection_name(), CLOSETS_COLLECTION_NAME, ASSETS_COLLECTION_NAME]
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
            f"``get_configured_collection_name()``), the closets collection "
            f"``get_closets_collection()``, or the fixed ``mempalace_assets`` "
            f"collection — not an ad-hoc string."
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
    """Get a first-class MemPalace collection (drawers, closets, or media assets).

    The wrapper front-loads the drawers, closets, and fixed media asset collections and is the public surface
    MCP, miners, the search layer, and the CLI use to open a palace:

    * the **drawers** collection — the verbatim document store. Its name is
      configurable (default ``mempalace_drawers``); read it via
      :func:`mempalace.config.get_configured_collection_name` rather than
      hard-coding the string.
    * the **closets** collection — the searchable index layer. Always named
      :data:`mempalace.palace.CLOSETS_COLLECTION_NAME`` ("mempalace_closets");
      open it through :func:`mempalace.palace.get_closets_collection`.
    * the **media assets** collection — local image, audio, and video references
      stored separately from drawer documents. Always named
      :data:`mempalace.palace.ASSETS_COLLECTION_NAME`.

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
    from ..config import MempalaceConfig

    # Read (and so validate) the configured model before the backend can
    # create a palace folder, collection or identity sidecar: a misspelled
    # model name (UnknownEmbeddingModelError) must leave nothing behind.
    configured_model = MempalaceConfig().embedding_model
    if collection_name == ASSETS_COLLECTION_NAME:
        # Validate provider configuration before opening storage. The identity
        # checker intentionally degrades gracefully for legacy collections,
        # but invalid EmbeddingGemma 2 settings must not be hidden by it.
        if configured_model == "embeddinggemma2":
            from ..embedding import get_embedding_function

            get_embedding_function(model="embeddinggemma2")
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


def _backend_has_server_embedder(palace_path, backend) -> bool:
    """Whether the palace's backend advertises ``server_embedder``."""
    try:
        capabilities = get_backend_for_palace(palace_path, explicit=backend).capabilities
    except Exception:
        return False
    return "server_embedder" in capabilities


def set_palace_embedder_identity(
    palace_path: str,
    model: Optional[str] = None,
    *,
    force: bool = False,
    backend: Optional[str] = None,
    collection_name: Optional[str] = None,
    only_if_exists: bool = False,
    report: Optional[dict] = None,
):
    """Record (or force-override) a palace collection's embedder identity (RFC 001).

    Backs ``mempalace palace set-embedder``. Returns ``(old, new)`` identities.
    Without ``force``, refuses to overwrite an existing identity that names a
    different model (the user must confirm they know the vectors are
    compatible). Opens with the identity check skipped so a mismatched palace —
    the exact state being repaired — can be opened at all.

    An unreadable record (see
    :class:`~mempalace.backends.base.EmbedderIdentityUnreadableError`) is
    replaced without ``force``: running this command is the confirmation the
    write refusal asks for. ``old`` is then ``None`` and ``report["unreadable"]``
    holds the read error. ``only_if_exists`` opens without creating the
    collection and returns ``None`` when it does not exist yet.
    """
    from ..backends.base import (
        CollectionNotInitializedError,
        EmbedderIdentity,
        EmbedderIdentityMismatchError,
        PalaceNotFoundError,
    )
    from ..config import MempalaceConfig
    from ..embedding import (
        _normalize_stored_model_name,
        _resolve_embedding_model,
        current_model_name,
        get_embedder_identity,
        get_embedding_function,
    )

    configured = MempalaceConfig().embedding_model
    requested = (model or "").strip().lower()
    target = requested or (configured or "").strip().lower()
    if not target:
        # No model given and none configured — there is nothing to record, and
        # recording a nameless identity is a silent no-op in every backend.
        raise ValueError(
            "no embedder model to record: pass --model NAME or configure MEMPALACE_EMBEDDING_MODEL"
        )
    if requested and not _backend_has_server_embedder(palace_path, backend):
        # Refuse a misspelled --model (UnknownEmbeddingModelError) before the
        # open below can create the palace folder, chroma.sqlite3 or a
        # collection. A server embedder's names are its own and skip this.
        _resolve_embedding_model(requested)
    try:
        collection = get_collection(
            palace_path,
            collection_name=collection_name,
            create=not only_if_exists,
            backend=backend,
            _skip_identity_check=True,
        )
    except (CollectionNotInitializedError, PalaceNotFoundError):
        if only_if_exists:
            return None
        raise
    core_embedder = _server_embedder_identity(collection) is None
    if requested and core_embedder:
        # Record the model the name embeds with, as the factory resolves it:
        # `--model all-minilm-l6-v2` is minilm. A server embedder's names are
        # its own and are recorded as given.
        target = _resolve_embedding_model(requested)
    if target == (configured or "").strip().lower():
        # Recording the in-use model — probe its dimension (already loaded).
        new = get_embedder_identity()
    elif core_embedder and target == "embeddinggemma2":
        # EmbeddingGemma 2 is recorded by its full identity (model, revision,
        # dimension, modalities), never the bare name: a bare stored
        # "embeddinggemma2" reads as a legacy MiniLM palace. Built from the
        # configured EmbeddingGemma 2 settings without loading the model.
        new = EmbedderIdentity(
            model_name=current_model_name(target),
            dimension=get_embedding_function(model=target).dimension,
        )
    else:
        # Explicit override of a non-configured model: record the name only,
        # never load a foreign model (which can be a large download) just to
        # probe a dimension. The model-name check is the actual protection.
        new = EmbedderIdentity(model_name=target, dimension=0)
    try:
        old = collection.get_stored_embedder_identity()
    except Exception as exc:
        old = None
        if report is not None:
            report["unreadable"] = str(exc)
    old_name = getattr(old, "model_name", "")
    if old is not None and core_embedder:
        # A legacy raw name that stands for the same model is not a swap.
        old_name = _normalize_stored_model_name(old_name)
    if old is not None and old_name != new.model_name and not force:
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
