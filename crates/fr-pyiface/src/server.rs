//! Phase N: native Rust HTTP server bypassing uvicorn / ASGI for max-perf.
//!
//! Architecture:
//!   * `tokio` multi-thread runtime, 1 thread per logical core (configurable)
//!   * `hyper 1.x` HTTP/1.1 server, accept loop on TCP listener
//!   * Per-request: parse method + path + headers + body in Rust, then enter
//!     Python ONCE via `Python::with_gil` to invoke the existing
//!     `FastAPI._dispatch` → handler chain.
//!   * Response: status + headers + bytes back through hyper, no ASGI shim.
//!
//! Why this beats uvicorn at the same RPS budget:
//!   - No ASGI scope dict construction per request
//!   - No Starlette TestClient / asgiref portal hop
//!   - No middleware stack churn (CORS / GZip / ServerError / Exception each
//!     wrap the inner app even when they're a no-op for the request)
//!   - Handler call & JSON serialize are still in Python+Rust, but the
//!     enter/leave cost is one GIL-acquire instead of three.
//!
//! Entry point: `app.run_native(host="127.0.0.1", port=8000, workers=N)` from
//! Python. Blocks the calling thread until ctrl-C / `tokio::signal::ctrl_c()`.

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request as HyperRequest, Response as HyperResponse, StatusCode};
use hyper_util::rt::TokioIo;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;

/// Run the FastAPI app via hyper. Blocks the calling Python thread (releases
/// the GIL during accept loop; only re-acquires per request). Returns when
/// ctrl-C is received or when the listener errors out.
pub fn run_native(
    py: Python<'_>,
    app: PyObject,
    host: &str,
    port: u16,
    workers: usize,
) -> PyResult<()> {
    // Build a multi-thread tokio runtime sized to `workers`. Threads spawn
    // independently of the Python interpreter; only the request handler
    // blocks on the GIL.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers.max(1))
        .enable_all()
        .build()
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("tokio runtime: {e}")))?;

    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad addr: {e}")))?;

    let app_arc = Arc::new(PyAppHandle { app });

    // Release the GIL while the server runs; the per-request handler will
    // re-acquire it when it needs to call into Python.
    py.allow_threads(move || -> PyResult<()> {
        rt.block_on(async move {
            let listener = TcpListener::bind(addr).await.map_err(|e| {
                pyo3::exceptions::PyOSError::new_err(format!("bind {addr}: {e}"))
            })?;
            eprintln!("[fastapi_rust] listening on http://{}", addr);

            loop {
                let (stream, _peer) = match listener.accept().await {
                    Ok(s) => s,
                    Err(e) => {
                        eprintln!("[fastapi_rust] accept error: {e}");
                        continue;
                    }
                };

                // Disable Nagle for low-latency benchmarks (avoids 40ms ack
                // delay on small payloads under low RPS).
                let _ = stream.set_nodelay(true);

                let app = Arc::clone(&app_arc);
                tokio::spawn(async move {
                    let io = TokioIo::new(stream);
                    let svc = service_fn(move |req: HyperRequest<Incoming>| {
                        let app = Arc::clone(&app);
                        async move { handle_request(app, req).await }
                    });
                    if let Err(e) = http1::Builder::new()
                        .keep_alive(true)
                        .serve_connection(io, svc)
                        .await
                    {
                        // Connection errors are routine (client closed early
                        // etc.); only log if you really want visibility.
                        let _ = e;
                    }
                });
            }
            #[allow(unreachable_code)]
            Ok::<(), PyErr>(())
        })
    })
}

struct PyAppHandle {
    app: PyObject,
}

// PyObject is Send + !Sync; we wrap it under Arc so the per-connection
// task can share a borrow. All Python access goes through Python::with_gil.
unsafe impl Send for PyAppHandle {}
unsafe impl Sync for PyAppHandle {}

