-- MemPalace Knowledge Graph Schema
-- SQLite database at ~/.mempalace/knowledge_graph.db

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'unknown',
    properties TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS triples (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    confidence REAL DEFAULT 1.0,
    source_closet TEXT,
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS attributes (
    entity_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    valid_from TEXT,
    valid_to TEXT,
    PRIMARY KEY (entity_id, key, valid_from)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object);
CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to);
