"""MemPalace taxonomy canonicalization — SBAI-6290 / SBAI-7040.

Collapse drifted wing/room aliases into canonical slugs (lowercase-hyphen),
validate on write, and assert non-empty canonical set at startup.

Quick usage in mcp_server.py tool_add_drawer:

    from .taxonomy import validate_wing_room
    wing, room = validate_wing_room(wing, room)  # Call before persistence

Quick usage at startup:

    from .taxonomy import assert_canonical_set_nonempty
    assert_canonical_set_nonempty()  # raises if broken
"""

import re
from typing import Optional, Tuple, Dict, Set

# ── Slug normalization ───────────────────────────────────────────────────────

_CANONICAL_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_slug(value: str) -> str:
    """Convert any slug variant to canonical lowercase-hyphen form.

    Rules:
      - Lowercase everything
      - Replace underscores with hyphens
      - Collapse consecutive hyphens/underscores to single hyphen
      - Strip leading/trailing hyphens
      - Remove "wing_" prefix variants (wing_, wing-)
    """
    if not value or not value.strip():
        raise ValueError("slug cannot be empty")

    s = value.strip().lower()
    s = s.replace("_", "-").replace(" ", "-")
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip("-")
    if not s:
        raise ValueError(f"slug normalized to empty string: {value!r}")
    return s


def is_canonical_slug(value: str) -> bool:
    """Check if a value is already in canonical slug form."""
    return bool(_CANONICAL_SLUG_RE.match(value))


# ── Canonical wings ──────────────────────────────────────────────────────────

CANONICAL_WINGS: Set[str] = {
    # From config.json topic_wings
    "pipeline",
    "core",
    "cloud",
    "app",
    "infra",
    "tickets",
    "architecture",
    # Observed legitimate wings
    "codex-manager",
    "model-manager",
    "studiobrain-manager",
    # Agent diary wings
    "user",
    "agent",
    "team",
    "code",
    "myproject",
    "hardware",
    "ue5",
    "ai-research",
}


# ── Wing alias map ───────────────────────────────────────────────────────────

WING_ALIASES: Dict[str, str] = {
    # codex-manager variants
    "wing_codex_manager": "codex-manager",
    "wing-codex-manager": "codex-manager",
    "codex-manager": "codex-manager",
    "codex_manager": "codex-manager",

    # model-manager variants
    "model-manager": "model-manager",
    "model_manager": "model-manager",
    "wing_model-manager": "model-manager",
    "wing_model_manager": "model-manager",

    # studiobrain-manager variants
    "wing_manager": "studiobrain-manager",
    "wing_manager-codex": "studiobrain-manager",
    "wing-studiobrain-manager-codex": "studiobrain-manager",
    "wing_studiobrain-manager-codex": "studiobrain-manager",
    "wing_studiobrain-manager": "studiobrain-manager",
    "wing-studiobrain-manager": "studiobrain-manager",
    "studiobrain-manager": "studiobrain-manager",
    "studiobrain_manager": "studiobrain-manager",

    # One-off accident wings
    "SBAI-2326": "tickets",
    "studiobrain-docs": "architecture",
    "wing_claudeqa": "pipeline",
    "wing_qwen-agent": "pipeline",
    "charactercrew": "app",
}


# ── Canonical rooms per wing ─────────────────────────────────────────────────

CANONICAL_ROOMS: Dict[str, Set[str]] = {
    "pipeline": {
        "agent", "worker", "manager", "boss", "dispatch",
        "triage", "acpx", "brainmon", "diary",
    },
    "core": {
        "backend", "frontend", "rust", "sb-server", "axum",
        "routes", "sdk", "storage", "models", "tests",
    },
    "cloud": {
        "billing", "tenant", "storage", "metering",
        "brainbits", "k8s", "argocd",
    },
    "app": {
        "desktop", "mobile", "tauri", "capacitor",
        "fileserver", "rclone", "bore", "sidecar",
    },
    "infra": {
        "k3s", "proxmox", "docker", "systemd",
        "firewall", "dns", "lxc", "nas", "network",
        "wifi", "vpn", "monitoring", "secrets",
    },
    "tickets": {
        "sbai", "jira", "pr", "merge", "review",
        "planning", "triage",
    },
    "architecture": {
        "design", "decision", "migration", "refactor",
        "repo-boundaries", "di", "patterns",
    },
    "codex-manager": {
        "sessions", "agents", "queue", "config",
    },
    "model-manager": {
        "models", "inference", "embeddings", "local",
    },
    "studiobrain-manager": {
        "brainmon", "workers", "agents", "pipeline",
        "config", "logs", "alerts",
    },
    "user": {
        "diary", "preferences", "projects", "contacts",
    },
    "agent": {
        "diary", "observations", "learnings",
    },
    "team": {
        "decisions", "meetings", "plans",
    },
    "code": {
        "features", "bugs", "refactors", "reviews",
    },
    "myproject": {
        "planning", "design", "tasks", "notes",
    },
    "hardware": {
        "brainz", "nas", "networking", "gpus",
    },
    "ue5": {
        "verse", "unreal", "assets", "maps",
    },
    "ai-research": {
        "papers", "models", "experiments", "benchmarks",
    },
}


