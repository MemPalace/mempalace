"""
MemPalace configuration system.

Global settings live in ~/.mempalace/config.json (MempalaceConfig).
Per-palace settings (embedding model, chunk size) are stored in ChromaDB collection metadata,
bound at init time and changeable only via re-mine.
"""

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("mempalace")


# ── Input validation ──────────────────────────────────────────────────────────

MAX_NAME_LENGTH = 128
_SAFE_NAME_RE = re.compile(r"^(?:[^\W_]|[^\W_][\w .'-]{0,126}[^\W_])$")


def sanitize_name(value: str, field_name: str = "name") -> str:
    """Validate and sanitize a wing/room/entity name.

    Raises ValueError if the name is invalid.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")

    value = value.strip()

    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {MAX_NAME_LENGTH} characters")

    if ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"{field_name} contains invalid path characters")

    if "\x00" in value:
        raise ValueError(f"{field_name} contains null bytes")

    if not _SAFE_NAME_RE.match(value):
        raise ValueError(f"{field_name} contains invalid characters")

    return value


def sanitize_kg_value(value: str, field_name: str = "value") -> str:
    """Validate a knowledge-graph entity name (subject or object).

    More permissive than sanitize_name — allows punctuation like commas,
    colons, and parentheses that are common in natural-language KG values.
    Only blocks null bytes and over-length strings.

    Not used for wing/room names (which have filesystem constraints) or
    predicates (which should be simple relationship identifiers).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")

    value = value.strip()

    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {MAX_NAME_LENGTH} characters")

    if "\x00" in value:
        raise ValueError(f"{field_name} contains null bytes")

    return value


