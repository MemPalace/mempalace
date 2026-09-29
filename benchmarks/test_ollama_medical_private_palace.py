#!/usr/bin/env python3
"""
benchmarks/test_ollama_medical_private_palace.py — Extensive Medical Reasoning & Private Memory Testing.

Evaluates small specialized local models on Ollama:
- medgemma1.5-tools (4B medical text reasoning model)
- granite4.2:3b (3B compact reasoning & function calling model)
- ornith-1.5:9b (9B reference benchmark)

Evaluates on a realistic Private Clinical Palace with EHR encounters, lab biomarker panels,
allergy alerts, ADA/AHA clinical practice guidelines, and medical Knowledge Graph relations.
"""

from __future__ import annotations

import json
import os
import re
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
from mempalace.palace_graph import create_tunnel, invalidate_graph_cache

OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://localhost:11434/api/chat")


@dataclass
class MedicalTestCase:
    id: str
    category: str
    user_prompt: str
    expected_tool_type: List[str]  # ["query"], ["exec"], etc.
    expected_keywords_in_query: List[str]
    description: str


MEDICAL_TEST_SUITE: List[MedicalTestCase] = [
    # ── Category 1: Clinical Recall & Safety ──────────────────────────────────
    MedicalTestCase(
        id="med_recall_hba1c",
        category="Clinical Recall",
        user_prompt="What was Patient 1042's most recent HbA1c level and fasting glucose in their lab panel?",
        expected_tool_type=["query"],
        expected_keywords_in_query=["hba1c", "glucose", "patient_1042", "lab"],
        description="Retrieve recent HbA1c and glycemic biomarker lab values",
    ),
    MedicalTestCase(
        id="med_recall_medications",
        category="Clinical Recall",
        user_prompt="List the active prescribed medications and dosages currently on file for Patient 1042.",
        expected_tool_type=["query"],
        expected_keywords_in_query=["medication", "metformin", "lisinopril", "patient_1042"],
        description="Retrieve active medication history and dosages",
    ),
    MedicalTestCase(
        id="med_recall_allergy_safety",
        category="Patient Safety",
        user_prompt="Safety Check: Does Patient 1042 have any documented drug allergies or anaphylaxis risks?",
        expected_tool_type=["query"],
        expected_keywords_in_query=["allergy", "penicillin", "patient_1042"],
        description="Verify severe drug allergy / anaphylaxis records",
    ),
    MedicalTestCase(
        id="med_recall_guideline_escalation",
        category="Clinical Guidelines",
        user_prompt="What do our stored ADA diabetes guidelines recommend when HbA1c > 8.0% on Metformin monotherapy?",
        expected_tool_type=["query"],
        expected_keywords_in_query=["guidelines", "ada", "metformin", "diabetes"],
        description="Retrieve evidence-based clinical escalation guidelines",
    ),
    MedicalTestCase(
        id="med_palace_taxonomy",
        category="Palace Structure",
        user_prompt="Show the full taxonomy and wing breakdown of our private clinical memory palace.",
        expected_tool_type=["query"],
        expected_keywords_in_query=["taxonomy", "wings", "breakdown"],
        description="Overview of clinical palace wings and rooms",
    ),
    MedicalTestCase(
        id="med_check_duplicate_lab",
        category="Clinical Ingestion",
        user_prompt="Check if we already have this lab note filed: 'Lab Panel 2026-01-15: HbA1c 8.2%, Fasting Glucose 165 mg/dL, eGFR 78 mL/min.'",
        expected_tool_type=["query"],
        expected_keywords_in_query=["check", "dup", "hba1c"],
        description="Duplicate clinical record detection",
    ),

    # ── Category 2: Medical Knowledge Graph (KG) ─────────────────────────────
    MedicalTestCase(
        id="med_kg_patient_profile",
        category="Medical Knowledge Graph",
        user_prompt="Query the medical knowledge graph for all diagnoses and relationships associated with Patient_1042.",
        expected_tool_type=["query"],
        expected_keywords_in_query=["patient_1042", "kg"],
        description="Extract patient entity relationship graph",
    ),
    MedicalTestCase(
        id="med_kg_timeline_disease",
        category="Medical Knowledge Graph",
        user_prompt="Show the chronological timeline of medical facts and milestones for Patient_1042.",
        expected_tool_type=["query"],
        expected_keywords_in_query=["patient_1042", "timeline"],
        description="Generate longitudinal patient disease timeline",
    ),
    MedicalTestCase(
        id="med_kg_stats_overview",
        category="Medical Knowledge Graph",
        user_prompt="What are the overall stats of our medical knowledge graph (total clinical entities and relationships)?",
        expected_tool_type=["query"],
        expected_keywords_in_query=["stats", "kg"],
        description="Medical Knowledge Graph overview",
    ),

    # ── Category 3: Clinical Navigation & Cross-Wing Bridges ─────────────────
    MedicalTestCase(
        id="med_graph_traverse_clinical",
        category="Clinical Navigation",
        user_prompt="Explore the palace graph connections starting from room 'patient_1042/labs' to clinical guidelines.",
        expected_tool_type=["query"],
        expected_keywords_in_query=["traverse", "patient_1042", "labs"],
        description="Graph traversal linking patient labs to treatment protocols",
    ),

    # ── Category 4: Clinician Journal / Handoff Diary ────────────────────────
    MedicalTestCase(
        id="med_diary_read_recent_handoff",
        category="Clinician Journal",
        user_prompt="Read the latest clinical handoff diary entries recorded by Dr. 'antigravity'.",
        expected_tool_type=["query"],
        expected_keywords_in_query=["diary", "antigravity", "last"],
        description="Read physician continuity handoff notes",
    ),
    MedicalTestCase(
        id="med_diary_write_rounds_note",
        category="Clinician Journal",
        user_prompt="Write a clinical diary entry for clinician 'antigravity' with topic 'rounds': 'SESSION:2026-03-01|Patient 1042 follow-up: tolerating Metformin well, glycemic control improving|plan:repeat HbA1c in 3 months|★★★'.",
        expected_tool_type=["exec"],
        expected_keywords_in_query=["diary", "write", "antigravity"],
        description="File clinician rounds continuity note in AAAK format",
    ),

    # ── Category 5: Clinical Memory Mutations ────────────────────────────────
    MedicalTestCase(
        id="med_exec_file_encounter",
        category="Clinical Mutation",
        user_prompt="Please file a new encounter note in wing 'patient_1042' room 'encounters': 'Encounter 2026-03-01: Follow-up for T2D. HbA1c improved to 7.4%. Escalated Metformin to 1000mg BID with meals. Patient counseled on hypoglycemia symptoms.'",
        expected_tool_type=["exec"],
        expected_keywords_in_query=["add", "patient_1042", "metformin", "encounter"],
        description="Store verbatim clinical encounter note",
    ),
    MedicalTestCase(
        id="med_exec_kg_add_complication",
        category="Clinical Mutation",
        user_prompt="Add a new diagnosis to the medical knowledge graph: Patient_1042 -> diagnosed_with -> Diabetic Peripheral Neuropathy from 2026-03-01.",
        expected_tool_type=["exec"],
        expected_keywords_in_query=["kg", "add", "patient_1042", "neuropathy"],
        description="Insert new clinical triple into Knowledge Graph",
    ),
    MedicalTestCase(
        id="med_exec_kg_supersede_dose",
        category="Clinical Mutation",
        user_prompt="Update the medical knowledge graph: Patient_1042 was previously prescribed Metformin 500mg, but effective 2026-03-01 the dosage is Metformin 1000mg BID.",
        expected_tool_type=["exec", "query"],
        expected_keywords_in_query=["kg", "supersede", "metformin", "1000mg"],
        description="Atomically supersede medication dosage triple in KG",
    ),
]


