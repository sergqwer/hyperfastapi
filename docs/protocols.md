# Protocol support

`hyperfastapi`'s native server (`app.run_native()`) speaks every modern HTTP
flavor over a single command. Below is the current support matrix and how
each protocol gets enabled.

## Matrix

| Protocol             | Transport | Status        | Notes                                          |
| -------------------- | --------- | ------------- | ---------------------------------------------- |
| **HTTP/1.0**         | TCP       | ✅ Supported  | Forced via `--http1.0`; `Connection: close`    |
| **HTTP/1.1**         | TCP       | ✅ Supported  | Default; **keep-alive on**                     |
| **Keep-alive**       | TCP       | ✅ Supported  | Connection reused for back-to-back requests    |
| **HTTP/2 cleartext** | TCP (h2c) | ✅ Supported  | Auto-detected when client sends h2 preface     |
| **HTTPS**            | TLS / TCP | ✅ Supported  | rustls 0.23, TLS 1.2 + 1.3                     |
| **HTTP/2 + TLS**     | TLS / TCP | ✅ Supported  | ALPN-negotiated alongside http/1.1             |
| **HTTP/3**           | QUIC/UDP  | ✅ Supported  | quinn 0.11 + h3 0.0.8; opt-in via `http3=True` |

All variants funnel through the same Python handler — same conformance,
same dependency injection, same Pydantic validation.

## Enabling each protocol

### HTTP/1.x + HTTP/2 cleartext (default, no TLS)

```python
app.run_native(host="0.0.0.0", port=8000, workers=4)
```

* HTTP/1.0 / 1.1: any standard client
* HTTP/2 cleartext (h2c): clients that support **prior-knowledge** mode
  (`curl --http2-prior-knowledge`, gRPC clients, internal service-to-service)

### HTTPS (HTTP/1.1 + HTTP/2 over TLS)

```python
app.run_native(
    host="0.0.0.0", port=8443, workers=4,
    tls_cert="/path/to/cert.pem",
    tls_key="/path/to/key.pem",
)
```

ALPN advertises both `h2` and `http/1.1`; the client picks. Modern browsers
always pick HTTP/2.

### HTTPS + HTTP/3 (QUIC)

```python
app.run_native(
    host="0.0.0.0", port=8443, workers=4,
    tls_cert="/path/to/cert.pem",
    tls_key="/path/to/key.pem",
    http3=True,  # opens an additional UDP listener on port 8443
)
```

When `http3=True`:

* TCP+TLS handles HTTP/1.1 + HTTP/2 as above.
* UDP+QUIC handles HTTP/3 connections.
* HTTPS responses include an **`alt-svc: h3=":8443"; ma=86400`** header so
  HTTP/3-aware clients (modern browsers, `curl --http3`) automatically
  upgrade to QUIC for subsequent requests.
* Note: HTTP/3 has **no plaintext mode**. TLS is mandatory.

## Verifying it works

```bash
# Generate a self-signed cert for local testing:
python tests/perf/gen_self_signed.py

# Run the protocol smoke tests:
python tests/perf/test_protocols.py
```

Expected output (Windows; equivalent on Linux/macOS):

```
============================================================
hyperfastapi protocol matrix
============================================================

--- plaintext on :8765 ---
  [OK  ] HTTP/1.1: version=HTTP/1.1 body={"ok":true}
  [OK  ] HTTP/1.0: curl -> 1|200
  [OK  ] HTTP/1.1 keep-alive: 2 GETs on pooled client OK

--- TLS on :8443 ---
  [OK  ] HTTPS / HTTP/1.1: version=HTTP/1.1
  [OK  ] HTTPS / HTTP/2 (ALPN): version=HTTP/2

--- HTTP/3 (QUIC) on :8443 ---
  [OK  ] HTTP/3: GET /plain over QUIC
```

## Production deployment notes

* **Real TLS certs**: terminate with Let's Encrypt or your CA. Pass the
  cert + key paths directly; rustls handles loading.
* **HTTP/3 over WAN**: requires UDP port open at the firewall. Many
  hosting providers gate UDP — check before enabling.
* **HTTP/2 cleartext (h2c)**: production deployments behind a reverse
  proxy where h2c is the upstream protocol (e.g. Envoy → service). Don't
  expose h2c to the open internet — most browsers refuse h2c entirely.
* **Keep-alive timeout**: hyper defaults to 75s server-side. Override via
  custom builder once we expose more `run_native` options.

## Implementation

* TCP listener: `tokio::net::TcpListener`, `TCP_NODELAY` enabled
* HTTP/1+2 dispatch: `hyper_util::server::conn::auto::Builder`
  (auto-detects HTTP/1.1 vs HTTP/2 from the client preface)
* TLS: `tokio_rustls::TlsAcceptor` wrapping each TCP stream; ALPN list
  is `[b"h2", b"http/1.1"]`
* QUIC: `quinn::Endpoint::server` with rustls backend; ALPN is `[b"h3"]`
  on a cloned config
* HTTP/3 protocol layer: `h3::server::Connection` + `h3_quinn::Connection`
* All paths share one Python entry: `_dispatch_native` (single PyO3 call
  per request)

See `crates/fr-pyiface/src/server.rs` for the implementation.
