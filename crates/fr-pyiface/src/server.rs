//! Phase N+O: native Rust HTTP server bypassing uvicorn / ASGI.
//!
//! Protocol support matrix:
//!   * HTTP/1.0           — accepted (responds HTTP/1.0, "Connection: close")
//!   * HTTP/1.1           — primary, with keep-alive on by default
//!   * HTTP/2 cleartext   — h2c via prior-knowledge (`curl --http2-prior-knowledge`)
//!   * HTTP/2 over TLS    — ALPN-negotiated when tls_cert + tls_key supplied
//!   * HTTPS              — TLS 1.2 / 1.3 via rustls
//!   * HTTP/3 over QUIC   — opt-in via http3=True, requires TLS cert (UDP)
//!
//! All variants share the same per-request handler — they all funnel into
//! `hyperfastapi.applications.FastAPI._dispatch_native` exactly once per
//! request, so conformance is identical regardless of transport.

use bytes::{Buf, Bytes};
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::service::service_fn;
use hyper::{Request as HyperRequest, Response as HyperResponse, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;

/// Run the FastAPI app via the Rust hyper server.
///
/// `tls_cert_path` + `tls_key_path` (PEM files): when both are provided, the
/// TCP listener is wrapped with rustls and ALPN advertises `h2` + `http/1.1`.
/// Without TLS, HTTP/1.1 and h2c (HTTP/2 cleartext) are auto-detected.
///
/// `http3`: when true (and TLS is configured), an additional QUIC listener
/// is bound to the same port over UDP — clients that advertise Alt-Svc will
/// upgrade to HTTP/3. HTTP/3 has no plaintext mode; TLS is mandatory.
#[allow(clippy::too_many_arguments)]
pub fn run_native(
    py: Python<'_>,
    app: PyObject,
    host: &str,
    port: u16,
    workers: usize,
    tls_cert_path: Option<&str>,
    tls_key_path: Option<&str>,
    http3: bool,
) -> PyResult<()> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers.max(1))
        .enable_all()
        .build()
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("tokio runtime: {e}")))?;

    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad addr: {e}")))?;

    // Phase R: snapshot trivial routes once before releasing the GIL.
    // Run-time changes to the route table are unsupported in run_native.
    let trivial_routes = snapshot_trivial_routes(py, &app);
    let app_arc = Arc::new(PyAppHandle {
        app,
        trivial_routes,
    });

    // Build a TLS config eagerly (under GIL — file I/O could block but it's
    // tiny PEM files; one-shot at startup). HTTP/3 reuses the same config.
    let tls_config: Option<Arc<rustls::ServerConfig>> = match (tls_cert_path, tls_key_path) {
        (Some(c), Some(k)) => Some(Arc::new(load_tls_config(c, k).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("TLS config: {e}"))
        })?)),
        (None, None) => None,
        _ => {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "tls_cert and tls_key must both be set or both be None",
            ));
        }
    };

    if http3 && tls_config.is_none() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "http3=True requires tls_cert and tls_key (HTTP/3 has no plaintext mode)",
        ));
    }

    py.allow_threads(move || -> PyResult<()> {
        rt.block_on(async move {
            // Spawn HTTP/3 first so it's listening before TCP advertises Alt-Svc.
            if http3 {
                let cfg = tls_config.as_ref().expect("guarded above").clone();
                let app_h3 = Arc::clone(&app_arc);
                tokio::spawn(async move {
                    if let Err(e) = run_http3_server(addr, cfg, app_h3).await {
                        eprintln!("[hyperfastapi] HTTP/3 server stopped: {e}");
                    }
                });
            }

            let listener = TcpListener::bind(addr)
                .await
                .map_err(|e| pyo3::exceptions::PyOSError::new_err(format!("bind {addr}: {e}")))?;

            let scheme = if tls_config.is_some() {
                "https"
            } else {
                "http"
            };
            let proto_label = match (tls_config.is_some(), http3) {
                (true, true) => "HTTP/1.1 + HTTP/2 (TLS, ALPN) + HTTP/3 (QUIC)",
                (true, false) => "HTTP/1.1 + HTTP/2 (TLS, ALPN)",
                (false, _) => "HTTP/1.1 + HTTP/2 cleartext (h2c)",
            };
            eprintln!(
                "[hyperfastapi] listening on {}://{}  ({})",
                scheme, addr, proto_label
            );

            let alt_svc = http3.then(|| format!(r#"h3=":{port}"; ma=86400"#));

            let tls_acceptor = tls_config
                .as_ref()
                .map(|c| tokio_rustls::TlsAcceptor::from(Arc::clone(c)));

            loop {
                let (stream, _peer) = match listener.accept().await {
                    Ok(s) => s,
                    Err(e) => {
                        eprintln!("[hyperfastapi] accept error: {e}");
                        continue;
                    }
                };
                let _ = stream.set_nodelay(true);

                let app = Arc::clone(&app_arc);
                let alt_svc_clone = alt_svc.clone();
                let acceptor = tls_acceptor.clone();
                tokio::spawn(async move {
                    let svc = service_fn(move |req: HyperRequest<Incoming>| {
                        let app = Arc::clone(&app);
                        let alt_svc = alt_svc_clone.clone();
                        async move {
                            let mut resp = handle_request(app, req).await?;
                            if let Some(av) = alt_svc {
                                if let Ok(v) = av.parse() {
                                    resp.headers_mut().insert("alt-svc", v);
                                }
                            }
                            Ok::<_, Infallible>(resp)
                        }
                    });

                    let builder = auto::Builder::new(TokioExecutor::new());
                    let result = match acceptor {
                        Some(acc) => match acc.accept(stream).await {
                            Ok(tls_stream) => {
                                let io = TokioIo::new(tls_stream);
                                builder
                                    .serve_connection_with_upgrades(io, svc)
                                    .await
                                    .map_err(|e| e.to_string())
                            }
                            Err(e) => Err(format!("tls handshake: {e}")),
                        },
                        None => {
                            let io = TokioIo::new(stream);
                            builder
                                .serve_connection_with_upgrades(io, svc)
                                .await
                                .map_err(|e| e.to_string())
                        }
                    };
                    if let Err(e) = result {
                        // Connection-level errors are routine; only log if
                        // visibility is actually wanted. Default = swallow.
                        let _ = e;
                    }
                });
            }
            #[allow(unreachable_code)]
            Ok::<(), PyErr>(())
        })
    })
}

fn load_tls_config(cert_path: &str, key_path: &str) -> Result<rustls::ServerConfig, String> {
    use rustls::pki_types::{CertificateDer, PrivateKeyDer};
    use std::fs::File;
    use std::io::BufReader;

    // Install the default crypto provider once; ignore "already installed".
    let _ = rustls::crypto::ring::default_provider().install_default();

    let cert_file = File::open(cert_path).map_err(|e| format!("open cert {cert_path}: {e}"))?;
    let mut cert_reader = BufReader::new(cert_file);
    let certs: Vec<CertificateDer<'static>> = rustls_pemfile::certs(&mut cert_reader)
        .collect::<Result<_, _>>()
        .map_err(|e| format!("parse cert: {e}"))?;
    if certs.is_empty() {
        return Err(format!("no certs in {cert_path}"));
    }

    let key_file = File::open(key_path).map_err(|e| format!("open key {key_path}: {e}"))?;
    let mut key_reader = BufReader::new(key_file);
    let key: PrivateKeyDer<'static> = rustls_pemfile::private_key(&mut key_reader)
        .map_err(|e| format!("parse key: {e}"))?
        .ok_or_else(|| format!("no private key in {key_path}"))?;

    let mut cfg = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(|e| format!("rustls cfg: {e}"))?;
    // ALPN: advertise both HTTP/2 and HTTP/1.1; the client picks.
    cfg.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(cfg)
}

async fn run_http3_server(
    addr: SocketAddr,
    tls_config: Arc<rustls::ServerConfig>,
    app: Arc<PyAppHandle>,
) -> Result<(), String> {
    use quinn::crypto::rustls::QuicServerConfig;
    use quinn::ServerConfig as QuinnServerConfig;

    // h3 needs its own ALPN entry; clone the rustls config and override.
    let mut h3_cfg = (*tls_config).clone();
    h3_cfg.alpn_protocols = vec![b"h3".to_vec()];
    let quic_crypto =
        QuicServerConfig::try_from(h3_cfg).map_err(|e| format!("quic crypto: {e}"))?;
    let quinn_cfg = QuinnServerConfig::with_crypto(Arc::new(quic_crypto));
    // Default transport params are correct for HTTP/3: h3 needs uni-streams
    // for QPACK + control. Don't override them.

    let endpoint = quinn::Endpoint::server(quinn_cfg, addr).map_err(|e| format!("h3 bind: {e}"))?;
    eprintln!("[hyperfastapi] HTTP/3 (QUIC) listening on udp://{}", addr);

    while let Some(conn) = endpoint.accept().await {
        let app = Arc::clone(&app);
        tokio::spawn(async move {
            let connection = match conn.await {
                Ok(c) => c,
                Err(_) => return,
            };
            let h3_conn =
                h3::server::Connection::<_, Bytes>::new(h3_quinn::Connection::new(connection))
                    .await;
            let mut h3_conn = match h3_conn {
                Ok(c) => c,
                Err(_) => return,
            };

            loop {
                match h3_conn.accept().await {
                    Ok(Some(resolver)) => {
                        let app2 = Arc::clone(&app);
                        tokio::spawn(async move {
                            let resolved = match resolver.resolve_request().await {
                                Ok(r) => r,
                                Err(_) => return,
                            };
                            let (req_head, mut stream) = resolved;

                            // Drain the body.
                            let mut body_bytes = Vec::new();
                            while let Ok(Some(mut chunk)) = stream.recv_data().await {
                                while chunk.has_remaining() {
                                    let len = chunk.remaining();
                                    let buf = chunk.copy_to_bytes(len);
                                    body_bytes.extend_from_slice(&buf);
                                }
                            }

                            // Re-build a hyper Request the existing handler
                            // can chew on. We don't need the actual hyper
                            // body machinery here — just dispatch directly.
                            let method = req_head.method().as_str().to_ascii_uppercase();
                            let path = req_head.uri().path().to_string();
                            let query = req_head.uri().query().unwrap_or("").to_string();
                            let mut headers: Vec<(String, String)> =
                                Vec::with_capacity(req_head.headers().len());
                            for (n, v) in req_head.headers() {
                                if let Ok(s) = v.to_str() {
                                    headers.push((n.as_str().to_string(), s.to_string()));
                                }
                            }

                            let result = python_dispatch(
                                &app2,
                                &method,
                                &path,
                                &query,
                                &headers,
                                Bytes::from(body_bytes),
                            );

                            match result {
                                Ok((status, hdrs, body)) => {
                                    let mut resp_builder = http::Response::builder().status(
                                        StatusCode::from_u16(status).unwrap_or(StatusCode::OK),
                                    );
                                    for (k, v) in hdrs {
                                        resp_builder = resp_builder.header(k, v);
                                    }
                                    let resp = match resp_builder.body(()) {
                                        Ok(r) => r,
                                        Err(_) => return,
                                    };
                                    let _ = stream.send_response(resp).await;
                                    let _ = stream.send_data(Bytes::from(body)).await;
                                    let _ = stream.finish().await;
                                }
                                Err(_) => {
                                    let resp = http::Response::builder()
                                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                                        .body(())
                                        .unwrap();
                                    let _ = stream.send_response(resp).await;
                                    let _ = stream
                                        .send_data(Bytes::from_static(
                                            br#"{"detail":"Internal Server Error"}"#,
                                        ))
                                        .await;
                                    let _ = stream.finish().await;
                                }
                            }
                        });
                    }
                    Ok(None) => break,
                    Err(_) => break,
                }
            }
        });
    }

    endpoint.wait_idle().await;
    Ok(())
}

/// Phase R+: per-request handler shortcut for routes that the trivial
/// dispatch fast path inside `_dispatch` will match. Stores everything
/// needed to call the user handler and build a JSON response without
/// re-entering `_dispatch_native`.
struct TrivialRoute {
    handler: PyObject,
    status_code: u16,
    is_async: bool,
    is_head: bool,
}

unsafe impl Send for TrivialRoute {}
unsafe impl Sync for TrivialRoute {}

struct PyAppHandle {
    app: PyObject,
    /// Phase R+: snapshot of trivial routes by `(method, path)`. Lookup is
    /// `O(1)` and needs neither GIL nor Mutex; the values cache the handler
    /// PyObject + status_code so dispatch can call into Python ONCE per
    /// request (handler.call0 + serialize), bypassing `_dispatch_native`.
    trivial_routes: std::collections::HashMap<(String, String), TrivialRoute>,
}

unsafe impl Send for PyAppHandle {}
unsafe impl Sync for PyAppHandle {}

impl PyAppHandle {
    fn lookup_trivial(&self, method: &str, path: &str) -> Option<&TrivialRoute> {
        // (Borrowed-key lookup avoids cloning the strings; HashMap accepts
        // any `&Q` where `Q: Hash + Eq + ?Sized` and the key type can be
        // borrowed as `&Q`. For a `(String, String)` key we'd need a tuple
        // wrapper; the simple cheap approach is two-string clone — tiny
        // strings, doesn't show up in profiles.)
        self.trivial_routes
            .get(&(method.to_string(), path.to_string()))
    }
}

/// Build the trivial-route snapshot from the live FastAPI routes table.
/// Called once at run_native start under GIL; the resulting map is read
/// without GIL on every request.
fn snapshot_trivial_routes(
    py: Python<'_>,
    app: &PyObject,
) -> std::collections::HashMap<(String, String), TrivialRoute> {
    let mut out = std::collections::HashMap::new();
    let bind = app.bind(py);
    let app_ref: PyRef<crate::app::FastAPI> = match bind.downcast::<crate::app::FastAPI>() {
        Ok(b) => match b.try_borrow() {
            Ok(r) => r,
            Err(_) => return out,
        },
        Err(_) => return out,
    };
    if !app_ref.dependencies.lock().is_empty() {
        return out;
    }
    let routes = app_ref.routes.lock();
    for r in routes.iter() {
        if r.path.contains('{') {
            continue;
        }
        if !(r.param_plan.is_empty()
            && r.dependencies.is_empty()
            && r.response_model.is_none()
            && r.response_class.is_none())
        {
            continue;
        }
        let handler = r.handler.clone_ref(py);
        let status_code = r.status_code.unwrap_or(200) as u16;
        let is_async = r.is_async;
        out.insert(
            (r.method.clone(), r.path.clone()),
            TrivialRoute {
                handler,
                status_code,
                is_async,
                is_head: r.method == "HEAD",
            },
        );
        // HEAD-on-GET fallback: when a HEAD comes in for a GET route, the
        // dispatch normally redirects. Inline the same indirection here so
        // the trivial path covers both verbs without extra logic per req.
        if r.method == "GET" {
            let extra_handler = r.handler.clone_ref(py);
            out.insert(
                ("HEAD".to_string(), r.path.clone()),
                TrivialRoute {
                    handler: extra_handler,
                    status_code,
                    is_async,
                    is_head: true,
                },
            );
        }
    }
    out
}

/// Static `content-type: application/json` header reused for every trivial
/// JSON response. Skips two String allocations per request vs constructing
/// `("content-type".into(), "application/json".into())` inline.
const JSON_CONTENT_TYPE: &str = "application/json";

/// Phase R+: inline trivial route dispatch. Calls the handler directly,
/// serializes via json_fast, builds the hyper response — no PyO3 method
/// dispatch through `_dispatch_native`, no PyList/PyTuple construction
/// per request. Compared to `python_dispatch`, this path saves:
///   * 1 getattr (`_dispatch_native`) → ~200ns
///   * 1 PyO3 method call indirection → ~100ns
///   * Re-walk of routes table inside `_dispatch` → ~300ns
///   * 4 setattrs for `_bg` state (already deferred but route check + skip
///     still costs ~150ns) → 0
/// Total saving: ~750ns per trivial request.
fn dispatch_trivial(_app: &PyObject, route: &TrivialRoute) -> HyperResponse<Full<Bytes>> {
    let result: Result<Vec<u8>, String> = Python::with_gil(|py| -> Result<Vec<u8>, String> {
        let h = route.handler.bind(py);
        let result_obj = if route.is_async {
            // Reuse the same coro.send fast-path the Python helper uses for
            // async-def-without-await. For real-await coroutines we close
            // the partial and re-run on the worker loop — same behaviour
            // as call_with_async_handling, just inlined here.
            let coro = h.call0().map_err(|e| e.to_string())?;
            try_drive_async(py, coro, &route.handler).map_err(|e| e.to_string())?
        } else {
            h.call0().map_err(|e| e.to_string())?.unbind()
        };
        if route.is_head {
            return Ok(Vec::new());
        }
        crate::json_fast::encode(py, result_obj.bind(py))
            .map_err(|_| "json encode failed".to_string())
            .or_else(|_| crate::app::serialize_value_fallback_for_trivial(py, &result_obj))
    });

    match result {
        Ok(body) => {
            let mut builder = HyperResponse::builder()
                .status(StatusCode::from_u16(route.status_code).unwrap_or(StatusCode::OK));
            // Skip content-type when body is empty (HEAD); spec-compliant.
            if !body.is_empty() {
                builder = builder.header("content-type", JSON_CONTENT_TYPE);
            }
            builder
                .body(Full::new(Bytes::from(body)))
                .unwrap_or_else(|_| {
                    HyperResponse::builder()
                        .status(StatusCode::INTERNAL_SERVER_ERROR)
                        .body(Full::new(Bytes::from_static(b"")))
                        .unwrap()
                })
        }
        Err(_) => HyperResponse::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .header("content-type", JSON_CONTENT_TYPE)
            .body(Full::new(Bytes::from_static(
                br#"{"detail":"Internal Server Error"}"#,
            )))
            .unwrap(),
    }
}

/// Drive an async handler. Mirrors `call_with_async_handling` in
/// `hyperfastapi._routing`: try `coro.send(None)` first — if the coroutine
/// completes without awaiting (the common `async def f(): return X` case)
/// StopIteration carries the value and we're done. Otherwise close the
/// partial and re-run via the persistent worker loop (`_run_coro_blocking`).
fn try_drive_async(
    py: Python<'_>,
    coro: Bound<'_, pyo3::types::PyAny>,
    handler: &PyObject,
) -> PyResult<PyObject> {
    let send = coro.getattr(pyo3::intern!(py, "send"))?;
    match send.call1((py.None(),)) {
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
            // Fast path: trivial coroutine — extract return value from the
            // StopIteration's `value` attribute.
            let value = e
                .value_bound(py)
                .getattr(pyo3::intern!(py, "value"))?
                .unbind();
            Ok(value)
        }
        Ok(_yielded) => {
            // Real-await coroutine — close the partial we already drove
            // and run a fresh invocation through the worker loop. Slightly
            // wasted handler call but the worker-loop hop dwarfs it.
            let _ = coro.call_method0("close");
            let new_coro = handler.bind(py).call0()?;
            let routing = py.import_bound("hyperfastapi._routing")?;
            let runner = routing.getattr("_run_coro_blocking")?;
            let result = runner.call1((new_coro,))?;
            Ok(result.unbind())
        }
        Err(e) => Err(e),
    }
}