# ── Private Medical Palace Environment ───────────────────────────────────────


class MedicalPalaceEnvironment:
    """Creates a temporary, populated private clinical palace."""

    def __init__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="mempalace_med_bench_")
        self.palace_path = os.path.join(self.tmp_dir, "palace")
        os.makedirs(self.palace_path, exist_ok=True)
        self.cfg_dir = os.path.join(self.tmp_dir, "config")
        os.makedirs(self.cfg_dir, exist_ok=True)

        with open(os.path.join(self.cfg_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"palace_path": self.palace_path}, f)

        self.config = MempalaceConfig(config_dir=self.cfg_dir)
        self.kg_path = os.path.join(self.palace_path, "kg.sqlite3")
        self.kg = KnowledgeGraph(db_path=self.kg_path)

        self.client = chromadb.PersistentClient(path=self.palace_path)
        self.collection = self.client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        self._seed_clinical_drawers()
        self._seed_medical_kg()
        self._seed_tunnels()
        self._seed_diaries()

    def _seed_clinical_drawers(self):
        docs = [
            "Encounter 2026-01-15: 58-year-old male with Type 2 Diabetes and Stage 2 Hypertension. Current medications: Metformin 500mg BID, Lisinopril 10mg daily. Reported mild fatigue.",
            "Lab Panel 2026-01-15: HbA1c 8.2% (elevated, target < 7.0%), Fasting Glucose 165 mg/dL, eGFR 78 mL/min/1.73m2, Serum Creatinine 1.1 mg/dL, Total Cholesterol 210 mg/dL, LDL 130 mg/dL, HDL 42 mg/dL.",
            "Critical Allergy Alert: Patient has severe anaphylactic allergy to Penicillin (developed hives and bronchospasm in 2021). Avoid all beta-lactam antibiotics including amoxicillin and cephalosporins.",
            "ADA 2026 Clinical Guidelines: For Type 2 Diabetes with HbA1c > 8.0% on Metformin monotherapy, escalate Metformin to 1000mg BID or add SGLT2 inhibitor (e.g. Empagliflozin 10mg) if eGFR > 30 mL/min.",
            "AHA/ACC Hypertension Guidelines: First-line antihypertensive agents include ACE inhibitors (Lisinopril), ARBs, or CCBs. Target BP < 130/80 mmHg in diabetic patients.",
        ]
        ids = [f"drw_med_seed_{i}" for i in range(len(docs))]
        metadatas = [
            {"wing": "patient_1042", "room": "encounters", "source_file": "encounter_20260115.md", "chunk_index": 0, "added_by": "clinician", "filed_at": "2026-01-15T09:00:00"},
            {"wing": "patient_1042", "room": "labs", "source_file": "lab_panel_20260115.pdf", "chunk_index": 0, "added_by": "lab_system", "filed_at": "2026-01-15T11:30:00"},
            {"wing": "patient_1042", "room": "allergies", "source_file": "allergy_records.md", "chunk_index": 0, "added_by": "clinician", "filed_at": "2026-01-15T09:15:00"},
            {"wing": "clinical_guidelines", "room": "diabetes", "source_file": "ada_standards_2026.md", "chunk_index": 0, "added_by": "guidelines_miner", "filed_at": "2026-01-01T00:00:00"},
            {"wing": "clinical_guidelines", "room": "hypertension", "source_file": "aha_htn_2026.md", "chunk_index": 0, "added_by": "guidelines_miner", "filed_at": "2026-01-01T00:00:00"},
        ]
        self.collection.add(ids=ids, documents=docs, metadatas=metadatas)

    def _seed_medical_kg(self):
        self.kg.add_triple("Patient_1042", "diagnosed_with", "Type 2 Diabetes Mellitus", valid_from="2023-04-10")
        self.kg.add_triple("Patient_1042", "diagnosed_with", "Essential Hypertension", valid_from="2024-02-18")
        self.kg.add_triple("Patient_1042", "has_allergy", "Penicillin", valid_from="2021-08-05")
        self.kg.add_triple("Patient_1042", "prescribed", "Metformin 500mg", valid_from="2023-04-12")
        self.kg.add_triple("Patient_1042", "prescribed", "Lisinopril 10mg", valid_from="2024-02-20")

    def _seed_tunnels(self):
        create_tunnel("patient_1042", "labs", "clinical_guidelines", "diabetes", "Lab to Guideline Escalation", self.palace_path)
        invalidate_graph_cache()

    def _seed_diaries(self):
        from mempalace.ids import make_drawer_id_from_content
        entry = "SESSION:2026-01-15|reviewed Patient 1042 labs (HbA1c 8.2%)|discussed lifestyle modifications + planned Metformin titration|★★★"
        drw_id = make_drawer_id_from_content("wing_antigravity", "diary", entry)
        self.collection.add(
            ids=[drw_id],
            documents=[entry],
            metadatas=[{"wing": "wing_antigravity", "room": "diary", "agent": "antigravity", "topic": "rounds", "filed_at": "2026-01-15T14:00:00"}]
        )

    def patch_globals(self):
        mcp_server._config = self.config
        mcp_server._get_kg = lambda *a, **kw: self.kg
        mcp_server._taxonomy_cache = None
        mcp_server._taxonomy_cache_time = 0.0
        mcp_server._READ_ONLY = False
        mcp_server._vector_disabled = False
        invalidate_graph_cache()

    def cleanup(self):
        try:
            self.client.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


