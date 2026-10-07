from __future__ import annotations

from pathlib import Path

from neo4j import GraphDatabase

from .models import MemoryRecord, RelationshipRecord, SourcePointer
from .normalizer import neo4j_memory_payload


class Neo4jClient:
    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j", store_content: bool = False) -> None:
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.database = database
        self.store_content = store_content

    def close(self) -> None:
        self.driver.close()

    def verify_connectivity(self) -> None:
        self.driver.verify_connectivity()

    def create_schema(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts" / "create_schema.cypher"
        statements = [stmt.strip() for stmt in script.read_text().split(";") if stmt.strip()]
        with self.driver.session(database=self.database) as session:
            for stmt in statements:
                session.run(stmt)

    def upsert_memory_records(self, records: list[MemoryRecord]) -> None:
        with self.driver.session(database=self.database) as session:
            for record in records:
                session.execute_write(self._upsert_memory, record, self.store_content)

    def upsert_relationship_records(self, relationships: list[RelationshipRecord]) -> None:
        with self.driver.session(database=self.database) as session:
            for rel in relationships:
                session.execute_write(self._upsert_relationship, rel)

    def soft_delete_memories(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (m:Memory) WHERE m.id IN $ids SET m.sync_deleted_at = datetime(), m.last_synced_at = datetime()", ids=memory_ids)

    def hard_delete_memories(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (m:Memory) WHERE m.id IN $ids DETACH DELETE m", ids=memory_ids)

    def get_memory_source(self, memory_id: str) -> SourcePointer:
        with self.driver.session(database=self.database) as session:
            row = session.run("MATCH (m:Memory {id: $id}) RETURN m.source_path AS path, m.source_record_locator AS locator", id=memory_id).single()
            if not row:
                raise LookupError(f"Memory {memory_id} not found in Neo4j")
            return SourcePointer(row["path"], row["locator"])

    def check_no_duplication(self) -> int:
        with self.driver.session(database=self.database) as session:
            row = session.run(
                """
                MATCH (m:Memory)
                WHERE any(key IN keys(m) WHERE key IN ['content', 'raw_payload', 'full_text', 'body', 'text'])
                RETURN count(m) AS duplicated_nodes
                """
            ).single()
            return int(row["duplicated_nodes"] if row else 0)

    @staticmethod
    def _upsert_memory(tx, record: MemoryRecord, store_content: bool) -> None:
        room_key = f"{record.wing}/{record.room}"
        closet_key = f"{room_key}/{record.closet}"
        drawer_key = f"{closet_key}/{record.drawer}"
        payload = neo4j_memory_payload(record, store_content)
        tx.run(
            """
            MERGE (m:Memory {id: $payload.id})
            SET m += $payload,
                m.last_synced_at = datetime(),
                m.sync_deleted_at = null
            MERGE (w:Wing {name: $wing})
            MERGE (r:Room {key: $room_key})
            SET r.name = $room
            MERGE (c:Closet {key: $closet_key})
            SET c.name = $closet
            MERGE (d:Drawer {key: $drawer_key})
            SET d.name = $drawer
            MERGE (d)-[:IN_CLOSET]->(c)
            MERGE (c)-[:IN_ROOM]->(r)
            MERGE (r)-[:IN_WING]->(w)
            MERGE (m)-[:BELONGS_TO]->(d)
            MERGE (sf:SourceFile {path: $payload.source_path})
            SET sf.modified_at = $payload.source_modified_at,
                sf.hash = $payload.source_file_hash
            MERGE (m)-[:FROM_FILE]->(sf)
            FOREACH (person IN $people |
              MERGE (p:Person {name: person})
              MERGE (m)-[:MENTIONS]->(p)
            )
            FOREACH (topic IN $topics |
              MERGE (t:Topic {name: topic})
              MERGE (m)-[:ABOUT]->(t)
            )
            FOREACH (project IN $projects |
              MERGE (p:Project {name: project})
              MERGE (m)-[:RELATED_TO_PROJECT]->(p)
            )
            FOREACH (tag IN $tags |
              MERGE (t:Tag {name: tag})
              MERGE (m)-[:TAGGED_AS]->(t)
            )
            """,
            payload=payload,
            wing=record.wing,
            room=record.room,
            closet=record.closet,
            drawer=record.drawer,
            room_key=room_key,
            closet_key=closet_key,
            drawer_key=drawer_key,
            people=record.people,
            topics=record.topics,
            projects=record.projects,
            tags=record.tags,
        )

    @staticmethod
    def _upsert_relationship(tx, rel: RelationshipRecord) -> None:
        cypher = "SIMILAR_TO" if rel.relationship_type.upper() in {"SIMILAR_TO", "SIMILAR", "SIMILARITY"} or rel.score is not None else "RELATED_TO"
        tx.run(
            f"""
            MATCH (a:Memory {{id: $source}})
            MATCH (b:Memory {{id: $target}})
            MERGE (a)-[r:{cypher}]->(b)
            SET r.relationship_type = $relationship_type,
                r.score = $score
            """,
            source=rel.source_memory_id,
            target=rel.target_memory_id,
            relationship_type=rel.relationship_type,
            score=rel.score,
        )
