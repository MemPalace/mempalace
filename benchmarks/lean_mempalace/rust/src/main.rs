use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::time::Instant;
use rayon::prelude::*;
use rusqlite::{params, Connection, OpenFlags};
use serde::Serialize;
use windows_sys::Win32::System::ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS};
use windows_sys::Win32::System::Threading::GetCurrentProcess;

const DIM: usize = 384;

#[derive(Clone, Debug)]
struct Candidate {
    id_idx: usize,
    distance: f32,
}

impl PartialEq for Candidate {
    fn eq(&self, other: &Self) -> bool {
        self.distance == other.distance
    }
}

impl Eq for Candidate {}

impl PartialOrd for Candidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Candidate {
    fn cmp(&self, other: &Self) -> Ordering {
        self.distance.partial_cmp(&other.distance).unwrap_or(Ordering::Equal)
    }
}

fn get_rss_mb() -> f64 {
    unsafe {
        let handle = GetCurrentProcess();
        let mut counters: PROCESS_MEMORY_COUNTERS = std::mem::zeroed();
        counters.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
        if GetProcessMemoryInfo(
            handle,
            &mut counters,
            std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        ) != 0
        {
            counters.WorkingSetSize as f64 / (1024.0 * 1024.0)
        } else {
            0.0
        }
    }
}

#[inline(always)]
fn dot_product(a: &[f32], b: &[f32]) -> f32 {
    debug_assert_eq!(a.len(), DIM);
    debug_assert_eq!(b.len(), DIM);
    let mut sum = 0.0f32;
    for i in 0..DIM {
        sum += a[i] * b[i];
    }
    sum
}

#[inline(always)]
fn l2_norm(v: &[f32]) -> f32 {
    let mut sum = 0.0f32;
    for i in 0..DIM {
        sum += v[i] * v[i];
    }
    sum.sqrt()
}

#[derive(Serialize)]
struct TopHitOut {
    id: String,
    distance: f64,
}

#[derive(Serialize)]
struct BenchResult {
    language: String,
    mode: String,
    rows_indexed: usize,
    embedding_dim: usize,
    rss_start_mb: f64,
    rss_after_load_mb: f64,
    rss_end_mb: f64,
    load_time_ms: f64,
    cold_first_query_ms: f64,
    total_cold_start_ms: f64,
    warm_p50_ms: f64,
    warm_p95_ms: f64,
    warm_p99_ms: f64,
    warm_min_ms: f64,
    warm_max_ms: f64,
    warm_parallel_p50_ms: f64,
    warm_parallel_p95_ms: f64,
    filtered_p50_ms: f64,
    filtered_p95_ms: f64,
    top_10: Vec<TopHitOut>,
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    let idx = ((p / 100.0) * (sorted.len() as f64)).floor() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mode = std::env::args().nth(1).unwrap_or_else(|| "drawers".to_string());
    let rss_start = get_rss_mb();
    let t_start = Instant::now();

