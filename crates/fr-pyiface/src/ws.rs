//! Native WebSocket support for `run_native()`.
//!
//! Architecture:
//!   * Hyper detects the `Upgrade: websocket` request and we do the HTTP/1.1
//!     101 handshake ourselves (Sec-WebSocket-Accept = base64(sha1(key+magic))).
//!   * After upgrade, the TCP stream is wrapped with tokio-tungstenite so we
//!     get framed read/write halves.
//!   * Two tokio tasks per connection:
//!     - **read loop**: pulls frames from the WS, dispatches them onto a
//!       Python `asyncio.Queue` so the user handler can `await ws.receive_*`.
//!     - **write loop**: drains a tokio `mpsc` channel of outbound frames the
//!       Python handler queued via `await ws.send_*`.
//!   * The user's `async def ws_handler(ws):` runs on the persistent worker
//!     loop (same one HTTP async handlers use). It gets a `NativeWebSocket`
//!     Python wrapper that talks to the read/write tasks via the channels
//!     above. When the handler returns or raises, both loops terminate.

use base64::Engine;
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use http_body_util::Full;
use hyper::body::Incoming;
use hyper::header::{
    CONNECTION, SEC_WEBSOCKET_ACCEPT, SEC_WEBSOCKET_KEY, SEC_WEBSOCKET_VERSION, UPGRADE,
};
use hyper::upgrade::Upgraded;
use hyper::{Request as HyperRequest, Response as HyperResponse, StatusCode};
use hyper_util::rt::TokioIo;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyModule, PyString};
use sha1::{Digest, Sha1};
use std::convert::Infallible;
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// Detect a WebSocket upgrade request. RFC 6455 requires:
///   * GET method, HTTP/1.1
///   * `Upgrade: websocket`
///   * `Connection: Upgrade`
///   * `Sec-WebSocket-Version: 13`
///   * `Sec-WebSocket-Key: <base64 16 bytes>`
pub fn is_websocket_upgrade(req: &HyperRequest<Incoming>) -> bool {
    if req.method() != hyper::Method::GET {
        return false;
    }
    let h = req.headers();
    let upgrade_ok = h
        .get(UPGRADE)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.eq_ignore_ascii_case("websocket"))
        .unwrap_or(false);
    let connection_ok = h
        .get(CONNECTION)
        .and_then(|v| v.to_str().ok())
        .map(|s| {
            s.to_ascii_lowercase()
                .split(',')
                .any(|p| p.trim() == "upgrade")
        })
        .unwrap_or(false);
    let version_ok = h
        .get(SEC_WEBSOCKET_VERSION)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim() == "13")
        .unwrap_or(false);
    let key_present = h.contains_key(SEC_WEBSOCKET_KEY);
    upgrade_ok && connection_ok && version_ok && key_present
}

/// Compute the `Sec-WebSocket-Accept` header value from the client's key.
fn ws_accept_key(client_key: &str) -> String {
    let mut hasher = Sha1::new();
    hasher.update(client_key.as_bytes());
    hasher.update(WS_GUID.as_bytes());
    let digest = hasher.finalize();
    base64::engine::general_purpose::STANDARD.encode(digest)
}

/// Build the 101 Switching Protocols response and arrange for the upgraded
/// stream to be handed off to the WebSocket-driving task once the response
/// has been sent.
pub async fn handle_websocket(
    req: HyperRequest<Incoming>,
    app: PyObject,
    path: String,
) -> Result<HyperResponse<Full<Bytes>>, Infallible> {
    let key = match req
        .headers()
        .get(SEC_WEBSOCKET_KEY)
        .and_then(|v| v.to_str().ok())
    {
        Some(k) => k.to_string(),
        None => return Ok(reject_upgrade()),
    };
    let accept = ws_accept_key(&key);

    let app_arc = Arc::new(WsAppHandle { app });

    // Look up the Python handler eagerly so we can reject early on 404.
    let handler = match Python::with_gil(|py| -> Option<PyObject> {
        let app_obj = app_arc.app.bind(py);
        let lookup = app_obj.getattr("_lookup_websocket").ok()?;
        let r = lookup.call1((path.as_str(),)).ok()?;
        if r.is_none() {
            None
        } else {
            Some(r.unbind())
        }
    }) {
        Some(h) => h,
        None => return Ok(reject_404()),
    };

    // Schedule the upgrade work to run AFTER hyper sends the 101 response.
    tokio::spawn(async move {
        match hyper::upgrade::on(req).await {
            Ok(upgraded) => drive_websocket(upgraded, app_arc, handler).await,
            Err(_) => {}
        }
    });

    let resp = HyperResponse::builder()
        .status(StatusCode::SWITCHING_PROTOCOLS)
        .header(UPGRADE, "websocket")
        .header(CONNECTION, "Upgrade")
        .header(SEC_WEBSOCKET_ACCEPT, accept)
        .body(Full::new(Bytes::new()))
        .unwrap();
    Ok(resp)
}

fn reject_upgrade() -> HyperResponse<Full<Bytes>> {
    HyperResponse::builder()
        .status(StatusCode::BAD_REQUEST)
        .body(Full::new(Bytes::from_static(b"missing Sec-WebSocket-Key")))
        .unwrap()
}

fn reject_404() -> HyperResponse<Full<Bytes>> {
    HyperResponse::builder()
        .status(StatusCode::NOT_FOUND)
        .body(Full::new(Bytes::from_static(b"no websocket route")))
        .unwrap()
}

struct WsAppHandle {
    app: PyObject,
}
unsafe impl Send for WsAppHandle {}
unsafe impl Sync for WsAppHandle {}

