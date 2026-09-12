// Count memories by wing
MATCH (m:Memory)-[:BELONGS_TO]->(:Drawer)-[:IN_CLOSET]->(:Closet)-[:IN_ROOM]->(:Room)-[:IN_WING]->(w:Wing)
WHERE m.sync_deleted_at IS NULL
RETURN w.name AS wing, count(m) AS memories
ORDER BY memories DESC;

// Full palace path
MATCH (m:Memory)-[:BELONGS_TO]->(d:Drawer)-[:IN_CLOSET]->(c:Closet)-[:IN_ROOM]->(r:Room)-[:IN_WING]->(w:Wing)
WHERE m.sync_deleted_at IS NULL
RETURN m.title AS memory,
       w.name AS wing,
       r.name AS room,
       c.name AS closet,
       d.name AS drawer,
       m.source_path AS source,
       m.source_record_locator AS locator
ORDER BY wing, room, closet, drawer, memory;

// Readable Chroma-backed memories with previews
MATCH (m:Memory)-[:BELONGS_TO]->(d:Drawer)-[:IN_CLOSET]->(c:Closet)-[:IN_ROOM]->(r:Room)-[:IN_WING]->(w:Wing)
WHERE m.sync_deleted_at IS NULL
  AND m.source_record_locator STARTS WITH 'chroma:embedding:'
RETURN m.id AS memory_id,
       m.title AS title,
       w.name AS wing,
       r.name AS room,
       c.name AS hall,
       m.snippet AS preview,
       m.source_record_locator AS locator
ORDER BY wing, room, title
LIMIT 100;

// File-backed graph for Browser or Bloom
MATCH path = (m:Memory)-[:BELONGS_TO]->(:Drawer)-[:IN_CLOSET]->(:Closet)-[:IN_ROOM]->(:Room)-[:IN_WING]->(:Wing)
WHERE m.source_path IS NOT NULL AND m.sync_deleted_at IS NULL
RETURN path
LIMIT 300;

// Memories by topic
MATCH (m:Memory)-[:ABOUT]->(t:Topic)
WHERE m.sync_deleted_at IS NULL
RETURN t.name AS topic, count(m) AS memories
ORDER BY memories DESC;

// Recently synced memories
MATCH (m:Memory)
WHERE m.last_synced_at IS NOT NULL
RETURN m.title,
       m.source_path,
       m.source_record_locator,
       m.last_synced_at
ORDER BY m.last_synced_at DESC
LIMIT 50;

// Soft-deleted memories
MATCH (m:Memory)
WHERE m.sync_deleted_at IS NOT NULL
RETURN m.id,
       m.title,
       m.source_path,
       m.sync_deleted_at
ORDER BY m.sync_deleted_at DESC;

// Source files
MATCH (sf:SourceFile)<-[:FROM_FILE]-(m:Memory)
RETURN sf.path AS source_file, count(m) AS memories
ORDER BY memories DESC;