/// Cache the interned `_dispatch_native` Python str so getattr on every
/// request hits the cached method-resolution-order slot instead of doing a
/// fresh string→hash on each call.
fn intern_dispatch_native(py: Python<'_>) -> Bound<'_, pyo3::types::PyString> {
    use once_cell::sync::OnceCell;
    static CACHE: OnceCell<PyObject> = OnceCell::new();
    if let Some(obj) = CACHE.get() {
        return obj.bind(py).clone().downcast_into::<pyo3::types::PyString>()
            .expect("cached _dispatch_native name is a str");
    }
    let s = pyo3::intern!(py, "_dispatch_native").to_owned();
    let _ = CACHE.set(s.clone().unbind().into_any());
    s
}

async fn handle_request(
    app: Arc<PyAppHandle>,
    req: HyperRequest<Incoming>,
) -> Result<HyperResponse<Full<Bytes>>, Infallible> {
    let method = req.method().as_str().to_ascii_uppercase();
    let path = req.uri().path().to_string();
    let query = req.uri().query().unwrap_or("").to_string();

    // Materialize headers into Vec<(String, String)>. Conservative O(N) copy;
    // ~95% of requests have <12 headers, so capacity hint is small.
    let mut headers: Vec<(String, String)> = Vec::with_capacity(12);
    for (name, value) in req.headers() {
        let v = match value.to_str() {
            Ok(s) => s.to_string(),
            Err(_) => continue, // skip non-utf8 values; rare
        };
        headers.push((name.as_str().to_string(), v));
    }

    // Drain body to bytes. For small POST bodies this is one chunk; for
    // larger ones we accumulate. Hard cap should come from app limits;
    // for now, trust the client.
    let body_bytes: Bytes = match req.into_body().collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => Bytes::new(),
    };

    // Enter Python ONCE. Call the existing Rust dispatch (`_dispatch` on the
    // FastAPI PyClass) — this gives us the same conformance guarantees as
    // the ASGI path: routing, deps, validation, handler, response_model,
    // JSON serialize. The only thing we skip is the ASGI middleware stack
    // (CORS, GZip, etc.) — for max-perf benchmarks that's the point.
    let result: Result<(u16, Vec<(String, String)>, Vec<u8>), String> =
        Python::with_gil(|py| {
            let app_obj = app.app.bind(py);
            // Single fused entry point — _dispatch_native does bg reset + the
            // full _dispatch in one PyO3 call (saves a getattr + a call vs
            // two separate Python entries).
            let dispatch = app_obj
                .getattr(intern_dispatch_native(py))
                .map_err(|e| e.to_string())?;
            let py_headers = PyList::empty_bound(py);
            for (k, v) in &headers {
                py_headers
                    .append(pyo3::types::PyTuple::new_bound(py, [k.as_str(), v.as_str()]))
                    .map_err(|e| e.to_string())?;
            }
            let py_body = PyBytes::new_bound(py, &body_bytes);

            let tup = dispatch
                .call1((method.as_str(), path.as_str(), query.as_str(), &py_headers, py_body))
                .map_err(|e| e.to_string())?;
            let (status, hdrs, body): (u16, Vec<(String, String)>, Vec<u8>) =
                tup.extract().map_err(|e| e.to_string())?;
            Ok((status, hdrs, body))
        });

    let (status, hdrs, body) = match result {
        Ok(r) => r,
        Err(msg) => {
            // Handler raised — render a generic 500. Real apps will have
            // registered exception handlers via the ASGI path; this fallback
            // is just so the connection doesn't hang.
            let body = format!(r#"{{"detail":"Internal Server Error","error":{:?}}}"#, msg);
            let resp = HyperResponse::builder()
                .status(StatusCode::INTERNAL_SERVER_ERROR)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
            return Ok(resp);
        }
    };

    let mut builder = HyperResponse::builder()
        .status(StatusCode::from_u16(status).unwrap_or(StatusCode::OK));
    for (k, v) in hdrs {
        builder = builder.header(k, v);
    }
    let resp = builder
        .body(Full::new(Bytes::from(body)))
        .unwrap_or_else(|_| {
            HyperResponse::builder()
                .status(StatusCode::INTERNAL_SERVER_ERROR)
                .body(Full::new(Bytes::from_static(b"")))
                .unwrap()
        });
    Ok(resp)
}
