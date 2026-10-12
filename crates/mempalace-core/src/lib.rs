use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::path::Path;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum MemPalaceError {
    #[error("Database error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("SQL type conversion error: {0}")]
    FromSql(#[from] rusqlite::types::FromSqlError),
    #[error("Collection '{0}' not found in database")]
    CollectionNotFound(String),
    #[error("Dimension mismatch: expected {expected}, got {actual}")]
    DimensionMismatch { expected: usize, actual: usize },
    #[error("Invalid argument: {0}")]
    InvalidArgument(String),
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Hit {
    pub id: String,
    pub distance: f32,
    pub similarity: f32,
    pub wing: Option<String>,
    pub room: Option<String>,
}

#[derive(Clone, Debug)]
struct Candidate {
    id_idx: usize,
    distance: f32,
}

impl PartialEq for Candidate {
    fn eq(&self, other: &Self) -> bool {
        self.distance == other.distance && self.id_idx == other.id_idx
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
        self.distance
            .total_cmp(&other.distance)
            .then_with(|| self.id_idx.cmp(&other.id_idx))
    }
}

#[inline(always)]
fn dot_product(a: &[f32], b: &[f32]) -> f32 {
    let len = a.len();
    let mut sum = 0.0f32;
    let chunks = len / 8;
    for c in 0..chunks {
        let base = c * 8;
        sum += a[base] * b[base]
            + a[base + 1] * b[base + 1]
            + a[base + 2] * b[base + 2]
            + a[base + 3] * b[base + 3]
            + a[base + 4] * b[base + 4]
            + a[base + 5] * b[base + 5]
            + a[base + 6] * b[base + 6]
            + a[base + 7] * b[base + 7];
    }
    for i in (chunks * 8)..len {
        sum += a[i] * b[i];
    }
    sum
}

#[inline(always)]
fn l2_norm(v: &[f32]) -> f32 {
    let mut sum = 0.0f32;
    for x in v {
        sum += x * x;
    }
    sum.sqrt()
}

// Every finite f32 product and norm fits in f64, including subnormal inputs.
// Keep ordinary embeddings on the f32 path; recompute unsafe intermediates.
fn cosine_wide(a: &[f32], b: &[f32]) -> f32 {
    let (mut dot, mut a_squared, mut b_squared) = (0.0f64, 0.0f64, 0.0f64);
    for (&a, &b) in a.iter().zip(b) {
        let (a, b) = (f64::from(a), f64::from(b));
        dot += a * b;
        a_squared += a * a;
        b_squared += b * b;
    }
    if a_squared == 0.0 || b_squared == 0.0 {
        return 0.0;
    }
    (dot / (a_squared.sqrt() * b_squared.sqrt())).clamp(-1.0, 1.0) as f32
}

pub struct VectorIndex {
    ids: Vec<String>,
    vectors: Vec<f32>,
    norms: Vec<f32>,
    dim: usize,
    wing_ids: Vec<usize>,
    wing_names: Vec<String>,
    wing_map: HashMap<String, usize>,
    rooms: Vec<Option<String>>,
}

impl VectorIndex {
    pub fn new(dim: usize) -> Self {
        Self {
            ids: Vec::new(),
            vectors: Vec::new(),
            norms: Vec::new(),
            dim,
            wing_ids: Vec::new(),
            wing_names: vec!["".to_string()], // 0 = none / unknown
            wing_map: HashMap::new(),
            rooms: Vec::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.ids.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ids.is_empty()
    }

    pub fn dim(&self) -> usize {
        self.dim
    }

    fn intern_wing(&mut self, wing: Option<&str>) -> usize {
        match wing {
            None => 0,
            Some(w) => {
                if let Some(&id) = self.wing_map.get(w) {
                    id
                } else {
                    let new_id = self.wing_names.len();
                    self.wing_names.push(w.to_string());
                    self.wing_map.insert(w.to_string(), new_id);
                    new_id
                }
            }
        }
    }

    pub fn load_from_sqlite<P: AsRef<Path>>(
        db_path: P,
        collection_name: Option<&str>,
    ) -> Result<Self, MemPalaceError> {
        let conn = Connection::open_with_flags(
            db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )?;
        conn.execute_batch(
            "PRAGMA busy_timeout=5000;
             PRAGMA mmap_size=1073741824;
             PRAGMA cache_size=-131072;",
        )?;

        // The public default is the verbatim drawer collection, never an
        // unscoped scan that mixes drawers, closets, and embedding dimensions.
        let col_name = collection_name.unwrap_or("mempalace_drawers");
        let collection_columns = conn
            .prepare("PRAGMA table_info(collections)")?
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<Result<Vec<_>, _>>()?;
        let document_columns = conn
            .prepare("PRAGMA table_xinfo(documents)")?
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<Result<Vec<_>, _>>()?;
        let dimension_expr = if collection_columns.iter().any(|c| c == "dimension") {
            "dimension"
        } else {
            "NULL"
        };
        let row: Result<(i64, Option<i64>), rusqlite::Error> = conn.query_row(
            // Older migrations add a nullable dimension column. Infer from
            // the first encoded vector; every blob is validated below, so
            // mixed dimensions and truncated floats still fail explicitly.
            &format!(
                "SELECT id, COALESCE({dimension_expr}, (
                SELECT length(embedding) / 4 FROM documents
                WHERE collection_id = collections.id ORDER BY rowid LIMIT 1
            )) FROM collections WHERE name = ?1"
            ),
            [col_name],
            |r| Ok((r.get(0)?, r.get(1)?)),
        );
        let (collection_id, dim) = match row {
            Ok(row) => row,
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                return Err(MemPalaceError::CollectionNotFound(col_name.to_string()));
            }
            Err(err) => return Err(err.into()),
        };
        let dim = usize::try_from(dim.unwrap_or(0))
            .map_err(|_| MemPalaceError::InvalidArgument("negative dimension".into()))?;
        let expected_bytes = dim
            .checked_mul(std::mem::size_of::<f32>())
            .ok_or_else(|| MemPalaceError::InvalidArgument("dimension overflow".into()))?;
        let mut index = Self::new(dim);
        let wing = if document_columns.iter().any(|c| c == "wing") {
            "wing"
        } else {
            "json_extract(metadata_json, '$.wing')"
        };
        let room = if document_columns.iter().any(|c| c == "room") {
            "room"
        } else {
            "json_extract(metadata_json, '$.room')"
        };
        let mut stmt = conn.prepare(&format!(
            "SELECT id, embedding, {wing}, {room} FROM documents WHERE collection_id = ?1 ORDER BY rowid"
        ))?;
        let mut rows = stmt.query([collection_id])?;

        while let Some(row) = rows.next()? {
            let id: String = row.get(0)?;
            let blob_ref = row.get_ref(1)?.as_blob()?;

            if dim == 0 || blob_ref.len() != expected_bytes {
                return Err(MemPalaceError::InvalidArgument(format!(
                    "invalid embedding length for document {id}"
                )));
            }

            // SQLite exposes bytes with no f32 alignment guarantee. Decode
            // little-endian floats directly into our owned contiguous buffer.
            let start = index.vectors.len();
            for bytes in blob_ref.chunks_exact(4) {
                let value = f32::from_le_bytes(bytes.try_into().unwrap());
                if !value.is_finite() {
                    return Err(MemPalaceError::InvalidArgument(format!(
                        "non-finite embedding for document {id}"
                    )));
                }
                index.vectors.push(value);
            }
            index.norms.push(l2_norm(&index.vectors[start..]));
            index.ids.push(id);

            let wing_val: Option<String> = row.get(2).ok();
            let wid = index.intern_wing(wing_val.as_deref());
            index.wing_ids.push(wid);

            let room_val: Option<String> = row.get(3).ok();
            index.rooms.push(room_val);
        }

        Ok(index)
    }

    fn validate_query(&self, query_vec: &[f32]) -> Result<(), MemPalaceError> {
        if query_vec.len() != self.dim {
            return Err(MemPalaceError::DimensionMismatch {
                expected: self.dim,
                actual: query_vec.len(),
            });
        }
        if query_vec.iter().any(|x| !x.is_finite()) {
            return Err(MemPalaceError::InvalidArgument(
                "query must contain finite floats".into(),
            ));
        }
        Ok(())
    }

    fn retain(heap: &mut BinaryHeap<Candidate>, candidate: Candidate, k: usize) {
        if heap.len() < k {
            heap.push(candidate);
        } else if heap.peek().is_some_and(|top| candidate < *top) {
            *heap.peek_mut().unwrap() = candidate;
        }
    }

    fn scan(
        &self,
        query_vec: &[f32],
        k: usize,
        wing: Option<usize>,
        range: std::ops::Range<usize>,
    ) -> BinaryHeap<Candidate> {
        let mut heap = BinaryHeap::with_capacity(k.min(range.len()));
        let q_norm = l2_norm(query_vec);
        for i in range {
            if wing.is_some_and(|wid| self.wing_ids[i] != wid) {
                continue;
            }
            let vec_start = i * self.dim;
            let vector = &self.vectors[vec_start..vec_start + self.dim];
            let dot = dot_product(vector, query_vec);
            let denom = self.norms[i] * q_norm;
            let cos = if dot.is_finite() && denom.is_normal() && denom > 0.0 {
                (dot / denom).clamp(-1.0, 1.0)
            } else {
                cosine_wide(vector, query_vec)
            };
            Self::retain(
                &mut heap,
                Candidate {
                    id_idx: i,
                    distance: 1.0 - cos,
                },
                k,
            );
        }
        heap
    }

    fn hits(&self, heap: BinaryHeap<Candidate>) -> Vec<Hit> {
        heap.into_sorted_vec()
            .into_iter()
            .map(|c| Hit {
                id: self.ids[c.id_idx].clone(),
                distance: c.distance,
                similarity: (1.0 - c.distance).max(0.0),
                wing: Some(self.wing_names[self.wing_ids[c.id_idx]].clone())
                    .filter(|s| !s.is_empty()),
                room: self.rooms[c.id_idx].clone(),
            })
            .collect()
    }

    pub fn query(
        &self,
        query_vec: &[f32],
        k: usize,
        filter_wing: Option<&str>,
    ) -> Result<Vec<Hit>, MemPalaceError> {
        self.validate_query(query_vec)?;
        let k = k.min(self.len());
        if k == 0 {
            return Ok(Vec::new());
        }
        let wing = match filter_wing {
            Some(name) => match self.wing_map.get(name) {
                Some(&id) => Some(id),
                None => return Ok(Vec::new()),
            },
            None => None,
        };
        Ok(self.hits(self.scan(query_vec, k, wing, 0..self.len())))
    }

    pub fn query_parallel(
        &self,
        query_vec: &[f32],
        k: usize,
        filter_wing: Option<&str>,
    ) -> Result<Vec<Hit>, MemPalaceError> {
        self.validate_query(query_vec)?;
        let k = k.min(self.len());
        if k == 0 {
            return Ok(Vec::new());
        }
        if self.len() < 10000 {
            return self.query(query_vec, k, filter_wing);
        }
        let wing = match filter_wing {
            Some(name) => match self.wing_map.get(name) {
                Some(&id) => Some(id),
                None => return Ok(Vec::new()),
            },
            None => None,
        };
        let chunks = rayon::current_num_threads().max(1);
        let chunk_size = self.len().div_ceil(chunks);
        let heaps: Vec<_> = (0..chunks)
            .into_par_iter()
            .map(|chunk| {
                let start = (chunk * chunk_size).min(self.len());
                self.scan(
                    query_vec,
                    k,
                    wing,
                    start..(start + chunk_size).min(self.len()),
                )
            })
            .collect();
        let mut heap = BinaryHeap::with_capacity(k);
        for candidate in heaps.into_iter().flatten() {
            Self::retain(&mut heap, candidate, k);
        }
        Ok(self.hits(heap))
    }

    pub fn wing_counts(&self) -> HashMap<String, usize> {
        let mut counts = HashMap::new();
        for &wid in &self.wing_ids {
            let name = &self.wing_names[wid];
            if !name.is_empty() {
                *counts.entry(name.clone()).or_insert(0) += 1;
            }
        }
        counts
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_dot_and_norm() {
        let a = vec![1.0, 0.0, 0.0, 0.0];
        let b = vec![0.0, 1.0, 0.0, 0.0];
        assert_eq!(dot_product(&a, &b), 0.0);
        assert_eq!(l2_norm(&a), 1.0);
    }

    #[test]
    fn test_in_memory_index() {
        let mut index = VectorIndex::new(4);
        index.ids.push("doc1".to_string());
        index.vectors.extend_from_slice(&[1.0, 0.0, 0.0, 0.0]);
        index.norms.push(1.0);
        index.wing_ids.push(0);
        index.rooms.push(None);

        index.ids.push("doc2".to_string());
        index.vectors.extend_from_slice(&[0.0, 1.0, 0.0, 0.0]);
        index.norms.push(1.0);
        index.wing_ids.push(0);
        index.rooms.push(None);

        let hits = index.query(&[1.0, 0.0, 0.0, 0.0], 1, None).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "doc1");
        assert!(hits[0].distance.abs() < 1e-6);
    }

    fn filled(count: usize) -> VectorIndex {
        let mut index = VectorIndex::new(2);
        for i in 0..count {
            index.ids.push(i.to_string());
            index.vectors.extend_from_slice(&[1.0, 0.0]);
            index.norms.push(1.0);
            index.wing_ids.push(0);
            index.rooms.push(None);
        }
        index
    }

    #[test]
    fn parallel_boundaries_and_ties_match_serial() {
        for count in [0, 9999, 10000, 10001] {
            let index = filled(count);
            for k in [0, 1, 7, count + 1] {
                let serial = index.query(&[1.0, 0.0], k, None).unwrap();
                let parallel = index.query_parallel(&[1.0, 0.0], k, None).unwrap();
                assert_eq!(
                    serial.iter().map(|h| &h.id).collect::<Vec<_>>(),
                    parallel.iter().map(|h| &h.id).collect::<Vec<_>>()
                );
                assert_eq!(serial.len(), k.min(count));
                for (i, hit) in parallel.iter().enumerate() {
                    assert_eq!(hit.id, i.to_string());
                }
            }
            assert!(index
                .query_parallel(&[1.0, 0.0], 5, Some("missing"))
                .unwrap()
                .is_empty());
        }
    }

    #[test]
    fn validates_queries_and_wing_ids_do_not_wrap() {
        let mut index = filled(10000);
        assert!(index.query_parallel(&[1.0], 1, None).is_err());
        assert!(index.query_parallel(&[f32::NAN, 0.0], 1, None).is_err());
        for i in 0..65537 {
            assert_eq!(index.intern_wing(Some(&i.to_string())), i + 1);
        }
    }

    #[test]
    fn sqlite_load_is_scoped_and_validates_blobs() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("vectors.sqlite3");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE collections(id INTEGER, name TEXT, dimension INTEGER);
            CREATE TABLE documents(id TEXT, embedding BLOB, wing TEXT, room TEXT, collection_id INTEGER);
            INSERT INTO collections VALUES(1, 'mempalace_drawers', 2), (2, 'mempalace_closets', 1);").unwrap();
        let blob: Vec<u8> = [1.0f32, 0.0].iter().flat_map(|v| v.to_le_bytes()).collect();
        conn.execute(
            "INSERT INTO documents VALUES('odd-length-id', ?1, 'wing', NULL, 1)",
            [&blob],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO documents VALUES('closet', ?1, NULL, NULL, 2)",
            [1.0f32.to_le_bytes().as_slice()],
        )
        .unwrap();
        let index = VectorIndex::load_from_sqlite(&path, None).unwrap();
        assert_eq!(index.len(), 1);
        assert_eq!(
            index.query(&[1.0, 0.0], 2, Some("wing")).unwrap()[0].id,
            "odd-length-id"
        );
        assert_eq!(
            VectorIndex::load_from_sqlite(&path, Some("mempalace_closets"))
                .unwrap()
                .dim(),
            1
        );
        assert!(matches!(
            VectorIndex::load_from_sqlite(&path, Some("missing")),
            Err(MemPalaceError::CollectionNotFound(_))
        ));
        conn.execute(
            "UPDATE documents SET embedding=x'0001' WHERE collection_id=1",
            [],
        )
        .unwrap();
        assert!(VectorIndex::load_from_sqlite(&path, None).is_err());
    }

    #[test]
    fn migrated_null_dimension_is_inferred_and_validated() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("legacy.sqlite3");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch("CREATE TABLE collections(id INTEGER, name TEXT, dimension INTEGER);
            CREATE TABLE documents(id TEXT, embedding BLOB, wing TEXT, room TEXT, collection_id INTEGER);
            INSERT INTO collections VALUES(1, 'mempalace_drawers', NULL);
            INSERT INTO documents VALUES('a', x'0000803f00000000', NULL, NULL, 1);").unwrap();
        let index = VectorIndex::load_from_sqlite(&path, None).unwrap();
        assert_eq!(index.dim(), 2);
        assert_eq!(index.query(&[1.0, 0.0], 1, None).unwrap()[0].id, "a");
        let dimension: Option<i64> = conn
            .query_row("SELECT dimension FROM collections", [], |r| r.get(0))
            .unwrap();
        assert_eq!(dimension, None); // Inference must not migrate a read-only database.
        conn.execute(
            "INSERT INTO documents VALUES('b', x'0000803f', NULL, NULL, 1)",
            [],
        )
        .unwrap();
        assert!(VectorIndex::load_from_sqlite(&path, None).is_err());
        conn.execute("DELETE FROM documents", []).unwrap();
        assert!(VectorIndex::load_from_sqlite(&path, None)
            .unwrap()
            .is_empty());
        conn.execute(
            "INSERT INTO documents VALUES('bad', x'0000803f00', NULL, NULL, 1)",
            [],
        )
        .unwrap();
        assert!(VectorIndex::load_from_sqlite(&path, None).is_err());
    }

    #[test]
    fn finite_extreme_magnitudes_keep_cosine_distances_finite() {
        for magnitude in [1e20f32, 1e-30, f32::from_bits(1)] {
            for count in [4, 10004] {
                let mut index = VectorIndex::new(2);
                for i in 0..count {
                    let vector = match i {
                        0 => [magnitude, 0.0],
                        1 => [0.0, magnitude],
                        2 => [0.0, 0.0],
                        _ => [-magnitude, 0.0],
                    };
                    index.ids.push(i.to_string());
                    index.vectors.extend_from_slice(&vector);
                    index.norms.push(l2_norm(&vector));
                    index.wing_ids.push(0);
                    index.rooms.push(None);
                }
                for query in [[magnitude, 0.0], [0.0, 0.0]] {
                    let expected = if query[0] == 0.0 {
                        [1.0, 1.0, 1.0, 1.0]
                    } else {
                        [0.0, 1.0, 1.0, 2.0]
                    };
                    for hits in [
                        index.query(&query, 4, None).unwrap(),
                        index.query_parallel(&query, 4, None).unwrap(),
                    ] {
                        for (i, hit) in hits.iter().enumerate() {
                            assert_eq!(hit.id, i.to_string());
                            assert_eq!(hit.distance, expected[i]);
                            assert!(hit.similarity.is_finite());
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn pre_locus_schema_is_readable_without_migration() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("old.sqlite3");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(r#"CREATE TABLE collections(id INTEGER, name TEXT);
            CREATE TABLE documents(id TEXT, embedding BLOB, metadata_json TEXT, collection_id INTEGER);
            INSERT INTO collections VALUES(1, 'mempalace_drawers');
            INSERT INTO documents VALUES('a', x'0000803f00000000', '{"wing":"project","room":"notes"}', 1);"#).unwrap();
        let before = std::fs::read(&path).unwrap();
        let index = VectorIndex::load_from_sqlite(&path, None).unwrap();
        let hits = index.query(&[1.0, 0.0], 1, Some("project")).unwrap();
        assert_eq!(hits[0].id, "a");
        assert_eq!(hits[0].room.as_deref(), Some("notes"));
        assert_eq!(std::fs::read(&path).unwrap(), before);
    }
}
