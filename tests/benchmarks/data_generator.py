"""
Deterministic data factory for MemPalace scale benchmarks.

Generates realistic project files, conversations, and KG triples at
configurable scale levels. All randomness uses seeded RNG for reproducibility.

Planted "needle" drawers enable recall measurement without an LLM judge.
"""

import hashlib
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

import chromadb
import yaml


# ── Scale configurations ─────────────────────────────────────────────────

SCALE_CONFIGS = {
    "small": {
        "drawers": 1_000,
        "wings": 3,
        "rooms_per_wing": 5,
        "kg_entities": 50,
        "kg_triples": 200,
        "needles": 20,
        "search_queries": 20,
    },
    "medium": {
        "drawers": 10_000,
        "wings": 8,
        "rooms_per_wing": 12,
        "kg_entities": 200,
        "kg_triples": 2_000,
        "needles": 50,
        "search_queries": 50,
    },
    "large": {
        "drawers": 50_000,
        "wings": 15,
        "rooms_per_wing": 20,
        "kg_entities": 500,
        "kg_triples": 10_000,
        "needles": 100,
        "search_queries": 100,
    },
    "stress": {
        "drawers": 100_000,
        "wings": 25,
        "rooms_per_wing": 30,
        "kg_entities": 1_000,
        "kg_triples": 50_000,
        "needles": 200,
        "search_queries": 200,
    },
}

# ── Vocabulary banks for realistic content ───────────────────────────────

WING_NAMES = [
    "webapp",
    "backend_api",
    "mobile_app",
    "data_pipeline",
    "ml_platform",
    "devops",
    "auth_service",
    "payments",
    "analytics",
    "docs_site",
    "cli_tool",
    "dashboard",
    "notification_service",
    "search_engine",
    "user_mgmt",
    "inventory",
    "reporting",
    "testing_infra",
    "monitoring",
    "email_service",
    "chat_bot",
    "file_storage",
    "scheduler",
    "gateway",
    "marketplace",
]

ROOM_NAMES = [
    "backend",
    "frontend",
    "api",
    "database",
    "auth",
    "tests",
    "docs",
    "config",
    "deployment",
    "models",
    "views",
    "controllers",
    "middleware",
    "utils",
    "schemas",
    "migrations",
    "fixtures",
    "scripts",
    "styles",
    "components",
    "hooks",
    "services",
    "routes",
    "templates",
    "static",
    "media",
    "logging",
    "cache",
    "queue",
    "workers",
]

TECH_TERMS = [
    "authentication",
    "authorization",
    "middleware",
    "endpoint",
    "REST API",
    "GraphQL",
    "WebSocket",
    "database migration",
    "ORM",
    "query optimization",
    "caching strategy",
    "load balancer",
    "rate limiting",
    "pagination",
    "serialization",
    "validation",
    "error handling",
    "logging framework",
    "monitoring",
    "deployment pipeline",
    "CI/CD",
    "containerization",
    "microservice",
    "event sourcing",
    "message queue",
    "pub/sub",
    "connection pooling",
    "session management",
    "token refresh",
    "CORS",
    "SSL termination",
    "health check",
    "circuit breaker",
    "retry logic",
    "batch processing",
    "stream processing",
    "data pipeline",
    "ETL",
    "feature flag",
    "A/B testing",
    "blue-green deployment",
    "canary release",
]

CODE_SNIPPETS = [
    "def process_request(data):\n    validated = schema.validate(data)\n    result = handler.execute(validated)\n    return Response(result, status=200)\n",
    "class UserRepository:\n    def __init__(self, db):\n        self.db = db\n    def find_by_id(self, user_id):\n        return self.db.query(User).filter(User.id == user_id).first()\n",
    "async def fetch_data(url, timeout=30):\n    async with aiohttp.ClientSession() as session:\n        async with session.get(url, timeout=timeout) as resp:\n            return await resp.json()\n",
    "const handleSubmit = async (formData) => {\n  try {\n    const response = await api.post('/users', formData);\n    dispatch({ type: 'USER_CREATED', payload: response.data });\n  } catch (error) {\n    setError(error.message);\n  }\n};\n",
    "SELECT u.name, COUNT(o.id) as order_count\nFROM users u\nLEFT JOIN orders o ON u.id = o.user_id\nWHERE u.created_at > '2025-01-01'\nGROUP BY u.name\nHAVING COUNT(o.id) > 5\nORDER BY order_count DESC;\n",
]

