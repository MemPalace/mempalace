use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use mempalace_core::{MemPalaceError, VectorIndex};

fn to_py_err(err: MemPalaceError) -> PyErr {
    match err {
        MemPalaceError::Sqlite(e) => PyIOError::new_err(format!("SQLite error: {}", e)),
        MemPalaceError::FromSql(e) => PyValueError::new_err(format!("SQL conversion error: {}", e)),
        MemPalaceError::CollectionNotFound(s) => {
            PyValueError::new_err(format!("Collection '{}' not found", s))
        }
        MemPalaceError::DimensionMismatch { expected, actual } => PyValueError::new_err(
            format!("Dimension mismatch: expected {}, got {}", expected, actual),
        ),
        MemPalaceError::InvalidArgument(s) => PyValueError::new_err(s),
        MemPalaceError::Io(e) => PyIOError::new_err(format!("IO error: {}", e)),
    }
}

#[pyclass(name = "NativeVectorIndex")]
pub struct PyNativeVectorIndex {
    inner: VectorIndex,
}

#[pymethods]
impl PyNativeVectorIndex {
    #[staticmethod]
    #[pyo3(signature = (db_path, collection_name=None))]
    pub fn load_from_sqlite(
        py: Python<'_>,
        db_path: &str,
        collection_name: Option<&str>,
    ) -> PyResult<Self> {
        let index = py.allow_threads(|| {
            VectorIndex::load_from_sqlite(db_path, collection_name)
        }).map_err(to_py_err)?;
        Ok(Self { inner: index })
    }

    pub fn len(&self) -> usize {
        self.inner.len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    pub fn dim(&self) -> usize {
        self.inner.dim()
    }

    #[pyo3(signature = (query_embedding, k=10, filter_wing=None))]
    pub fn query(
        &self,
        py: Python<'_>,
        query_embedding: Vec<f32>,
        k: usize,
        filter_wing: Option<&str>,
    ) -> PyResult<Vec<(String, f32, f32, Option<String>, Option<String>)>> {
        let hits = py.allow_threads(|| {
            self.inner.query(&query_embedding, k, filter_wing)
        }).map_err(to_py_err)?;
        Ok(hits
            .into_iter()
            .map(|h| (h.id, h.distance, h.similarity, h.wing, h.room))
            .collect())
    }

    #[pyo3(signature = (query_embedding, k=10, filter_wing=None))]
    pub fn query_parallel(
        &self,
        py: Python<'_>,
        query_embedding: Vec<f32>,
        k: usize,
        filter_wing: Option<&str>,
    ) -> PyResult<Vec<(String, f32, f32, Option<String>, Option<String>)>> {
        let hits = py.allow_threads(|| {
            self.inner.query_parallel(&query_embedding, k, filter_wing)
        }).map_err(to_py_err)?;
        Ok(hits
            .into_iter()
            .map(|h| (h.id, h.distance, h.similarity, h.wing, h.room))
            .collect())
    }

    pub fn wing_counts(&self) -> HashMap<String, usize> {
        self.inner.wing_counts()
    }
}

#[pymodule]
fn mempalace_core_rs(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyNativeVectorIndex>()?;
    Ok(())
}