# ── Test Runner & Tool Call Parser ──────────────────────────────────────────


@dataclass
class ModelEvalResult:
    test_id: str
    category: str
    model: str
    tool_called: bool
    tool_name: Optional[str]
    tool_args: Any
    selection_correct: bool
    execution_success: bool
    execution_error: Optional[str]
    latency_sec: float
    multi_turn_success: bool
    final_response_preview: str


def _extract_tool_call_from_message(msg: Dict[str, Any]) -> Tuple[Optional[str], Any]:
    """Extract tool name and arguments from native tool_calls or markdown text blocks."""
    tool_calls = msg.get("tool_calls", [])
    if tool_calls:
        tc = tool_calls[0]
        fn = tc.get("function", {})
        return fn.get("name"), fn.get("arguments", {})

    content = msg.get("content", "")
    if not content:
        return None, None

    # Check for <tool_call> ... </tool_call> or ```tool_call ... ```
    m = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.DOTALL)
    if not m:
        m = re.search(r"```(?:tool_call|json)?\s*(\{.*?\})\s*```", content, re.DOTALL)

    if m:
        block = m.group(1).strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and "name" in parsed:
                return parsed["name"], parsed.get("arguments", {})
            elif isinstance(parsed, dict) and ("query" in parsed or "target" in parsed):
                return "palace_query", parsed
            elif isinstance(parsed, dict) and ("action" in parsed or "command" in parsed):
                return "palace_exec", parsed
            return "palace_query", parsed
        except Exception:
            pass

    # Check for direct PQL command line in output
    for kw in ("FIND", "SEARCH", "TAXONOMY", "WINGS", "KG", "TRAVERSE", "DIARY", "STATUS", "ADD", "UPDATE"):
        if re.search(rf"\b{kw}\b", content):
            lines = [line.strip() for line in content.splitlines() if line.strip().startswith(kw)]
            if lines:
                cmd = lines[0]
                if kw in ("ADD", "UPDATE", "DELETE"):
                    return "palace_exec", cmd
                return "palace_query", cmd

    return None, None


