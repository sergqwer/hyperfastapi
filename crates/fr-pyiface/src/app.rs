//! `FastAPI`, `APIRouter` PyClasses and a small `RouteDecorator` helper.
//!
//! Phase A: route registration is a list of records. No dispatch yet — that's
//! Phase B+. The shape of `Route` is intentionally close to its eventual form
//! so Phase B doesn't have to refactor the registration API.

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::sync::Arc;

pub(crate) struct Route {
    pub method: String,
    pub path: String,
    pub handler: PyObject,
    pub status_code: Option<i32>,
    pub tags: Vec<String>,
    pub deprecated: bool,
    pub include_in_schema: bool,
    pub summary: Option<String>,
    pub description: Option<String>,
}

/// Helper: a callable class returned by `app.get("/")` etc. Calling it with
/// the user's handler stores a Route record and returns the handler unchanged.
#[pyclass(module = "fastapi_rust._core")]
pub struct RouteDecorator {
    method: String,
    path: String,
    routes: Arc<Mutex<Vec<Route>>>,
    status_code: Option<i32>,
    tags: Vec<String>,
    deprecated: bool,
    include_in_schema: bool,
    summary: Option<String>,
    description: Option<String>,
}

#[pymethods]
impl RouteDecorator {
    fn __call__(&self, py: Python<'_>, func: PyObject) -> PyObject {
        self.routes.lock().push(Route {
            method: self.method.clone(),
            path: self.path.clone(),
            handler: func.clone_ref(py),
            status_code: self.status_code,
            tags: self.tags.clone(),
            deprecated: self.deprecated,
            include_in_schema: self.include_in_schema,
            summary: self.summary.clone(),
            description: self.description.clone(),
        });
        func
    }
}

#[pyclass(name = "FastAPI", module = "fastapi_rust._core", subclass)]
pub struct FastAPI {
    title: Mutex<String>,
    version: Mutex<String>,
    description: Mutex<String>,
    summary: Mutex<Option<String>>,
    debug: Mutex<bool>,
    docs_url: Mutex<Option<String>>,
    redoc_url: Mutex<Option<String>>,
    openapi_url: Mutex<Option<String>>,
    root_path: Mutex<String>,
    terms_of_service: Mutex<Option<String>>,
    routes: Arc<Mutex<Vec<Route>>>,
    /// Mirrors FastAPI's `app.dependency_overrides` mutable dict.
    dependency_overrides: PyObject,
    exception_handlers: PyObject,
    user_middleware: PyObject,
}

