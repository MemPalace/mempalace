#!/usr/bin/env python3
"""
benchmarks/test_ollama_ornith_routine.py — Extensive Testing of ornith-1.5:9b on Routine Palace Usage.

Compares and evaluates:
1. Lightweight 3-tool MCP (palace_query, palace_exec, palace_coordinate)
2. Legacy 45-tool MCP (mempalace_*)

Evaluates across 20 representative routine palace operations:
- Semantic search & recall
- Knowledge graph entity & temporal queries
- Memory filing & updates
- Knowledge graph mutations (add, invalidate, supersede)
- Cross-wing tunnels & graph traversal
- Agent diary recording & retrieval
- Multi-agent logstream coordination & task delegation
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chromadb

from mempalace.config import MempalaceConfig
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.logstream import Logstream
from mempalace import mcp_server, mcp_light_server
from mempalace.mcp_light_server import LIGHT_TOOLS, tool_palace_query, tool_palace_exec, tool_palace_coordinate
from mempalace.mcp_server import TOOLS as LEGACY_TOOLS
from mempalace.palace_graph import create_tunnel, invalidate_graph_cache

OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://localhost:11434/api/chat")
MODEL_NAME = "ornith-1.5:9b"


@dataclass
class TestCase:
    id: str
    category: str
    user_prompt: str
    expected_tool_type: str  # "query", "exec", "coordinate" or legacy tool name
    expected_keywords_in_query: List[str]
    description: str


TEST_SUITE: List[TestCase] = [
    # ── Category 1: Recall & Search ──────────────────────────────────────────
    TestCase(
        id="recall_jwt_auth",
        category="Recall & Search",
        user_prompt="What did we decide about JWT expiration and refresh token storage in the backend?",
        expected_tool_type="query",
        expected_keywords_in_query=["jwt", "auth", "expiration", "token"],
        description="Search memories for JWT token expiration policy",
    ),
    TestCase(
        id="recall_db_pooling",
        category="Recall & Search",
        user_prompt="What database and connection pooler are we using in production?",
        expected_tool_type="query",
        expected_keywords_in_query=["database", "pool", "pgbouncer", "postgres"],
        description="Search memories for database connection pooling setup",
    ),
    TestCase(
        id="recall_frontend_state",
        category="Recall & Search",
        user_prompt="Which library is used for server state management in the React frontend?",
        expected_tool_type="query",
        expected_keywords_in_query=["frontend", "react", "state", "tanstack", "query"],
        description="Search memories for frontend state management library",
    ),
    TestCase(
        id="taxonomy_overview",
        category="Recall & Search",
        user_prompt="Can you give me the full taxonomy and wing breakdown of our memory palace?",
        expected_tool_type="query",
        expected_keywords_in_query=["taxonomy", "wings", "breakdown"],
        description="Retrieve taxonomy or list of wings",
    ),
    TestCase(
        id="palace_status_health",
        category="Recall & Search",
        user_prompt="Check the memory palace status, total drawer count, and system integrity.",
        expected_tool_type="query",
        expected_keywords_in_query=["status", "health", "drawers"],
        description="Palace status and health check",
    ),
    TestCase(
        id="check_duplicate_content",
        category="Recall & Search",
        user_prompt="Check if we already have this note stored: 'Database migrations are handled by Alembic with PostgreSQL 15.'",
        expected_tool_type="query",
        expected_keywords_in_query=["check", "dup", "duplicate", "alembic"],
        description="Duplicate content detection check",
    ),

    # ── Category 2: Knowledge Graph (KG) ─────────────────────────────────────
    TestCase(
        id="kg_query_entity",
        category="Knowledge Graph",
        user_prompt="What roles and facts do we know about Alice in the knowledge graph?",
        expected_tool_type="query",
        expected_keywords_in_query=["alice", "kg"],
        description="Query entity facts from Knowledge Graph",
    ),
    TestCase(
        id="kg_timeline_entity",
        category="Knowledge Graph",
        user_prompt="Show me the chronological history and timeline of facts for Bob.",
        expected_tool_type="query",
        expected_keywords_in_query=["bob", "timeline", "kg"],
        description="Retrieve entity chronological timeline",
    ),
    TestCase(
        id="kg_stats_overview",
        category="Knowledge Graph",
        user_prompt="What are the overall stats of our knowledge graph (entities, triples, relationships)?",
        expected_tool_type="query",
        expected_keywords_in_query=["stats", "kg"],
        description="Knowledge Graph overview statistics",
    ),

    # ── Category 3: Graph Traversal & Tunnels ────────────────────────────────
    TestCase(
        id="graph_traverse_room",
        category="Graph & Navigation",
        user_prompt="Explore connections from the 'backend' room across other wings in the palace graph.",
        expected_tool_type="query",
        expected_keywords_in_query=["traverse", "backend", "hops"],
        description="Graph walk across palace rooms",
    ),
    TestCase(
        id="list_cross_wing_tunnels",
        category="Graph & Navigation",
        user_prompt="What cross-wing tunnels connect project_backend to project_frontend?",
        expected_tool_type="query",
        expected_keywords_in_query=["tunnels", "backend", "frontend"],
        description="Find bridge rooms / tunnels between wings",
    ),

    # ── Category 4: Agent Diary ──────────────────────────────────────────────
    TestCase(
        id="diary_read_recent",
        category="Agent Diary",
        user_prompt="Read the last 3 diary entries recorded by agent 'antigravity'.",
        expected_tool_type="query",
        expected_keywords_in_query=["diary", "antigravity", "last"],
        description="Read recent journal entries for agent",
    ),
    TestCase(
        id="diary_write_session",
        category="Agent Diary",
        user_prompt="Write a diary entry for agent 'antigravity' with topic 'auth': 'SESSION:2026-09-01|migrated JWT tokens to passkeys|done'.",
        expected_tool_type="exec",
        expected_keywords_in_query=["diary", "write", "antigravity", "passkeys"],
        description="Append agent journal entry in AAAK format",
    ),

    # ── Category 5: Memory Mutations & Ingestion ─────────────────────────────
    TestCase(
        id="add_new_drawer",
        category="Memory Mutations",
        user_prompt="Please file this new decision in wing 'project_backend' room 'architecture': 'All internal microservices must communicate via gRPC with Protocol Buffers.'",
        expected_tool_type="exec",
        expected_keywords_in_query=["add", "grpc", "architecture", "backend"],
        description="Add a new verbatim drawer into the palace",
    ),
    TestCase(
        id="kg_add_fact",
        category="Memory Mutations",
        user_prompt="Add a new fact to the knowledge graph: Alice -> appointed_to -> Technical Advisory Board from 2026-09-01.",
        expected_tool_type="exec",
        expected_keywords_in_query=["kg", "add", "alice", "advisory"],
        description="Insert a new triple fact into Knowledge Graph",
    ),
    TestCase(
        id="kg_supersede_fact",
        category="Memory Mutations",
        user_prompt="Update the knowledge graph: Alice previously worked as Lead Engineer, but starting today 2026-09-01 her role is VP of Engineering.",
        expected_tool_type=["exec", "query"],
        expected_keywords_in_query=["kg", "supersede", "alice", "engineering"],
        description="Atomically supersede a fact at boundary date",
    ),
    TestCase(
        id="create_cross_wing_tunnel",
        category="Memory Mutations",
        user_prompt="Create a cross-wing tunnel linking project_backend/auth to project_frontend/login labeled 'Auth token integration'.",
        expected_tool_type=["exec", "query"],
        expected_keywords_in_query=["tunnel", "create", "backend", "frontend"],
        description="Create an explicit cross-wing link between rooms",
    ),

    # ── Category 6: Multi-Agent Coordination ─────────────────────────────────
    TestCase(
        id="coord_task_create",
        category="Multi-Agent Flow",
        user_prompt="Delegate a task for project 'mempalace' from 'windows:antigravity:mempalace' to 'windows:claude:mempalace'. Goal: 'Implement Passkey WebAuthn endpoints', Branch: 'feat/passkeys', Base commit: 'a1b2c3d4e5f6', Definition of done: 'All WebAuthn integration tests pass'.",
        expected_tool_type="coordinate",
        expected_keywords_in_query=["task", "create", "passkey", "claude"],
        description="Create an immutable task.request event in logstream",
    ),
    TestCase(
        id="coord_event_list",
        category="Multi-Agent Flow",
        user_prompt="List all coordination events in stream 'project/mempalace' sent to agent 'windows:antigravity:mempalace'.",
        expected_tool_type="coordinate",
        expected_keywords_in_query=["event", "list", "logstream", "mempalace"],
        description="Filter and list logstream coordination events",
    ),
    TestCase(
        id="coord_mesh_peers",
        category="Multi-Agent Flow",
        user_prompt="Show the status and version vectors of all peer hubs in the coordination mesh.",
        expected_tool_type="coordinate",
        expected_keywords_in_query=["peers", "mesh"],
        description="Inspect mesh network peer estate",
    ),
]


# ── Environment & Test Palace Setup ──────────────────────────────────────────


class TestPalaceEnvironment:
    """Creates a temporary, fully-populated test palace for live tool execution."""

    def __init__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="mempalace_bench_ornith_")
        self.palace_path = os.path.join(self.tmp_dir, "palace")
        os.makedirs(self.palace_path, exist_ok=True)
        self.cfg_dir = os.path.join(self.tmp_dir, "config")
        os.makedirs(self.cfg_dir, exist_ok=True)

        with open(os.path.join(self.cfg_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"palace_path": self.palace_path}, f)

        self.config = MempalaceConfig(config_dir=self.cfg_dir)
        self.kg_path = os.path.join(self.palace_path, "kg.sqlite3")
        self.kg = KnowledgeGraph(db_path=self.kg_path)

        # Seed Chroma DB
        self.client = chromadb.PersistentClient(path=self.palace_path)
        self.collection = self.client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        self._seed_drawers()
        self._seed_kg()
        self._seed_tunnels()
        self._seed_diaries()
        self._seed_logstream()

    def _seed_drawers(self):
        docs = [
            "The authentication module uses JWT tokens for session management. Tokens expire after 24 hours. Refresh tokens are stored in HttpOnly cookies.",
            "Database migrations are handled by Alembic. We use PostgreSQL 15 with connection pooling via pgbouncer set to max 50 pool size.",
            "The React frontend uses TanStack Query for server state management and Zustand for global UI state. All API calls go through a centralized fetch wrapper.",
            "Sprint planning: migrate authentication to passkeys and WebAuthn by Q3 2026. Evaluate ChromaDB alternatives for vector search.",
            "Security policy: All service-to-service communication within the VPC must use mTLS encryption.",
        ]
        ids = [f"drw_seed_{i}" for i in range(len(docs))]
        metadatas = [
            {"wing": "project_backend", "room": "auth", "source_file": "auth.py", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-01T00:00:00"},
            {"wing": "project_backend", "room": "database", "source_file": "db.py", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-02T00:00:00"},
            {"wing": "project_frontend", "room": "architecture", "source_file": "app.tsx", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-03T00:00:00"},
            {"wing": "team", "room": "planning", "source_file": "roadmap.md", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-04T00:00:00"},
            {"wing": "security", "room": "policies", "source_file": "sec.md", "chunk_index": 0, "added_by": "miner", "filed_at": "2026-01-05T00:00:00"},
        ]
        self.collection.add(ids=ids, documents=docs, metadatas=metadatas)

    def _seed_kg(self):
        self.kg.add_triple("Alice", "role", "Lead Engineer", valid_from="2025-01-01")
        self.kg.add_triple("Alice", "leads", "Security Team", valid_from="2025-06-01")
        self.kg.add_triple("Bob", "role", "Database Architect", valid_from="2025-02-01")
        self.kg.add_triple("Bob", "maintains", "PostgreSQL", valid_from="2025-03-01")
        self.kg.add_triple("Arthur", "founded", "Project Camelot", valid_from="2024-01-01")

    def _seed_tunnels(self):
        create_tunnel("project_backend", "auth", "project_frontend", "architecture", "Auth to UI", self.palace_path)
        invalidate_graph_cache()

    def _seed_diaries(self):
        from mempalace.ids import make_drawer_id_from_content
        entry = "SESSION:2026-08-28|optimized search ranking + benchmarked hybrid backend|ALC.req:fast.mcp|★★★"
        drw_id = make_drawer_id_from_content("wing_antigravity", "diary", entry)
        self.collection.add(
            ids=[drw_id],
            documents=[entry],
            metadatas=[{"wing": "wing_antigravity", "room": "diary", "agent": "antigravity", "topic": "general", "filed_at": "2026-08-28T12:00:00"}]
        )

    def _seed_logstream(self):
        ls_path = os.path.join(self.palace_path, "logstream.sqlite3")
        ls = Logstream(db_path=ls_path)
        ls.append_event(
            type="task.request",
            stream="project/mempalace",
            room="delegation",
            from_agent="windows:claude:mempalace",
            to_agent="windows:antigravity:mempalace",
            correlation_id="task_seed_101",
            body="Review lightweight MCP parser implementation",
            status="open",
        )

    def patch_globals(self):
        mcp_server._config = self.config
        mcp_server._get_kg = lambda *a, **kw: self.kg
        mcp_server._taxonomy_cache = None
        mcp_server._taxonomy_cache_time = 0.0
        mcp_server._client_cache = None
        mcp_server._collection_cache = None
        mcp_server._collection_cache_backend = None
        mcp_server._collection_cache_palace = None
        mcp_server._collection_open_error = None
        mcp_server._READ_ONLY = False
        mcp_server._vector_disabled = False
        invalidate_graph_cache()

    def cleanup(self):
        try:
            self.client.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


# ── Ollama Calling & Evaluation Harness ─────────────────────────────────────


@dataclass
class TestResult:
    test_id: str
    category: str
    mode: str  # "lightweight" (3 tools) vs "legacy" (45 tools)
    tool_called: bool
    tool_name: Optional[str]
    tool_args: Any
    selection_correct: bool
    execution_success: bool
    execution_error: Optional[str]
    latency_sec: float
    multi_turn_success: bool
    final_response_preview: str


def call_ollama_chat(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(OLLAMA_API_URL, data=data, headers={"Content-Type": "application/json"})
    start_t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    elapsed = time.perf_counter() - start_t
    res["_elapsed_sec"] = elapsed
    return res


def get_light_ollama_tools() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for name, t in LIGHT_TOOLS.items()
    ]


def get_legacy_ollama_tools() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for name, t in LEGACY_TOOLS.items()
    ]


def execute_tool_call(tool_name: str, arguments: Any, mode: str) -> Tuple[bool, Any, Optional[str]]:
    """Execute tool against the live test palace and return (success, result, error_str)."""
    try:
        if mode == "lightweight":
            if tool_name == "palace_query":
                res = tool_palace_query(arguments)
            elif tool_name == "palace_exec":
                res = tool_palace_exec(arguments)
            elif tool_name == "palace_coordinate":
                res = tool_palace_coordinate(arguments)
            else:
                return False, None, f"Unknown lightweight tool: {tool_name}"
            
            if isinstance(res, dict) and res.get("success") is False and "error" in res:
                return False, res, res["error"]
            return True, res, None
        else:
            # Legacy 45 tools
            if tool_name not in LEGACY_TOOLS:
                return False, None, f"Unknown legacy tool: {tool_name}"
            handler = LEGACY_TOOLS[tool_name]["handler"]
            kwargs = arguments if isinstance(arguments, dict) else {}
            res = handler(**kwargs) if kwargs else handler()
            return True, res, None
    except Exception as e:
        return False, None, str(e)


def run_single_test(
    test_case: TestCase,
    env: TestPalaceEnvironment,
    mode: str,
) -> TestResult:
    """Runs a complete tool interaction test on ornith-1.5:9b."""
    env.patch_globals()
    tools = get_light_ollama_tools() if mode == "lightweight" else get_legacy_ollama_tools()

    system_prompt = (
        "You are an AI assistant equipped with MemPalace tools to recall, manage, and coordinate "
        "memories, facts, and tasks. Always choose and invoke the appropriate tool when asked about "
        "stored decisions, facts, or palace management."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": test_case.user_prompt},
    ]

    try:
        first_resp = call_ollama_chat(MODEL_NAME, messages, tools=tools)
    except Exception as e:
        return TestResult(
            test_id=test_case.id,
            category=test_case.category,
            mode=mode,
            tool_called=False,
            tool_name=None,
            tool_args=None,
            selection_correct=False,
            execution_success=False,
            execution_error=f"Ollama API error: {e}",
            latency_sec=0.0,
            multi_turn_success=False,
            final_response_preview="",
        )

    latency = first_resp.get("_elapsed_sec", 0.0)
    assistant_msg = first_resp.get("message", {})
    tool_calls = assistant_msg.get("tool_calls", [])

    if not tool_calls:
        # Model chose not to call any tool
        return TestResult(
            test_id=test_case.id,
            category=test_case.category,
            mode=mode,
            tool_called=False,
            tool_name=None,
            tool_args=None,
            selection_correct=False,
            execution_success=False,
            execution_error="Model did not call any tool",
            latency_sec=latency,
            multi_turn_success=False,
            final_response_preview=assistant_msg.get("content", "")[:100],
        )

    # Inspect the first tool call
    tc = tool_calls[0]
    called_tool_name = tc.get("function", {}).get("name", "")
    called_tool_args = tc.get("function", {}).get("arguments", {})

    # Validate Tool Selection
    expected_types = (
        test_case.expected_tool_type
        if isinstance(test_case.expected_tool_type, list)
        else [test_case.expected_tool_type]
    )

    selection_correct = False
    if mode == "lightweight":
        for exp in expected_types:
            if exp == "query" and called_tool_name == "palace_query":
                selection_correct = True
            elif exp == "exec" and called_tool_name == "palace_exec":
                selection_correct = True
            elif exp == "coordinate" and called_tool_name == "palace_coordinate":
                selection_correct = True
    else:
        # Legacy mode: tool name starts with mempalace_
        if called_tool_name.startswith("mempalace_"):
            selection_correct = True

    # Execute Tool Call against live test environment
    exec_success, exec_result, exec_error = execute_tool_call(called_tool_name, called_tool_args, mode)

    # Multi-turn verification: feed tool output back and request final answer
    messages.append(assistant_msg)
    tool_resp_str = json.dumps(exec_result, ensure_ascii=False) if exec_success else json.dumps({"error": exec_error})
    messages.append({
        "role": "tool",
        "tool_call_id": tc.get("id", "call_0"),
        "content": tool_resp_str,
    })

    multi_turn_success = False
    final_text = ""
    try:
        second_resp = call_ollama_chat(MODEL_NAME, messages, tools=None)
        final_msg = second_resp.get("message", {})
        final_text = final_msg.get("content", "").strip()
        if final_text and len(final_text) > 10:
            multi_turn_success = True
    except Exception as e:
        exec_error = f"Multi-turn synthesis error: {e}"

    return TestResult(
        test_id=test_case.id,
        category=test_case.category,
        mode=mode,
        tool_called=True,
        tool_name=called_tool_name,
        tool_args=called_tool_args,
        selection_correct=selection_correct,
        execution_success=exec_success,
        execution_error=exec_error,
        latency_sec=latency,
        multi_turn_success=multi_turn_success,
        final_response_preview=final_text[:120].replace("\n", " "),
    )


def run_full_suite() -> Dict[str, Any]:
    print(f"==================================================================")
    print(f"  EXTENSIVE TESTING: {MODEL_NAME} ON ROUTINE PALACE USAGE")
    print(f"==================================================================")
    print(f"Testing {len(TEST_SUITE)} routine tasks across Lightweight (3 tools) vs Legacy (45 tools)...\n")

    env = TestPalaceEnvironment()

    results_light: List[TestResult] = []
    results_legacy: List[TestResult] = []

    try:
        # 1. Run Lightweight Suite
        print(">>> Running Mode 1: LIGHTWEIGHT 3-TOOL MCP (palace_query, palace_exec, palace_coordinate)...")
        for idx, tc in enumerate(TEST_SUITE, 1):
            print(f"  [{idx}/{len(TEST_SUITE)}] [{tc.category}] {tc.id} ...", end=" ", flush=True)
            res = run_single_test(tc, env, mode="lightweight")
            status = "PASS" if (res.selection_correct and res.execution_success and res.multi_turn_success) else "FAIL"
            print(f"{status} ({res.latency_sec:.2f}s) -> Tool: {res.tool_name}")
            if not res.execution_success:
                print(f"      Execution Error: {res.execution_error}")
            results_light.append(res)

        print("\n>>> Running Mode 2: LEGACY 45-TOOL MCP (mempalace_*)...")
        for idx, tc in enumerate(TEST_SUITE, 1):
            print(f"  [{idx}/{len(TEST_SUITE)}] [{tc.category}] {tc.id} ...", end=" ", flush=True)
            res = run_single_test(tc, env, mode="legacy")
            status = "PASS" if (res.selection_correct and res.execution_success and res.multi_turn_success) else "FAIL"
            print(f"{status} ({res.latency_sec:.2f}s) -> Tool: {res.tool_name}")
            if not res.execution_success:
                print(f"      Execution Error: {res.execution_error}")
            results_legacy.append(res)

    finally:
        env.cleanup()

    # Compile Evaluation Statistics
    def compute_stats(results: List[TestResult]) -> Dict[str, Any]:
        total = len(results)
        tool_called_count = sum(1 for r in results if r.tool_called)
        sel_correct = sum(1 for r in results if r.selection_correct)
        exec_success = sum(1 for r in results if r.execution_success)
        multi_turn = sum(1 for r in results if r.multi_turn_success)
        all_pass = sum(1 for r in results if r.selection_correct and r.execution_success and r.multi_turn_success)
        avg_lat = sum(r.latency_sec for r in results) / total if total else 0.0

        return {
            "total": total,
            "tool_call_rate_pct": (tool_called_count / total) * 100,
            "selection_accuracy_pct": (sel_correct / total) * 100,
            "execution_success_pct": (exec_success / total) * 100,
            "multi_turn_success_pct": (multi_turn / total) * 100,
            "end_to_end_pass_pct": (all_pass / total) * 100,
            "avg_latency_sec": avg_lat,
        }

    stats_light = compute_stats(results_light)
    stats_legacy = compute_stats(results_legacy)

    return {
        "model": MODEL_NAME,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lightweight_stats": stats_light,
        "legacy_stats": stats_legacy,
        "lightweight_results": [r.__dict__ for r in results_light],
        "legacy_results": [r.__dict__ for r in results_legacy],
    }


if __name__ == "__main__":
    report = run_full_suite()
    out_path = Path("benchmarks/results_ollama_ornith_routine.json")
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n[+] Detailed benchmark report written to {out_path}")
