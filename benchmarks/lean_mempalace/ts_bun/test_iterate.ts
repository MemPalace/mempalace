import { Database } from "bun:sqlite";

function getRssMb(): number {
  return process.memoryUsage().rss / (1024 * 1024);
}

const db = new Database(process.env.MEMPALACE_DB_PATH!, { readonly: true });
const count = (db.query("SELECT count(*) as count FROM documents WHERE collection_id = (SELECT id FROM collections WHERE name = 'mempalace_drawers')").get() as any).count;

const ids: string[] = new Array(count);
const wings: (string | null)[] = new Array(count);
const mat = new Float32Array(count * 384);
const norms = new Float32Array(count);

const query = db.query("SELECT id, embedding, wing FROM documents WHERE collection_id = (SELECT id FROM collections WHERE name = 'mempalace_drawers') ORDER BY rowid");
let i = 0;
for (const row of query.iterate() as any) {
  ids[i] = row.id;
  wings[i] = row.wing;
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

Bun.gc(true);
console.log("RSS after Bun.gc(true):", getRssMb().toFixed(2), "MB");
db.close();