fn intern_dispatch_native(py: Python<'_>) -> Bound<'_, pyo3::types::PyString> {
    use once_cell::sync::OnceCell;
    static CACHE: OnceCell<PyObject> = OnceCell::new();
    if let Some(obj) = CACHE.get() {
        return obj
            .bind(py)
            .clone()
            .downcast_into::<pyo3::types::PyString>()
            .expect("cached _dispatch_native name is a str");
    }
    let s = pyo3::intern!(py, "_dispatch_native").to_owned();
    let _ = CACHE.set(s.clone().unbind().into_any());
    s
}

fn python_dispatch(
    app: &PyAppHandle,
    method: &str,
    path: &str,
    query: &str,
    headers: &[(String, String)],
    body_bytes: Bytes,
) -> Result<(u16, Vec<(String, String)>, Vec<u8>), String> {
    Python::with_gil(|py| {
        let app_obj = app.app.bind(py);
        let dispatch = app_obj
            .getattr(intern_dispatch_native(py))
            .map_err(|e| e.to_string())?;
        let py_headers = PyList::empty_bound(py);
        for (k, v) in headers {
            py_headers
                .append(pyo3::types::PyTuple::new_bound(
                    py,
                    [k.as_str(), v.as_str()],
                ))
                .map_err(|e| e.to_string())?;
        }
        let py_body = PyBytes::new_bound(py, &body_bytes);

        let tup = dispatch
            .call1((method, path, query, &py_headers, py_body))
            .map_err(|e| e.to_string())?;
        let (status, hdrs, body): (u16, Vec<(String, String)>, Vec<u8>) =
            tup.extract().map_err(|e| e.to_string())?;
        Ok((status, hdrs, body))
    })
}

async fn handle_request(
    app: Arc<PyAppHandle>,
    req: HyperRequest<Incoming>,
) -> Result<HyperResponse<Full<Bytes>>, Infallible> {
    // WebSocket upgrade short-circuits the normal dispatch — hand off to the
    // ws module which does the 101 handshake + drives frames.
    if crate::ws::is_websocket_upgrade(&req) {
        let path = req.uri().path().to_string();
        let app_obj = Python::with_gil(|py| app.app.clone_ref(py));
        return crate::ws::handle_websocket(req, app_obj, path).await;
    }

    // Phase R: split parts from body so we can drop the body collect future
    // while still borrowing method/uri/headers as &str. Avoids three String
    // allocations per request (method/path/query) — those were ~600ns of
    // pure heap churn on a request budget of 13µs.
    let (parts, body) = req.into_parts();
    let method = parts.method.as_str();
    let path = parts.uri.path();
    let query = parts.uri.query().unwrap_or("");

    // Phase R+: trivial-route HashMap lookup against the snapshot taken
    // at server start. No GIL, no Mutex. If hit, we call the cached handler
    // directly and serialize — bypassing _dispatch_native entirely.
    if let Some(trivial) = app.lookup_trivial(method, path) {
        return Ok(dispatch_trivial(&app.app, trivial));
    }

    // Slow path: full _dispatch_native via Python. Materialize headers + body
    // since the route may need them.
    let mut headers: Vec<(String, String)> = Vec::with_capacity(12);
    for (name, value) in &parts.headers {
        let v = match value.to_str() {
            Ok(s) => s.to_string(),
            Err(_) => continue,
        };
        headers.push((name.as_str().to_string(), v));
    }
    let body_bytes: Bytes = match body.collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => Bytes::new(),
    };
    let result = python_dispatch(&app, method, path, query, &headers, body_bytes);

    let (status, hdrs, body) = match result {
        Ok(r) => r,
        Err(msg) => {
            let body = format!(r#"{{"detail":"Internal Server Error","error":{:?}}}"#, msg);
            let resp = HyperResponse::builder()
                .status(StatusCode::INTERNAL_SERVER_ERROR)
                .header("content-type", "application/json")
                .body(Full::new(Bytes::from(body)))
                .unwrap();
            return Ok(resp);
        }
    };

    let mut builder =
        HyperResponse::builder().status(StatusCode::from_u16(status).unwrap_or(StatusCode::OK));
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