PROSE_TEMPLATES = [
    "The {component} module handles {task}. It was refactored in {month} to improve {quality}. Key design decision: {decision}.",
    "Bug report: {component} fails when {condition}. Root cause: {cause}. Fixed by {fix}. Regression test added in {test_file}.",
    "Architecture decision: switched from {old_tech} to {new_tech} for {reason}. Migration completed {date}. Performance improved by {percent}%.",
    "Meeting notes: discussed {topic} with {person}. Agreed to {action}. Deadline: {deadline}. Follow-up: {followup}.",
    "Feature spec: {feature_name} allows users to {capability}. Dependencies: {deps}. Estimated effort: {effort} days.",
]

ENTITY_NAMES = [
    "Alice",
    "Bob",
    "Carol",
    "Dave",
    "Eve",
    "Frank",
    "Grace",
    "Heidi",
    "Ivan",
    "Judy",
    "Karl",
    "Linda",
    "Mike",
    "Nina",
    "Oscar",
    "Pat",
    "Quinn",
    "Rita",
    "Steve",
    "Tina",
    "Ursula",
    "Victor",
    "Wendy",
    "Xander",
]

ENTITY_TYPES = ["person", "project", "tool", "concept", "team", "service"]

PREDICATES = [
    "works_on",
    "manages",
    "reports_to",
    "collaborates_with",
    "created",
    "maintains",
    "uses",
    "depends_on",
    "replaced",
    "reviewed",
    "deployed",
    "tested",
    "documented",
    "mentors",
    "leads",
    "contributes_to",
]


