//! Core HTTP types for the fastapi_rust runtime.
//!
//! Phase A: skeleton only — crate compiles with no symbols. Subsequent phases
//! fill in `server`, `request`, `response`, `body`, `headers`, `limits`, `pool`.

#![allow(dead_code)]

pub mod limits;

pub use limits::Limits;