/// Outbound frames the Python handler queues via `await ws.send_*`.
#[derive(Debug)]
enum OutFrame {
    Text(String),
    Binary(Vec<u8>),
    Close { code: u16, reason: String },
}

#[pyclass]
struct WsSendHandle {
    tx: mpsc::UnboundedSender<OutFrame>,
}

#[pymethods]
impl WsSendHandle {
    fn send_text(&self, text: &str) -> PyResult<()> {
        self.tx
            .send(OutFrame::Text(text.to_string()))
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("websocket closed"))
    }
    fn send_bytes(&self, data: &Bound<'_, PyBytes>) -> PyResult<()> {
        self.tx
            .send(OutFrame::Binary(data.as_bytes().to_vec()))
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("websocket closed"))
    }
    fn close(&self, code: u16, reason: &str) -> PyResult<()> {
        self.tx
            .send(OutFrame::Close {
                code,
                reason: reason.to_string(),
            })
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("websocket closed"))
    }
}

async fn drive_websocket(upgraded: Upgraded, _app: Arc<WsAppHandle>, handler: PyObject) {
    let io = TokioIo::new(upgraded);
    let ws = WebSocketStream::from_raw_socket(
        io,
        tokio_tungstenite::tungstenite::protocol::Role::Server,
        None,
    )
    .await;
    let (mut sink, mut source) = ws.split();

    // Outbound channel: Python → Rust write task.
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<OutFrame>();

    // Build the Python wrapper + start the handler coroutine on the worker
    // loop. We need the asyncio.Queue (for inbound frames) the handler reads
    // from. The Python side stashes both into a NativeWebSocket instance.
    let in_queue: PyObject = match Python::with_gil(|py| -> PyResult<PyObject> {
        let ws_module = PyModule::import_bound(py, "hyperfastapi._ws")?;
        let make = ws_module.getattr("_native_create")?;
        let send_handle = WsSendHandle { tx: out_tx.clone() };
        let send_obj = Py::new(py, send_handle)?;
        let pair = make.call1((handler.clone_ref(py), send_obj))?;
        // Returns the asyncio.Queue we should push received frames into.
        let q: PyObject = pair.extract()?;
        Ok(q)
    }) {
        Ok(q) => q,
        Err(e) => {
            eprintln!("[ws] Python handler launch failed: {e}");
            return;
        }
    };

    // Read loop: Rust WS → Python asyncio.Queue.
    let in_queue_for_reader = Python::with_gil(|py| in_queue.clone_ref(py));
    let reader = tokio::spawn(async move {
        while let Some(msg) = source.next().await {
            let msg = match msg {
                Ok(m) => m,
                Err(_) => break,
            };
            let put = match msg {
                Message::Text(t) => Some(PyFramePayload::Text(t.to_string())),
                Message::Binary(b) => Some(PyFramePayload::Binary(b.to_vec())),
                Message::Close(_) => {
                    push_to_py_queue(&in_queue_for_reader, PyFramePayload::Disconnect);
                    break;
                }
                Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => None,
            };
            if let Some(frame) = put {
                push_to_py_queue(&in_queue_for_reader, frame);
            }
        }
        // Connection closed — let Python handler unblock on receive.
        push_to_py_queue(&in_queue_for_reader, PyFramePayload::Disconnect);
    });

    // Write loop: Python outbox → Rust WS.
    let writer = tokio::spawn(async move {
        while let Some(frame) = out_rx.recv().await {
            let send_result = match frame {
                OutFrame::Text(t) => sink.send(Message::Text(t.into())).await,
                OutFrame::Binary(b) => sink.send(Message::Binary(b.into())).await,
                OutFrame::Close { code, reason } => {
                    let frame = tokio_tungstenite::tungstenite::protocol::CloseFrame {
                        code:
                            tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode::from(
                                code,
                            ),
                        reason: reason.into(),
                    };
                    let _ = sink.send(Message::Close(Some(frame))).await;
                    break;
                }
            };
            if send_result.is_err() {
                break;
            }
        }
        let _ = sink.close().await;
    });

    let _ = tokio::join!(reader, writer);
}

enum PyFramePayload {
    Text(String),
    Binary(Vec<u8>),
    Disconnect,
}

/// Push a frame onto the Python-side asyncio.Queue. Crosses the runtime
/// boundary via `loop.call_soon_threadsafe(queue.put_nowait, msg)`.
fn push_to_py_queue(queue: &PyObject, payload: PyFramePayload) {
    let _ = Python::with_gil(|py| -> PyResult<()> {
        let q = queue.bind(py);
        let item = match payload {
            PyFramePayload::Text(t) => {
                let d = PyDict::new_bound(py);
                d.set_item("kind", PyString::new_bound(py, "text"))?;
                d.set_item("data", PyString::new_bound(py, &t))?;
                d.into_any().unbind()
            }
            PyFramePayload::Binary(b) => {
                let d = PyDict::new_bound(py);
                d.set_item("kind", PyString::new_bound(py, "bytes"))?;
                d.set_item("data", PyBytes::new_bound(py, &b))?;
                d.into_any().unbind()
            }
            PyFramePayload::Disconnect => {
                let d = PyDict::new_bound(py);
                d.set_item("kind", PyString::new_bound(py, "disconnect"))?;
                d.into_any().unbind()
            }
        };
        // queue is an asyncio.Queue but the put_nowait must be called on the
        // worker loop's thread; the Python wrapper's helper does this via
        // call_soon_threadsafe. We call its sync helper.
        let helper =
            PyModule::import_bound(py, "hyperfastapi._ws")?.getattr("_native_push_frame")?;
        helper.call1((q, item))?;
        Ok(())
    });
}

pub fn register_pyclass(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<WsSendHandle>()
}