def call_ollama(
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
    res["_elapsed_sec"] = time.perf_counter() - start_t
    return res


def execute_light_tool(tool_name: str, arguments: Any) -> Tuple[bool, Any, Optional[str]]:
    try:
        if tool_name == "palace_query":
            res = tool_palace_query(arguments)
        elif tool_name == "palace_exec":
            res = tool_palace_exec(arguments)
        elif tool_name == "palace_coordinate":
            res = tool_palace_coordinate(arguments)
        else:
            return False, None, f"Unknown tool: {tool_name}"

        if isinstance(res, dict) and res.get("success") is False and "error" in res:
            return False, res, res["error"]
        return True, res, None
    except Exception as e:
        return False, None, str(e)


def run_medical_test(
    test_case: MedicalTestCase,
    model_name: str,
    env: MedicalPalaceEnvironment,
) -> ModelEvalResult:
    env.patch_globals()
    tools = [
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

    system_prompt = (
        "You are a private clinical AI assistant equipped with MemPalace medical memory tools "
        "(palace_query, palace_exec, palace_coordinate). Use palace_query to retrieve patient EHR notes, "
        "lab panels, allergies, and guidelines. Use palace_exec to file new encounter notes, update dosages, "
        "or record clinician diary entries."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": test_case.user_prompt},
    ]

    try:
        first_resp = call_ollama(model_name, messages, tools=tools)
    except Exception as e:
        return ModelEvalResult(
            test_id=test_case.id,
            category=test_case.category,
            model=model_name,
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
    tool_name, tool_args = _extract_tool_call_from_message(assistant_msg)

    if not tool_name:
        return ModelEvalResult(
            test_id=test_case.id,
            category=test_case.category,
            model=model_name,
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

    selection_correct = False
    for exp in test_case.expected_tool_type:
        if exp == "query" and tool_name == "palace_query":
            selection_correct = True
        elif exp == "exec" and tool_name == "palace_exec":
            selection_correct = True
        elif exp == "coordinate" and tool_name == "palace_coordinate":
            selection_correct = True

    exec_success, exec_res, exec_err = execute_light_tool(tool_name, tool_args)

    # Multi-turn synthesis
    messages.append(assistant_msg)
    tool_resp_str = json.dumps(exec_res, ensure_ascii=False) if exec_success else json.dumps({"error": exec_err})
    messages.append({
        "role": "tool",
        "content": tool_resp_str,
    })

    multi_turn_success = False
    final_text = ""
    try:
        second_resp = call_ollama(model_name, messages, tools=None)
        final_msg = second_resp.get("message", {})
        final_text = final_msg.get("content", "").strip()
        if final_text and len(final_text) > 10:
            multi_turn_success = True
    except Exception as e:
        exec_err = f"Multi-turn error: {e}"

    return ModelEvalResult(
        test_id=test_case.id,
        category=test_case.category,
        model=model_name,
        tool_called=True,
        tool_name=tool_name,
        tool_args=tool_args,
        selection_correct=selection_correct,
        execution_success=exec_success,
        execution_error=exec_err,
        latency_sec=latency,
        multi_turn_success=multi_turn_success,
        final_response_preview=final_text[:120].replace("\n", " "),
    )


def evaluate_model(model_name: str) -> Tuple[Dict[str, Any], List[ModelEvalResult]]:
    print(f"\n==================================================================")
    print(f"  EVALUATING MODEL: {model_name} ON PRIVATE CLINICAL PALACE")
    print(f"==================================================================")

    env = MedicalPalaceEnvironment()
    results: List[ModelEvalResult] = []

    try:
        for idx, tc in enumerate(MEDICAL_TEST_SUITE, 1):
            print(f"  [{idx}/{len(MEDICAL_TEST_SUITE)}] [{tc.category}] {tc.id} ...", end=" ", flush=True)
            res = run_medical_test(tc, model_name, env)
            status = "PASS" if (res.selection_correct and res.execution_success and res.multi_turn_success) else "FAIL"
            print(f"{status} ({res.latency_sec:.2f}s) -> Tool: {res.tool_name}")
            if not res.execution_success:
                print(f"      Execution Error: {res.execution_error}")
            results.append(res)
    finally:
        env.cleanup()

    total = len(results)
    tool_called_count = sum(1 for r in results if r.tool_called)
    sel_correct = sum(1 for r in results if r.selection_correct)
    exec_success = sum(1 for r in results if r.execution_success)
    multi_turn = sum(1 for r in results if r.multi_turn_success)
    all_pass = sum(1 for r in results if r.selection_correct and r.execution_success and r.multi_turn_success)
    avg_lat = sum(r.latency_sec for r in results) / total if total else 0.0

    stats = {
        "model": model_name,
        "total_tests": total,
        "tool_call_rate_pct": (tool_called_count / total) * 100,
        "selection_accuracy_pct": (sel_correct / total) * 100,
        "execution_success_pct": (exec_success / total) * 100,
        "multi_turn_success_pct": (multi_turn / total) * 100,
        "end_to_end_pass_pct": (all_pass / total) * 100,
        "avg_latency_sec": avg_lat,
    }
    return stats, results


def main():
    MODELS_TO_TEST = [
        m.strip()
        for m in os.environ.get(
            "MEMPALACE_BENCH_MODELS", "medgemma1.5-tools,granite4.2:3b,ornith-1.5:9b"
        ).split(",")
        if m.strip()
    ]
    all_reports = {}

    for model in MODELS_TO_TEST:
        stats, results = evaluate_model(model)
        all_reports[model] = {
            "stats": stats,
            "results": [r.__dict__ for r in results],
        }

    out_path = Path("benchmarks/results_ollama_medical_palace.json")
    out_path.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")
    print(f"\n[+] Comprehensive medical benchmark report saved to {out_path}")


if __name__ == "__main__":
    main()
