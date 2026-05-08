//! PyO3 module entry — exposes the Rust core to Python as `fastapi_rust._core`.
//!
//! Phase A scope: FastAPI/APIRouter PyClasses that store route metadata, and
//! the FastAPI-specific param markers (Body/Query/Path/Header/Cookie/Form/File/
//! Depends/Security). Everything else (Request/Response/WebSocket/HTTPException/
//! status/...) is re-exported from Starlette in the Python compat layer —
//! Starlette already provides those types and FastAPI itself merely re-exports
//! them. Phase B-G replaces the simple records with the real dispatch logic.

use pyo3::prelude::*;

mod app;
mod params;

#[pymodule]
fn _core(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Initialize CPU feature detection once at module load so the SIMD
    // dispatch flags are set before any request hits Phase C+ code paths.
    fr_json::init();

    m.add("__version__", env!("CARGO_PKG_VERSION"))?;

    // ---- Application + router --------------------------------------------
    m.add_class::<app::FastAPI>()?;
    m.add_class::<app::APIRouter>()?;
    m.add_class::<app::RouteDecorator>()?;
    m.add_class::<app::ApiRouteDecorator>()?;
    m.add_class::<app::IdentityDecorator>()?;

    // ---- Param marker classes --------------------------------------------
    // Each is callable (it's a class), so `from fastapi_rust import Query;
    // Query(default=None)` returns an instance — matching FastAPI's behavior
    // where `fastapi.Query` is a function that returns `fastapi.params.Query`.
    m.add_class::<params::Body>()?;
    m.add_class::<params::Query>()?;
    m.add_class::<params::Path>()?;
    m.add_class::<params::Header>()?;
    m.add_class::<params::Cookie>()?;
    m.add_class::<params::Form>()?;
    m.add_class::<params::File>()?;
    m.add_class::<params::Depends>()?;
    m.add_class::<params::Security>()?;

    Ok(())
}