class PalaceDataGenerator:
    """Generate deterministic, realistic test data at configurable scale."""

    def __init__(self, seed=42, scale="small"):
        self.rng = random.Random(seed)
        self.scale = scale
        self.cfg = SCALE_CONFIGS[scale]
        self.wings = WING_NAMES[: self.cfg["wings"]]
        self.rooms_by_wing = {}
        for wing in self.wings:
            n = self.cfg["rooms_per_wing"]
            rooms = self.rng.sample(ROOM_NAMES, min(n, len(ROOM_NAMES)))
            self.rooms_by_wing[wing] = rooms
        # Planted needles for recall measurement
        self.needles = []
        self._generate_needles()

    def _generate_needles(self):
        """Create unique needle content for recall testing."""
        topics = [
            "Fibonacci sequence optimization uses memoization with O(n) space complexity",
            "PostgreSQL vacuum autovacuum threshold set to 50 percent for table users",
            "Redis cluster failover timeout configured at 30 seconds with sentinel monitoring",
            "Kubernetes horizontal pod autoscaler targets 70 percent CPU utilization",
            "GraphQL subscription uses WebSocket transport with heartbeat interval 25 seconds",
            "JWT token rotation policy requires refresh every 15 minutes with sliding window",
            "Elasticsearch index sharding strategy uses 5 primary shards with 1 replica each",
            "Docker multi-stage build reduces image size from 1.2GB to 180MB for production",
            "Apache Kafka consumer group rebalance timeout set to 45 seconds",
            "MongoDB change streams resume token persisted every 100 operations",
            "gRPC streaming uses bidirectional flow control with 64KB window size",
            "Prometheus alerting rule fires when p99 latency exceeds 500ms for 5 minutes",
            "Terraform state locking uses DynamoDB with consistent reads enabled",
            "Nginx rate limiting configured at 100 requests per second with burst of 50",
            "SQLAlchemy connection pool size set to 20 with max overflow of 10 connections",
            "React concurrent mode uses startTransition for non-urgent state updates",
            "AWS Lambda cold start mitigation uses provisioned concurrency of 10 instances",
            "Git bisect automated with custom test script for regression hunting",
            "OpenTelemetry trace sampling rate set to 10 percent in production environment",
            "Celery worker prefetch multiplier set to 1 for fair task distribution",
        ]
        for i in range(self.cfg["needles"]):
            topic = topics[i % len(topics)]
            wing = self.rng.choice(self.wings)
            room = self.rng.choice(self.rooms_by_wing[wing])
            needle_id = f"NEEDLE_{i:04d}"
            content = f"{needle_id}: {topic}. This is a unique planted needle for recall benchmarking at scale."
            self.needles.append(
                {
                    "id": needle_id,
                    "content": content,
                    "wing": wing,
                    "room": room,
                    "query": topic.split(" uses ")[0]
                    if " uses " in topic
                    else topic.split(" set to ")[0]
                    if " set to " in topic
                    else topic[:60],
                }
            )

    def _random_text(self, min_chars=600, max_chars=900):
        """Generate a random text block of realistic content."""
        parts = []
        total = 0
        target = self.rng.randint(min_chars, max_chars)
        while total < target:
            choice = self.rng.random()
            if choice < 0.3:
                text = self.rng.choice(CODE_SNIPPETS)
            elif choice < 0.7:
                template = self.rng.choice(PROSE_TEMPLATES)
                text = template.format(
                    component=self.rng.choice(ROOM_NAMES),
                    task=self.rng.choice(TECH_TERMS),
                    month=self.rng.choice(["January", "February", "March", "April", "May"]),
                    quality=self.rng.choice(
                        ["performance", "readability", "test coverage", "latency"]
                    ),
                    decision=self.rng.choice(TECH_TERMS),
                    condition=self.rng.choice(TECH_TERMS) + " is null",
                    cause=self.rng.choice(["race condition", "null pointer", "timeout", "OOM"]),
                    fix="adding " + self.rng.choice(TECH_TERMS),
                    test_file=f"test_{self.rng.choice(ROOM_NAMES)}.py",
                    old_tech=self.rng.choice(["MySQL", "Flask", "REST", "Jenkins"]),
                    new_tech=self.rng.choice(
                        ["PostgreSQL", "FastAPI", "GraphQL", "GitHub Actions"]
                    ),
                    reason=self.rng.choice(TECH_TERMS),
                    date=f"2025-{self.rng.randint(1, 12):02d}-{self.rng.randint(1, 28):02d}",
                    percent=self.rng.randint(10, 80),
                    topic=self.rng.choice(TECH_TERMS),
                    person=self.rng.choice(ENTITY_NAMES),
                    action=self.rng.choice(["refactor", "migrate", "optimize", "test"]),
                    deadline=f"2025-{self.rng.randint(1, 12):02d}-{self.rng.randint(1, 28):02d}",
                    followup=self.rng.choice(TECH_TERMS),
                    feature_name=self.rng.choice(TECH_TERMS),
                    capability=self.rng.choice(TECH_TERMS),
                    deps=", ".join(self.rng.sample(TECH_TERMS, 2)),
                    effort=self.rng.randint(1, 15),
                )
            else:
                words = self.rng.sample(TECH_TERMS, min(5, len(TECH_TERMS)))
                text = (
                    " ".join(words)
                    + ". "
                    + self.rng.choice(TECH_TERMS)
                    + " implementation details follow.\n"
                )
            parts.append(text)
            total += len(text)
        return "\n".join(parts)[:max_chars]

    # ── Project tree generation (for mine() tests) ───────────────────────

    def generate_project_tree(self, base_path, wing=None, rooms=None, n_files=50):
        """
        Write realistic project files + mempalace.yaml to base_path.

        Returns the project path suitable for passing to mine().
        """
        base = Path(base_path)
        base.mkdir(parents=True, exist_ok=True)
        wing = wing or self.rng.choice(self.wings)
        rooms = rooms or self.rooms_by_wing.get(wing, ["general"])

        # Write mempalace.yaml
        room_defs = [{"name": r, "description": f"{r} code and docs"} for r in rooms]
        with open(base / "mempalace.yaml", "w") as f:
            yaml.dump({"wing": wing, "rooms": room_defs}, f)

        # Write files distributed across room directories
        files_written = 0
        for i in range(n_files):
            room = rooms[i % len(rooms)]
            room_dir = base / room
            room_dir.mkdir(parents=True, exist_ok=True)

            ext = self.rng.choice([".py", ".js", ".md", ".ts", ".yaml"])
            filename = f"file_{i:04d}{ext}"
            content = self._random_text(400, 2000)
            (room_dir / filename).write_text(content, encoding="utf-8")
            files_written += 1

        return str(base), wing, rooms, files_written

    # ── Conversation file generation (for mine_convos() tests) ───────────

    def generate_conversation_files(self, base_path, wing=None, n_files=20):
        """Write conversation transcript files for convo_miner tests."""
        base = Path(base_path)
        base.mkdir(parents=True, exist_ok=True)
        wing = wing or self.rng.choice(self.wings)

        for i in range(n_files):
            lines = []
            n_exchanges = self.rng.randint(5, 20)
            for j in range(n_exchanges):
                user_msg = f"> User: {self.rng.choice(TECH_TERMS)}? How does {self.rng.choice(TECH_TERMS)} work with {self.rng.choice(TECH_TERMS)}?"
                ai_msg = self._random_text(200, 600)
                lines.append(user_msg)
                lines.append(ai_msg)
                lines.append("")

            (base / f"convo_{i:04d}.txt").write_text("\n".join(lines), encoding="utf-8")

        return str(base), wing

    # ── Direct palace population (bypasses mining for speed) ─────────────

    def populate_palace_directly(self, palace_path, n_drawers=None, include_needles=True):
        """
        Insert drawers directly into ChromaDB, bypassing the mining pipeline.

        Much faster than mining for benchmarks that only care about
        search/MCP behavior on a pre-populated palace.

        Returns (client, collection, needle_info).
        """
        n_drawers = n_drawers or self.cfg["drawers"]
        os.makedirs(palace_path, exist_ok=True)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        batch_size = 500
        docs = []
        ids = []
        metas = []

        # Insert needles first
        needle_info = []
        if include_needles:
            for needle in self.needles:
                needle_id = f"drawer_{needle['wing']}_{needle['room']}_{hashlib.md5(needle['id'].encode()).hexdigest()[:16]}"
                docs.append(needle["content"])
                ids.append(needle_id)
                metas.append(
                    {
                        "wing": needle["wing"],
                        "room": needle["room"],
                        "source_file": f"needle_{needle['id']}.txt",
                        "chunk_index": 0,
                        "added_by": "benchmark",
                        "filed_at": datetime.now().isoformat(),
                    }
                )
                needle_info.append(
                    {
                        "id": needle_id,
                        "query": needle["query"],
                        "wing": needle["wing"],
                        "room": needle["room"],
                    }
                )

        # Fill remaining drawers with realistic content
        remaining = n_drawers - len(docs)
        for i in range(remaining):
            wing = self.wings[i % len(self.wings)]
            rooms = self.rooms_by_wing[wing]
            room = rooms[i % len(rooms)]
            content = self._random_text(400, 800)
            drawer_id = f"drawer_{wing}_{room}_{hashlib.md5(f'gen_{i}'.encode()).hexdigest()[:16]}"

            docs.append(content)
            ids.append(drawer_id)
            metas.append(
                {
                    "wing": wing,
                    "room": room,
                    "source_file": f"generated_{i:06d}.txt",
                    "chunk_index": i % 10,
                    "added_by": "benchmark",
                    "filed_at": datetime.now().isoformat(),
                }
            )

            # Flush in batches
            if len(docs) >= batch_size:
                col.add(documents=docs, ids=ids, metadatas=metas)
                docs, ids, metas = [], [], []

        # Flush remainder
        if docs:
            col.add(documents=docs, ids=ids, metadatas=metas)

        return client, col, needle_info

    # ── KG triple generation ─────────────────────────────────────────────

    def generate_kg_triples(self, n_entities=None, n_triples=None):
        """
        Generate realistic entity-relationship triples.

        Returns (entities, triples) where:
          entities = [(name, type), ...]
          triples = [(subject, predicate, object, valid_from, valid_to), ...]
        """
        n_entities = n_entities or self.cfg["kg_entities"]
        n_triples = n_triples or self.cfg["kg_triples"]

        # Generate entities
        entities = []
        entity_names = []
        for i in range(n_entities):
            if i < len(ENTITY_NAMES):
                name = ENTITY_NAMES[i]
            else:
                name = f"Entity_{i:04d}"
            etype = self.rng.choice(ENTITY_TYPES)
            entities.append((name, etype))
            entity_names.append(name)

        # Generate triples
        triples = []
        base_date = datetime(2024, 1, 1)
        for i in range(n_triples):
            subject = self.rng.choice(entity_names)
            obj = self.rng.choice(entity_names)
            while obj == subject:
                obj = self.rng.choice(entity_names)
            predicate = self.rng.choice(PREDICATES)
            days_offset = self.rng.randint(0, 730)
            valid_from = (base_date + timedelta(days=days_offset)).strftime("%Y-%m-%d")
            # 30% chance of having a valid_to
            valid_to = None
            if self.rng.random() < 0.3:
                end_offset = self.rng.randint(30, 365)
                valid_to = (base_date + timedelta(days=days_offset + end_offset)).strftime(
                    "%Y-%m-%d"
                )
            triples.append((subject, predicate, obj, valid_from, valid_to))

        return entities, triples

    # ── Search query generation ──────────────────────────────────────────

    def generate_search_queries(self, n_queries=None):
        """
        Generate search queries with expected results.

        Returns list of {"query": str, "expected_wing": str|None, "expected_room": str|None, "is_needle": bool}.
        Needle queries have known-good answers for recall measurement.
        """
        n_queries = n_queries or self.cfg["search_queries"]
        queries = []

        # Half are needle queries (known-good answers)
        n_needle = min(n_queries // 2, len(self.needles))
        for needle in self.needles[:n_needle]:
            queries.append(
                {
                    "query": needle["query"],
                    "expected_wing": needle["wing"],
                    "expected_room": needle["room"],
                    "needle_id": needle["id"],
                    "is_needle": True,
                }
            )

        # Other half are generic queries (measure latency, not recall)
        n_generic = n_queries - n_needle
        for _ in range(n_generic):
            queries.append(
                {
                    "query": self.rng.choice(TECH_TERMS) + " " + self.rng.choice(TECH_TERMS),
                    "expected_wing": None,
                    "expected_room": None,
                    "needle_id": None,
                    "is_needle": False,
                }
            )

        self.rng.shuffle(queries)
        return queries
