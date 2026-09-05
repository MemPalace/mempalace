import { Database } from "bun:sqlite";

interface TopHit {
  idx: number;
  id: string;
  distance: number;
}

class MinHeapTopK {
  private k: number;
  public heap: { idx: number; distance: number }[];

  constructor(k: number) {
    this.k = k;
    this.heap = [];
  }

  push(idx: number, distance: number) {
    if (this.heap.length < this.k) {
      this.heap.push({ idx, distance });
      this.siftUp(this.heap.length - 1);
    } else if (distance < this.heap[0].distance) {
      this.heap[0] = { idx, distance };
      this.siftDown(0);
    }
  }

  private siftUp(i: number) {
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (this.heap[i].distance > this.heap[parent].distance) {
        const tmp = this.heap[i];
        this.heap[i] = this.heap[parent];
        this.heap[parent] = tmp;
        i = parent;
      } else {
        break;
      }
    }
  }

  private siftDown(i: number) {
    const len = this.heap.length;
    while ((i << 1) + 1 < len) {
      let left = (i << 1) + 1;
      let right = left + 1;
      let largest = i;
      if (this.heap[left].distance > this.heap[largest].distance) largest = left;
      if (right < len && this.heap[right].distance > this.heap[largest].distance) largest = right;
      if (largest !== i) {
        const tmp = this.heap[i];
        this.heap[i] = this.heap[largest];
        this.heap[largest] = tmp;
        i = largest;
      } else {
        break;
      }
    }
  }

  getSortedResults(ids: string[]): TopHit[] {
    return [...this.heap]
      .sort((a, b) => a.distance - b.distance)
      .map(h => ({ idx: h.idx, id: ids[h.idx], distance: h.distance }));
  }
}

function getRssMb(): number {
  return process.memoryUsage().rss / (1024 * 1024);
}

// Inline Worker Code for Multi-threaded Scan
const workerCode = `
self.onmessage = (e) => {
  const { id, start, end, matSab, normsSab, queryVec, qNorm, k, filterWid, wingIdsSab } = e.data;
  const mat = new Float32Array(matSab);
  const norms = new Float32Array(normsSab);
  const wingIds = wingIdsSab ? new Uint16Array(wingIdsSab) : null;
  const q = new Float32Array(queryVec);

  const heap = [];
  for (let j = start; j < end; j++) {
    if (filterWid !== 0 && wingIds && wingIds[j] !== filterWid) continue;
    const offset = j * 384;
    let dot = 0;
    for (let d = 0; d < 384; d += 8) {
      dot += mat[offset + d] * q[d]
           + mat[offset + d + 1] * q[d + 1]
           + mat[offset + d + 2] * q[d + 2]
           + mat[offset + d + 3] * q[d + 3]
           + mat[offset + d + 4] * q[d + 4]
           + mat[offset + d + 5] * q[d + 5]
           + mat[offset + d + 6] * q[d + 6]
           + mat[offset + d + 7] * q[d + 7];
    }
    const denom = norms[j] * qNorm;
    let cos = denom > 0 ? dot / denom : 0;
    if (cos > 1.0) cos = 1.0;
    if (cos < -1.0) cos = -1.0;
    const dist = 1.0 - cos;

    if (heap.length < k) {
      heap.push({ idx: j, dist });
      heap.sort((a, b) => b.dist - a.dist);
    } else if (dist < heap[0].dist) {
      heap[0] = { idx: j, dist };
      heap.sort((a, b) => b.dist - a.dist);
    }
  }
  postMessage({ id, results: heap });
};
`;

