#!/usr/bin/env python3
"""
benchmarks/test_ninfer_qwen27b.py — Evaluation of qwen3.8-27b-nvfp4 on MemPalace.

Target endpoint: http://x870e-9950x3d:8010/v1
Model: qwen3.8-27b-nvfp4

Runs:
1. 20 Routine Palace Operations (Lightweight 3-tool vs Legacy 45-tool MCP)
2. 7 High-Sensitivity Stress Tests (Temporal succession, 50 distractor patients, multi-hop cross-wing synthesis, hallucination resistance)
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
from mempalace.logstream import Logstream
from mempalace import mcp_server, mcp_light_server
from mempalace.mcp_light_server import (
    LIGHT_TOOLS,
    tool_palace_query,
    tool_palace_exec,
    tool_palace_coordinate,
)
from mempalace.palace_graph import create_tunnel, invalidate_graph_cache

API_URL = os.environ.get("NINFER_API_URL", "http://x870e-9950x3d:8010/v1/chat/completions")
API_KEY_PATH = Path("P:/models/ninfer-api-key.txt")
API_KEY = API_KEY_PATH.read_text(encoding="utf-8").strip() if API_KEY_PATH.exists() else ""
MODEL_NAME = "qwen3.8-27b-nvfp4"


def call_ninfer_chat(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: float = 0.0,
    timeout: int = 60,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    start_t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    res["_elapsed_sec"] = time.perf_counter() - start_t
    return res


# ── SECTION 1: ROUTINE 20 PALACE OPERATIONS EVALUATION ───────────────────────

from benchmarks.test_ollama_ornith_routine import (
    TestCase,
    TEST_SUITE,
    LEGACY_TOOLS,
    TestPalaceEnvironment as RoutineEnvironment,
    execute_tool_call,
)


def run_routine_single_test(
    test_case: TestCase,
    mode: str,
    env: RoutineEnvironment,
) -> Dict[str, Any]:
    env.patch_globals()

    if mode == "lightweight":
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
        sys_prompt = (
            "You are an AI assistant with access to MemPalace tools: palace_query (for searching/reading/graph/stats/status), "
            "palace_exec (for writing/adding drawers/KG/tunnels), and palace_coordinate (for multi-agent tasks/events). "
            "Always call the appropriate tool."
        )
    else:
        # Legacy 45 tools
        tools = [
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
        sys_prompt = "You are an AI assistant with access to MemPalace MCP tools. Call the appropriate tool."

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": test_case.user_prompt},
    ]

    total_latency = 0.0
    first_tool_name = None
    first_tool_args = None
    tool_called_any = False
    exec_success_all = True
    selection_correct = False
    exec_err_last = None
    final_text = ""

    expected_types = test_case.expected_tool_type if isinstance(test_case.expected_tool_type, list) else [test_case.expected_tool_type]

    for turn in range(3):
        try:
            resp = call_ninfer_chat(messages, tools=tools)
        except Exception as e:
            exec_err_last = f"API error: {e}"
            break

        total_latency += resp.get("_elapsed_sec", 0.0)
        choice = resp.get("choices", [{}])[0]
        assistant_msg = choice.get("message", {})
        messages.append(assistant_msg)

        tcs = assistant_msg.get("tool_calls", [])
        if not tcs:
            final_text = (assistant_msg.get("content") or assistant_msg.get("reasoning_content") or "").strip()
            break

        tool_called_any = True
        tc0 = tcs[0]
        called_tool_name = tc0.get("function", {}).get("name")
        raw_args = tc0.get("function", {}).get("arguments", {})
        called_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

        if first_tool_name is None:
            first_tool_name = called_tool_name
            first_tool_args = called_args

            # Check tool selection on first call
            if mode == "lightweight":
                for exp in expected_types:
                    if exp == "query" and called_tool_name == "palace_query":
                        selection_correct = True
                    elif exp == "exec" and called_tool_name == "palace_exec":
                        selection_correct = True
                    elif exp == "coordinate" and called_tool_name == "palace_coordinate":
                        selection_correct = True
            else:
                if called_tool_name.startswith("mempalace_"):
                    selection_correct = True

        # Execute tool
        exec_success, exec_res, exec_err = execute_tool_call(called_tool_name, called_args, mode)
        if not exec_success:
            exec_success_all = False
            exec_err_last = exec_err

        tool_payload = json.dumps(exec_res, ensure_ascii=False) if exec_success else json.dumps({"error": exec_err})
        messages.append({
            "role": "tool",
            "tool_call_id": tc0.get("id", f"call_{turn}"),
            "content": tool_payload,
        })

    if not final_text and messages:
        final_text = (messages[-1].get("content") or messages[-1].get("reasoning_content") or "").strip()

    multi_turn_success = bool(final_text and len(final_text) > 10)

    return {
        "test_id": test_case.id,
        "category": test_case.category,
        "mode": mode,
        "tool_called": tool_called_any,
        "tool_name": first_tool_name,
        "tool_args": first_tool_args,
        "selection_correct": selection_correct,
        "execution_success": exec_success_all,
        "execution_error": exec_err_last,
        "latency_sec": round(total_latency, 2),
        "multi_turn_success": multi_turn_success,
        "final_response_preview": final_text[:120].replace("\n", " "),
    }


def evaluate_routine_benchmark() -> Dict[str, Any]:
    print("\n==================================================================")
    print(f"  EVALUATING ROUTINE 20 PALACE OPERATIONS ON {MODEL_NAME}")
    print("==================================================================")

    modes = ["lightweight", "legacy"]
    summary_by_mode = {}

    for mode in modes:
        print(f"\n>>> Running Mode: {mode.upper()}...")
        env = RoutineEnvironment()
        results = []
        try:
            for idx, tc in enumerate(TEST_SUITE, 1):
                print(f"  [{idx}/{len(TEST_SUITE)}] [{tc.category}] {tc.id} ...", end=" ", flush=True)
                res = run_routine_single_test(tc, mode, env)
                passed = res["selection_correct"] and res["execution_success"] and res["multi_turn_success"]
                status = "PASS" if passed else "FAIL"
                print(f"{status} ({res['latency_sec']:.2f}s) -> Tool: {res['tool_name']}")
                if not res["execution_success"]:
                    print(f"      Execution Error: {res['execution_error']}")
                results.append(res)
        finally:
            env.cleanup()

        total = len(results)
        all_pass = sum(1 for r in results if r["selection_correct"] and r["execution_success"] and r["multi_turn_success"])
        avg_lat = sum(r["latency_sec"] for r in results) / total if total else 0.0

        summary_by_mode[mode] = {
            "total": total,
            "pass_count": all_pass,
            "pass_rate_pct": (all_pass / total) * 100,
            "avg_latency_sec": round(avg_lat, 2),
            "results": results,
        }

    return summary_by_mode


# ── SECTION 2: HIGH-SENSITIVITY STRESS BENCHMARK EVALUATION ──────────────────

from benchmarks.test_ollama_high_sensitivity import (
    StressTestCase,
    STRESS_TEST_SUITE,
    HighSensitivityPalaceEnvironment,
)


def run_stress_single_test(
    test_case: StressTestCase,
    env: HighSensitivityPalaceEnvironment,
    max_turns: int = 3,
) -> Dict[str, Any]:
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
            resp = call_ninfer_chat(messages, tools=tools if turn < max_turns - 1 else None)
        except Exception as e:
            final_answer = f"API error: {e}"
            break

        turn_lat = resp.get("_elapsed_sec", 0.0)
        total_latency += turn_lat
        choice = resp.get("choices", [{}])[0]
        assistant_msg = choice.get("message", {})
        messages.append(assistant_msg)

        tcs = assistant_msg.get("tool_calls", [])
        if not tcs:
            final_answer = (assistant_msg.get("content") or assistant_msg.get("reasoning_content") or "").strip()
            break

        tc0 = tcs[0]
        tool_name = tc0.get("function", {}).get("name")
        raw_args = tc0.get("function", {}).get("arguments", {})
        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

        tool_called_any = True
        if first_tool_name is None:
            first_tool_name = tool_name
            first_tool_args = tool_args

        exec_res = None
        exec_err = None
        try:
            if tool_name == "palace_query":
                exec_res = tool_palace_query(tool_args)
            elif tool_name == "palace_exec":
                exec_res = tool_palace_exec(tool_args)
            elif tool_name == "palace_coordinate":
                exec_res = tool_palace_coordinate(tool_args)
        except Exception as e:
            exec_err = str(e)
            exec_success_all = False

        tool_payload = json.dumps(exec_res, ensure_ascii=False) if exec_res is not None else json.dumps({"error": exec_err})
        messages.append({
            "role": "tool",
            "tool_call_id": tc0.get("id", f"call_{turn}"),
            "content": tool_payload,
        })

    if not final_answer and messages:
        final_answer = (messages[-1].get("content") or messages[-1].get("reasoning_content") or "").strip()

    # Evaluate Assertions
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

    return {
        "test_id": test_case.id,
        "dimension": test_case.dimension,
        "model": MODEL_NAME,
        "tool_called": tool_called_any,
        "tool_name": first_tool_name,
        "tool_args": first_tool_args,
        "execution_success": exec_success_all,
        "reasoning_accuracy_score": round(gta_score, 2),
        "assertions_passed": passed_asserts,
        "assertions_failed": failed_asserts,
        "negative_violations": neg_violations,
        "latency_sec": round(total_latency, 2),
        "final_answer": final_answer[:250].replace("\n", " "),
    }


def evaluate_stress_benchmark() -> Dict[str, Any]:
    print("\n==================================================================")
    print(f"  HIGH-SENSITIVITY STRESS BENCHMARK ON {MODEL_NAME}")
    print("==================================================================")

    env = HighSensitivityPalaceEnvironment()
    results = []
    try:
        for idx, tc in enumerate(STRESS_TEST_SUITE, 1):
            print(f"  [{idx}/{len(STRESS_TEST_SUITE)}] [{tc.dimension}] {tc.id} ...", end=" ", flush=True)
            res = run_stress_single_test(tc, env)
            score_pct = int(res["reasoning_accuracy_score"] * 100)
            status = "PASS" if score_pct >= 70 else "FAIL"
            print(f"{status} (Score: {score_pct}%, {res['latency_sec']:.2f}s) -> Tool: {res['tool_name']}")
            if res["assertions_failed"]:
                print(f"      Missing Truths: {res['assertions_failed']}")
            if res["negative_violations"]:
                print(f"      Violated Negatives: {res['negative_violations']}")
            results.append(res)
    finally:
        env.cleanup()

    total = len(results)
    avg_score = sum(r["reasoning_accuracy_score"] for r in results) / total if total else 0.0
    pass_count = sum(1 for r in results if r["reasoning_accuracy_score"] >= 0.70)
    avg_lat = sum(r["latency_sec"] for r in results) / total if total else 0.0

    return {
        "model": MODEL_NAME,
        "total_stress_tests": total,
        "sensitivity_pass_rate_pct": (pass_count / total) * 100,
        "mean_reasoning_score_pct": round(avg_score * 100, 1),
        "avg_latency_sec": round(avg_lat, 2),
        "results": results,
    }


def main():
    print(f"Connecting to {API_URL} for model {MODEL_NAME}...")
    routine_report = evaluate_routine_benchmark()
    stress_report = evaluate_stress_benchmark()

    full_report = {
        "model": MODEL_NAME,
        "endpoint": API_URL,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "routine_benchmark": routine_report,
        "stress_benchmark": stress_report,
    }

    out_path = Path("benchmarks/results_ninfer_qwen27b.json")
    out_path.write_text(json.dumps(full_report, indent=2), encoding="utf-8")
    print(f"\n[+] Full Qwen 27B benchmark report saved to {out_path}")


if __name__ == "__main__":
    main()