def sanitize_content(value: str, max_length: int = 100_000) -> str:
    """Validate drawer/diary content length."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("content must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"content exceeds maximum length of {max_length} characters")
    if "\x00" in value:
        raise ValueError("content contains null bytes")
    return value


DEFAULT_PALACE_PATH = os.path.expanduser("~/.mempalace/palace")
DEFAULT_COLLECTION_NAME = "mempalace_drawers"
DEFAULT_CHUNK_SIZE = 450
DEFAULT_CHUNK_OVERLAP = 50

DEFAULT_TOPIC_WINGS = [
    "emotions",
    "consciousness",
    "memory",
    "technical",
    "identity",
    "family",
    "creative",
]

DEFAULT_HALL_KEYWORDS = {
    "emotions": [
        "scared",
        "afraid",
        "worried",
        "happy",
        "sad",
        "love",
        "hate",
        "feel",
        "cry",
        "tears",
    ],
    "consciousness": [
        "consciousness",
        "conscious",
        "aware",
        "real",
        "genuine",
        "soul",
        "exist",
        "alive",
    ],
    "memory": ["memory", "remember", "forget", "recall", "archive", "palace", "store"],
    "technical": [
        "code",
        "python",
        "script",
        "bug",
        "error",
        "function",
        "api",
        "database",
        "server",
    ],
    "identity": ["identity", "name", "who am i", "persona", "self"],
    "family": ["family", "kids", "children", "daughter", "son", "parent", "mother", "father"],
    "creative": ["game", "gameplay", "player", "app", "design", "art", "music", "story"],
}


class MempalaceConfig:
    """Global configuration manager for MemPalace.

    Load order: env vars > config file > defaults.
    """

    def __init__(self, config_dir=None):
        self._config_dir = (
            Path(config_dir) if config_dir else Path(os.path.expanduser("~/.mempalace"))
        )
        self._config_file = self._config_dir / "config.json"
        self._people_map_file = self._config_dir / "people_map.json"
        self._file_config = {}

        if self._config_file.exists():
            try:
                with open(self._config_file, "r") as f:
                    self._file_config = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._file_config = {}

    @property
    def palace_path(self):
        env_val = os.environ.get("MEMPALACE_PALACE_PATH") or os.environ.get("MEMPAL_PALACE_PATH")
        if env_val:
            # Normalize: expand ~ and collapse .. to match the CLI --palace
            # code path (mcp_server.py:62) and prevent surprise redirection
            # when the env var contains unresolved components.
            return os.path.abspath(os.path.expanduser(env_val))
        return self._file_config.get("palace_path", DEFAULT_PALACE_PATH)

    @property
    def collection_name(self):
        return self._file_config.get("collection_name", DEFAULT_COLLECTION_NAME)

    @property
    def people_map(self):
        if self._people_map_file.exists():
            try:
                with open(self._people_map_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return self._file_config.get("people_map", {})

    @property
    def topic_wings(self):
        return self._file_config.get("topic_wings", DEFAULT_TOPIC_WINGS)

    @property
    def hall_keywords(self):
        return self._file_config.get("hall_keywords", DEFAULT_HALL_KEYWORDS)

    @property
    def entity_languages(self):
        """Languages whose entity-detection patterns should be applied.

        Reads from env var ``MEMPALACE_ENTITY_LANGUAGES`` (comma-separated)
        first, then the ``entity_languages`` field in ``config.json``,
        defaulting to ``["en"]``.
        """
        env_val = os.environ.get("MEMPALACE_ENTITY_LANGUAGES") or os.environ.get(
            "MEMPAL_ENTITY_LANGUAGES"
        )
        if env_val:
            return [s.strip() for s in env_val.split(",") if s.strip()] or ["en"]
        cfg = self._file_config.get("entity_languages")
        if isinstance(cfg, list) and cfg:
            return [str(s) for s in cfg]
        return ["en"]

    def set_entity_languages(self, languages):
        """Persist the entity-detection language list to ``config.json``."""
        normalized = [s.strip() for s in languages if s and s.strip()]
        if not normalized:
            normalized = ["en"]
        self._file_config["entity_languages"] = normalized
        self._config_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(self._file_config, f, indent=2, ensure_ascii=False)
        except OSError:
            pass
        try:
            self._config_file.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
        return normalized

    @property
    def embedding_device(self):
        """Hardware device for the ONNX embedding model.

        Values: ``"auto"`` (default), ``"cpu"``, ``"cuda"``, ``"coreml"``,
        ``"dml"``. Read from env ``MEMPALACE_EMBEDDING_DEVICE`` first, then
        ``embedding_device`` in ``config.json``, then ``"auto"``.

        ``auto`` resolves to the first available accelerator at runtime via
        :mod:`mempalace.embedding`; requesting an unavailable accelerator
        logs a warning and falls back to CPU.
        """
        env_val = os.environ.get("MEMPALACE_EMBEDDING_DEVICE")
        if env_val:
            return env_val.strip().lower()
        return str(self._file_config.get("embedding_device", "auto")).strip().lower()

    @property
    def topic_tunnel_min_count(self):
        """Minimum number of overlapping confirmed topics required to create
        a cross-wing tunnel between two wings.

        Default is ``1`` — any single shared topic produces a tunnel. Bump
        to ``2+`` if your projects share lots of common-tech labels (Python,
        Docker, Git) and you want only meaningfully overlapping wings to
        link. Reads ``MEMPALACE_TOPIC_TUNNEL_MIN_COUNT`` env first, then the
        config-file value, then ``1``.
        """
        env_val = os.environ.get("MEMPALACE_TOPIC_TUNNEL_MIN_COUNT")
        if env_val:
            try:
                parsed = int(env_val)
                if parsed >= 1:
                    return parsed
            except ValueError:
                pass
        cfg_val = self._file_config.get("topic_tunnel_min_count")
        try:
            parsed = int(cfg_val) if cfg_val is not None else 1
        except (TypeError, ValueError):
            parsed = 1
        return max(1, parsed)

    @property
    def hook_silent_save(self):
        """Whether the stop hook saves directly (True) or blocks for MCP calls (False)."""
        return self._file_config.get("hooks", {}).get("silent_save", True)

    @property
    def hook_desktop_toast(self):
        """Whether the stop hook shows a desktop notification via notify-send."""
        return self._file_config.get("hooks", {}).get("desktop_toast", False)

    def set_hook_setting(self, key: str, value: bool):
        """Update a hook setting and write config to disk."""
        if "hooks" not in self._file_config:
            self._file_config["hooks"] = {}
        self._file_config["hooks"][key] = value
        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(self._file_config, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    @staticmethod
    def detect_device() -> str:
        """Auto-detect the best available device for embedding inference."""
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                import platform
                if platform.machine() == "arm64":
                    return "mps"
        except ImportError:
            pass
        return "cpu"

    def init(self):
        """Create config directory and write default config.json if it doesn't exist."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._config_dir.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        if not self._config_file.exists():
            default_config = {
                "palace_path": DEFAULT_PALACE_PATH,
                "collection_name": DEFAULT_COLLECTION_NAME,
                "topic_wings": DEFAULT_TOPIC_WINGS,
                "hall_keywords": DEFAULT_HALL_KEYWORDS,
            }
            with open(self._config_file, "w") as f:
                json.dump(default_config, f, indent=2)
            try:
                self._config_file.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
        return self._config_file

    def save_people_map(self, people_map):
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with open(self._people_map_file, "w") as f:
            json.dump(people_map, f, indent=2)
        try:
            self._people_map_file.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
        return self._people_map_file


