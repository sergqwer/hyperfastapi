# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

* **HTTP/2 (cleartext + ALPN over TLS)** — `run_native()` now switches on
  `hyper_util::server::conn::auto::Builder` so HTTP/1.1 and HTTP/2 are
  served from the same TCP listener. Cleartext h2 (h2c) is auto-detected
  from the client preface; over TLS, ALPN advertises `h2` + `http/1.1`.
* **HTTPS via rustls 0.23** — opt-in by passing
  `tls_cert="..." tls_key="..."` paths (PEM files). Mandatory for HTTP/3.
* **HTTP/3 (QUIC) via quinn 0.11 + h3 0.0.8** — opt-in via `http3=True`,
  binds an additional UDP listener on the same port. Plaintext responses
  on the TCP path advertise `alt-svc: h3=":<port>"` so clients
  auto-upgrade.
* `tests/perf/test_protocols.py` — end-to-end smoke tests covering every
  protocol with real httpx + curl + aioquic clients.
* `tests/perf/gen_self_signed.py` — helper to generate a local cert/key
  pair for HTTPS / HTTP/3 testing.

## [0.1.0] — 2026-05-08

Initial release. **Drop-in FastAPI replacement** with a Rust core delivering
**5×** single-process and **12×+** multi-process throughput vs FastAPI on uvicorn.

### Added

* **FastAPI-compatible Python API** — `from hyperfastapi import FastAPI, APIRouter, Depends, ...` works as a drop-in replacement.
* **Native HTTP server** via `app.run_native(host, port, workers)` — Rust hyper 1.x + tokio multi-thread runtime, bypasses uvicorn entirely for max throughput.
* **Rust-native JSON encoder** in dispatch hot path — walks PyObjects directly via PyO3, uses ryu/itoa, falls back to `json.dumps` + `jsonable_encoder` for exotic types (Pydantic models, datetime, UUID, Decimal).
* **Trivial-route fast path** — when a route has no params/deps/path-params, skip per-request `PyDict` allocations entirely.
* **Persistent async-worker loop** — coroutines from sync dispatch submit to a single daemon-thread loop instead of spawning thread+loop per request (saves ~500µs/req for `async def` handlers).
* **Pre-computed route metadata** — `is_async` flag, `dependencies` merge from router/app level, `marker_kwargs` carrying full param-marker kwargs dict.
* **abi3 wheels** — single Linux/macOS/Windows wheel covers Python 3.10..3.13.

### Conformance

* **514 / 514** behavioral tests passing (100%) — covers request params, responses, deps, security (10 schemes), OpenAPI 3.1, WebSockets, exceptions, middleware (uvicorn path), background tasks, lifespan, StaticFiles, templating.
* **29 / 29** adversarial misuse tests passing — content-type strictness, validation evasion, path traversal, auth-header tampering.
* **24 / 24** perf-gate tests passing.

### Performance

Single-process (Windows, Intel i7, 5s @ c=100, bombardier):

| Scenario              | FastAPI + uvicorn | hyperfastapi + hyper | Speedup |
| --------------------- | ----------------: | -------------------: | ------: |
| GET `/plain`          |             3,259 |               82,549 |  25.3 × |
| GET `/with-middleware`|             3,162 |               57,334 |  18.1 × |
| POST `/post-validated`|             2,898 |               37,349 |  12.9 × |
| GET `/with-chain`     |             2,006 |               25,980 |  13.0 × |
| GET `/with-query`     |             2,819 |               34,346 |  12.2 × |
| GET `/async`          |             8,920 |               18,285 |   2.0 × |

4-process aggregate clears **100,000 RPS** on 5/6 scenarios (peak: **193,808 RPS** on `/with-middleware`).

### Limitations

* **HTTP/2** — `run_native()` only enables HTTP/1.1 in this release.
* **TLS** — terminate at your load balancer (nginx / HAProxy / cloud LB).
* **ASGI middleware** (`CORSMiddleware`, `GZipMiddleware`, custom `@app.middleware("http")`) — bypassed by `run_native()`. Use uvicorn for full ASGI middleware support.
* **`async def` throughput** — currently 2× FastAPI single-process and 0.7× FastAPI multi-process. Cross-thread coroutine submission overhead is the bottleneck; planned: native async dispatch path through `_dispatch_async`.

[Unreleased]: https://github.com/sergqwer/hyperfastapi/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sergqwer/hyperfastapi/releases/tag/v0.1.0
