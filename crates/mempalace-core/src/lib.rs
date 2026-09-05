use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::path::Path;
use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
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

pub struct VectorIndex {
    ids: Vec<String>,
    vectors: Vec<f32>,
    norms: Vec<f32>,
    dim: usize,
    wing_ids: Vec<u16>,
    wing_names: Vec<String>,
    wing_map: HashMap<String, u16>,
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

    fn intern_wing(&mut self, wing: Option<&str>) -> u16 {
        match wing {
            None => 0,
            Some(w) => {
                if let Some(&id) = self.wing_map.get(w) {
                    id
                } else {
                    let new_id = self.wing_names.len() as u16;
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

        let (collection_id, dim) = if let Some(col_name) = collection_name {
            let row: (i64, Option<i64>) = conn
                .query_row(
                    "SELECT id, dimension FROM collections WHERE name = ?1",
                    [col_name],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .map_err(|_| MemPalaceError::CollectionNotFound(col_name.to_string()))?;
            (Some(row.0), row.1.unwrap_or(384) as usize)
        } else {
            let dim: Option<i64> = conn
                .query_row("SELECT dimension FROM collections LIMIT 1", [], |r| r.get(0))
                .unwrap_or(Some(384));
            (None, dim.unwrap_or(384) as usize)
        };

        let mut index = Self::new(dim);

        let sql = if collection_id.is_some() {
            "SELECT id, embedding, wing, room FROM documents WHERE collection_id = ?1 ORDER BY rowid"
        } else {
            "SELECT id, embedding, wing, room FROM documents ORDER BY rowid"
        };

        let mut stmt = conn.prepare(sql)?;
        let expected_bytes = dim * std::mem::size_of::<f32>();

        let mut rows = if let Some(cid) = collection_id {
            stmt.query([cid])?
        } else {
            stmt.query([])?
        };

        while let Some(row) = rows.next()? {
            let id: String = row.get(0)?;
            let blob_ref = row.get_ref(1)?.as_blob()?;

            if blob_ref.len() != expected_bytes {
                continue;
            }

            let slice: &[f32] = unsafe {
                std::slice::from_raw_parts(blob_ref.as_ptr() as *const f32, dim)
            };

            let norm = l2_norm(slice);
            index.ids.push(id);
            index.vectors.extend_from_slice(slice);
            index.norms.push(norm);

            let wing_val: Option<String> = row.get(2).ok();
            let wid = index.intern_wing(wing_val.as_deref());
            index.wing_ids.push(wid);

            let room_val: Option<String> = row.get(3).ok();
            index.rooms.push(room_val);
        }

        Ok(index)
    }

    pub fn query(
        &self,
        query_vec: &[f32],
        k: usize,
        filter_wing: Option<&str>,
    ) -> Result<Vec<Hit>, MemPalaceError> {
        if query_vec.len() != self.dim {
            return Err(MemPalaceError::DimensionMismatch {
                expected: self.dim,
                actual: query_vec.len(),
            });
        }
        if self.ids.is_empty() || k == 0 {
            return Ok(Vec::new());
        }

        let filter_wid = match filter_wing {
            Some(w) => match self.wing_map.get(w) {
                Some(&id) => id,
                None => return Ok(Vec::new()), // filter matches nothing
            },
            None => 0,
        };

        let q_norm = l2_norm(query_vec);
        let mut heap: BinaryHeap<Candidate> = BinaryHeap::with_capacity(k + 1);

        let count = self.ids.len();
        let dim = self.dim;

        for i in 0..count {
            if filter_wid != 0 && self.wing_ids[i] != filter_wid {
                continue;
            }

            let vec_start = i * dim;
            let vec_slice = &self.vectors[vec_start..vec_start + dim];
            let dot = dot_product(vec_slice, query_vec);
            let denom = self.norms[i] * q_norm;
            let cos = if denom > 0.0 { (dot / denom).clamp(-1.0, 1.0) } else { 0.0 };
            let dist = 1.0 - cos;

            if heap.len() < k {
                heap.push(Candidate { id_idx: i, distance: dist });
            } else if dist < heap.peek().unwrap().distance {
                let mut top = heap.peek_mut().unwrap();
                top.id_idx = i;
                top.distance = dist;
            }
        }

        let mut candidates = heap.into_sorted_vec();
        let hits = candidates
            .drain(..)
            .map(|c| {
                let sim = (1.0 - c.distance).max(0.0);
                Hit {
                    id: self.ids[c.id_idx].clone(),
                    distance: c.distance,
                    similarity: sim,
                    wing: Some(self.wing_names[self.wing_ids[c.id_idx] as usize].clone())
                        .filter(|s| !s.is_empty()),
                    room: self.rooms[c.id_idx].clone(),
                }
            })
            .collect();

        Ok(hits)
    }

    pub fn query_parallel(
        &self,
        query_vec: &[f32],
        k: usize,
        filter_wing: Option<&str>,
    ) -> Result<Vec<Hit>, MemPalaceError> {
        if query_vec.len() != self.dim {
            return Err(MemPalaceError::DimensionMismatch {
                expected: self.dim,
                actual: query_vec.len(),
            });
        }
        if self.ids.len() < 10000 {
            // For smaller datasets, single-thread is faster than thread spawn overhead
            return self.query(query_vec, k, filter_wing);
        }

        let filter_wid = match filter_wing {
            Some(w) => match self.wing_map.get(w) {
                Some(&id) => id,
                None => return Ok(Vec::new()),
            },
            None => 0,
        };

        let q_norm = l2_norm(query_vec);
        let count = self.ids.len();
        let dim = self.dim;

        let num_chunks = rayon::current_num_threads().max(1);
        let chunk_size = (count + num_chunks - 1) / num_chunks;

        let chunk_heaps: Vec<BinaryHeap<Candidate>> = (0..num_chunks)
            .into_par_iter()
            .map(|chunk_idx| {
                let start = chunk_idx * chunk_size;
                let end = (start + chunk_size).min(count);
                let mut local_heap: BinaryHeap<Candidate> = BinaryHeap::with_capacity(k + 1);

                for i in start..end {
                    if filter_wid != 0 && self.wing_ids[i] != filter_wid {
                        continue;
                    }

                    let vec_start = i * dim;
                    let vec_slice = &self.vectors[vec_start..vec_start + dim];
                    let dot = dot_product(vec_slice, query_vec);
                    let denom = self.norms[i] * q_norm;
                    let cos = if denom > 0.0 { (dot / denom).clamp(-1.0, 1.0) } else { 0.0 };
                    let dist = 1.0 - cos;

                    if local_heap.len() < k {
                        local_heap.push(Candidate { id_idx: i, distance: dist });
                    } else if dist < local_heap.peek().unwrap().distance {
                        let mut top = local_heap.peek_mut().unwrap();
                        top.id_idx = i;
                        top.distance = dist;
                    }
                }
                local_heap
            })
            .collect();

        // Merge chunk heaps into global top-k heap
        let mut final_heap: BinaryHeap<Candidate> = BinaryHeap::with_capacity(k + 1);
        for local in chunk_heaps {
            for c in local {
                if final_heap.len() < k {
                    final_heap.push(c);
                } else if c.distance < final_heap.peek().unwrap().distance {
                    let mut top = final_heap.peek_mut().unwrap();
                    *top = c;
                }
            }
        }

        let hits = final_heap
            .into_sorted_vec()
            .into_iter()
            .map(|c| {
                let sim = (1.0 - c.distance).max(0.0);
                Hit {
                    id: self.ids[c.id_idx].clone(),
                    distance: c.distance,
                    similarity: sim,
                    wing: Some(self.wing_names[self.wing_ids[c.id_idx] as usize].clone())
                        .filter(|s| !s.is_empty()),
                    room: self.rooms[c.id_idx].clone(),
                }
            })
            .collect();

        Ok(hits)
    }

    pub fn wing_counts(&self) -> HashMap<String, usize> {
        let mut counts = HashMap::new();
        for &wid in &self.wing_ids {
            let name = &self.wing_names[wid as usize];
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
}
