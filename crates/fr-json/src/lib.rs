//! SIMD JSON parse + serialize.
//!
//! Phase A: stub with the runtime AVX2 detection helper. Phase C wires this
//! into the body parser; Phase J profiles and tightens the hot path.

#![allow(dead_code)]

pub mod cpu;

pub use cpu::{has_avx2, init};
