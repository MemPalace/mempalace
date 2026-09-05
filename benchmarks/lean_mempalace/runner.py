import subprocess
import json
import os
import sys
import time

def run_proc(cmd, cwd=None):
    t0 = time.perf_counter()
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
    t1 = time.perf_counter()
    wall_ms = (t1 - t0) * 1000.0
    if p.returncode != 0:
        print(f"Error running {cmd}: {p.stderr}")
        return None, wall_ms
    try:
        # find JSON block in output
        out = p.stdout.strip()
        idx_start = out.find('{')
        idx_end = out.rfind('}')
        if idx_start != -1 and idx_end != -1:
            data = json.loads(out[idx_start:idx_end+1])
            data["process_wall_time_ms"] = round(wall_ms, 2)
            return data, wall_ms
    except Exception as e:
        print(f"Failed to parse JSON from {cmd}: {e}")
        print("Stdout:", p.stdout)
    return None, wall_ms

def main():
    print("=================================================================")
    print("  MEMPALACE ENGINE PERFORMANCE & RESOURCE USAGE BENCHMARK SUITE  ")
    print("=================================================================")
    print("Data: C:\\Users\\igorl\\.mempalace\\palace\\sqlite_exact.sqlite3 (1.75 GB)")
    print()

    implementations = [
        ("Python 3.14.5 (numpy)", ["python", r"p:\MemPalace\mempalace\benchmarks\lean_mempalace\bench_python.py"]),
        ("TypeScript (Bun 1.4.1)", ["bun", "run", r"p:\MemPalace\mempalace\benchmarks\lean_mempalace\ts_bun\bench.ts"]),
        ("Rust 1.97.0-nightly", [r"p:\MemPalace\mempalace\benchmarks\lean_mempalace\rust\target\release\lean_mempalace_rust.exe"]),
        ("Go 1.27.1", [r"p:\MemPalace\mempalace\benchmarks\lean_mempalace\go\lean_mempalace_go.exe"]),
    ]

    all_results = {}

    for mode in ["drawers", "all"]:
        print(f"\n>>> RUNNING BENCHMARK SUITE: MODE = '{mode.upper()}' <<<")
        all_results[mode] = {}
        for name, base_cmd in implementations:
            cmd = base_cmd + [mode]
            print(f"Executing {name} [{mode}]...")
            data, wall_ms = run_proc(cmd)
            if data:
                all_results[mode][name] = data
                print(f"  Rows: {data.get('rows_indexed')}")
                print(f"  Load Time: {data.get('load_time_ms')} ms")
                print(f"  Cold 1st Query: {data.get('cold_first_query_ms')} ms (Process wall: {data.get('process_wall_time_ms')} ms)")
                print(f"  Warm p50: {data.get('warm_p50_ms')} ms | p95: {data.get('warm_p95_ms')} ms")
                if "python_opt_warm_p50_ms" in data:
                    print(f"  Python Optimized Warm p50: {data.get('python_opt_warm_p50_ms')} ms | p95: {data.get('python_opt_warm_p95_ms')} ms")
                if "warm_parallel_p50_ms" in data:
                    print(f"  Parallel Warm p50: {data.get('warm_parallel_p50_ms')} ms | p95: {data.get('warm_parallel_p95_ms')} ms")
                print(f"  Filtered p50: {data.get('filtered_p50_ms')} ms | p95: {data.get('filtered_p95_ms')} ms")
                print(f"  RSS (Loaded): {data.get('rss_after_load_mb')} MB | RSS (End): {data.get('rss_end_mb')} MB")
                print()
            else:
                print(f"  FAILED to execute {name}")

    with open(r"p:\MemPalace\mempalace\benchmarks\lean_mempalace\benchmark_results.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print("\nBenchmark completed. Results saved to benchmark_results.json")

if __name__ == "__main__":
    main()