async function runBench(mode = "drawers") {
  const dbPath = "C:/Users/igorl/.mempalace/palace/sqlite_exact.sqlite3";
  const rssStart = getRssMb();
  const tStart = performance.now();

  const db = new Database(dbPath, { readonly: true });
  db.run("PRAGMA busy_timeout=2000;");

  // Query vector sample
  const sampleRow = db.query("SELECT embedding FROM documents WHERE id = 'drawer_44fb808c93188a039e5ce4ef712ebe0a'").get() as { embedding: Uint8Array };
  const queryVec = new Float32Array(sampleRow.embedding.buffer, sampleRow.embedding.byteOffset, 384);

  // Load
  const tLoadStart = performance.now();
  let countSql = "SELECT count(*) as count FROM documents";
  let loadSql = "SELECT collection_id, id, embedding, wing FROM documents ORDER BY rowid";
  if (mode === "drawers") {
    countSql = "SELECT count(*) as count FROM documents WHERE collection_id = 1";
    loadSql = "SELECT collection_id, id, embedding, wing FROM documents WHERE collection_id = 1 ORDER BY rowid";
  }

  const count = (db.query(countSql).get() as any).count;
  const cids = new Uint8Array(count);
  const ids: string[] = new Array(count);
  
  // Dense typed arrays backed by SharedArrayBuffer for zero-copy worker access
  const matSab = new SharedArrayBuffer(count * 384 * 4);
  const normsSab = new SharedArrayBuffer(count * 4);
  const wingIdsSab = new SharedArrayBuffer(count * 2);

  const mat = new Float32Array(matSab);
  const norms = new Float32Array(normsSab);
  const wingIds = new Uint16Array(wingIdsSab);

  const wingMap = new Map<string, number>();
  wingMap.set("", 0);

  const query = db.query(loadSql);
  let i = 0;
  for (const row of query.iterate() as any) {
    cids[i] = row.collection_id;
    ids[i] = row.id;
    const w = row.wing || "";
    let wid = wingMap.get(w);
    if (wid === undefined) {
      wid = wingMap.size;
      wingMap.set(w, wid);
    }
    wingIds[i] = wid;

    const blob = row.embedding;
    const v = new Float32Array(blob.buffer, blob.byteOffset, 384);
    const offset = i * 384;
    let sumSq = 0;
    for (let d = 0; d < 384; d++) {
      const val = v[d];
      mat[offset + d] = val;
      sumSq += val * val;
    }
    norms[i] = Math.sqrt(sumSq);
    i++;
  }

  const tLoadEnd = performance.now();
  const loadTimeMs = tLoadEnd - tLoadStart;
  Bun.gc(true);
  const rssAfterLoad = getRssMb();

  // Query vector norm
  let qSumSq = 0;
  for (let d = 0; d < 384; d++) qSumSq += queryVec[d] * queryVec[d];
  const qNorm = Math.sqrt(qSumSq);

  const targetWingName = "claude_conversations_windows";
  const targetWingId = wingMap.get(targetWingName) || 0;

  // Single-threaded search function with unrolled SIMD-friendly dot product
  function searchSingle(k = 10, filterWid = 0): TopHit[] {
    const heap = new MinHeapTopK(k);
    for (let j = 0; j < i; j++) {
      if (filterWid !== 0 && wingIds[j] !== filterWid) continue;

      const offset = j * 384;
      let dot = 0;
      for (let d = 0; d < 384; d += 8) {
        dot += mat[offset + d] * queryVec[d]
             + mat[offset + d + 1] * queryVec[d + 1]
             + mat[offset + d + 2] * queryVec[d + 2]
             + mat[offset + d + 3] * queryVec[d + 3]
             + mat[offset + d + 4] * queryVec[d + 4]
             + mat[offset + d + 5] * queryVec[d + 5]
             + mat[offset + d + 6] * queryVec[d + 6]
             + mat[offset + d + 7] * queryVec[d + 7];
      }
      const denom = norms[j] * qNorm;
      let cos = denom > 0 ? dot / denom : 0;
      if (cos > 1.0) cos = 1.0;
      if (cos < -1.0) cos = -1.0;
      const dist = 1.0 - cos;

      heap.push(j, dist);
    }
    return heap.getSortedResults(ids);
  }

  // Setup Worker pool for Parallel search
  const numWorkers = 8;
  const workerBlob = new Blob([workerCode], { type: "application/javascript" });
  const workerUrl = URL.createObjectURL(workerBlob);
  const workers: Worker[] = [];
  for (let w = 0; w < numWorkers; w++) {
    workers.push(new Worker(workerUrl));
  }

  async function searchParallel(k = 10, filterWid = 0): Promise<TopHit[]> {
    const chunkSize = Math.ceil(i / numWorkers);
    const promises: Promise<any[]>[] = [];
    for (let w = 0; w < numWorkers; w++) {
      const start = w * chunkSize;
      const end = Math.min(i, start + chunkSize);
      if (start >= end) continue;
      promises.push(new Promise((resolve) => {
        const handler = (e: any) => {
          if (e.data.id === w) {
            workers[w].removeEventListener("message", handler);
            resolve(e.data.results);
          }
        };
        workers[w].addEventListener("message", handler);
        workers[w].postMessage({
          id: w,
          start,
          end,
          matSab,
          normsSab,
          wingIdsSab,
          queryVec: queryVec.buffer,
          qNorm,
          k,
          filterWid
        });
      }));
    }
    const workerResults = await Promise.all(promises);
    const allHits = workerResults.flat();
    allHits.sort((a, b) => a.dist - b.dist);
    return allHits.slice(0, k).map(h => ({ idx: h.idx, id: ids[h.idx], distance: h.dist }));
  }

  // Hydration statement using covering primary key index (collection_id, id)
  const hydrateStmt = db.query(`SELECT id, document, metadata_json FROM documents WHERE collection_id = ? AND id = ?`);
  function hydrate(hits: TopHit[]) {
    const out = [];
    for (const h of hits) {
      out.push(hydrateStmt.get(cids[h.idx], h.id));
    }
    return out;
  }

  // Cold first query
  const tColdQStart = performance.now();
  const firstHits = searchSingle(10);
  hydrate(firstHits);
  const tColdQEnd = performance.now();
  const coldFirstQueryMs = tColdQEnd - tColdQStart;
  const totalColdMs = tColdQEnd - tStart;

  // JIT Warm-up iterations (3 runs not recorded)
  for (let w = 0; w < 3; w++) {
    hydrate(searchSingle(10));
    hydrate(await searchParallel(10));
    hydrate(searchSingle(10, targetWingId));
  }

  // Warm queries (25 measured iterations)
  const warmLatencies: number[] = [];
  let warmHits: TopHit[] = [];
  for (let iter = 0; iter < 25; iter++) {
    const t0 = performance.now();
    warmHits = searchSingle(10);
    hydrate(warmHits);
    const t1 = performance.now();
    warmLatencies.push(t1 - t0);
  }

  // Warm parallel queries (25 measured iterations)
  const parallelLatencies: number[] = [];
  for (let iter = 0; iter < 25; iter++) {
    const t0 = performance.now();
    const pHits = await searchParallel(10);
    hydrate(pHits);
    const t1 = performance.now();
    parallelLatencies.push(t1 - t0);
  }

  // Filtered queries (25 measured iterations)
  const filteredLatencies: number[] = [];
  for (let iter = 0; iter < 25; iter++) {
    const t0 = performance.now();
    const hits = searchSingle(10, targetWingId);
    hydrate(hits);
    const t1 = performance.now();
    filteredLatencies.push(t1 - t0);
  }

  for (const w of workers) {
    w.terminate();
  }

  warmLatencies.sort((a, b) => a - b);
  parallelLatencies.sort((a, b) => a - b);
  filteredLatencies.sort((a, b) => a - b);

  function percentile(arr: number[], p: number): number {
    const idx = Math.min(arr.length - 1, Math.floor((p / 100) * arr.length));
    return arr[idx];
  }

  const rssEnd = getRssMb();
  db.close();

  const result = {
    language: "TypeScript (Bun 1.4.1 + bun:sqlite)",
    mode,
    rows_indexed: i,
    embedding_dim: 384,
    rss_start_mb: Math.round(rssStart * 100) / 100,
    rss_after_load_mb: Math.round(rssAfterLoad * 100) / 100,
    rss_end_mb: Math.round(rssEnd * 100) / 100,
    load_time_ms: Math.round(loadTimeMs * 100) / 100,
    cold_first_query_ms: Math.round(coldFirstQueryMs * 100) / 100,
    total_cold_start_ms: Math.round(totalColdMs * 100) / 100,
    warm_p50_ms: Math.round(percentile(warmLatencies, 50) * 100) / 100,
    warm_p95_ms: Math.round(percentile(warmLatencies, 95) * 100) / 100,
    warm_p99_ms: Math.round(percentile(warmLatencies, 99) * 100) / 100,
    warm_min_ms: Math.round(warmLatencies[0] * 100) / 100,
    warm_max_ms: Math.round(warmLatencies[warmLatencies.length - 1] * 100) / 100,
    warm_parallel_p50_ms: Math.round(percentile(parallelLatencies, 50) * 100) / 100,
    warm_parallel_p95_ms: Math.round(percentile(parallelLatencies, 95) * 100) / 100,
    filtered_p50_ms: Math.round(percentile(filteredLatencies, 50) * 100) / 100,
    filtered_p95_ms: Math.round(percentile(filteredLatencies, 95) * 100) / 100,
    top_10: warmHits.map(h => ({ id: h.id, distance: Math.round(h.distance * 1000000) / 1000000 }))
  };

  console.log(JSON.stringify(result, null, 2));
}

const mode = process.argv[2] || "drawers";
runBench(mode);