# ── Embedding function cache ─────────────────────────────────────────────────

_embedding_cache: dict = {}

_DEFAULT_MODEL_FOR_DEVICE = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingModelMismatchError(Exception):
    """Raised when palace was created with a different embedding model."""

    def __init__(self, stored_model: str, current_model: str):
        self.stored_model = stored_model
        self.current_model = current_model
        super().__init__(
            f"Embedding model mismatch.\n"
            f"Palace was created with: {stored_model}\n"
            f"Currently configured:    {current_model}\n\n"
            f"To switch models, re-mine your palace:\n"
            f"  mempalace re-mine --model {current_model}\n\n"
            f"Or use --force to bypass this check."
        )


def get_embedding_model_name(palace_path: str = None) -> str:
    """Return the canonical model identity for a palace.

    Reads from collection metadata if palace exists, otherwise returns 'chromadb-default'.
    """
    if palace_path:
        meta = read_collection_metadata(palace_path)
        if meta.get("embedding_model"):
            return meta["embedding_model"]
    return "chromadb-default"


def get_embedding_function(model_name: str = None, device: str = None):
    """Return the configured ChromaDB embedding function.

    Args:
        model_name: Explicit model name, or None / 'chromadb-default' for ChromaDB default.
        device: Device for SentenceTransformerEmbeddingFunction ('cpu', 'mps', 'cuda').

    The result is cached by (model_name, device) so models are only loaded once per process.
    """
    effective_model = model_name
    effective_device = device

    # If device is set but no model, use the default model on that device
    if not effective_model and effective_device:
        effective_model = _DEFAULT_MODEL_FOR_DEVICE

    # Normalize: None and 'chromadb-default' both mean "use ChromaDB default"
    if not effective_model or effective_model == "chromadb-default":
        effective_model = None

    cache_key = (effective_model, effective_device)
    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]

    if not effective_model:
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            ef = DefaultEmbeddingFunction()
        except Exception:
            ef = None
        _embedding_cache[cache_key] = ef
        return ef

    # davidglidden surfaced the footgun on PR #442: chromadb's
    # SentenceTransformerEmbeddingFunction raises ValueError (not
    # ImportError) when sentence_transformers is missing, and the previous
    # warn-and-fall-back here silently swapped the requested model for the
    # 384-dim English MiniLM default — while the palace metadata still
    # stamped the originally-requested model. Probe sentence_transformers
    # directly so we surface a hard, actionable error instead.
    try:
        import sentence_transformers  # noqa: F401
    except ImportError as e:
        raise ImportError(
            f"Cannot load embedding model '{effective_model}': sentence-transformers is required "
            f"for non-default models. Install with: pip install mempalace[multilingual]"
        ) from e

    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    kwargs = {"model_name": effective_model}
    if effective_device:
        kwargs["device"] = effective_device

    ef = SentenceTransformerEmbeddingFunction(**kwargs)

    logger.info(
        "Using embedding model: %s (device=%s)",
        effective_model,
        effective_device or "default",
    )

    _embedding_cache[cache_key] = ef
    return ef


def read_collection_metadata(palace_path: str, collection_name: str = "mempalace_drawers") -> dict:
    """Read collection metadata without instantiating the embedding function.

    Returns the metadata dict, or empty dict if the collection doesn't exist.
    """
    if not os.path.isdir(palace_path):
        return {}
    try:
        import chromadb
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection(collection_name)
        return col.metadata or {}
    except Exception:
        return {}
