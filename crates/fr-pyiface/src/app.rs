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
    fn __call__(&self, py: Python<'_>, func: PyObject) -> PyObject {
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

    /// Phase B-1 dispatch — linear search by (method, path), call handler with
    /// no args, serialize result. Returns (status, headers, body).
    ///
    /// Note: Phase B-1 ignores request body and path/query/header params.
    /// Phase B-2 adds path matching, Phase B-3 adds query/header/cookie.
    #[pyo3(signature = (method, path, body = None))]
    fn _dispatch(
        &self,
        py: Python<'_>,
        method: String,
        path: String,
        body: Option<&Bound<'_, PyBytes>>,
    ) -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {
        let _ = body; // Phase B-2 will use it

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

        // Find a matching route. Two-pass: exact method first, then HEAD→GET fallback.
        let (handler, status_code, response_model);
        {
            let routes = self.routes.lock();
            let mut path_exists = false;
            let mut found: Option<usize> = None;

            for (i, r) in routes.iter().enumerate() {
                if r.path == path {
                    path_exists = true;
                    if r.method == method {
                        found = Some(i);
                        break;
                    }
                }
            }
            if found.is_none() && method == "HEAD" {
                for (i, r) in routes.iter().enumerate() {
                    if r.path == path && r.method == "GET" {
                        found = Some(i);
                        break;
                    }
                }
            }

            match found {
                Some(i) => {
                    let r = &routes[i];
                    handler = r.handler.clone_ref(py);
                    status_code = r.status_code.unwrap_or(200) as u16;
                    response_model = r.response_model.as_ref().map(|p| p.clone_ref(py));
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

        // Call the handler with no args (Phase B-1 limitation).
        let result = handler.call0(py)?;
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
    fn __call__(&self, py: Python<'_>, func: PyObject) -> PyObject {
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
            });
        }
        func
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
