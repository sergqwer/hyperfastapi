//! Param marker classes — Body, Query, Path, Header, Cookie, Form, File,
//! Depends, Security.
//!
//! In FastAPI, `from fastapi import Query` imports a function that returns a
//! `fastapi.params.Query` instance. Here we collapse the two: each marker IS
//! a class, and calling it constructs the instance directly. This keeps the
//! Python-visible API identical (`Query(default=None)` still works) while
//! avoiding an extra factory layer.
//!
//! Phase A: stores constructor args verbatim. Phase B reads them in the route
//! compiler to build the parameter extraction plan. Phase C/E adds full
//! validation logic for the new `pattern`/`gt`/`le`/...constraints.

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Common shape: every param marker stores the user's `default` plus any
/// remaining constraint kwargs. The route compiler consumes these later.
macro_rules! make_param {
    ($name:literal, $rust_name:ident) => {
        #[pyclass(name = $name, module = "hyperfastapi._core")]
        pub struct $rust_name {
            default: Mutex<Option<PyObject>>,
            kwargs: PyObject,
        }

        #[pymethods]
        impl $rust_name {
            #[new]
            #[pyo3(signature = (default = None, /, **kwargs))]
            fn new(
                py: Python<'_>,
                default: Option<PyObject>,
                kwargs: Option<&Bound<'_, PyDict>>,
            ) -> Self {
                let kw_obj = match kwargs {
                    Some(k) => k.clone().unbind().into(),
                    None => PyDict::new_bound(py).unbind().into(),
                };
                Self {
                    default: Mutex::new(default),
                    kwargs: kw_obj,
                }
            }

            #[getter]
            fn default(&self, py: Python<'_>) -> Option<PyObject> {
                self.default.lock().as_ref().map(|p| p.clone_ref(py))
            }

            #[getter]
            fn kwargs(&self, py: Python<'_>) -> PyObject {
                self.kwargs.clone_ref(py)
            }
        }
    };
}

make_param!("Body", Body);
make_param!("Query", Query);
make_param!("Path", Path);
make_param!("Header", Header);
make_param!("Cookie", Cookie);
make_param!("Form", Form);
make_param!("File", File);

/// `Depends(dependency, use_cache=True)` — captures the callable for the route
/// compiler to wire into the DI graph (Phase D).
#[pyclass(name = "Depends", module = "hyperfastapi._core")]
pub struct Depends {
    dependency: Mutex<Option<PyObject>>,
    use_cache: Mutex<bool>,
}

#[pymethods]
impl Depends {
    #[new]
    #[pyo3(signature = (dependency = None, *, use_cache = true))]
    fn new(dependency: Option<PyObject>, use_cache: bool) -> Self {
        Self {
            dependency: Mutex::new(dependency),
            use_cache: Mutex::new(use_cache),
        }
    }

    #[getter]
    fn dependency(&self, py: Python<'_>) -> Option<PyObject> {
        self.dependency.lock().as_ref().map(|p| p.clone_ref(py))
    }

    #[getter]
    fn use_cache(&self) -> bool {
        *self.use_cache.lock()
    }

    fn __repr__(&self) -> String {
        let has = self.dependency.lock().is_some();
        format!("Depends({})", if has { "<dependency>" } else { "None" })
    }
}

/// `Security(dependency, scopes=None, use_cache=True)` — extends Depends with
/// OAuth2 scopes. Phase E uses scopes for the OpenAPI security entries.
#[pyclass(name = "Security", module = "hyperfastapi._core")]
pub struct Security {
    dependency: Mutex<Option<PyObject>>,
    scopes: Mutex<Vec<String>>,
    use_cache: Mutex<bool>,
}

#[pymethods]
impl Security {
    #[new]
    #[pyo3(signature = (dependency = None, *, scopes = None, use_cache = true))]
    fn new(dependency: Option<PyObject>, scopes: Option<Vec<String>>, use_cache: bool) -> Self {
        Self {
            dependency: Mutex::new(dependency),
            scopes: Mutex::new(scopes.unwrap_or_default()),
            use_cache: Mutex::new(use_cache),
        }
    }

    #[getter]
    fn dependency(&self, py: Python<'_>) -> Option<PyObject> {
        self.dependency.lock().as_ref().map(|p| p.clone_ref(py))
    }

    #[getter]
    fn scopes(&self) -> Vec<String> {
        self.scopes.lock().clone()
    }

    #[getter]
    fn use_cache(&self) -> bool {
        *self.use_cache.lock()
    }
}
