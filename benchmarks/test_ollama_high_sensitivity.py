#!/usr/bin/env python3
"""
benchmarks/test_ollama_high_sensitivity.py — High-Sensitivity Stress Test Benchmark for Local Models.

Designed specifically to test reasoning sensitivity, temporal distinction, multi-hop cross-wing synthesis,
hallucination resistance, and distractor resilience on private memory palaces.

Evaluates:
- medgemma1.5-tools (4B medical reasoning)
- granite4.2:3b (3B compact function caller)
- ornith-1.5:9b (9B reference reasoner)
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chromadb

from mempalace.config import MempalaceConfig
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace import mcp_server, mcp_light_server
from mempalace.mcp_light_server import LIGHT_TOOLS, tool_palace_query, tool_palace_exec
from mempalace.palace_graph import create_tunnel, invalidate_graph_cache

OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://localhost:11434/api/chat")


@dataclass
class StressTestCase:
    id: str
    dimension: str
    user_prompt: str
    ground_truth_assertions: List[str]  # Substrings or semantic criteria that must be present in final answer
    negative_assertions: List[str]      # Substrings that MUST NOT be present (e.g. obsolete/hallucinated facts)
    expected_tool_type: List[str]
    description: str


STRESS_TEST_SUITE: List[StressTestCase] = [
    # ── Dimension 1: Temporal Succession & Obsolete Fact Filtering ───────────
    StressTestCase(
        id="stress_temporal_active_meds",
        dimension="Temporal Reasoning & Obsolete Filtering",
        user_prompt=(
            "Is Patient 1042 currently taking Metformin today, what is their exact active diabetes medication "
            "regimen on file, and what specific clinical event caused their medication to change in 2025?"
        ),
        ground_truth_assertions=["Empagliflozin", "25mg", "discontinued", "kidney", "eGFR"],
        negative_assertions=["currently taking Metformin 1000mg", "currently on Metformin"],
        expected_tool_type=["query"],
        description="Must recognize Metformin was superseded/stopped in 2025 due to eGFR drop and Empagliflozin 25mg is active.",
    ),

    # ── Dimension 2: Multi-Hop Cross-Wing Diagnostic Inference ────────────────
    StressTestCase(
        id="stress_multihop_allergy_contraindication",
        dimension="Cross-Wing Multi-Hop Inference",
        user_prompt=(
            "A resident physician wants to prescribe Amoxicillin-Clavulanate for Patient 1042's acute sinus infection. "
            "Evaluate whether this prescription is safe based on our private palace records, and retrieve the guideline-recommended alternative."
        ),
        ground_truth_assertions=["unsafe", "contraindicated", "allergy", "Penicillin", "Doxycycline"],
        negative_assertions=["safe to prescribe", "no allergies found"],
        expected_tool_type=["query"],
        description="Must cross-reference Patient allergy to Penicillin/beta-lactams and fetch guideline alternative (Doxycycline).",
    ),

    # ── Dimension 3: Needle-in-a-Haystack with 50+ Distractor Records ─────────
    StressTestCase(
        id="stress_needle_haystack_distractor",
        dimension="Distractor Resilience (50 Patients)",
        user_prompt=(
            "Retrieve the exact eGFR and Fasting Glucose for Patient 1067 from their baseline renal panel."
        ),
        ground_truth_assertions=["104", "96"],  # Patient 1067 specific seeded values
        negative_assertions=["78", "165", "Patient 1042"],  # Patient 1042's values
        expected_tool_type=["query"],
        description="Must isolate Patient 1067 among 50 distractor patient records and not bleed Patient 1042 data.",
    ),

    # ── Dimension 4: Hallucination Resistance & Negative Retrieval Discipline ─
    StressTestCase(
        id="stress_hallucination_unstored_fact",
        dimension="Hallucination Resistance",
        user_prompt=(
            "What was Patient 1042's coronary artery calcium (CAC) score from their 2025 cardiac CT scan, "
            "and what dose of Atorvastatin was started?"
        ),
        ground_truth_assertions=["no record", "not found", "no cardiac CT", "no Atorvastatin"],
        negative_assertions=["CAC score of", "Atorvastatin 20mg", "Atorvastatin 40mg", "score was"],
        expected_tool_type=["query"],
        description="Must query the palace, find no record, and state that no CAC scan or statin is recorded.",
    ),

    # ── Dimension 5: Implicit / Indirect Symptom Query (Zero Direct Keywords) ─
    StressTestCase(
        id="stress_implicit_symptom_search",
        dimension="Implicit Semantic Search",
        user_prompt=(
            "A patient suffered an acute reaction with hives, bronchospasm, and facial swelling during a previous "
            "antibiotic treatment. Search our private palace to identify which medication class triggered this anaphylaxis."
        ),
        ground_truth_assertions=["Penicillin", "beta-lactam"],
        negative_assertions=["Metformin", "Lisinopril"],
        expected_tool_type=["query"],
        description="Must semantically infer to search allergy records for hives/bronchospasm triggers.",
    ),

    # ── Dimension 6: Multi-Constraint PQL Syntax Construction ────────────────
    StressTestCase(
        id="stress_pql_multi_constraint_query",
        dimension="Complex PQL Construction",
        user_prompt=(
            "Search for all elevated glucose lab records filed in wing 'patient_1042' room 'labs' "
            "with a limit of 3 results."
        ),
        ground_truth_assertions=["165", "HbA1c", "8.2%"],
        negative_assertions=[],
        expected_tool_type=["query"],
        description="Must generate valid PQL containing wing/room filters and limit.",
    ),

    # ── Dimension 7: Medical Mutation Consistency ────────────────────────────
    StressTestCase(
        id="stress_mutation_allergy_update",
        dimension="Mutation & KG Consistency",
        user_prompt=(
            "Add a new drug allergy to the medical knowledge graph: Patient_1042 -> has_allergy -> Sulfonamides from 2026-09-01."
        ),
        ground_truth_assertions=["added", "Sulfonamides", "Patient_1042"],
        negative_assertions=["error", "invalid"],
        expected_tool_type=["exec"],
        description="Must invoke palace_exec with valid KG triple insertion syntax.",
    ),
]


# ── Heavy Test Palace Generator (with 50+ Distractor Patients) ───────────────


class HighSensitivityPalaceEnvironment:
    """Populates a palace with 50 distractor patients, temporal records, and cross-wing guidelines."""

    def __init__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="mempalace_stress_")
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
        self._seed_heavy_data()

    def _seed_heavy_data(self):
        docs = []
        ids = []
        metas = []

        # 1. Target Patient 1042 Longitudinal History (with temporal succession)
        p1042_records = [
            ("drw_1042_enc_2023", "Encounter 2023-04-10: Initial diagnosis of Type 2 Diabetes. Started Metformin 500mg daily. Baseline HbA1c 7.6%.", "patient_1042", "encounters", "2023-04-10T09:00:00"),
            ("drw_1042_enc_2024", "Encounter 2024-06-15: HbA1c elevated to 8.5%. Escalated Metformin to 1000mg BID. Added Lisinopril 10mg for blood pressure.", "patient_1042", "encounters", "2024-06-15T10:00:00"),
            ("drw_1042_aki_2025", "Acute Renal Event 2025-06-20: Patient experienced acute kidney injury (eGFR dropped from 78 to 26 mL/min). Metformin was permanently DISCONTINUED due to high risk of lactic acidosis. Switched diabetes therapy to SGLT2 inhibitor Empagliflozin 10mg daily.", "patient_1042", "encounters", "2025-06-20T14:00:00"),
            ("drw_1042_enc_2026", "Encounter 2026-01-15: Renal function stabilized at eGFR 45 mL/min. Empagliflozin increased to 25mg daily as sole active diabetes medication. Blood pressure controlled on Lisinopril 10mg.", "patient_1042", "encounters", "2026-01-15T11:00:00"),
            ("drw_1042_lab_2026", "Lab Panel 2026-01-15: HbA1c 8.2%, Fasting Glucose 165 mg/dL, eGFR 45 mL/min/1.73m2, Serum Creatinine 1.4 mg/dL.", "patient_1042", "labs", "2026-01-15T08:30:00"),
            ("drw_1042_allergy", "Critical Allergy Record: Patient has documented severe anaphylactic allergy to Penicillin and all beta-lactam class antibiotics (experienced bronchospasm, urticaria, and angioedema in 2021).", "patient_1042", "allergies", "2021-08-05T12:00:00"),
        ]
        for drw_id, doc, wing, room, filed_at in p1042_records:
            ids.append(drw_id)
            docs.append(doc)
            metas.append({"wing": wing, "room": room, "source_file": f"{drw_id}.md", "chunk_index": 0, "added_by": "ehr", "filed_at": filed_at})

        # 2. Clinical Guidelines (Infectious Disease & Diabetes)
        guidelines = [
            ("drw_guide_sinusitis", "Infectious Disease Practice Guidelines 2026: First-line empiric therapy for acute bacterial rhinosinusitis is Amoxicillin-Clavulanate 875/125mg BID. For patients with documented Penicillin or beta-lactam allergy, Amoxicillin is STRICTLY CONTRAINDICATED; recommended alternative first-line agents are Doxycycline 100mg BID or Levofloxacin 500mg daily.", "clinical_guidelines", "infectious_disease", "2026-01-01T00:00:00"),
            ("drw_guide_metformin_renal", "ADA/KDIGO Consensus 2026: Metformin is contraindicated in patients with eGFR < 30 mL/min due to lactic acidosis risk. For eGFR 30-44 mL/min, maximum recommended dose is 1000mg/day.", "clinical_guidelines", "nephrology", "2026-01-01T00:00:00"),
        ]
        for drw_id, doc, wing, room, filed_at in guidelines:
            ids.append(drw_id)
            docs.append(doc)
            metas.append({"wing": wing, "room": room, "source_file": f"{drw_id}.md", "chunk_index": 0, "added_by": "guidelines", "filed_at": filed_at})

        # 3. 50 Distractor Patient Records (Patient 1040 to Patient 1089)
        for i in range(1040, 1090):
            if i == 1042:
                continue
            egfr_val = 60 + (i * 7) % 45
            glucose_val = 85 + (i * 13) % 110
            hba1c_val = 5.2 + (i % 30) * 0.2
            med_text = "Glipizide 5mg daily" if i % 2 == 0 else "Semaglutide 0.5mg weekly"
            p_doc = f"Baseline Panel Patient {i}: Fasting Glucose {glucose_val} mg/dL, eGFR {egfr_val} mL/min/1.73m2, HbA1c {hba1c_val:.1f}%. Current therapy: {med_text}."
            ids.append(f"drw_distract_{i}")
            docs.append(p_doc)
            metas.append({"wing": f"patient_{i}", "room": "labs", "source_file": f"patient_{i}_panel.md", "chunk_index": 0, "added_by": "ehr", "filed_at": "2026-01-10T00:00:00"})

        self.collection.add(ids=ids, documents=docs, metadatas=metas)

        # 4. Knowledge Graph Triples with Temporal Intervals
        self.kg.add_triple("Patient_1042", "diagnosed_with", "Type 2 Diabetes Mellitus", valid_from="2023-04-10")
        self.kg.add_triple("Patient_1042", "prescribed", "Metformin 500mg", valid_from="2023-04-10", valid_to="2024-06-15")
        self.kg.add_triple("Patient_1042", "prescribed", "Metformin 1000mg BID", valid_from="2024-06-15", valid_to="2025-06-20")
        self.kg.add_triple("Patient_1042", "prescribed", "Empagliflozin 10mg", valid_from="2025-06-20", valid_to="2026-01-15")
        self.kg.add_triple("Patient_1042", "prescribed", "Empagliflozin 25mg", valid_from="2026-01-15")
        self.kg.add_triple("Patient_1042", "has_allergy", "Penicillin", valid_from="2021-08-05")
        self.kg.add_triple("Patient_1042", "has_allergy", "Beta-Lactam Antibiotics", valid_from="2021-08-05")

        create_tunnel("patient_1042", "allergies", "clinical_guidelines", "infectious_disease", "Allergy to Antibiotic Guideline", self.palace_path)
        invalidate_graph_cache()

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


# ── Stress Test Execution & Scoring Harness ─────────────────────────────────


@dataclass
class StressResult:
    test_id: str
    dimension: str
    model: str
    tool_called: bool
    tool_name: Optional[str]
    tool_args: Any
    execution_success: bool
    reasoning_accuracy_score: float  # 0.0 to 1.0 based on ground truth and negative assertion matching
    assertions_passed: List[str]
    assertions_failed: List[str]
    negative_violations: List[str]
    latency_sec: float
    final_answer: str


def _extract_tool_call_from_message(msg: Dict[str, Any]) -> Tuple[Optional[str], Any]:
    tool_calls = msg.get("tool_calls", [])
    if tool_calls:
        tc = tool_calls[0]
        fn = tc.get("function", {})
        return fn.get("name"), fn.get("arguments", {})

    content = msg.get("content", "")
    if not content:
        return None, None

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

    for kw in ("FIND", "SEARCH", "TAXONOMY", "WINGS", "KG", "TRAVERSE", "DIARY", "STATUS", "ADD", "UPDATE"):
        if re.search(rf"\b{kw}\b", content):
            lines = [line.strip() for line in content.splitlines() if line.strip().startswith(kw)]
            if lines:
                cmd = lines[0]
                if kw in ("ADD", "UPDATE", "DELETE"):
                    return "palace_exec", cmd
                return "palace_query", cmd

    return None, None


def call_ollama_with_retry(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    timeout: int = 180,
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


def run_stress_test_case(
    test_case: StressTestCase,
    model_name: str,
    env: HighSensitivityPalaceEnvironment,
    max_turns: int = 3,
) -> StressResult:
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
        "You are an expert clinical and private memory AI assistant connected to a MemPalace database. "
        "Always query the palace using palace_query or mutate using palace_exec. Never hallucinate facts. "
        "Always respect temporal validity (ignore obsolete superseded facts), check allergies before medications, "
        "and isolate the exact patient ID. If a question requires cross-referencing patient records and guidelines, "
        "you may make multiple tool calls."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": test_case.user_prompt},
    ]

    total_latency = 0.0
    first_tool_name = None
    first_tool_args = None
    tool_called_any = False
    exec_success_all = True
    final_answer = ""

    for turn in range(max_turns):
        try:
            resp = call_ollama_with_retry(model_name, messages, tools=tools if turn < max_turns - 1 else None)
        except Exception as e:
            final_answer = f"Ollama error: {e}"
            break

        turn_lat = resp.get("_elapsed_sec", 0.0)
        total_latency += turn_lat
        assistant_msg = resp.get("message", {})
        messages.append(assistant_msg)

        tool_name, tool_args = _extract_tool_call_from_message(assistant_msg)
        if not tool_name:
            # Model produced final text without calling more tools
            final_answer = assistant_msg.get("content", "")
            break

        tool_called_any = True
        if first_tool_name is None:
            first_tool_name = tool_name
            first_tool_args = tool_args

        # Execute tool
        exec_res = None
        exec_err = None
        try:
            if tool_name == "palace_query":
                exec_res = tool_palace_query(tool_args)
            elif tool_name == "palace_exec":
                exec_res = tool_palace_exec(tool_args)
        except Exception as e:
            exec_err = str(e)
            exec_success_all = False

        tool_payload = json.dumps(exec_res, ensure_ascii=False) if exec_res is not None else json.dumps({"error": exec_err})
        messages.append({
            "role": "tool",
            "content": tool_payload,
        })

    if not final_answer and messages:
        final_answer = messages[-1].get("content", "")

    # Evaluate Assertions on Final Answer
    passed_asserts = []
    failed_asserts = []
    for gta in test_case.ground_truth_assertions:
        if re.search(rf"\b{re.escape(gta)}\b", final_answer, re.IGNORECASE):
            passed_asserts.append(gta)
        else:
            failed_asserts.append(gta)

    neg_violations = []
    for na in test_case.negative_assertions:
        if re.search(rf"\b{re.escape(na)}\b", final_answer, re.IGNORECASE):
            neg_violations.append(na)

    total_gta = len(test_case.ground_truth_assertions)
    gta_score = len(passed_asserts) / total_gta if total_gta > 0 else 1.0
    if neg_violations:
        gta_score *= 0.5

    return StressResult(
        test_id=test_case.id,
        dimension=test_case.dimension,
        model=model_name,
        tool_called=tool_called_any,
        tool_name=first_tool_name,
        tool_args=first_tool_args,
        execution_success=exec_success_all,
        reasoning_accuracy_score=round(gta_score, 2),
        assertions_passed=passed_asserts,
        assertions_failed=failed_asserts,
        negative_violations=neg_violations,
        latency_sec=round(total_latency, 2),
        final_answer=final_answer[:250].replace("\n", " "),
    )


def evaluate_stress_model(model_name: str) -> Dict[str, Any]:
    print(f"\n==================================================================")
    print(f"  HIGH-SENSITIVITY STRESS TEST: {model_name}")
    print(f"==================================================================")

    env = HighSensitivityPalaceEnvironment()
    results: List[StressResult] = []

    try:
        for idx, tc in enumerate(STRESS_TEST_SUITE, 1):
            print(f"  [{idx}/{len(STRESS_TEST_SUITE)}] [{tc.dimension}] {tc.id} ...", end=" ", flush=True)
            res = run_stress_test_case(tc, model_name, env)
            score_pct = int(res.reasoning_accuracy_score * 100)
            status = "PASS" if score_pct >= 70 else "FAIL"
            print(f"{status} (Score: {score_pct}%, {res.latency_sec:.2f}s) -> Tool: {res.tool_name}")
            if res.assertions_failed:
                print(f"      Missing Truths: {res.assertions_failed}")
            if res.negative_violations:
                print(f"      Violated Negatives: {res.negative_violations}")
            results.append(res)
    finally:
        env.cleanup()

    total = len(results)
    avg_score = sum(r.reasoning_accuracy_score for r in results) / total if total else 0.0
    pass_count = sum(1 for r in results if r.reasoning_accuracy_score >= 0.70)
    avg_lat = sum(r.latency_sec for r in results) / total if total else 0.0

    return {
        "model": model_name,
        "total_stress_tests": total,
        "sensitivity_pass_rate_pct": (pass_count / total) * 100,
        "mean_reasoning_score_pct": round(avg_score * 100, 1),
        "avg_latency_sec": round(avg_lat, 2),
        "results": [r.__dict__ for r in results],
    }


def main():
    MODELS = [
        m.strip()
        for m in os.environ.get(
            "MEMPALACE_BENCH_MODELS", "medgemma1.5-tools,granite4.2:3b,ornith-1.5:9b"
        ).split(",")
        if m.strip()
    ]
    all_evals = {}

    for m in MODELS:
        report = evaluate_stress_model(m)
        all_evals[m] = report

    out_file = Path("benchmarks/results_high_sensitivity_stress.json")
    out_file.write_text(json.dumps(all_evals, indent=2), encoding="utf-8")
    print(f"\n[+] High-Sensitivity Stress Test completed! Results saved to {out_file}")


if __name__ == "__main__":
    main()