# ── Room alias map ───────────────────────────────────────────────────────────

ROOM_ALIASES: Dict[str, Tuple[str, str]] = {
    # Case variants
    "BrainMon": ("studiobrain-manager", "brainmon"),
    "BRAINMON": ("studiobrain-manager", "brainmon"),
    "brainmon": ("studiobrain-manager", "brainmon"),

    # From mempalace.yaml room names
    "studiobrain_rust": ("core", "rust"),
    "studiobrain-rust": ("core", "rust"),
    "studiobrain_k8s_manifests": ("cloud", "k8s"),
    "studiobrain-k8s-manifests": ("cloud", "k8s"),
    "documentation": ("architecture", "design"),
    "docs": ("architecture", "design"),
    "studiobrain_proprietary": ("core", "backend"),
    "studiobrain-proprietary": ("core", "backend"),
    "accounts": ("cloud", "billing"),
    "logs": ("studiobrain-manager", "logs"),
    "backups": ("infra", "nas"),
    "docs_repo": ("architecture", "design"),
    "docs-repo": ("architecture", "design"),
    "community_plugins": ("core", "frontend"),
    "community-plugins": ("core", "frontend"),
    "core_worktrees": ("core", "backend"),
    "core-worktrees": ("core", "backend"),
    "templates": ("core", "frontend"),
    "monitoring": ("infra", "monitoring"),
    "studiobrain_model_manager": ("model-manager", "models"),
    "studiobrain-model-manager": ("model-manager", "models"),
    "app_worktrees": ("app", "desktop"),
    "app-worktrees": ("app", "desktop"),
    "landing": ("core", "frontend"),
    "plans": ("tickets", "planning"),
    "ai_worktrees": ("ai-research", "experiments"),
    "ai-worktrees": ("ai-research", "experiments"),
    "scripts": ("infra", "docker"),
    "frontend": ("core", "frontend"),
    "configuration": ("infra", "k3s"),
    "deploy": ("infra", "k3s"),
    "backend": ("core", "backend"),
    "api": ("core", "backend"),
    "testing": ("core", "tests"),
    "tests": ("core", "tests"),
    "general": ("core", "backend"),
}


# ── Resolution functions ─────────────────────────────────────────────────────


def resolve_wing(value: str) -> str:
    """Resolve a wing name (possibly aliased) to its canonical form.

    Raises ValueError if the wing cannot be resolved.
    """
    normalized = normalize_slug(value)

    if normalized in CANONICAL_WINGS:
        return normalized

    if normalized in WING_ALIASES:
        return WING_ALIASES[normalized]

    if value in WING_ALIASES:
        return WING_ALIASES[value]

    raise ValueError(f"Unknown wing: {value!r} (normalized: {normalized!r})")


def resolve_room(value: str, wing: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Resolve a room name to (canonical_wing, canonical_room).

    If wing is provided, the room is resolved within that wing's context.
    """
    normalized = normalize_slug(value)

    if value in ROOM_ALIASES:
        return ROOM_ALIASES[value]
    if normalized in ROOM_ALIASES:
        return ROOM_ALIASES[normalized]

    if wing is not None:
        canonical_wing = resolve_wing(wing)
        if canonical_wing in CANONICAL_ROOMS:
            allowed = CANONICAL_ROOMS[canonical_wing]
            if normalized not in allowed:
                return (canonical_wing, normalized)
        return (canonical_wing, normalized)

    return (None, normalized)


def validate_wing_room(wing: str, room: str) -> Tuple[str, str]:
    """Validate and normalize wing/room pair at the write boundary.

    Returns (canonical_wing, canonical_room).
    Raises ValueError if the wing/room cannot be resolved.

    This is the function to call from tool_add_drawer before writing.
    """
    canonical_wing = resolve_wing(wing)
    _, canonical_room = resolve_room(room, wing=canonical_wing)
    return (canonical_wing, canonical_room)


def is_canonical_wing(value: str) -> bool:
    """Check if a wing is already canonical."""
    normalized = normalize_slug(value)
    return normalized in CANONICAL_WINGS


def is_canonical_room(room: str, wing: str) -> bool:
    """Check if a room is canonical for a given wing."""
    try:
        canonical_wing = resolve_wing(wing)
    except ValueError:
        return False
    normalized = normalize_slug(room)
    if canonical_wing not in CANONICAL_ROOMS:
        return False
    return normalized in CANONICAL_ROOMS[canonical_wing]


# ── Startup assertion ────────────────────────────────────────────────────────


def assert_canonical_set_nonempty():
    """Assert that the canonical wing set is non-empty.

    This is a startup guard — an empty canonical set means the taxonomy
    is broken and cannot fail fast.

    Raises RuntimeError if the canonical set is empty.
    """
    if not CANONICAL_WINGS:
        raise RuntimeError(
            "MemPalace canonical wing set is EMPTY — taxonomy is broken. "
            "This is a startup assertion failure (SBAI-6290)."
        )
    for wing in CANONICAL_WINGS:
        if wing in CANONICAL_ROOMS and not CANONICAL_ROOMS[wing]:
            raise RuntimeError(
                f"MemPalace canonical wing '{wing}' has NO rooms defined — "
                "taxonomy is incomplete (SBAI-6290)."
            )
