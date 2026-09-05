import os
import sys
import time
import json
import sqlite3
import numpy as np

def get_rss_mb():
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except ImportError:
        import ctypes
        from ctypes import wintypes
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD),
                ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t),
            ]
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return counters.WorkingSetSize / (1024 * 1024)

def run_suite(mode="drawers"):
    db_path = r"C:\Users\igorl\.mempalace\palace\sqlite_exact.sqlite3"
    t_start = time.perf_counter()
    rss_start = get_rss_mb()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c = conn.cursor()
    c.execute("PRAGMA busy_timeout=2000")

    # Sample query vector
    c.execute("SELECT embedding FROM documents WHERE id = 'drawer_44fb808c93188a039e5ce4ef712ebe0a'")
    sample_blob = c.fetchone()[0]
    query_vec = np.frombuffer(sample_blob, dtype=np.float32)

    # Load
    t_load_start = time.perf_counter()
    if mode == "drawers":
        sql = "SELECT collection_id, id, embedding, wing FROM documents WHERE collection_id = 1 ORDER BY rowid"
    else:
        sql = "SELECT collection_id, id, embedding, wing FROM documents ORDER BY rowid"

    rows = c.execute(sql).fetchall()

    cids = []
    ids = []
    vecs = []
    wings = []
    for cid, doc_id, blob, wing in rows:
        if not blob or len(blob) != 384 * 4:
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        cids.append(cid)
        ids.append(doc_id)
        vecs.append(vec)
        wings.append(wing or "")

    mat = np.stack(vecs)
    t_load_end = time.perf_counter()
    load_time_ms = (t_load_end - t_load_start) * 1000
    rss_after_load = get_rss_mb()

    q_norm = float(np.linalg.norm(query_vec))

    # 1. Unoptimized cosine distance (current sqlite_exact.py implementation)
    def cosine_distances(matrix, q):
        norms = np.linalg.norm(matrix, axis=1)
        denom = norms * q_norm
        dots = matrix @ q
        cos = np.zeros(dots.shape, dtype=np.float32)
        np.divide(dots, denom, out=cos, where=denom > 0)
        np.clip(cos, -1.0, 1.0, out=cos)
        return 1.0 - cos

    # Pre-normalized unit matrix for optimized benchmark
    norms_pre = np.linalg.norm(mat, axis=1)
    norm_mat = mat / np.where(norms_pre[:, None] > 0, norms_pre[:, None], 1.0)
    q_unit = query_vec / (q_norm if q_norm > 0 else 1.0)

    def hydrate(top_indices):
        out = []
        for idx in top_indices:
            c.execute("SELECT id, document, metadata_json FROM documents WHERE collection_id = ? AND id = ?", (cids[idx], ids[idx]))
            out.append(c.fetchone())
        return out

    k = 10

    # Cold first query (unoptimized baseline)
    t_cold_q_start = time.perf_counter()
    dists = cosine_distances(mat, query_vec)
    order = np.argsort(dists, kind="mergesort")[:k]
    top_indices = [int(i) for i in order]
    top_ids = [ids[i] for i in top_indices]
    top_dists = [float(dists[i]) for i in top_indices]
    hydrate(top_indices)
    t_cold_q_end = time.perf_counter()
    cold_first_query_ms = (t_cold_q_end - t_cold_q_start) * 1000
    total_cold_ms = (t_cold_q_end - t_start) * 1000

    # Warm-up (3 runs)
    for _ in range(3):
        d_ = cosine_distances(mat, query_vec)
        hydrate(np.argsort(d_, kind="mergesort")[:k])

    # Warm queries (25 iterations) - Unoptimized sqlite_exact.py
    warm_latencies = []
    for _ in range(25):
        t0 = time.perf_counter()
        dists = cosine_distances(mat, query_vec)
        order = np.argsort(dists, kind="mergesort")[:k]
        top_indices = [int(i) for i in order]
        hydrate(top_indices)
        t1 = time.perf_counter()
        warm_latencies.append((t1 - t0) * 1000)

    # Warm queries (25 iterations) - Optimized Python (unit dot + argpartition)
    opt_warm_latencies = []
    for _ in range(25):
        t0 = time.perf_counter()
        dots = norm_mat @ q_unit
        dists = 1.0 - dots
        sub_indices = np.argpartition(dists, k)[:k]
        order = sub_indices[np.argsort(dists[sub_indices])]
        top_indices = [int(i) for i in order]
        hydrate(top_indices)
        t1 = time.perf_counter()
        opt_warm_latencies.append((t1 - t0) * 1000)

    # Filtered queries (wing == 'claude_conversations_windows')
    filtered_latencies = []
    keep_indices = [i for i, w in enumerate(wings) if w == "claude_conversations_windows"]
    idx_arr = np.array(keep_indices, dtype=np.intp)
    filt_cids = [cids[i] for i in keep_indices]
    filt_ids = [ids[i] for i in keep_indices]

    def hydrate_filtered(f_top_indices):
        out = []
        for f_idx in f_top_indices:
            c.execute("SELECT id, document, metadata_json FROM documents WHERE collection_id = ? AND id = ?", (filt_cids[f_idx], filt_ids[f_idx]))
            out.append(c.fetchone())
        return out

    # Warm-up filtered
    for _ in range(3):
        f_mat = mat[idx_arr]
        d_ = cosine_distances(f_mat, query_vec)
        hydrate_filtered(np.argsort(d_, kind="mergesort")[:k])

    for _ in range(25):
        t0 = time.perf_counter()
        filt_mat = mat[idx_arr]
        dists = cosine_distances(filt_mat, query_vec)
        order = np.argsort(dists, kind="mergesort")[:k]
        f_top_indices = [int(i) for i in order]
        hydrate_filtered(f_top_indices)
        t1 = time.perf_counter()
        filtered_latencies.append((t1 - t0) * 1000)

    rss_end = get_rss_mb()
    conn.close()

    warm_latencies.sort()
    opt_warm_latencies.sort()
    filtered_latencies.sort()

    return {
        "language": "Python 3.14.5 (numpy)",
        "mode": mode,
        "rows_indexed": len(ids),
        "embedding_dim": int(mat.shape[1]),
        "rss_start_mb": round(rss_start, 2),
        "rss_after_load_mb": round(rss_after_load, 2),
        "rss_end_mb": round(rss_end, 2),
        "load_time_ms": round(load_time_ms, 2),
        "cold_first_query_ms": round(cold_first_query_ms, 2),
        "total_cold_start_ms": round(total_cold_ms, 2),
        "warm_p50_ms": round(float(np.percentile(warm_latencies, 50)), 2),
        "warm_p95_ms": round(float(np.percentile(warm_latencies, 95)), 2),
        "warm_p99_ms": round(float(np.percentile(warm_latencies, 99)), 2),
        "warm_min_ms": round(float(np.min(warm_latencies)), 2),
        "warm_max_ms": round(float(np.max(warm_latencies)), 2),
        "python_opt_warm_p50_ms": round(float(np.percentile(opt_warm_latencies, 50)), 2),
        "python_opt_warm_p95_ms": round(float(np.percentile(opt_warm_latencies, 95)), 2),
        "filtered_p50_ms": round(float(np.percentile(filtered_latencies, 50)), 2),
        "filtered_p95_ms": round(float(np.percentile(filtered_latencies, 95)), 2),
        "top_10": [{"id": tid, "distance": round(td, 6)} for tid, td in zip(top_ids, top_dists)]
    }

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "drawers"
    res = run_suite(mode)
    print(json.dumps(res, indent=2))
