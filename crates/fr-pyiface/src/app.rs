//! `FastAPI`, `APIRouter` PyClasses, the `RouteDecorator` helper, and the
//! Phase B-1 dispatch implementation.
//!
//! Phase B-1 scope: linear-search router (O(n) — fine for hundreds of routes;
//! Phase J swaps to matchit). Routes are matched by exact path + method, with
//! HEAD falling back to GET when there's no explicit HEAD handler. Responses
//! are JSON-serialized via Python `json.dumps` + `jsonable_encoder` (Phase J
//! moves to Rust serde_json with preserve_order). 404 / 405 are emitted with
//! FastAPI's `{"detail": ...}` format.

use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::sync::Arc;

pub(crate) struct Route {
    pub method: String,
    pub path: String,
    pub handler: PyObject,
    pub status_code: Option<i32>,
    pub response_model: Option<PyObject>,
    pub tags: Vec<String>,
    pub deprecated: bool,
    pub include_in_schema: bool,
    pub summary: Option<String>,
    pub description: Option<String>,
    /// One entry per handler param the dispatch should extract; each entry
    /// is a Python dict of `{name, source, type, default, alias, required,
    /// validators, convert_underscores}` produced by
    /// `fastapi_rust._routing.compile_route_plan` at decorator time.
    pub param_plan: Vec<PyObject>,
    /// Phase C: handler return value gets wrapped in this Response subclass
    /// (HTMLResponse / PlainTextResponse / JSONResponse / ...). None means
    /// "fall back to plain JSON serialization".
    pub response_class: Option<PyObject>,
    /// Phase C: response_model_* dump kwargs.
    pub response_model_exclude_unset: bool,
    pub response_model_exclude_none: bool,
    pub response_model_exclude_defaults: bool,
    pub response_model_by_alias: bool,
    pub response_model_include: Option<PyObject>,
    pub response_model_exclude: Option<PyObject>,
    /// Route-level `dependencies=[Depends(...)]` — list of marker instances.
    pub dependencies: Vec<PyObject>,
    /// Phase E: list of `(scheme_name, scopes, model_dict)` triples discovered
    /// by walking the handler plan + route-level deps. Each model is the
    /// scheme's OpenAPI dict (`type`/`scheme`/`flows`/...). Drives
    /// `op["security"]` and `components.securitySchemes`.
    pub security: Vec<SecurityEntry>,
}

pub(crate) struct SecurityEntry {
    pub scheme_name: String,
    pub scopes: Vec<String>,
    pub model: PyObject,
}

impl SecurityEntry {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            scheme_name: self.scheme_name.clone(),
            scopes: self.scopes.clone(),
            model: self.model.clone_ref(py),
        }
    }
}

/// Callable class returned by `app.get("/")` etc. Calling it with the user's
/// handler stores a Route record and returns the handler unchanged.
#[pyclass(module = "fastapi_rust._core")]
pub struct RouteDecorator {
    method: String,
    path: String,
    routes: Arc<Mutex<Vec<Route>>>,
    status_code: Option<i32>,
    response_model: Option<PyObject>,
    tags: Vec<String>,
    deprecated: bool,
    include_in_schema: bool,
    summary: Option<String>,
    description: Option<String>,
    response_class: Option<PyObject>,
    response_model_exclude_unset: bool,
    response_model_exclude_none: bool,
    response_model_exclude_defaults: bool,
    response_model_by_alias: bool,
    response_model_include: Option<PyObject>,
    response_model_exclude: Option<PyObject>,
    dependencies: Vec<PyObject>,
}

#[pymethods]
impl RouteDecorator {
    fn __call__(&self, py: Python<'_>, func: PyObject) -> PyResult<PyObject> {
        let plan = compile_route_plan(py, &func, &self.path).unwrap_or_default();
        let security = extract_route_security(py, &plan, &self.dependencies).unwrap_or_default();
        self.routes.lock().push(Route {
            method: self.method.clone(),
            path: self.path.clone(),
            handler: func.clone_ref(py),
            status_code: self.status_code,
            response_model: self.response_model.as_ref().map(|p| p.clone_ref(py)),
            tags: self.tags.clone(),
            deprecated: self.deprecated,
            include_in_schema: self.include_in_schema,
            summary: self.summary.clone(),
            description: self.description.clone(),
            param_plan: plan,
            response_class: self.response_class.as_ref().map(|p| p.clone_ref(py)),
            response_model_exclude_unset: self.response_model_exclude_unset,
            response_model_exclude_none: self.response_model_exclude_none,
            response_model_exclude_defaults: self.response_model_exclude_defaults,
            response_model_by_alias: self.response_model_by_alias,
            response_model_include: self.response_model_include.as_ref().map(|p| p.clone_ref(py)),
            response_model_exclude: self.response_model_exclude.as_ref().map(|p| p.clone_ref(py)),
            dependencies: self.dependencies.iter().map(|p| p.clone_ref(py)).collect(),
            security,
        });
        Ok(func)
    }
}

/// Phase E: extract per-route security info by calling
/// `fastapi_rust._routing.extract_security_info(plan, route_deps)`. Returns
/// list of `SecurityEntry` triples (scheme_name, scopes, model_dict).
fn extract_route_security(
    py: Python<'_>,
    plan: &[PyObject],
    deps: &[PyObject],
) -> PyResult<Vec<SecurityEntry>> {
    let routing = py.import_bound("fastapi_rust._routing")?;
    let func = routing.getattr("extract_security_info")?;
    let plan_list = pyo3::types::PyList::empty_bound(py);
    for p in plan {
        plan_list.append(p.clone_ref(py))?;
    }
    let deps_list = pyo3::types::PyList::empty_bound(py);
    for d in deps {
        deps_list.append(d.clone_ref(py))?;
    }
    let result = func.call1((plan_list, deps_list))?;
    let list = result.downcast_into::<pyo3::types::PyList>()?;
    let mut out = Vec::with_capacity(list.len());
    for item in list.iter() {
        let dict = item.downcast::<PyDict>()?;
        let name: String = dict.get_item("scheme_name")?.unwrap().extract()?;
        let scopes_obj = dict.get_item("scopes")?.unwrap();
        let scopes: Vec<String> = scopes_obj.extract()?;
        let model = dict.get_item("model")?.unwrap().unbind().into();
        out.push(SecurityEntry { scheme_name: name, scopes, model });
    }
    Ok(out)
}

/// Inspect handler's Python signature via `fastapi_rust._routing.compile_route_plan`.
/// Each entry is a Python dict; we keep it as PyObject and extract fields at
/// dispatch time (Phase J can pre-parse if profiling shows it matters).
fn compile_route_plan(
    py: Python<'_>,
    handler: &PyObject,
    path: &str,
) -> PyResult<Vec<PyObject>> {
    let routing_mod = py.import_bound("fastapi_rust._routing")?;
    let compiler = routing_mod.getattr("compile_route_plan")?;
    let result = compiler.call1((handler.clone_ref(py), path))?;
    let list = result.downcast_into::<pyo3::types::PyList>()?;
    let mut out = Vec::with_capacity(list.len());
    for item in list.iter() {
        out.push(item.unbind().into());
    }
    Ok(out)
}