    let db_path = r"C:\Users\igorl\.mempalace\palace\sqlite_exact.sqlite3";
    let conn = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI)?;
    conn.busy_timeout(std::time::Duration::from_millis(2000))?;

    // Sample query vector
    let sample_blob: Vec<u8> = conn.query_row(
        "SELECT embedding FROM documents WHERE id = 'drawer_44fb808c93188a039e5ce4ef712ebe0a'",
        [],
        |r| r.get(0),
    )?;
    let query_vec: &[f32] = unsafe {
        std::slice::from_raw_parts(sample_blob.as_ptr() as *const f32, DIM)
    };
    let q_norm = l2_norm(query_vec);

    let capacity = if mode == "all" { 350_000 } else { 170_000 };
    let mut cids: Vec<i64> = Vec::with_capacity(capacity);
    let mut ids: Vec<String> = Vec::with_capacity(capacity);
    let mut wing_ids: Vec<u16> = Vec::with_capacity(capacity);
    let mut flat_embeddings: Vec<f32> = Vec::with_capacity(capacity * DIM);
    let mut norms: Vec<f32> = Vec::with_capacity(capacity);

    let mut wing_map: HashMap<String, u16> = HashMap::new();
    wing_map.insert("".to_string(), 0);

    let t_load_start = Instant::now();
    let query_sql = if mode == "drawers" {
        "SELECT collection_id, id, embedding, COALESCE(wing, '') FROM documents WHERE collection_id = 1 ORDER BY rowid"
    } else {
        "SELECT collection_id, id, embedding, COALESCE(wing, '') FROM documents ORDER BY rowid"
    };

    let mut stmt = conn.prepare(query_sql)?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let cid: i64 = row.get(0)?;
        let id: String = row.get(1)?;
        let blob = row.get_ref(2)?.as_blob()?;
        let wing_str: &str = row.get_ref(3)?.as_str()?;
        if blob.len() != DIM * 4 { continue; }

        let wid = if wing_str.is_empty() {
            0
        } else if let Some(&existing) = wing_map.get(wing_str) {
            existing
        } else {
            let next_id = wing_map.len() as u16;
            wing_map.insert(wing_str.to_string(), next_id);
            next_id
        };

        let f32_slice: &[f32] = unsafe { std::slice::from_raw_parts(blob.as_ptr() as *const f32, DIM) };
        let norm = l2_norm(f32_slice);
        norms.push(norm);
        flat_embeddings.extend_from_slice(f32_slice);
        cids.push(cid);
        ids.push(id);
        wing_ids.push(wid);
    }

    let load_time_ms = t_load_start.elapsed().as_secs_f64() * 1000.0;
    let rss_after_load = get_rss_mb();
    let n_docs = ids.len();

    let target_wing_id = *wing_map.get("claude_conversations_windows").unwrap_or(&0);

    let search_single = |k: usize, filter_wid: u16| -> Vec<Candidate> {
        let mut heap = BinaryHeap::with_capacity(k);
        for i in 0..n_docs {
            if filter_wid != 0 && wing_ids[i] != filter_wid {
                continue;
            }
            let offset = i * DIM;
            let vec_slice = &flat_embeddings[offset..offset + DIM];
            let dot = dot_product(vec_slice, query_vec);
            let denom = norms[i] * q_norm;
            let mut cos = if denom > 0.0 { dot / denom } else { 0.0 };
            if cos > 1.0 { cos = 1.0; }
            if cos < -1.0 { cos = -1.0; }
            let dist = 1.0 - cos;

            if heap.len() < k {
                heap.push(Candidate { id_idx: i, distance: dist });
            } else if let Some(mut top) = heap.peek_mut() {
                if dist < top.distance {
                    *top = Candidate { id_idx: i, distance: dist };
                }
            }
        }
        heap.into_sorted_vec()
    };

    let search_parallel = |k: usize, filter_wid: u16| -> Vec<Candidate> {
        let chunk_size = 4096;
        let num_chunks = (n_docs + chunk_size - 1) / chunk_size;

        let local_heaps: Vec<BinaryHeap<Candidate>> = (0..num_chunks).into_par_iter().map(|c| {
            let start = c * chunk_size;
            let end = (start + chunk_size).min(n_docs);
            let mut heap = BinaryHeap::with_capacity(k);
            for i in start..end {
                if filter_wid != 0 && wing_ids[i] != filter_wid {
                    continue;
                }
                let offset = i * DIM;
                let vec_slice = &flat_embeddings[offset..offset + DIM];
                let dot = dot_product(vec_slice, query_vec);
                let denom = norms[i] * q_norm;
                let mut cos = if denom > 0.0 { dot / denom } else { 0.0 };
                if cos > 1.0 { cos = 1.0; }
                if cos < -1.0 { cos = -1.0; }
                let dist = 1.0 - cos;

                if heap.len() < k {
                    heap.push(Candidate { id_idx: i, distance: dist });
                } else if let Some(mut top) = heap.peek_mut() {
                    if dist < top.distance {
                        *top = Candidate { id_idx: i, distance: dist };
                    }
                }
            }
            heap
        }).collect();

        let mut final_heap = BinaryHeap::with_capacity(k);
        for heap in local_heaps {
            for item in heap {
                if final_heap.len() < k {
                    final_heap.push(item);
                } else if let Some(mut top) = final_heap.peek_mut() {
                    if item.distance < top.distance {
                        *top = item;
                    }
                }
            }
        }
        final_heap.into_sorted_vec()
    };

    let mut hydrate_stmt = conn.prepare("SELECT id, document, metadata_json FROM documents WHERE collection_id = ? AND id = ?")?;
    let hydrate = |hits: &[Candidate], stmt: &mut rusqlite::Statement| {
        for hit in hits {
            let _ = stmt.query_row(params![cids[hit.id_idx], &ids[hit.id_idx]], |_| Ok(()));
        }
    };

    let t_cold_q_start = Instant::now();
    let first_hits = search_single(10, 0);
    hydrate(&first_hits, &mut hydrate_stmt);
    let cold_first_query_ms = t_cold_q_start.elapsed().as_secs_f64() * 1000.0;
    let total_cold_ms = t_start.elapsed().as_secs_f64() * 1000.0;

    // Warm-up iterations (3 runs not recorded)
    for _ in 0..3 {
        hydrate(&search_single(10, 0), &mut hydrate_stmt);
        hydrate(&search_parallel(10, 0), &mut hydrate_stmt);
        hydrate(&search_single(10, target_wing_id), &mut hydrate_stmt);
    }

    let mut warm_latencies = Vec::with_capacity(25);
    let mut warm_hits = Vec::new();
    for _ in 0..25 {
        let t0 = Instant::now();
        warm_hits = search_single(10, 0);
        hydrate(&warm_hits, &mut hydrate_stmt);
        warm_latencies.push(t0.elapsed().as_secs_f64() * 1000.0);
    }

    let mut parallel_latencies = Vec::with_capacity(25);
    for _ in 0..25 {
        let t0 = Instant::now();
        let p_hits = search_parallel(10, 0);
        hydrate(&p_hits, &mut hydrate_stmt);
        parallel_latencies.push(t0.elapsed().as_secs_f64() * 1000.0);
    }

    let mut filtered_latencies = Vec::with_capacity(25);
    for _ in 0..25 {
        let t0 = Instant::now();
        let f_hits = search_single(10, target_wing_id);
        hydrate(&f_hits, &mut hydrate_stmt);
        filtered_latencies.push(t0.elapsed().as_secs_f64() * 1000.0);
    }

    warm_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());
    parallel_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());
    filtered_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let rss_end = get_rss_mb();

    let top_10: Vec<TopHitOut> = warm_hits
        .iter()
        .map(|c| TopHitOut {
            id: ids[c.id_idx].clone(),
            distance: (c.distance as f64 * 1_000_000.0).round() / 1_000_000.0,
        })
        .collect();

    let result = BenchResult {
        language: "Rust (1.97.0-nightly + rusqlite + Rayon)".to_string(),
        mode,
        rows_indexed: n_docs,
        embedding_dim: DIM,
        rss_start_mb: (rss_start * 100.0).round() / 100.0,
        rss_after_load_mb: (rss_after_load * 100.0).round() / 100.0,
        rss_end_mb: (rss_end * 100.0).round() / 100.0,
        load_time_ms: (load_time_ms * 100.0).round() / 100.0,
        cold_first_query_ms: (cold_first_query_ms * 100.0).round() / 100.0,
        total_cold_start_ms: (total_cold_ms * 100.0).round() / 100.0,
        warm_p50_ms: (percentile(&warm_latencies, 50.0) * 100.0).round() / 100.0,
        warm_p95_ms: (percentile(&warm_latencies, 95.0) * 100.0).round() / 100.0,
        warm_p99_ms: (percentile(&warm_latencies, 99.0) * 100.0).round() / 100.0,
        warm_min_ms: (warm_latencies[0] * 100.0).round() / 100.0,
        warm_max_ms: (warm_latencies[warm_latencies.len() - 1] * 100.0).round() / 100.0,
        warm_parallel_p50_ms: (percentile(&parallel_latencies, 50.0) * 100.0).round() / 100.0,
        warm_parallel_p95_ms: (percentile(&parallel_latencies, 95.0) * 100.0).round() / 100.0,
        filtered_p50_ms: (percentile(&filtered_latencies, 50.0) * 100.0).round() / 100.0,
        filtered_p95_ms: (percentile(&filtered_latencies, 95.0) * 100.0).round() / 100.0,
        top_10,
    };

    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}
