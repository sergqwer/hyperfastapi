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
}

#[pymethods]
impl RouteDecorator {
    fn __call__(&self, py: Python<'_>, func: PyObject) -> PyResult<PyObject> {
        let plan = compile_route_plan(py, &func, &self.path).unwrap_or_default();
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
        });
        Ok(func)
    }
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

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn get(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("GET", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn post(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("POST", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn put(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("PUT", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn delete(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("DELETE", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn patch(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("PATCH", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn options(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("OPTIONS", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn head(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("HEAD", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
    }

    #[pyo3(signature = (path, *, status_code = None, tags = None, deprecated = false, include_in_schema = true, summary = None, description = None, response_model = None, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn trace(&self, path: String, status_code: Option<i32>, tags: Option<Vec<String>>, deprecated: bool, include_in_schema: bool, summary: Option<String>, description: Option<String>, response_model: Option<PyObject>, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("TRACE", path, status_code, response_model, tags, deprecated, include_in_schema, summary, description)
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
        }
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

    #[pyo3(signature = (path, endpoint, *, methods = None, **_kwargs))]
    fn add_api_route(&self, py: Python<'_>, path: String, endpoint: PyObject, methods: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
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
            });
        }
        Ok(())
    }

    #[pyo3(signature = (router, *, prefix = "".to_string(), tags = None, include_in_schema = true, **_kwargs))]
    fn include_router(&self, py: Python<'_>, router: PyRef<'_, APIRouter>, prefix: String, tags: Option<Vec<String>>, include_in_schema: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
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
                if !merged_tags.contains(t) { merged_tags.push(t.clone()); }
            }
            for t in &extra_tags {
                if !merged_tags.contains(t) { merged_tags.push(t.clone()); }
            }

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
        // Build paths from registered routes (Phase H expands this).
        let paths_dict = PyDict::new_bound(py);
        for r in self.routes.lock().iter() {
            if !r.include_in_schema || r.method == "WEBSOCKET" { continue; }
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
            path_item.set_item(r.method.to_lowercase(), op)?;
            paths_dict.set_item(&r.path, path_item)?;
        }
        dict.set_item("paths", paths_dict)?;
        let components = PyDict::new_bound(py);
        components.set_item("schemas", PyDict::new_bound(py))?;
        dict.set_item("components", components)?;
        Ok(dict.unbind().into())
    }

    /// Phase B-3 dispatch. Linear-search routes by template, extract params
    /// from path/query/header/cookie based on the compiled plan, cast types,
    /// validate, call handler, serialize response.
    ///
    /// `query_string`: raw bytes-as-str (we URL-decode via `urllib.parse.parse_qs`).
    /// `headers`: list of (name, value) tuples from ASGI scope.
    /// `body`: bytes of the request body (Phase C wires body parsing).
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
        let _ = body; // Phase C will use it

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
        let (handler, status_code, response_model, param_plan, path_params);
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

        // Build kwargs from the param plan, accumulating validation errors.
        let kwargs = PyDict::new_bound(py);
        let mut errors: Vec<ValidationErrorEntry> = Vec::new();
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
            ) {
                Ok(ParamExtraction::Value { name, value }) => {
                    kwargs.set_item(name, value)?;
                }
                Ok(ParamExtraction::UseDefault) => {}
                Err(err) => errors.push(err),
            }
        }

        if !errors.is_empty() {
            return build_validation_error_response(py, &errors);
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
        let response_class = starlette_responses.getattr("Response")?;
        if result.bind(py).is_instance(&response_class)? {
            return extract_response_object(py, &result, is_head);
        }

        // 204 / 304 / 1xx forbid bodies — even if the handler returned a value,
        // we send empty.
        if is_no_content_status(status_code) {
            return Ok((status_code, vec![], Vec::new()));
        }

        // response_model filtering: validate + dump via Pydantic.
        let body = if let Some(model_class) = response_model {
            let validated = model_class.call_method1(py, "model_validate", (result,))?;
            let json_str: String = validated
                .call_method0(py, "model_dump_json")?
                .extract(py)?;
            json_str.into_bytes()
        } else {
            // Plain serialization via Python json.dumps + jsonable_encoder.
            serialize_value_to_json(py, &result)?
        };

        let headers = vec![("content-type".into(), "application/json".into())];
        let body = if is_head { Vec::new() } else { body };
        Ok((status_code, headers, body))
    }
}

impl FastAPI {
    #[allow(clippy::too_many_arguments)]
    fn make_decorator(
        &self,
        method: &str,
        path: String,
        status_code: Option<i32>,
        response_model: Option<PyObject>,
        tags: Option<Vec<String>>,
        deprecated: bool,
        include_in_schema: bool,
        summary: Option<String>,
        description: Option<String>,
    ) -> RouteDecorator {
        RouteDecorator {
            method: method.to_string(),
            path,
            routes: self.routes.clone(),
            status_code,
            response_model,
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
    fn new(prefix: String, tags: Option<Vec<String>>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self {
            prefix: Mutex::new(prefix),
            tags: Mutex::new(tags.unwrap_or_default()),
            routes: Arc::new(Mutex::new(Vec::with_capacity(8))),
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

    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn get(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("GET", path, response_model, tags, include_in_schema, status_code, deprecated)
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn post(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("POST", path, response_model, tags, include_in_schema, status_code, deprecated)
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn put(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("PUT", path, response_model, tags, include_in_schema, status_code, deprecated)
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn delete(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("DELETE", path, response_model, tags, include_in_schema, status_code, deprecated)
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn patch(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("PATCH", path, response_model, tags, include_in_schema, status_code, deprecated)
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn options(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("OPTIONS", path, response_model, tags, include_in_schema, status_code, deprecated)
    }
    #[pyo3(signature = (path, *, tags = None, response_model = None, include_in_schema = true, status_code = None, deprecated = false, **_kwargs))]
    #[allow(clippy::too_many_arguments)]
    fn head(&self, path: String, tags: Option<Vec<String>>, response_model: Option<PyObject>, include_in_schema: bool, status_code: Option<i32>, deprecated: bool, _kwargs: Option<&Bound<'_, PyDict>>) -> RouteDecorator {
        self.make_decorator("HEAD", path, response_model, tags, include_in_schema, status_code, deprecated)
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
            });
        }
        Ok(())
    }
}

impl APIRouter {
    #[allow(clippy::too_many_arguments)]
    fn make_decorator(
        &self,
        method: &str,
        path: String,
        response_model: Option<PyObject>,
        tags: Option<Vec<String>>,
        include_in_schema: bool,
        status_code: Option<i32>,
        deprecated: bool,
    ) -> RouteDecorator {
        RouteDecorator {
            method: method.to_string(),
            path,
            routes: self.routes.clone(),
            status_code,
            response_model,
            tags: tags.unwrap_or_default(),
            deprecated,
            include_in_schema,
            summary: None,
            description: None,
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
        d.set_item("input", &self.input)?;
        Ok(d)
    }
}

fn build_validation_error_response(
    py: Python<'_>,
    errors: &[ValidationErrorEntry],
) -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {
    let detail = pyo3::types::PyList::empty_bound(py);
    for err in errors {
        detail.append(err.to_py_dict(py)?)?;
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
fn extract_one_param(
    py: Python<'_>,
    spec: &Bound<'_, PyDict>,
    path_params: &[(String, String)],
    query: &Bound<'_, PyDict>,
    headers: &Bound<'_, PyDict>,
    cookies: &Bound<'_, PyDict>,
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

    // Skip Phase D/C-only sources (depends, body) — Phase B-3 doesn't extract them.
    if source == "depends" || source == "body" || source == "form" || source == "file" || source == "security" {
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