/// Helper to clone a Vec<PyObject> by cloning each reference. Used in
/// include_router etc. — Vec<PyObject> doesn't impl Clone since PyObject doesn't.
fn clone_param_plan(py: Python<'_>, plan: &[PyObject]) -> Vec<PyObject> {
    plan.iter().map(|p| p.clone_ref(py)).collect()
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
    dependency_overrides: PyObject,
    exception_handlers: PyObject,
    user_middleware: PyObject,
    default_response_class: Mutex<Option<PyObject>>,
    /// App-level `dependencies=[Depends(...)]` — applied to every route.
    dependencies: Mutex<Vec<PyObject>>,
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
        default_response_class = None,
        dependencies = None,
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
        default_response_class: Option<PyObject>,
        dependencies: Option<Vec<PyObject>>,
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
            default_response_class: Mutex::new(default_response_class),
            dependencies: Mutex::new(dependencies.unwrap_or_default()),
        })
    }

    // ---- Attributes ------------------------------------------------------

    #[getter]
    fn title(&self) -> String { self.title.lock().clone() }
    #[getter]
    fn version(&self) -> String { self.version.lock().clone() }
    #[getter]
    fn description(&self) -> String { self.description.lock().clone() }
    #[getter]
    fn summary(&self) -> Option<String> { self.summary.lock().clone() }
    #[getter]
    fn debug(&self) -> bool { *self.debug.lock() }
    #[getter]
    fn root_path(&self) -> String { self.root_path.lock().clone() }
    #[getter]
    fn terms_of_service(&self) -> Option<String> { self.terms_of_service.lock().clone() }
    #[getter]
    fn docs_url(&self) -> Option<String> { self.docs_url.lock().clone() }
    #[getter]
    fn redoc_url(&self) -> Option<String> { self.redoc_url.lock().clone() }
    #[getter]
    fn openapi_url(&self) -> Option<String> { self.openapi_url.lock().clone() }

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

    /// Phase B returns dict-shaped routes for OpenAPI introspection. Phase H
    /// will return Starlette-compatible Route objects.
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

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn get(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "GET", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn post(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "POST", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn put(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "PUT", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn delete(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "DELETE", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn patch(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "PATCH", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn options(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "OPTIONS", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn head(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "HEAD", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn trace(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "TRACE", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    /// Multi-method decorator. Phase B-1: registers a Route per method.
    #[pyo3(signature = (path, *, methods = None, **_kwargs))]
    fn api_route(&self, path: String, methods: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> ApiRouteDecorator {
        let methods = methods.unwrap_or_else(|| vec!["GET".into()]);
        ApiRouteDecorator {
            methods,
            path,
            routes: self.routes.clone(),
        }
    }

    fn websocket(&self, path: String) -> RouteDecorator {
        RouteDecorator {
            method: "WEBSOCKET".to_string(),
            path,
            routes: self.routes.clone(),
            status_code: None,
            response_model: None,
            tags: vec![],
            deprecated: false,
            include_in_schema: false,
            summary: None,
            description: None,
            response_class: None,
            response_model_exclude_unset: false,
            response_model_exclude_none: false,
            response_model_exclude_defaults: false,
            response_model_by_alias: true,
            response_model_include: None,
            response_model_exclude: None,
            dependencies: vec![],
        }
    }

    /// Phase G: look up a registered WebSocket handler for the given path.
    /// Returns the handler PyObject if a matching route exists, else None.
    fn _lookup_websocket(&self, py: Python<'_>, path: String) -> Option<PyObject> {
        for r in self.routes.lock().iter() {
            if r.method == "WEBSOCKET" && r.path == path {
                return Some(r.handler.clone_ref(py));
            }
        }
        None
    }

    /// Phase H: emit one dict per registered HTTP route with everything the
    /// Python OpenAPI builder needs (method, path, plan, response_model,
    /// security entries, deprecated/tags/summary/description, status_code,
    /// include_in_schema). WebSocket routes are skipped — Phase H doesn't
    /// surface them in OpenAPI.
    fn _routes_summary(&self, py: Python<'_>) -> PyResult<PyObject> {
        let list = pyo3::types::PyList::empty_bound(py);
        for r in self.routes.lock().iter() {
            if r.method == "WEBSOCKET" { continue; }
            let d = PyDict::new_bound(py);
            d.set_item("method", &r.method)?;
            d.set_item("path", &r.path)?;
            d.set_item("status_code", r.status_code)?;
            d.set_item("response_model", r.response_model.as_ref().map(|p| p.clone_ref(py)))?;
            d.set_item("response_class", r.response_class.as_ref().map(|p| p.clone_ref(py)))?;
            d.set_item("tags", r.tags.clone())?;
            d.set_item("deprecated", r.deprecated)?;
            d.set_item("include_in_schema", r.include_in_schema)?;
            d.set_item("summary", r.summary.clone())?;
            d.set_item("description", r.description.clone())?;
            d.set_item("response_model_exclude_unset", r.response_model_exclude_unset)?;
            d.set_item("response_model_exclude_none", r.response_model_exclude_none)?;
            d.set_item("response_model_exclude_defaults", r.response_model_exclude_defaults)?;
            d.set_item("response_model_by_alias", r.response_model_by_alias)?;
            // Plan: list of plan dicts.
            let plan_list = pyo3::types::PyList::empty_bound(py);
            for p in &r.param_plan { plan_list.append(p.bind(py))?; }
            d.set_item("param_plan", plan_list)?;
            // Security: list of dicts {scheme_name, scopes, model}.
            let sec_list = pyo3::types::PyList::empty_bound(py);
            for s in &r.security {
                let sd = PyDict::new_bound(py);
                sd.set_item("scheme_name", &s.scheme_name)?;
                sd.set_item("scopes", s.scopes.clone())?;
                sd.set_item("model", s.model.clone_ref(py))?;
                sec_list.append(sd)?;
            }
            d.set_item("security", sec_list)?;
            list.append(d)?;
        }
        Ok(list.unbind().into())
    }

    /// Phase B-2 extension: alternative `_dispatch` is now in module-level dispatch
    /// but we keep the route-level fields in sync — see below.

    fn middleware(&self, _kind: String) -> IdentityDecorator { IdentityDecorator }
    fn exception_handler(&self, _exc: PyObject) -> IdentityDecorator { IdentityDecorator }
    fn on_event(&self, _event: String) -> IdentityDecorator { IdentityDecorator }

    #[pyo3(signature = (_middleware_class, **_kwargs))]
    fn add_middleware(&self, _middleware_class: PyObject, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        Ok(())
    }

    #[pyo3(signature = (path, endpoint, *, methods = None, response_class = None, **_kwargs))]
    fn add_api_route(&self, py: Python<'_>, path: String, endpoint: PyObject, methods: Option<Vec<String>>, response_class: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        let methods = methods.unwrap_or_else(|| vec!["GET".into()]);
        let plan = compile_route_plan(py, &endpoint, &path).unwrap_or_default();
        let mut routes = self.routes.lock();
        for method in methods {
            routes.push(Route {
                method,
                path: path.clone(),
                handler: endpoint.clone_ref(py),
                status_code: None,
                response_model: None,
                tags: vec![],
                deprecated: false,
                include_in_schema: true,
                summary: None,
                description: None,
                param_plan: clone_param_plan(py, &plan),
                response_class: response_class.as_ref().map(|p| p.clone_ref(py)),
                response_model_exclude_unset: false,
                response_model_exclude_none: false,
                response_model_exclude_defaults: false,
                response_model_by_alias: true,
                response_model_include: None,
                response_model_exclude: None, dependencies: vec![], security: vec![],
            });
        }
        Ok(())
    }

    #[pyo3(signature = (router, *, prefix = "".to_string(), tags = None, include_in_schema = true, default_response_class = None, **_kwargs))]
    fn include_router(&self, py: Python<'_>, router: PyRef<'_, APIRouter>, prefix: String, tags: Option<Vec<String>>, include_in_schema: bool, default_response_class: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        let mut routes = self.routes.lock();
        let extra_tags = tags.unwrap_or_default();
        let router_prefix = router.prefix.lock().clone();
        let router_tags = router.tags.lock().clone();
        // Effective default_response_class for routes from this include:
        // explicit kwarg > router's default > app's default > None.
        let router_default_rc = router.default_response_class.lock().as_ref().map(|p| p.clone_ref(py));
        let effective_default_rc = default_response_class
            .or(router_default_rc);
        for r in router.routes.lock().iter() {
            let mut joined = String::with_capacity(prefix.len() + router_prefix.len() + r.path.len());
            joined.push_str(&prefix);
            joined.push_str(&router_prefix);
            joined.push_str(&r.path);

            let mut merged_tags = r.tags.clone();
            for t in &router_tags {
                if !merged_tags.contains(t) { merged_tags.push(t.clone()); }
            }
            for t in &extra_tags {
                if !merged_tags.contains(t) { merged_tags.push(t.clone()); }
            }

            // If route doesn't have its own response_class, fall back to the
            // include_router default (which may be the router's default).
            let route_rc = r
                .response_class
                .as_ref()
                .map(|p| p.clone_ref(py))
                .or_else(|| effective_default_rc.as_ref().map(|p| p.clone_ref(py)));
            routes.push(Route {
                method: r.method.clone(),
                path: joined,
                handler: r.handler.clone_ref(py),
                status_code: r.status_code,
                response_model: r.response_model.as_ref().map(|p| p.clone_ref(py)),
                tags: merged_tags,
                deprecated: r.deprecated,
                // include_router-level include_in_schema=False acts as a hard
                // override: every route from the router is hidden from OpenAPI.
                include_in_schema: r.include_in_schema && include_in_schema,
                summary: r.summary.clone(),
                description: r.description.clone(),
                param_plan: clone_param_plan(py, &r.param_plan),
                response_class: route_rc,
                response_model_exclude_unset: r.response_model_exclude_unset,
                response_model_exclude_none: r.response_model_exclude_none,
                response_model_exclude_defaults: r.response_model_exclude_defaults,
                response_model_by_alias: r.response_model_by_alias,
                response_model_include: r.response_model_include.as_ref().map(|p| p.clone_ref(py)),
                response_model_exclude: r.response_model_exclude.as_ref().map(|p| p.clone_ref(py)), dependencies: r.dependencies.iter().map(|p| p.clone_ref(py)).collect(),
                security: r.security.iter().map(|s| s.clone_ref(py)).collect(),
            });
        }
        Ok(())
    }

    fn openapi(&self, py: Python<'_>) -> PyResult<PyObject> {
        let dict = PyDict::new_bound(py);
        dict.set_item("openapi", "3.1.0")?;
        let info = PyDict::new_bound(py);
        info.set_item("title", &*self.title.lock())?;
        info.set_item("version", &*self.version.lock())?;
        let desc = self.description.lock().clone();
        if !desc.is_empty() { info.set_item("description", desc)?; }
        if let Some(s) = self.summary.lock().clone() { info.set_item("summary", s)?; }
        if let Some(t) = self.terms_of_service.lock().clone() { info.set_item("termsOfService", t)?; }
        dict.set_item("info", info)?;
        // Phase E: aggregate security schemes across all routes.
        let security_schemes = PyDict::new_bound(py);
        // Build paths from registered routes (Phase H expands this).
        let paths_dict = PyDict::new_bound(py);
        for r in self.routes.lock().iter() {
            if !r.include_in_schema || r.method == "WEBSOCKET" { continue; }
            // Register every scheme this route depends on into the global
            // securitySchemes dict (first occurrence wins).
            for s in &r.security {
                if security_schemes.get_item(&s.scheme_name)?.is_none() {
                    security_schemes.set_item(&s.scheme_name, s.model.clone_ref(py))?;
                }
            }
            let path_item = paths_dict
                .get_item(&r.path)?
                .map(|v| v.downcast_into::<PyDict>().ok())
                .flatten()
                .unwrap_or_else(|| PyDict::new_bound(py));
            let op = PyDict::new_bound(py);
            if !r.tags.is_empty() {
                op.set_item("tags", r.tags.clone())?;
            }
            if r.deprecated {
                op.set_item("deprecated", true)?;
            }
            if let Some(s) = &r.summary { op.set_item("summary", s)?; }
            if let Some(d) = &r.description { op.set_item("description", d)?; }
            // Phase H fills in real responses/parameters/requestBody schemas.
            let responses = PyDict::new_bound(py);
            let status_str = r.status_code.unwrap_or(200).to_string();
            let resp = PyDict::new_bound(py);
            resp.set_item("description", "Successful Response")?;
            responses.set_item(status_str, resp)?;
            op.set_item("responses", responses)?;
            // Phase E: per-route security array — list of {scheme_name: scopes}.
            if !r.security.is_empty() {
                let security_list = pyo3::types::PyList::empty_bound(py);
                for s in &r.security {
                    let entry = PyDict::new_bound(py);
                    entry.set_item(&s.scheme_name, s.scopes.clone())?;
                    security_list.append(entry)?;
                }
                op.set_item("security", security_list)?;
            }
            path_item.set_item(r.method.to_lowercase(), op)?;
            paths_dict.set_item(&r.path, path_item)?;
        }
        dict.set_item("paths", paths_dict)?;
        let components = PyDict::new_bound(py);
        components.set_item("schemas", PyDict::new_bound(py))?;
        if !security_schemes.is_empty() {
            components.set_item("securitySchemes", security_schemes)?;
        }
        dict.set_item("components", components)?;
        Ok(dict.unbind().into())
    }

    /// Phase B-3 + Phase C-1 dispatch. Linear-search routes by template, extract
    /// params from path/query/header/cookie/body based on the compiled plan,
    /// cast types, validate, call handler, serialize response.
    ///
    /// `query_string`: raw bytes-as-str (we URL-decode via `urllib.parse.parse_qs`).
    /// `headers`: list of (name, value) tuples from ASGI scope.
    /// `body`: bytes of the request body (Pydantic models go straight through
    /// `__pydantic_validator__.validate_json`).
    #[pyo3(signature = (method, path, query_string = None, headers = None, body = None))]
    fn _dispatch(
        &self,
        py: Python<'_>,
        method: String,
        path: String,
        query_string: Option<String>,
        headers: Option<Vec<(String, String)>>,
        body: Option<&Bound<'_, PyBytes>>,
    ) -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {

        // Auto-serve OpenAPI schema at the configured `openapi_url`. Phase H
        // will replace this with a properly-cached pre-built schema.
        let openapi_url = self.openapi_url.lock().clone();
        if let Some(ref url) = openapi_url {
            if path == *url && (method == "GET" || method == "HEAD") {
                let schema = self.openapi(py)?;
                let json_bytes = serialize_value_to_json(py, &schema)?;
                let body = if method == "HEAD" { Vec::new() } else { json_bytes };
                return Ok((
                    200,
                    vec![("content-type".into(), "application/json".into())],
                    body,
                ));
            }
        }
        // Auto-serve Swagger UI HTML at docs_url; Phase H wires the real CDN page.
        let docs_url = self.docs_url.lock().clone();
        if let Some(ref url) = docs_url {
            if path == *url && (method == "GET" || method == "HEAD") {
                let title = self.title.lock().clone();
                let html = build_docs_html(&title, openapi_url.as_deref().unwrap_or("/openapi.json"));
                let body = if method == "HEAD" { Vec::new() } else { html.into_bytes() };
                return Ok((
                    200,
                    vec![("content-type".into(), "text/html; charset=utf-8".into())],
                    body,
                ));
            }
        }
        let redoc_url = self.redoc_url.lock().clone();
        if let Some(ref url) = redoc_url {
            if path == *url && (method == "GET" || method == "HEAD") {
                let title = self.title.lock().clone();
                let html = build_redoc_html(&title, openapi_url.as_deref().unwrap_or("/openapi.json"));
                let body = if method == "HEAD" { Vec::new() } else { html.into_bytes() };
                return Ok((
                    200,
                    vec![("content-type".into(), "text/html; charset=utf-8".into())],
                    body,
                ));
            }
        }

        // Find a matching route via path-template matching.
        // Two-pass: exact method first, then HEAD→GET fallback.
        let (handler, status_code, response_model, param_plan, path_params,
             response_class, rm_exclude_unset, rm_exclude_none,
             rm_exclude_defaults, rm_by_alias, rm_include, rm_exclude,
             route_deps_markers);
        {
            let routes = self.routes.lock();
            let mut path_exists = false;
            let mut method_match: Option<(usize, Vec<(String, String)>)> = None;

            for (i, r) in routes.iter().enumerate() {
                if let Some(params) = match_path_template(&r.path, &path) {
                    path_exists = true;
                    if r.method == method {
                        method_match = Some((i, params));
                        break;
                    }
                }
            }
            if method_match.is_none() && method == "HEAD" {
                for (i, r) in routes.iter().enumerate() {
                    if r.method == "GET" {
                        if let Some(params) = match_path_template(&r.path, &path) {
                            method_match = Some((i, params));
                            break;
                        }
                    }
                }
            }

            match method_match {
                Some((i, params)) => {
                    let r = &routes[i];
                    handler = r.handler.clone_ref(py);
                    status_code = r.status_code.unwrap_or(200) as u16;
                    response_model = r.response_model.as_ref().map(|p| p.clone_ref(py));
                    param_plan = clone_param_plan(py, &r.param_plan);
                    path_params = params;
                    response_class = r
                        .response_class
                        .as_ref()
                        .map(|p| p.clone_ref(py))
                        .or_else(|| self.default_response_class.lock().as_ref().map(|p| p.clone_ref(py)));
                    rm_exclude_unset = r.response_model_exclude_unset;
                    rm_exclude_none = r.response_model_exclude_none;
                    rm_exclude_defaults = r.response_model_exclude_defaults;
                    rm_by_alias = r.response_model_by_alias;
                    rm_include = r.response_model_include.as_ref().map(|p| p.clone_ref(py));
                    rm_exclude = r.response_model_exclude.as_ref().map(|p| p.clone_ref(py));
                    route_deps_markers = r.dependencies.iter().map(|p| p.clone_ref(py)).collect::<Vec<_>>();
                }
                None => {
                    drop(routes);
                    if path_exists {
                        return Ok((
                            405,
                            vec![("content-type".into(), "application/json".into())],
                            br#"{"detail":"Method Not Allowed"}"#.to_vec(),
                        ));
                    }
                    return Ok((
                        404,
                        vec![("content-type".into(), "application/json".into())],
                        br#"{"detail":"Not Found"}"#.to_vec(),
                    ));
                }
            }
        }

        // ---- Build dispatch contexts (parsed once per request) ----------
        // Query: parsed via urllib.parse.parse_qs to handle URL-decoding +
        // multi-values correctly. Returns dict[str, list[str]].
        let query_dict: Bound<'_, PyDict> = if let Some(qs) = query_string.as_deref() {
            if qs.is_empty() {
                PyDict::new_bound(py)
            } else {
                let urllib = py.import_bound("urllib.parse")?;
                let parse_qs = urllib.getattr("parse_qs")?;
                let kw = PyDict::new_bound(py);
                kw.set_item("keep_blank_values", true)?;
                let parsed = parse_qs.call((qs,), Some(&kw))?;
                parsed.downcast_into::<PyDict>().map_err(|e| pyo3::PyErr::from(e))?
            }
        } else {
            PyDict::new_bound(py)
        };

        // Headers: case-insensitive lookup map (lowercase → list of values).
        // Multiple headers with the same name (X-Tag: a; X-Tag: b arriving
        // as two separate header tuples) collected into a list — required
        // for `Annotated[list[str], Header()]` params.
        let header_lookup: Bound<'_, PyDict> = PyDict::new_bound(py);
        let header_list: Vec<(String, String)> = headers.unwrap_or_default();
        for (k, v) in &header_list {
            let key = k.to_ascii_lowercase();
            match header_lookup.get_item(&key)? {
                Some(existing) => {
                    existing.call_method1("append", (v,))?;
                }
                None => {
                    let lst = pyo3::types::PyList::empty_bound(py);
                    lst.append(v)?;
                    header_lookup.set_item(key, lst)?;
                }
            }
        }

        // Cookies: parse the Cookie header(s) into name → value map.
        let cookie_dict: Bound<'_, PyDict> = PyDict::new_bound(py);
        if let Some(cookie_list) = header_lookup.get_item("cookie")? {
            if let Ok(values) = cookie_list.extract::<Vec<String>>() {
                for s in values {
                    for chunk in s.split(';') {
                        let chunk = chunk.trim();
                        if let Some((k, v)) = chunk.split_once('=') {
                            cookie_dict.set_item(k.trim(), v.trim())?;
                        }
                    }
                }
            }
        }

        // ---- Form / multipart parsing (lazy: only if any form/file param) -
        // We grab Content-Type from header_lookup so we can dispatch between
        // urlencoded and multipart in Python.
        // Phase E: a class-as-Depends (e.g. OAuth2PasswordRequestForm) places
        // its Form fields inside the dep_plan, so we must walk recursively.
        fn plan_has_form(py: Python<'_>, plan_obj: &PyObject) -> bool {
            let dict = match plan_obj.bind(py).downcast::<PyDict>() {
                Ok(d) => d.clone(),
                Err(_) => return false,
            };
            let s: String = dict.get_item("source").ok().flatten()
                .and_then(|v| v.extract().ok()).unwrap_or_default();
            if s == "form" || s == "file" { return true; }
            if s == "depends" || s == "security" {
                if let Ok(Some(sub)) = dict.get_item("dep_plan") {
                    if let Ok(list) = sub.downcast::<pyo3::types::PyList>() {
                        for item in list.iter() {
                            let obj: PyObject = item.unbind().into();
                            if plan_has_form(py, &obj) { return true; }
                        }
                    }
                }
            }
            false
        }
        let route_deps_have_form = route_deps_markers.iter().any(|m| {
            // Route-level Depends(): peek at the marker's dependency callable's
            // signature for Form params. Cheaper to just always parse on a
            // form-shaped Content-Type — but here we keep behaviour scoped.
            let _ = m; // markers are opaque from Rust; default to false.
            false
        });
        let _ = route_deps_have_form;
        let needs_form = param_plan.iter().any(|p| plan_has_form(py, p));
        let form_dict: Bound<'_, PyDict> = if needs_form {
            let body_bytes_for_form: Vec<u8> = body
                .map(|b| b.as_bytes().to_vec())
                .unwrap_or_default();
            let content_type: String = header_lookup
                .get_item("content-type")?
                .and_then(|v| v.extract::<Vec<String>>().ok())
                .and_then(|vs| vs.into_iter().next())
                .unwrap_or_default();
            let routing_mod = py.import_bound("fastapi_rust._routing")?;
            let parser = routing_mod.getattr("parse_form_body")?;
            let bytes_obj = pyo3::types::PyBytes::new_bound(py, &body_bytes_for_form);
            let parsed = parser.call1((bytes_obj, content_type))?;
            parsed.downcast_into::<PyDict>().map_err(pyo3::PyErr::from)?
        } else {
            PyDict::new_bound(py)
        };

        // Build kwargs from the param plan, accumulating validation errors.
        let kwargs = PyDict::new_bound(py);
        let mut errors: Vec<ValidationErrorEntry> = Vec::new();
        let mut pydantic_error_dicts: Vec<PyObject> = Vec::new();
        for spec_obj in &param_plan {
            let spec: Bound<'_, pyo3::types::PyAny> = spec_obj.bind(py).clone();
            let spec_dict = match spec.downcast::<PyDict>() {
                Ok(d) => d.clone(),
                Err(_) => continue,
            };
            match extract_one_param(
                py,
                &spec_dict,
                &path_params,
                &query_dict,
                &header_lookup,
                &cookie_dict,
                &form_dict,
            ) {
                Ok(ParamExtraction::Value { name, value }) => {
                    kwargs.set_item(name, value)?;
                }
                Ok(ParamExtraction::UseDefault) => {}
                Err(err) => errors.push(err),
            }
        }

        // ---- Phase C-1: body extraction -------------------------------------
        // Pydantic body params: validate the raw bytes through pydantic-core
        // directly. For multi-body or `Body(embed=True)`, parse JSON once and
        // dispatch each entry against its key in the envelope.
        let body_bytes: Vec<u8> = body
            .map(|b| b.as_bytes().to_vec())
            .unwrap_or_default();
        extract_body_params(
            py,
            &param_plan,
            &body_bytes,
            &kwargs,
            &mut errors,
            &mut pydantic_error_dicts,
        )?;

        if !errors.is_empty() || !pydantic_error_dicts.is_empty() {
            return build_validation_error_response_mixed(
                py,
                &errors,
                &pydantic_error_dicts,
            );
        }

        // ---- Phase D: dependencies (Depends / Security) --------------------
        // App-level + route-level dependencies=[Depends(...)] turn into
        // synthetic plan entries that run before the handler's own deps.
        let app_deps_markers: Vec<PyObject> = self
            .dependencies
            .lock()
            .iter()
            .map(|p| p.clone_ref(py))
            .collect();
        let extra_deps_count = app_deps_markers.len() + route_deps_markers.len();
        let needs_deps = extra_deps_count > 0 || param_plan.iter().any(|p| {
            let dict = match p.bind(py).downcast::<PyDict>() {
                Ok(d) => d.clone(),
                Err(_) => return false,
            };
            let s: String = dict.get_item("source").ok().flatten()
                .and_then(|v| v.extract().ok()).unwrap_or_default();
            s == "depends" || s == "security" || s == "background_tasks"
        });
        if needs_deps {
            let routing_mod = py.import_bound("fastapi_rust._routing")?;
            let resolver = routing_mod.getattr("resolve_dependencies")?;
            let expander = routing_mod.getattr("expand_route_level_dependencies")?;
            let plan_list = pyo3::types::PyList::empty_bound(py);
            // Prepend app + route-level deps as synthetic _internal entries.
            if !app_deps_markers.is_empty() || !route_deps_markers.is_empty() {
                let mut all_extra: Vec<PyObject> = Vec::with_capacity(extra_deps_count);
                for d in &app_deps_markers { all_extra.push(d.clone_ref(py)); }
                for d in &route_deps_markers { all_extra.push(d.clone_ref(py)); }
                let synthetic = expander.call1((all_extra,))?;
                if let Ok(list) = synthetic.downcast::<pyo3::types::PyList>() {
                    for item in list.iter() {
                        plan_list.append(item)?;
                    }
                }
            }
            for p in &param_plan {
                plan_list.append(p.bind(py))?;
            }
            let path_params_list = pyo3::types::PyList::empty_bound(py);
            for (n, v) in &path_params {
                path_params_list.append(pyo3::types::PyTuple::new_bound(py, [n.as_str(), v.as_str()]))?;
            }
            let result = resolver.call1((
                plan_list,
                path_params_list,
                query_dict.clone(),
                header_lookup.clone(),
                cookie_dict.clone(),
                form_dict.clone(),
                body.map(|b| b.as_bytes().to_vec()).unwrap_or_default(),
                self.dependency_overrides.bind(py),
            ))?;
            let tup: (Bound<'_, PyDict>, Bound<'_, pyo3::types::PyAny>) = result.extract()?;
            let dep_kwargs = tup.0;
            let err_obj = tup.1;
            // err is None on success, dict on HTTPException-or-validation.
            if !err_obj.is_none() {
                let err_dict = err_obj.downcast::<PyDict>()?;
                let status: u16 = err_dict
                    .get_item("status")?
                    .and_then(|v| v.extract::<u16>().ok())
                    .unwrap_or(500);
                let detail = err_dict
                    .get_item("detail")?
                    .map(|v| v.unbind().into())
                    .unwrap_or_else(|| py.None());
                let body_dict = PyDict::new_bound(py);
                body_dict.set_item("detail", detail)?;
                let body_obj: PyObject = body_dict.unbind().into();
                let json_bytes = serialize_value_to_json(py, &body_obj)?;
                let mut hdrs = vec![("content-type".into(), "application/json".into())];
                if let Some(h) = err_dict.get_item("headers")? {
                    if let Ok(h_list) = h.extract::<Vec<(String, String)>>() {
                        for (k, v) in h_list {
                            hdrs.push((k, v));
                        }
                    } else if let Ok(h_map) = h.extract::<std::collections::HashMap<String, String>>() {
                        for (k, v) in h_map {
                            hdrs.push((k, v));
                        }
                    }
                }
                return Ok((status, hdrs, json_bytes));
            }
            // Merge resolved dep kwargs into kwargs.
            for (k, v) in dep_kwargs.iter() {
                kwargs.set_item(k, v)?;
            }
        }

        // Call the handler with extracted kwargs.
        let result = if kwargs.is_empty() {
            handler.call0(py)?
        } else {
            handler.call_bound(py, (), Some(&kwargs))?
        };
        let is_head = method == "HEAD";

        // Detect Starlette Response — handler returned a fully-formed response.
        let starlette_responses = py.import_bound("starlette.responses")?;
        let starlette_response_class = starlette_responses.getattr("Response")?;
        if result.bind(py).is_instance(&starlette_response_class)? {
            return extract_response_object(py, &result, is_head);
        }

        // 204 / 304 / 1xx forbid bodies — even if the handler returned a value,
        // we send empty.
        if is_no_content_status(status_code) {
            return Ok((status_code, vec![], Vec::new()));
        }

        // ---- response_model: validate, then dump to a Python value -----------
        // Path: always end up with a Python dict/list/scalar in `payload_obj`.
        // From there, either wrap in user-specified response_class or fall
        // through to default JSON encoding.
        let payload_obj: PyObject = if let Some(model_class) = response_model {
            let validated = model_class.call_method1(py, "model_validate", (result,))?;
            let dump_kwargs = PyDict::new_bound(py);
            dump_kwargs.set_item("by_alias", rm_by_alias)?;
            if rm_exclude_unset { dump_kwargs.set_item("exclude_unset", true)?; }
            if rm_exclude_none { dump_kwargs.set_item("exclude_none", true)?; }
            if rm_exclude_defaults { dump_kwargs.set_item("exclude_defaults", true)?; }
            if let Some(inc) = &rm_include { dump_kwargs.set_item("include", inc.clone_ref(py))?; }
            if let Some(exc) = &rm_exclude { dump_kwargs.set_item("exclude", exc.clone_ref(py))?; }
            validated.call_method_bound(py, "model_dump", (), Some(&dump_kwargs))?
        } else {
            result
        };

        // ---- response_class wrapping (HTMLResponse, PlainTextResponse, ...) --
        // For non-JSON response classes we construct the class with the payload
        // as content (Starlette renders bytes/str without re-encoding).
        if let Some(rc) = response_class {
            let is_json_rc = is_json_response_class(py, &rc);
            if !is_json_rc {
                let kwargs_obj = PyDict::new_bound(py);
                kwargs_obj.set_item("status_code", status_code)?;
                let response_obj = rc.call_bound(
                    py,
                    (payload_obj.clone_ref(py),),
                    Some(&kwargs_obj),
                )?;
                return extract_response_object(py, &response_obj, is_head);
            }
        }

        // ---- Default JSON serialization --------------------------------------
        let body = serialize_value_to_json(py, &payload_obj)?;
        let headers = vec![("content-type".into(), "application/json".into())];
        let body = if is_head { Vec::new() } else { body };
        Ok((status_code, headers, body))
    }
}

/// Bundle the per-route decorator kwargs that came in from the user's
/// `@app.get("/foo", response_class=..., response_model_exclude_unset=...)`
/// call. Keeps function signatures readable.
pub(crate) struct DecoratorOpts {
    pub method: &'static str,
    pub path: String,
    pub status_code: Option<i32>,
    pub response_model: Option<PyObject>,
    pub tags: Option<Vec<String>>,
    pub deprecated: bool,
    pub include_in_schema: bool,
    pub summary: Option<String>,
    pub description: Option<String>,
    pub response_class: Option<PyObject>,
    pub response_model_exclude_unset: bool,
    pub response_model_exclude_none: bool,
    pub response_model_exclude_defaults: bool,
    pub response_model_by_alias: bool,
    pub response_model_include: Option<PyObject>,
    pub response_model_exclude: Option<PyObject>,
    pub dependencies: Vec<PyObject>,
}

impl FastAPI {
    fn make_decorator(&self, opts: DecoratorOpts) -> RouteDecorator {
        RouteDecorator {
            method: opts.method.to_string(),
            path: opts.path,
            routes: self.routes.clone(),
            status_code: opts.status_code,
            response_model: opts.response_model,
            tags: opts.tags.unwrap_or_default(),
            deprecated: opts.deprecated,
            include_in_schema: opts.include_in_schema,
            summary: opts.summary,
            description: opts.description,
            response_class: opts.response_class,
            response_model_exclude_unset: opts.response_model_exclude_unset,
            response_model_exclude_none: opts.response_model_exclude_none,
            response_model_exclude_defaults: opts.response_model_exclude_defaults,
            response_model_by_alias: opts.response_model_by_alias,
            response_model_include: opts.response_model_include,
            response_model_exclude: opts.response_model_exclude,
            dependencies: opts.dependencies,
        }
    }
}

#[pyclass(name = "APIRouter", module = "fastapi_rust._core", subclass)]
pub struct APIRouter {
    pub(crate) prefix: Mutex<String>,
    pub(crate) tags: Mutex<Vec<String>>,
    pub(crate) routes: Arc<Mutex<Vec<Route>>>,
    pub(crate) default_response_class: Mutex<Option<PyObject>>,
    pub(crate) dependencies: Mutex<Vec<PyObject>>,
}

#[pymethods]
impl APIRouter {
    #[new]
    #[pyo3(signature = (*, prefix = "".to_string(), tags = None, default_response_class = None, dependencies = None, **_kwargs))]
    fn new(prefix: String, tags: Option<Vec<String>>, default_response_class: Option<PyObject>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self {
            prefix: Mutex::new(prefix),
            tags: Mutex::new(tags.unwrap_or_default()),
            routes: Arc::new(Mutex::new(Vec::with_capacity(8))),
            default_response_class: Mutex::new(default_response_class),
            dependencies: Mutex::new(dependencies.unwrap_or_default()),
        }
    }

    #[getter]
    fn prefix(&self) -> String { self.prefix.lock().clone() }
    #[getter]
    fn tags(&self) -> Vec<String> { self.tags.lock().clone() }

    #[getter]
    fn routes(&self, py: Python<'_>) -> PyObject {
        PyList::empty_bound(py).unbind().into()
    }

    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, summary = None, description = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn get(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, summary: Option<String>, description: Option<String>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "GET", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, summary = None, description = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn post(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, summary: Option<String>, description: Option<String>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "POST", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, summary = None, description = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn put(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, summary: Option<String>, description: Option<String>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "PUT", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, summary = None, description = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn delete(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, summary: Option<String>, description: Option<String>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "DELETE", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, summary = None, description = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn patch(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, summary: Option<String>, description: Option<String>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "PATCH", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, summary = None, description = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn options(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, summary: Option<String>, description: Option<String>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "OPTIONS", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, response_class = None, response_model_exclude_unset = false, response_model_exclude_none = false, response_model_exclude_defaults = false, response_model_by_alias = true, response_model_include = None, response_model_exclude = None, summary = None, description = None, dependencies = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn head(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, response_class: Option<PyObject>, response_model_exclude_unset: bool, response_model_exclude_none: bool, response_model_exclude_defaults: bool, response_model_by_alias: bool, response_model_include: Option<PyObject>, response_model_exclude: Option<PyObject>, summary: Option<String>, description: Option<String>, dependencies: Option<Vec<PyObject>>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator(DecoratorOpts { method: "HEAD", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description, response_class, response_model_exclude_unset, response_model_exclude_none, response_model_exclude_defaults, response_model_by_alias, response_model_include, response_model_exclude, dependencies: dependencies.unwrap_or_default() })
    }

    #[pyo3(signature = (router, *, prefix = "".to_string(), tags = None, **_kwargs))]
    fn include_router(&self, py: Python<'_>, router: PyRef<'_, APIRouter>, prefix: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
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
                if !merged_tags.contains(t) { merged_tags.push(t.clone()); }
            }
            for t in &extra_tags {
                if !merged_tags.contains(t) { merged_tags.push(t.clone()); }
            }

            my_routes.push(Route {
                method: r.method.clone(),
                path: joined,
                handler: r.handler.clone_ref(py),
                status_code: r.status_code,
                response_model: r.response_model.as_ref().map(|p| p.clone_ref(py)),
                tags: merged_tags,
                deprecated: r.deprecated,
                include_in_schema: r.include_in_schema,
                summary: r.summary.clone(),
                description: r.description.clone(),
                param_plan: clone_param_plan(py, &r.param_plan),
                response_class: r.response_class.as_ref().map(|p| p.clone_ref(py)),
                response_model_exclude_unset: r.response_model_exclude_unset,
                response_model_exclude_none: r.response_model_exclude_none,
                response_model_exclude_defaults: r.response_model_exclude_defaults,
                response_model_by_alias: r.response_model_by_alias,
                response_model_include: r.response_model_include.as_ref().map(|p| p.clone_ref(py)),
                response_model_exclude: r.response_model_exclude.as_ref().map(|p| p.clone_ref(py)), dependencies: r.dependencies.iter().map(|p| p.clone_ref(py)).collect(),
                security: r.security.iter().map(|s| s.clone_ref(py)).collect(),
            });
        }
        Ok(())
    }
}

impl APIRouter {
    fn make_decorator(&self, opts: DecoratorOpts) -> RouteDecorator {
        RouteDecorator {
            method: opts.method.to_string(),
            path: opts.path,
            routes: self.routes.clone(),
            status_code: opts.status_code,
            response_model: opts.response_model,
            tags: opts.tags.unwrap_or_default(),
            deprecated: opts.deprecated,
            include_in_schema: opts.include_in_schema,
            summary: opts.summary,
            description: opts.description,
            response_class: opts.response_class,
            response_model_exclude_unset: opts.response_model_exclude_unset,
            response_model_exclude_none: opts.response_model_exclude_none,
            response_model_exclude_defaults: opts.response_model_exclude_defaults,
            response_model_by_alias: opts.response_model_by_alias,
            response_model_include: opts.response_model_include,
            response_model_exclude: opts.response_model_exclude,
            dependencies: opts.dependencies,
        }
    }
}

#[pyclass(module = "fastapi_rust._core")]
pub struct IdentityDecorator;

#[pymethods]
impl IdentityDecorator {
    fn __call__(&self, func: PyObject) -> PyObject { func }
}

/// Decorator returned by `app.api_route(path, methods=[...])` — registers one
/// route per method when called with the handler.
#[pyclass(module = "fastapi_rust._core")]
pub struct ApiRouteDecorator {
    methods: Vec<String>,
    path: String,
    routes: Arc<Mutex<Vec<Route>>>,
}

#[pymethods]
impl ApiRouteDecorator {
    fn __call__(&self, py: Python<'_>, func: PyObject) -> PyResult<PyObject> {
        let plan = compile_route_plan(py, &func, &self.path).unwrap_or_default();
        let mut routes = self.routes.lock();
        for method in &self.methods {
            routes.push(Route {
                method: method.clone(),
                path: self.path.clone(),
                handler: func.clone_ref(py),
                status_code: None,
                response_model: None,
                tags: vec![],
                deprecated: false,
                include_in_schema: true,
                summary: None,
                description: None,
                param_plan: clone_param_plan(py, &plan),
                response_class: None,
                response_model_exclude_unset: false,
                response_model_exclude_none: false,
                response_model_exclude_defaults: false,
                response_model_by_alias: true,
                response_model_include: None,
                response_model_exclude: None, dependencies: vec![], security: vec![],
            });
        }
        Ok(func)
    }
}

// ---------- helpers --------------------------------------------------------

/// 204 No Content, 304 Not Modified, and 1xx informational responses MUST
/// have an empty body per RFC 9110.
fn is_no_content_status(s: u16) -> bool {
    matches!(s, 204 | 304) || (100..200).contains(&s)
}

/// True when `cls` is JSONResponse (or a subclass thereof). For JSONResponse,
/// going through the class would double-encode the dict we already built — we
/// prefer to ship the bytes directly and skip the Starlette layer.
fn is_json_response_class(py: Python<'_>, cls: &PyObject) -> bool {
    let starlette = match py.import_bound("starlette.responses") {
        Ok(m) => m,
        Err(_) => return false,
    };
    let json_response = match starlette.getattr("JSONResponse") {
        Ok(c) => c,
        Err(_) => return false,
    };
    if cls.bind(py).is(&json_response) {
        return true;
    }
    // Use Python's builtin issubclass — handles user-defined JSONResponse subclasses.
    let builtins = match py.import_bound("builtins") {
        Ok(m) => m,
        Err(_) => return false,
    };
    let issubclass = match builtins.getattr("issubclass") {
        Ok(f) => f,
        Err(_) => return false,
    };
    issubclass
        .call1((cls.clone_ref(py), json_response))
        .ok()
        .and_then(|r| r.extract::<bool>().ok())
        .unwrap_or(false)
}

fn extract_response_object(
    py: Python<'_>,
    response: &PyObject,
    is_head: bool,
) -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {
    let bound = response.bind(py);
    let status_code: u16 = bound.getattr("status_code")?.extract()?;
    let body_obj = bound.getattr("body")?;
    let body: Vec<u8> = body_obj.extract().unwrap_or_default();
    let body = if is_head || is_no_content_status(status_code) { Vec::new() } else { body };

    // Headers may be a list of (k, v) tuples (Starlette internal raw_headers)
    // or a Headers mapping. Walk the public `headers` if present.
    let mut headers_out: Vec<(String, String)> = Vec::new();
    if let Ok(h) = bound.getattr("raw_headers") {
        if let Ok(items) = h.extract::<Vec<(Vec<u8>, Vec<u8>)>>() {
            for (k, v) in items {
                let k_str = String::from_utf8_lossy(&k).to_string();
                let v_str = String::from_utf8_lossy(&v).to_string();
                headers_out.push((k_str, v_str));
            }
        }
    }
    if headers_out.is_empty() {
        // Fall back to .headers.items() — Starlette MutableHeaders.
        if let Ok(headers) = bound.getattr("headers") {
            if let Ok(items_iter) = headers.call_method0("items") {
                if let Ok(iter) = items_iter.iter() {
                    for item_res in iter {
                        let item: Bound<'_, pyo3::types::PyAny> = item_res?;
                        let pair: (String, String) = item.extract()?;
                        headers_out.push(pair);
                    }
                }
            }
        }
    }
    Ok((status_code, headers_out, body))
}

/// Minimal Swagger UI page — Phase H replaces with the proper CDN-template.
/// Uses `r##".."##` because the embedded JS contains `"#` (quote+hash) which
/// would terminate a `r#".."#` raw string early.
fn build_docs_html(title: &str, openapi_url: &str) -> String {
    format!(
        r##"<!DOCTYPE html>
<html>
<head>
<title>{title} - Swagger UI</title>
<link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
const ui = SwaggerUIBundle({{
    url: "{openapi_url}",
    dom_id: "#swagger-ui",
    deepLinking: true
}});
</script>
</body>
</html>"##
    )
}

/// Minimal ReDoc page — Phase H replaces with the proper CDN-template.
fn build_redoc_html(title: &str, openapi_url: &str) -> String {
    format!(
        r##"<!DOCTYPE html>
<html>
<head>
<title>{title} - ReDoc</title>
<link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
<style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
<redoc spec-url="{openapi_url}"></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js"></script>
</body>
</html>"##
    )
}

fn serialize_value_to_json(py: Python<'_>, value: &PyObject) -> PyResult<Vec<u8>> {
    // json.dumps(value, default=jsonable_encoder).encode("utf-8")
    let json_mod = py.import_bound("json")?;
    let encoders_mod = py.import_bound("fastapi_rust.encoders")?;
    let encoder = encoders_mod.getattr("jsonable_encoder")?;
    let kwargs = PyDict::new_bound(py);
    kwargs.set_item("default", encoder)?;
    kwargs.set_item("ensure_ascii", false)?;
    kwargs.set_item("separators", (",", ":"))?;
    let dumps = json_mod.getattr("dumps")?;
    let result = dumps.call((value.clone_ref(py),), Some(&kwargs))?;
    let s: String = result.extract()?;
    Ok(s.into_bytes())
}

// ---------- Phase B-2: path matching + casting -----------------------------

/// Match an incoming request `path` against a registered template like
/// `/items/{item_id}` or `/files/{file_path:path}`. Returns the extracted
/// `(name, value)` pairs in template order, or `None` if the path doesn't
/// match the template.
///
/// Catch-all syntax: `{name:path}` consumes the rest of the URL, including
/// any slashes. Plain `{name}` matches a single path segment.
fn match_path_template(template: &str, path: &str) -> Option<Vec<(String, String)>> {
    // Fast path: no placeholders at all → bytewise equality.
    if !template.contains('{') {
        return if template == path { Some(Vec::new()) } else { None };
    }

    // Split by '/' so each segment can be matched / captured independently.
    let template_segs: Vec<&str> = template.split('/').collect();
    let path_segs: Vec<&str> = path.split('/').collect();

    let mut params: Vec<(String, String)> = Vec::with_capacity(2);

    let mut t_idx = 0usize;
    let mut p_idx = 0usize;

    while t_idx < template_segs.len() {
        let t_seg = template_segs[t_idx];

        if t_seg.starts_with('{') && t_seg.ends_with('}') {
            let inner = &t_seg[1..t_seg.len() - 1];
            let (name, type_hint) = match inner.split_once(':') {
                Some((n, ty)) => (n, Some(ty)),
                None => (inner, None),
            };

            if matches!(type_hint, Some("path")) {
                // Catch-all: consume everything remaining in the path. Even if
                // we're at the last template segment, we accept the rest of the
                // URL, slashes included.
                if p_idx >= path_segs.len() {
                    return None;
                }
                let rest = path_segs[p_idx..].join("/");
                params.push((name.to_string(), rest));
                return Some(params);
            }

            // Single-segment placeholder.
            if p_idx >= path_segs.len() {
                return None;
            }
            params.push((name.to_string(), path_segs[p_idx].to_string()));
        } else {
            // Literal segment must match exactly.
            if p_idx >= path_segs.len() || path_segs[p_idx] != t_seg {
                return None;
            }
        }
        t_idx += 1;
        p_idx += 1;
    }

    // Both must be fully consumed.
    if p_idx == path_segs.len() {
        Some(params)
    } else {
        None
    }
}

/// Cast a raw path-param string to a typed Python value, or build a Pydantic-v2
/// shaped validation error if the cast fails.
#[allow(dead_code)]
fn cast_path_param(
    py: Python<'_>,
    value: &str,
    kind: &str,
    name: &str,
) -> Result<PyObject, ValidationErrorEntry> {
    match kind {
        "path:int" => match value.parse::<i64>() {
            Ok(n) => Ok(n.into_py(py)),
            Err(_) => Err(ValidationErrorEntry {
                err_type: "int_parsing",
                loc: ("path", name.to_string()),
                msg: "Input should be a valid integer, unable to parse string as an integer",
                input: value.to_string(),
            }),
        },
        "path:float" => match value.parse::<f64>() {
            Ok(n) => Ok(n.into_py(py)),
            Err(_) => Err(ValidationErrorEntry {
                err_type: "float_parsing",
                loc: ("path", name.to_string()),
                msg: "Input should be a valid number, unable to parse string as a number",
                input: value.to_string(),
            }),
        },
        "path:bool" => {
            let lc = value.to_ascii_lowercase();
            match lc.as_str() {
                "true" | "1" | "yes" | "on" => Ok(true.into_py(py)),
                "false" | "0" | "no" | "off" => Ok(false.into_py(py)),
                _ => Err(ValidationErrorEntry {
                    err_type: "bool_parsing",
                    loc: ("path", name.to_string()),
                    msg: "Input should be a valid boolean, unable to interpret input",
                    input: value.to_string(),
                }),
            }
        }
        "path:uuid" => {
            let uuid_mod = py
                .import_bound("uuid")
                .map_err(|_| ValidationErrorEntry::generic("path", name, value, "uuid_parsing", "UUID module unavailable"))?;
            let uuid_class = uuid_mod
                .getattr("UUID")
                .map_err(|_| ValidationErrorEntry::generic("path", name, value, "uuid_parsing", "UUID class unavailable"))?;
            match uuid_class.call1((value,)) {
                Ok(u) => Ok(u.unbind().into()),
                Err(_) => Err(ValidationErrorEntry {
                    err_type: "uuid_parsing",
                    loc: ("path", name.to_string()),
                    msg: "Input should be a valid UUID, unable to parse string as a UUID",
                    input: value.to_string(),
                }),
            }
        }
        // "path:str", "path:any", or unknown — pass through verbatim.
        _ => Ok(value.into_py(py)),
    }
}

/// Pydantic-v2 shaped validation error entry. `err_type` is the machine
/// identifier (`"int_parsing"`, `"missing"`, ...); `loc` is a tuple like
/// `("path", "item_id")`; `msg` is human-readable; `input` is the offending value.
pub(crate) struct ValidationErrorEntry {
    pub err_type: &'static str,
    pub loc: (&'static str, String),
    pub msg: &'static str,
    pub input: String,
}

impl ValidationErrorEntry {
    fn generic(loc_kind: &'static str, name: &str, input: &str, err_type: &'static str, msg: &'static str) -> Self {
        Self {
            err_type,
            loc: (loc_kind, name.to_string()),
            msg,
            input: input.to_string(),
        }
    }

    fn to_py_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new_bound(py);
        d.set_item("type", self.err_type)?;
        let loc_list = pyo3::types::PyList::new_bound(py, [self.loc.0, &self.loc.1]);
        d.set_item("loc", loc_list)?;
        d.set_item("msg", self.msg)?;
        // Pydantic-v2 sets `input` to None for "missing" errors; we mirror that
        // so test expectations of None vs {} pass.
        if self.err_type == "missing" {
            d.set_item("input", py.None())?;
        } else {
            d.set_item("input", &self.input)?;
        }
        Ok(d)
    }
}

#[allow(dead_code)]
fn build_validation_error_response(
    py: Python<'_>,
    errors: &[ValidationErrorEntry],
) -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {
    build_validation_error_response_mixed(py, errors, &[])
}

/// Build the 422 response body from a mix of statically-built Phase B-3 errors
/// (path/query/header/cookie cast or validator failures) AND pydantic-built
/// dicts (body validation, list[Model], etc.). Both shapes use the same
/// `{type, loc, msg, input}` keys so the merged list stays uniform.
fn build_validation_error_response_mixed(
    py: Python<'_>,
    errors: &[ValidationErrorEntry],
    pydantic_dicts: &[PyObject],
) -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {
    let detail = pyo3::types::PyList::empty_bound(py);
    for err in errors {
        detail.append(err.to_py_dict(py)?)?;
    }
    for d in pydantic_dicts {
        detail.append(d.bind(py))?;
    }
    let body = PyDict::new_bound(py);
    body.set_item("detail", detail)?;
    let body_obj: PyObject = body.unbind().into();
    let json_bytes = serialize_value_to_json(py, &body_obj)?;
    Ok((
        422,
        vec![("content-type".into(), "application/json".into())],
        json_bytes,
    ))
}

/// Pull `errors()` off a pydantic ValidationError and build a list of dicts
/// suitable for the `detail` array. Each error's `loc` tuple has `base_loc`
/// prepended (e.g. `["body", <param_name>]` for embed mode, or just `["body"]`
/// for a top-level model body).
fn pydantic_errors_to_dicts(
    py: Python<'_>,
    err: &PyErr,
    base_loc: &[String],
) -> PyResult<Vec<PyObject>> {
    let exc_value = err.value_bound(py);
    // Not all PyErrs are ValidationError — guard the call.
    let errors_callable = match exc_value.getattr("errors") {
        Ok(v) => v,
        Err(_) => return Ok(Vec::new()),
    };
    let errors_list = match errors_callable.call0() {
        Ok(v) => v,
        Err(_) => return Ok(Vec::new()),
    };
    let mut out: Vec<PyObject> = Vec::new();
    let iter = match errors_list.iter() {
        Ok(it) => it,
        Err(_) => return Ok(Vec::new()),
    };
    for item_res in iter {
        let item: Bound<'_, pyo3::types::PyAny> = item_res?;
        let dict = PyDict::new_bound(py);

        if let Ok(t) = item.get_item("type") {
            dict.set_item("type", t)?;
        }

        let new_loc = pyo3::types::PyList::empty_bound(py);
        for piece in base_loc {
            new_loc.append(piece)?;
        }
        if let Ok(loc_old) = item.get_item("loc") {
            if let Ok(sub_iter) = loc_old.iter() {
                for sub in sub_iter {
                    let s: Bound<'_, pyo3::types::PyAny> = sub?;
                    new_loc.append(s)?;
                }
            }
        }
        dict.set_item("loc", new_loc)?;

        if let Ok(m) = item.get_item("msg") {
            dict.set_item("msg", m)?;
        }
        if let Ok(inp) = item.get_item("input") {
            dict.set_item("input", inp)?;
        }
        out.push(dict.unbind().into());
    }
    Ok(out)
}

/// Phase C-1: walk the param_plan, find body entries, and validate the raw
/// body bytes against each one. Single non-embed Pydantic body uses the fast
/// `validate_json(bytes)` path (no Python dict round-trip); embed/multi-body
/// goes through `json.loads` + per-key `validate_python`.
fn extract_body_params(
    py: Python<'_>,
    plan: &[PyObject],
    body_bytes: &[u8],
    kwargs: &Bound<'_, PyDict>,
    _errors: &mut Vec<ValidationErrorEntry>,
    pydantic_error_dicts: &mut Vec<PyObject>,
) -> PyResult<()> {
    // Collect body entries (preserving order) — we need indices into the plan.
    let mut body_entries: Vec<Bound<'_, PyDict>> = Vec::new();
    for spec_obj in plan {
        let spec = spec_obj.bind(py);
        let dict = match spec.downcast::<PyDict>() {
            Ok(d) => d,
            Err(_) => continue,
        };
        let source: String = dict
            .get_item("source")
            .ok()
            .flatten()
            .and_then(|v| v.extract().ok())
            .unwrap_or_default();
        if source == "body" {
            body_entries.push(dict.clone());
        }
    }
    if body_entries.is_empty() {
        return Ok(());
    }

    // Determine layout: single non-embed body OR multi/embed.
    let any_embed = body_entries.iter().any(|d| {
        d.get_item("embed")
            .ok()
            .flatten()
            .and_then(|v| v.extract().ok())
            .unwrap_or(false)
    });
    let single = body_entries.len() == 1;

    if single && !any_embed {
        let entry = &body_entries[0];
        let name: String = entry
            .get_item("name").ok().flatten()
            .and_then(|v| v.extract().ok()).unwrap_or_default();
        let type_kind: String = entry
            .get_item("type").ok().flatten()
            .and_then(|v| v.extract().ok()).unwrap_or_else(|| "model".into());
        let required: bool = entry
            .get_item("required").ok().flatten()
            .and_then(|v| v.extract().ok()).unwrap_or(true);
        let model: Option<PyObject> = entry
            .get_item("model").ok().flatten().map(|v| v.unbind().into());

        if body_bytes.is_empty() {
            if required {
                pydantic_error_dicts.push(missing_body_dict(py, &[name.clone()], None)?);
            }
            return Ok(());
        }

        match type_kind.as_str() {
            "model" => {
                if let Some(model_class) = model {
                    let validator = model_class.getattr(py, "__pydantic_validator__")?;
                    let bytes_obj = pyo3::types::PyBytes::new_bound(py, body_bytes);
                    match validator.call_method1(py, "validate_json", (bytes_obj,)) {
                        Ok(model_instance) => {
                            kwargs.set_item(&name, model_instance)?;
                        }
                        Err(err) => {
                            let mut dicts = pydantic_errors_to_dicts(py, &err, &["body".into()])?;
                            pydantic_error_dicts.append(&mut dicts);
                        }
                    }
                }
            }
            "list[model]" => {
                // Build TypeAdapter(list[Model]) on the fly.
                let pydantic = py.import_bound("pydantic")?;
                let type_adapter_class = pydantic.getattr("TypeAdapter")?;
                if let Some(model_class) = model {
                    let typing = py.import_bound("typing")?;
                    let list_t = typing.getattr("List")?;
                    let target = list_t.get_item(model_class.bind(py))?;
                    let adapter = type_adapter_class.call1((target,))?;
                    let bytes_obj = pyo3::types::PyBytes::new_bound(py, body_bytes);
                    match adapter.call_method1("validate_json", (bytes_obj,)) {
                        Ok(values) => {
                            kwargs.set_item(&name, values)?;
                        }
                        Err(err) => {
                            let mut dicts = pydantic_errors_to_dicts(py, &err, &["body".into()])?;
                            pydantic_error_dicts.append(&mut dicts);
                        }
                    }
                }
            }
            // raw / dict / any → just json.loads.
            _ => {
                let json_mod = py.import_bound("json")?;
                let bytes_obj = pyo3::types::PyBytes::new_bound(py, body_bytes);
                match json_mod.getattr("loads")?.call1((bytes_obj,)) {
                    Ok(value) => {
                        kwargs.set_item(&name, value)?;
                    }
                    Err(err) => {
                        let mut dicts = pydantic_errors_to_dicts(py, &err, &["body".into()])?;
                        if dicts.is_empty() {
                            pydantic_error_dicts.push(json_invalid_dict(py, &["body".into()])?);
                        } else {
                            pydantic_error_dicts.append(&mut dicts);
                        }
                    }
                }
            }
        }
        return Ok(());
    }

    // Multi-body or embed — parse JSON envelope once.
    if body_bytes.is_empty() {
        for entry in &body_entries {
            let name: String = entry
                .get_item("name").ok().flatten()
                .and_then(|v| v.extract().ok()).unwrap_or_default();
            let required: bool = entry
                .get_item("required").ok().flatten()
                .and_then(|v| v.extract().ok()).unwrap_or(true);
            if required {
                pydantic_error_dicts.push(missing_body_dict(
                    py,
                    &["body".into(), name],
                    None,
                )?);
            }
        }
        return Ok(());
    }

    let json_mod = py.import_bound("json")?;
    let bytes_obj = pyo3::types::PyBytes::new_bound(py, body_bytes);
    let envelope = match json_mod.getattr("loads")?.call1((bytes_obj,)) {
        Ok(v) => v,
        Err(_) => {
            pydantic_error_dicts.push(json_invalid_dict(py, &["body".into()])?);
            return Ok(());
        }
    };
    let envelope_dict = match envelope.downcast::<PyDict>() {
        Ok(d) => d.clone(),
        Err(_) => {
            // Non-dict body where embed/multi-body is needed — every entry
            // gets a "missing" error.
            for entry in &body_entries {
                let name: String = entry
                    .get_item("name").ok().flatten()
                    .and_then(|v| v.extract().ok()).unwrap_or_default();
                let required: bool = entry
                    .get_item("required").ok().flatten()
                    .and_then(|v| v.extract().ok()).unwrap_or(true);
                if required {
                    pydantic_error_dicts.push(missing_body_dict(
                        py,
                        &["body".into(), name],
                        None,
                    )?);
                }
            }
            return Ok(());
        }
    };

    for entry in &body_entries {
        let name: String = entry
            .get_item("name").ok().flatten()
            .and_then(|v| v.extract().ok()).unwrap_or_default();
        let type_kind: String = entry
            .get_item("type").ok().flatten()
            .and_then(|v| v.extract().ok()).unwrap_or_else(|| "model".into());
        let required: bool = entry
            .get_item("required").ok().flatten()
            .and_then(|v| v.extract().ok()).unwrap_or(true);
        let model: Option<PyObject> = entry
            .get_item("model").ok().flatten().map(|v| v.unbind().into());

        let sub = match envelope_dict.get_item(&name)? {
            Some(v) => v,
            None => {
                if required {
                    pydantic_error_dicts.push(missing_body_dict(
                        py,
                        &["body".into(), name.clone()],
                        None,
                    )?);
                }
                continue;
            }
        };

        match type_kind.as_str() {
            "model" => {
                if let Some(model_class) = model {
                    let validator = model_class.getattr(py, "__pydantic_validator__")?;
                    match validator.call_method1(py, "validate_python", (sub,)) {
                        Ok(model_instance) => {
                            kwargs.set_item(&name, model_instance)?;
                        }
                        Err(err) => {
                            let mut dicts = pydantic_errors_to_dicts(
                                py,
                                &err,
                                &["body".into(), name.clone()],
                            )?;
                            pydantic_error_dicts.append(&mut dicts);
                        }
                    }
                }
            }
            "list[model]" => {
                if let Some(model_class) = model {
                    let pydantic = py.import_bound("pydantic")?;
                    let type_adapter_class = pydantic.getattr("TypeAdapter")?;
                    let typing = py.import_bound("typing")?;
                    let list_t = typing.getattr("List")?;
                    let target = list_t.get_item(model_class.bind(py))?;
                    let adapter = type_adapter_class.call1((target,))?;
                    match adapter.call_method1("validate_python", (sub,)) {
                        Ok(values) => {
                            kwargs.set_item(&name, values)?;
                        }
                        Err(err) => {
                            let mut dicts = pydantic_errors_to_dicts(
                                py,
                                &err,
                                &["body".into(), name.clone()],
                            )?;
                            pydantic_error_dicts.append(&mut dicts);
                        }
                    }
                }
            }
            _ => {
                kwargs.set_item(&name, sub)?;
            }
        }
    }
    Ok(())
}

/// Build a {type:"missing", loc:[..], msg:"Field required", input:None} dict.
fn missing_body_dict(
    py: Python<'_>,
    loc: &[String],
    input: Option<PyObject>,
) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(py);
    dict.set_item("type", "missing")?;
    let loc_list = pyo3::types::PyList::empty_bound(py);
    for s in loc {
        loc_list.append(s)?;
    }
    dict.set_item("loc", loc_list)?;
    dict.set_item("msg", "Field required")?;
    if let Some(i) = input {
        dict.set_item("input", i)?;
    } else {
        dict.set_item("input", py.None())?;
    }
    Ok(dict.unbind().into())
}

/// Build a {type:"json_invalid", loc:..} dict for malformed JSON body.
fn json_invalid_dict(py: Python<'_>, loc: &[String]) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(py);
    dict.set_item("type", "json_invalid")?;
    let loc_list = pyo3::types::PyList::empty_bound(py);
    for s in loc {
        loc_list.append(s)?;
    }
    dict.set_item("loc", loc_list)?;
    dict.set_item("msg", "JSON decode error")?;
    dict.set_item("input", py.None())?;
    Ok(dict.unbind().into())
}

// ---------- Phase B-3: query / header / cookie extraction ------------------

pub(crate) enum ParamExtraction {
    /// Extracted a value from the request — pass to the handler under `name`.
    Value { name: String, value: PyObject },
    /// No value found and the param has a Python default — let the handler's
    /// own default kick in, don't add it to kwargs.
    UseDefault,
}

/// Walk one entry of the compiled param plan and produce a typed Python value
/// or a validation error.
#[allow(clippy::too_many_arguments)]
fn extract_one_param(
    py: Python<'_>,
    spec: &Bound<'_, PyDict>,
    path_params: &[(String, String)],
    query: &Bound<'_, PyDict>,
    headers: &Bound<'_, PyDict>,
    cookies: &Bound<'_, PyDict>,
    form: &Bound<'_, PyDict>,
) -> Result<ParamExtraction, ValidationErrorEntry> {
    let name: String = match spec.get_item("name") {
        Ok(Some(v)) => v.extract().unwrap_or_default(),
        _ => return Ok(ParamExtraction::UseDefault),
    };
    let source: String = spec.get_item("source")
        .ok().flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or_else(|| "query".into());
    let type_kind: String = spec.get_item("type")
        .ok().flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or_else(|| "str".into());
    let alias: Option<String> = spec.get_item("alias")
        .ok().flatten()
        .and_then(|v| v.extract().ok());
    let required: bool = spec.get_item("required")
        .ok().flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or(true);
    let convert_underscores: bool = spec.get_item("convert_underscores")
        .ok().flatten()
        .and_then(|v| v.extract().ok())
        .unwrap_or(true);
    let validators: Option<PyObject> = spec.get_item("validators")
        .ok().flatten()
        .map(|v| v.unbind().into());

    // Body is handled in extract_body_params; depends/security in Phase D/E;
    // security_scopes / background_tasks are filled by resolve_dependencies.
    if source == "depends" || source == "body" || source == "security"
        || source == "security_scopes" || source == "background_tasks" {
        return Ok(ParamExtraction::UseDefault);
    }

    // ---- form / file: look up in pre-parsed form_dict -----------------------
    if source == "form" || source == "file" {
        let key = alias.as_deref().unwrap_or(&name).to_string();
        let raw = form.get_item(&key).ok().flatten();
        let raw_list: Option<Bound<'_, pyo3::types::PyList>> = raw
            .and_then(|v| v.downcast_into::<pyo3::types::PyList>().ok());
        // UploadFile single — pass through unmodified.
        if type_kind == "uploadfile" {
            let value = match raw_list.as_ref().and_then(|l| l.get_item(0).ok()) {
                Some(v) => v.unbind().into(),
                None => {
                    if required {
                        return Err(ValidationErrorEntry::missing("body", &key));
                    }
                    return Ok(ParamExtraction::UseDefault);
                }
            };
            return Ok(ParamExtraction::Value { name, value });
        }
        // list[UploadFile] — pass the entire list.
        if type_kind == "list[uploadfile]" {
            let value: PyObject = match raw_list {
                Some(l) => l.unbind().into(),
                None => {
                    if required {
                        return Err(ValidationErrorEntry::missing("body", &key));
                    }
                    return Ok(ParamExtraction::UseDefault);
                }
            };
            return Ok(ParamExtraction::Value { name, value });
        }
        // bytes File: read first UploadFile's underlying file.
        // Starlette ≥0.40 made UploadFile.read() async, so we go through
        // `.file` (BytesIO/SpooledTemporaryFile) directly.
        if type_kind == "bytes" {
            let bytes_value: Option<Vec<u8>> = raw_list
                .as_ref()
                .and_then(|l| l.get_item(0).ok())
                .and_then(|item| {
                    if let Ok(b) = item.extract::<Vec<u8>>() {
                        return Some(b);
                    }
                    if let Ok(s) = item.extract::<String>() {
                        return Some(s.into_bytes());
                    }
                    if let Ok(file_attr) = item.getattr("file") {
                        let _ = file_attr.call_method1("seek", (0,));
                        return file_attr
                            .call_method0("read")
                            .ok()
                            .and_then(|b| b.extract::<Vec<u8>>().ok());
                    }
                    None
                });
            if let Some(b) = bytes_value {
                return Ok(ParamExtraction::Value {
                    name,
                    value: pyo3::types::PyBytes::new_bound(py, &b).unbind().into(),
                });
            }
            if required {
                return Err(ValidationErrorEntry::missing("body", &key));
            }
            return Ok(ParamExtraction::UseDefault);
        }
        // Special handling: bytes File (Annotated[bytes, File()]) — read the
        // first UploadFile's contents and pass as bytes.
        if type_kind == "any" || type_kind == "str" || type_kind == "int"
            || type_kind == "float" || type_kind == "bool" || type_kind == "uuid"
            || type_kind.starts_with("list[")
        {
            // Form scalar fields go through the scalar/list path below by
            // re-using cast_scalar. Re-extract raw values as Vec<String>.
            let raw_values: Option<Vec<String>> = raw_list.as_ref().and_then(|l| {
                l.iter()
                    .filter_map(|item| item.extract::<String>().ok())
                    .collect::<Vec<_>>()
                    .into()
            });
            let raw_values = match raw_values {
                Some(v) if !v.is_empty() => v,
                _ => {
                    if required {
                        return Err(ValidationErrorEntry::missing("body", &key));
                    }
                    return Ok(ParamExtraction::UseDefault);
                }
            };
            if type_kind.starts_with("list[") {
                let inner = type_kind
                    .strip_prefix("list[")
                    .and_then(|s| s.strip_suffix(']'))
                    .unwrap_or("str");
                let pylist = pyo3::types::PyList::empty_bound(py);
                for v in &raw_values {
                    let cast = cast_scalar(py, v, inner, "body", &name)?;
                    pylist.append(cast).ok();
                }
                return Ok(ParamExtraction::Value { name, value: pylist.unbind().into() });
            }
            let value = cast_scalar(py, &raw_values[0], &type_kind, "body", &name)?;
            if let Some(validators_obj) = &validators {
                if let Ok(d) = validators_obj.bind(py).downcast::<PyDict>() {
                    run_validators(py, &value, d, "body", &name, &raw_values[0])?;
                }
            }
            return Ok(ParamExtraction::Value { name, value });
        }
        // Anything else falls through as a missing required param.
        if required {
            return Err(ValidationErrorEntry::missing("body", &key));
        }
        return Ok(ParamExtraction::UseDefault);
    }

    // The lookup key: alias if specified, else name. For headers, we may
    // need to convert underscores → dashes.
    let lookup_key: String = if let Some(a) = alias.as_deref() {
        a.to_string()
    } else if source == "header" && convert_underscores {
        name.replace('_', "-")
    } else {
        name.clone()
    };

    // Pull the raw value(s) from the appropriate source.
    let raw_values: Option<Vec<String>> = match source.as_str() {
        "path" => path_params
            .iter()
            .find(|(n, _)| n == &name)
            .map(|(_, v)| vec![v.clone()]),
        "query" => match query.get_item(&lookup_key).ok().flatten() {
            Some(v) => v.extract::<Vec<String>>().ok(),
            None => None,
        },
        "header" => match headers.get_item(lookup_key.to_ascii_lowercase()).ok().flatten() {
            // header_lookup is dict[str, list[str]] — same shape as query.
            Some(v) => v.extract::<Vec<String>>().ok(),
            None => None,
        },
        "cookie" => match cookies.get_item(&lookup_key).ok().flatten() {
            Some(v) => Some(vec![v.extract().unwrap_or_default()]),
            None => None,
        },
        _ => None,
    };

    // Missing handling.
    let raw_values = match raw_values {
        Some(v) if !v.is_empty() => v,
        _ => {
            if required {
                return Err(ValidationErrorEntry::missing(&source, &lookup_key));
            }
            return Ok(ParamExtraction::UseDefault);
        }
    };

    // List type → return a Python list, casting each item.
    if type_kind.starts_with("list[") {
        let inner = type_kind.strip_prefix("list[").and_then(|s| s.strip_suffix(']')).unwrap_or("str");
        let pylist = pyo3::types::PyList::empty_bound(py);
        for v in &raw_values {
            let cast = cast_scalar(py, v, inner, &source, &name)?;
            pylist.append(cast).ok();
        }
        return Ok(ParamExtraction::Value {
            name,
            value: pylist.unbind().into(),
        });
    }

    // Single-value: take the first.
    let value = cast_scalar(py, &raw_values[0], &type_kind, &source, &name)?;

    // Validators on scalars.
    if let Some(validators_obj) = &validators {
        if let Ok(d) = validators_obj.bind(py).downcast::<PyDict>() {
            run_validators(py, &value, d, &source, &name, &raw_values[0])?;
        }
    }

    Ok(ParamExtraction::Value { name, value })
}

/// Cast a raw string into the typed Python value. Returns ValidationError on
/// parse failure with the right loc.
fn cast_scalar(
    py: Python<'_>,
    value: &str,
    type_kind: &str,
    source: &str,
    name: &str,
) -> Result<PyObject, ValidationErrorEntry> {
    let source_static: &'static str = match source {
        "path" => "path",
        "query" => "query",
        "header" => "header",
        "cookie" => "cookie",
        "body" => "body",
        "form" => "body",
        "file" => "body",
        _ => "query",
    };
    match type_kind {
        "int" => match value.parse::<i64>() {
            Ok(n) => Ok(n.into_py(py)),
            Err(_) => Err(ValidationErrorEntry {
                err_type: "int_parsing",
                loc: (source_static, name.to_string()),
                msg: "Input should be a valid integer, unable to parse string as an integer",
                input: value.to_string(),
            }),
        },
        "float" => match value.parse::<f64>() {
            Ok(n) => Ok(n.into_py(py)),
            Err(_) => Err(ValidationErrorEntry {
                err_type: "float_parsing",
                loc: (source_static, name.to_string()),
                msg: "Input should be a valid number, unable to parse string as a number",
                input: value.to_string(),
            }),
        },
        "bool" => {
            let lc = value.to_ascii_lowercase();
            match lc.as_str() {
                "true" | "1" | "yes" | "on" => Ok(true.into_py(py)),
                "false" | "0" | "no" | "off" => Ok(false.into_py(py)),
                _ => Err(ValidationErrorEntry {
                    err_type: "bool_parsing",
                    loc: (source_static, name.to_string()),
                    msg: "Input should be a valid boolean, unable to interpret input",
                    input: value.to_string(),
                }),
            }
        }
        "uuid" => {
            let uuid_mod = py.import_bound("uuid").map_err(|_| ValidationErrorEntry::generic(
                source_static, name, value, "uuid_parsing", "UUID module unavailable"))?;
            let uuid_class = uuid_mod.getattr("UUID").map_err(|_| ValidationErrorEntry::generic(
                source_static, name, value, "uuid_parsing", "UUID class unavailable"))?;
            match uuid_class.call1((value,)) {
                Ok(u) => Ok(u.unbind().into()),
                Err(_) => Err(ValidationErrorEntry {
                    err_type: "uuid_parsing",
                    loc: (source_static, name.to_string()),
                    msg: "Input should be a valid UUID, unable to parse string as a UUID",
                    input: value.to_string(),
                }),
            }
        }
        // "str" / "any" / unknown → pass through.
        _ => Ok(value.into_py(py)),
    }
}

/// Apply Pydantic-v2-shaped validators (gt/ge/lt/le/min_length/max_length/pattern).
fn run_validators(
    py: Python<'_>,
    value: &PyObject,
    validators: &Bound<'_, PyDict>,
    source: &str,
    name: &str,
    raw: &str,
) -> Result<(), ValidationErrorEntry> {
    let source_static: &'static str = match source {
        "path" => "path",
        "query" => "query",
        "header" => "header",
        "cookie" => "cookie",
        _ => "query",
    };

    // Numeric comparisons — convert value to f64.
    let numeric_value: Option<f64> = value.bind(py).extract::<f64>().ok();

    if let (Ok(Some(gt)), Some(n)) = (validators.get_item("gt").map(|x| x.and_then(|v| v.extract::<f64>().ok())), numeric_value) {
        if !(n > gt) {
            return Err(ValidationErrorEntry {
                err_type: "greater_than",
                loc: (source_static, name.to_string()),
                msg: "Input should be greater than the configured threshold",
                input: raw.to_string(),
            });
        }
    }
    if let (Ok(Some(ge)), Some(n)) = (validators.get_item("ge").map(|x| x.and_then(|v| v.extract::<f64>().ok())), numeric_value) {
        if !(n >= ge) {
            return Err(ValidationErrorEntry {
                err_type: "greater_than_equal",
                loc: (source_static, name.to_string()),
                msg: "Input should be greater than or equal to the configured threshold",
                input: raw.to_string(),
            });
        }
    }
    if let (Ok(Some(lt)), Some(n)) = (validators.get_item("lt").map(|x| x.and_then(|v| v.extract::<f64>().ok())), numeric_value) {
        if !(n < lt) {
            return Err(ValidationErrorEntry {
                err_type: "less_than",
                loc: (source_static, name.to_string()),
                msg: "Input should be less than the configured threshold",
                input: raw.to_string(),
            });
        }
    }
    if let (Ok(Some(le)), Some(n)) = (validators.get_item("le").map(|x| x.and_then(|v| v.extract::<f64>().ok())), numeric_value) {
        if !(n <= le) {
            return Err(ValidationErrorEntry {
                err_type: "less_than_equal",
                loc: (source_static, name.to_string()),
                msg: "Input should be less than or equal to the configured threshold",
                input: raw.to_string(),
            });
        }
    }

    // String length / pattern.
    let str_value: Option<String> = value.bind(py).extract::<String>().ok();
    if let (Ok(Some(min_len)), Some(s)) = (validators.get_item("min_length").map(|x| x.and_then(|v| v.extract::<usize>().ok())), &str_value) {
        if s.chars().count() < min_len {
            return Err(ValidationErrorEntry {
                err_type: "string_too_short",
                loc: (source_static, name.to_string()),
                msg: "Input should have at least the configured minimum length",
                input: raw.to_string(),
            });
        }
    }
    if let (Ok(Some(max_len)), Some(s)) = (validators.get_item("max_length").map(|x| x.and_then(|v| v.extract::<usize>().ok())), &str_value) {
        if s.chars().count() > max_len {
            return Err(ValidationErrorEntry {
                err_type: "string_too_long",
                loc: (source_static, name.to_string()),
                msg: "Input should have at most the configured maximum length",
                input: raw.to_string(),
            });
        }
    }
    if let (Ok(Some(pattern)), Some(s)) = (validators.get_item("pattern").map(|x| x.and_then(|v| v.extract::<String>().ok())), &str_value) {
        // Use Python's `re.fullmatch` so behavior matches FastAPI/Pydantic exactly.
        let re_mod = py.import_bound("re").ok();
        if let Some(re_mod) = re_mod {
            if let Ok(fm) = re_mod.getattr("fullmatch") {
                if let Ok(result) = fm.call1((pattern.clone(), s.clone())) {
                    if result.is_none() {
                        return Err(ValidationErrorEntry {
                            err_type: "string_pattern_mismatch",
                            loc: (source_static, name.to_string()),
                            msg: "String should match the configured pattern",
                            input: raw.to_string(),
                        });
                    }
                }
            }
        }
    }
    Ok(())
}

impl ValidationErrorEntry {
    fn missing(source: &str, name: &str) -> Self {
        let source_static: &'static str = match source {
            "path" => "path",
            "query" => "query",
            "header" => "header",
            "cookie" => "cookie",
            "body" => "body",
            _ => "query",
        };
        Self {
            err_type: "missing",
            loc: (source_static, name.to_string()),
            msg: "Field required",
            input: String::new(),
        }
    }
}