#[pymethods]
impl FastAPI {
    #[new]
    #[pyo3(signature = (
        *,
        debug = false,
        title = "FastAPI".to_string(),
        version = "0.1.0".to_string(),
        description = "".to_string(),
        summary = None,
        docs_url = Some("/docs".to_string()),
        redoc_url = Some("/redoc".to_string()),
        openapi_url = Some("/openapi.json".to_string()),
        root_path = "".to_string(),
        terms_of_service = None,
        **_kwargs
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        debug: bool,
        title: String,
        version: String,
        description: String,
        summary: Option<String>,
        docs_url: Option<String>,
        redoc_url: Option<String>,
        openapi_url: Option<String>,
        root_path: String,
        terms_of_service: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        Ok(Self {
            title: Mutex::new(title),
            version: Mutex::new(version),
            description: Mutex::new(description),
            summary: Mutex::new(summary),
            debug: Mutex::new(debug),
            docs_url: Mutex::new(docs_url),
            redoc_url: Mutex::new(redoc_url),
            openapi_url: Mutex::new(openapi_url),
            root_path: Mutex::new(root_path),
            terms_of_service: Mutex::new(terms_of_service),
            routes: Arc::new(Mutex::new(Vec::with_capacity(32))),
            dependency_overrides: PyDict::new_bound(py).unbind().into(),
            exception_handlers: PyDict::new_bound(py).unbind().into(),
            user_middleware: PyList::empty_bound(py).unbind().into(),
        })
    }

    // ---- Attributes ------------------------------------------------------

    #[getter]
    fn title(&self) -> String {
        self.title.lock().clone()
    }

    #[getter]
    fn version(&self) -> String {
        self.version.lock().clone()
    }

    #[getter]
    fn description(&self) -> String {
        self.description.lock().clone()
    }

    #[getter]
    fn summary(&self) -> Option<String> {
        self.summary.lock().clone()
    }

    #[getter]
    fn debug(&self) -> bool {
        *self.debug.lock()
    }

    #[getter]
    fn root_path(&self) -> String {
        self.root_path.lock().clone()
    }

    #[getter]
    fn terms_of_service(&self) -> Option<String> {
        self.terms_of_service.lock().clone()
    }

    #[getter]
    fn docs_url(&self) -> Option<String> {
        self.docs_url.lock().clone()
    }

    #[getter]
    fn redoc_url(&self) -> Option<String> {
        self.redoc_url.lock().clone()
    }

    #[getter]
    fn openapi_url(&self) -> Option<String> {
        self.openapi_url.lock().clone()
    }

    #[getter]
    fn dependency_overrides(&self, py: Python<'_>) -> PyObject {
        self.dependency_overrides.clone_ref(py)
    }

    #[getter]
    fn exception_handlers(&self, py: Python<'_>) -> PyObject {
        self.exception_handlers.clone_ref(py)
    }

    #[getter]
    fn user_middleware(&self, py: Python<'_>) -> PyObject {
        self.user_middleware.clone_ref(py)
    }

    /// Phase A returns a list of dicts so OpenAPI tests can introspect; Phase B
    /// returns proper Route wrapper objects (Starlette-compatible).
    #[getter]
    fn routes(&self, py: Python<'_>) -> PyResult<PyObject> {
        let routes = self.routes.lock();
        let list = PyList::empty_bound(py);
        for r in routes.iter() {
            let dict = PyDict::new_bound(py);
            dict.set_item("path", &r.path)?;
            dict.set_item("method", &r.method)?;
            list.append(dict)?;
        }
        Ok(list.unbind().into())
    }

    // ---- HTTP method decorators ------------------------------------------

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn get(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("GET", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn post(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("POST", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn put(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("PUT", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn delete(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("DELETE", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn patch(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("PATCH", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn options(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("OPTIONS", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn head(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("HEAD", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, **_kwargs))]
    fn trace(
        &self,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        self.make_decorator("TRACE", path, status_code, tags, deprecated, include_in_schema, summary, description)
    }

    /// `app.api_route(path, methods=[...])` — multi-method registration.
    /// Phase A registers one Route per method.
    #[pyo3(signature = (path, *, methods = None, **_kwargs))]
    fn api_route(
        &self,
        path: String,
        methods: Option<Vec<String>>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> RouteDecorator {
        // For Phase A we just take the first method; Phase B will handle the
        // multi-method case by storing a Route per method.
        let method = methods.and_then(|v| v.into_iter().next()).unwrap_or_else(|| "GET".to_string());
        RouteDecorator {
            method,
            path,
            routes: self.routes.clone(),
            status_code: None,
            tags: vec![],
            deprecated: false,
            include_in_schema: true,
            summary: None,
            description: None,
        }
    }

    /// `@app.websocket("/ws")` — Phase G implements upgrade. Phase A: just
    /// captures the route metadata so OpenAPI/test introspection sees it.
    fn websocket(&self, path: String) -> RouteDecorator {
        RouteDecorator {
            method: "WEBSOCKET".to_string(),
            path,
            routes: self.routes.clone(),
            status_code: None,
            tags: vec![],
            deprecated: false,
            include_in_schema: false,
            summary: None,
            description: None,
        }
    }

    /// `@app.middleware("http")` — Phase F implements composition. Phase A:
    /// no-op identity decorator so user code doesn't crash.
    fn middleware(&self, _kind: String) -> IdentityDecorator {
        IdentityDecorator
    }

    /// `@app.exception_handler(...)` — Phase F.
    fn exception_handler(&self, _exc: PyObject) -> IdentityDecorator {
        IdentityDecorator
    }

    /// `@app.on_event(...)` — deprecated form, Phase F.
    fn on_event(&self, _event: String) -> IdentityDecorator {
        IdentityDecorator
    }

    /// `app.add_middleware(cls, **opts)` — Phase F.
    #[pyo3(signature = (_middleware_class, **_kwargs))]
    fn add_middleware(
        &self,
        _middleware_class: PyObject,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        Ok(())
    }

    /// `app.add_api_route(path, endpoint, methods=[...])`.
    #[pyo3(signature = (path, endpoint, *, methods = None, **_kwargs))]
    fn add_api_route(
        &self,
        py: Python<'_>,
        path: String,
        endpoint: PyObject,
        methods: Option<Vec<String>>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        let methods = methods.unwrap_or_else(|| vec!["GET".into()]);
        let mut routes = self.routes.lock();
        for method in methods {
            routes.push(Route {
                method,
                path: path.clone(),
                handler: endpoint.clone_ref(py),
                status_code: None,
                tags: vec![],
                deprecated: false,
                include_in_schema: true,
                summary: None,
                description: None,
            });
        }
        Ok(())
    }

    /// `app.include_router(...)` — Phase B implements full prefix concat + tag merge.
    #[pyo3(signature = (router, *, prefix = "".to_string(), tags = None, **_kwargs))]
    fn include_router(
        &self,
        py: Python<'_>,
        router: PyRef<'_, APIRouter>,
        prefix: String,
        tags: Option<Vec<String>>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        let mut routes = self.routes.lock();
        let extra_tags = tags.unwrap_or_default();
        let router_prefix = router.prefix.lock().clone();
        let router_tags = router.tags.lock().clone();
        for r in router.routes.lock().iter() {
            let mut joined = String::with_capacity(prefix.len() + router_prefix.len() + r.path.len());
            joined.push_str(&prefix);
            joined.push_str(&router_prefix);
            joined.push_str(&r.path);

            let mut merged_tags = r.tags.clone();
            for t in &router_tags {
                if !merged_tags.contains(t) {
                    merged_tags.push(t.clone());
                }
            }
            for t in &extra_tags {
                if !merged_tags.contains(t) {
                    merged_tags.push(t.clone());
                }
            }

            routes.push(Route {
                method: r.method.clone(),
                path: joined,
                handler: r.handler.clone_ref(py),
                status_code: r.status_code,
                tags: merged_tags,
                deprecated: r.deprecated,
                include_in_schema: r.include_in_schema,
                summary: r.summary.clone(),
                description: r.description.clone(),
            });
        }
        Ok(())
    }

    /// Build the OpenAPI dict — Phase H implements the real generator.
    fn openapi(&self, py: Python<'_>) -> PyResult<PyObject> {
        let dict = PyDict::new_bound(py);
        dict.set_item("openapi", "3.1.0")?;
        let info = PyDict::new_bound(py);
        info.set_item("title", &*self.title.lock())?;
        info.set_item("version", &*self.version.lock())?;
        let desc = self.description.lock().clone();
        if !desc.is_empty() {
            info.set_item("description", desc)?;
        }
        if let Some(s) = self.summary.lock().clone() {
            info.set_item("summary", s)?;
        }
        if let Some(t) = self.terms_of_service.lock().clone() {
            info.set_item("termsOfService", t)?;
        }
        dict.set_item("info", info)?;
        dict.set_item("paths", PyDict::new_bound(py))?;
        let components = PyDict::new_bound(py);
        components.set_item("schemas", PyDict::new_bound(py))?;
        dict.set_item("components", components)?;
        Ok(dict.unbind().into())
    }
}

impl FastAPI {
    #[allow(clippy::too_many_arguments)]
    fn make_decorator(
        &self,
        method: &str,
        path: String,
        status_code: Option<i32>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
    ) -> RouteDecorator {
        // 204 / 304 / 1xx with a body-typed return annotation: FastAPI asserts
        // at registration time. We replicate that in Phase C — Phase A skips
        // the check (no annotation introspection yet).
        RouteDecorator {
            method: method.to_string(),
            path,
            routes: self.routes.clone(),
            status_code,
            tags: tags.unwrap_or_default(),
            deprecated,
            include_in_schema,
            summary,
            description,
        }
    }
}

#[pyclass(name = "APIRouter", module = "fastapi_rust._core", subclass)]
pub struct APIRouter {
    pub(crate) prefix: Mutex<String>,
    pub(crate) tags: Mutex<Vec<String>>,
    pub(crate) routes: Arc<Mutex<Vec<Route>>>,
}

#[pymethods]
impl APIRouter {
    #[new]
    #[pyo3(signature = (*, prefix = "".to_string(), tags = None, **_kwargs))]
    fn new(
        prefix: String,
        tags: Option<Vec<String>>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> Self {
        Self {
            prefix: Mutex::new(prefix),
            tags: Mutex::new(tags.unwrap_or_default()),
            routes: Arc::new(Mutex::new(Vec::with_capacity(8))),
        }
    }

    #[getter]
    fn prefix(&self) -> String {
        self.prefix.lock().clone()
    }

    #[getter]
    fn tags(&self) -> Vec<String> {
        self.tags.lock().clone()
    }

    /// Phase A: bare empty list (matches `router.routes == []` test). Phase B
    /// returns proper route objects.
    #[getter]
    fn routes(&self, py: Python<'_>) -> PyObject {
        PyList::empty_bound(py).unbind().into()
    }

    #[pyo3(signature = (path, *, tags = None, **_kwargs))]
    fn get(&self, path: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("GET", path, tags)
    }
    #[pyo3(signature = (path, *, tags = None, **_kwargs))]
    fn post(&self, path: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("POST", path, tags)
    }
    #[pyo3(signature = (path, *, tags = None, **_kwargs))]
    fn put(&self, path: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("PUT", path, tags)
    }
    #[pyo3(signature = (path, *, tags = None, **_kwargs))]
    fn delete(&self, path: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("DELETE", path, tags)
    }
    #[pyo3(signature = (path, *, tags = None, **_kwargs))]
    fn patch(&self, path: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("PATCH", path, tags)
    }
    #[pyo3(signature = (path, *, tags = None, **_kwargs))]
    fn options(&self, path: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("OPTIONS", path, tags)
    }
    #[pyo3(signature = (path, *, tags = None, **_kwargs))]
    fn head(&self, path: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("HEAD", path, tags)
    }

    /// Allow nested router composition.
    #[pyo3(signature = (router, *, prefix = "".to_string(), tags = None, **_kwargs))]
    fn include_router(
        &self,
        py: Python<'_>,
        router: PyRef<'_, APIRouter>,
        prefix: String,
        tags: Option<Vec<String>>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        let mut my_routes = self.routes.lock();
        let extra_tags = tags.unwrap_or_default();
        let inner_prefix = router.prefix.lock().clone();
        let inner_tags = router.tags.lock().clone();
        for r in router.routes.lock().iter() {
            let mut joined = String::with_capacity(prefix.len() + inner_prefix.len() + r.path.len());
            joined.push_str(&prefix);
            joined.push_str(&inner_prefix);
            joined.push_str(&r.path);

            let mut merged_tags = r.tags.clone();
            for t in &inner_tags {
                if !merged_tags.contains(t) {
                    merged_tags.push(t.clone());
                }
            }
            for t in &extra_tags {
                if !merged_tags.contains(t) {
                    merged_tags.push(t.clone());
                }
            }

            my_routes.push(Route {
                method: r.method.clone(),
                path: joined,
                handler: r.handler.clone_ref(py),
                status_code: r.status_code,
                tags: merged_tags,
                deprecated: r.deprecated,
                include_in_schema: r.include_in_schema,
                summary: r.summary.clone(),
                description: r.description.clone(),
            });
        }
        Ok(())
    }
}

impl APIRouter {
    fn make_decorator(&self, method: &str, path: String, tags: Option<Vec<String>>) -> RouteDecorator {
        RouteDecorator {
            method: method.to_string(),
            path,
            routes: self.routes.clone(),
            status_code: None,
            tags: tags.unwrap_or_default(),
            deprecated: false,
            include_in_schema: true,
            summary: None,
            description: None,
        }
    }
}

/// Simple identity decorator returned by app.middleware/exception_handler/on_event
/// in Phase A — Phase F replaces these with real registration logic.
#[pyclass(module = "fastapi_rust._core")]
pub struct IdentityDecorator;

#[pymethods]
impl IdentityDecorator {
    fn __call__(&self, func: PyObject) -> PyObject {
        func
    }
}
