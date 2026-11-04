"""Phone notification storage helpers."""

from commonplace import events

SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_push_subscriptions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    from_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_push_active
    ON phone_push_subscriptions(workspace_id, user_id) WHERE active = 1;
CREATE TABLE IF NOT EXISTS phone_push_state (
    subscription_id TEXT NOT NULL REFERENCES phone_push_subscriptions(id),
    number INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('needs_you', 'blocked', 'done')),
    entered_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subscription_id, number, category)
);
CREATE TABLE IF NOT EXISTS phone_push_deliveries (
    id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES phone_push_subscriptions(id),
    workspace_id TEXT NOT NULL,
    number INTEGER,
    category TEXT NOT NULL CHECK (category IN ('needs_you', 'blocked', 'done', 'test')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'retry', 'accepted', 'dead', 'cancelled')),
    attempt INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error TEXT,
    next_at REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    accepted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_phone_push_due
    ON phone_push_deliveries(status, next_at);
CREATE INDEX IF NOT EXISTS idx_phone_push_subscription
    ON phone_push_deliveries(subscription_id, number, category);
"""


def ensure(db):
    events.ensure_schema(db)
    with db._lock:
        db._conn.executescript(SCHEMA)
        db._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) "
            "SELECT 'phone_push_seq', CAST(COALESCE(MAX(seq), 0) AS TEXT) FROM events"
        )


def meta_get(db, key):
    with db._lock:
        row = db._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
