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

    let app_arc = Arc::new(PyAppHandle { app });

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

struct PyAppHandle {
    app: PyObject,
}

unsafe impl Send for PyAppHandle {}
unsafe impl Sync for PyAppHandle {}

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
    let method = req.method().as_str().to_ascii_uppercase();
    let path = req.uri().path().to_string();
    let query = req.uri().query().unwrap_or("").to_string();

    // WebSocket upgrade short-circuits the normal dispatch — hand off to the
    // ws module which does the 101 handshake + drives frames.
    if crate::ws::is_websocket_upgrade(&req) {
        let app_obj = Python::with_gil(|py| app.app.clone_ref(py));
        return crate::ws::handle_websocket(req, app_obj, path).await;
    }

    let mut headers: Vec<(String, String)> = Vec::with_capacity(12);
    for (name, value) in req.headers() {
        let v = match value.to_str() {
            Ok(s) => s.to_string(),
            Err(_) => continue,
        };
        headers.push((name.as_str().to_string(), v));
    }

    let body_bytes: Bytes = match req.into_body().collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => Bytes::new(),
    };

    let result = python_dispatch(&app, &method, &path, &query, &headers, body_bytes);

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
