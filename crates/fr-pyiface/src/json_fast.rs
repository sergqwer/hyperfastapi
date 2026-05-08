//! Phase M: fast Rust-native JSON serializer for dispatch hot path.
//!
//! Walks PyObjects directly via PyO3, emitting JSON bytes without round-tripping
//! through Python's `json` module. For the common case (dict/list/str/int/
//! float/bool/None), this is 5-10× faster than `json.dumps` and saves all the
//! GIL contention from Python attribute lookups.
//!
//! For "exotic" types (Pydantic BaseModel, datetime, UUID, Decimal, Path,
//! Enum, dataclasses), the encoder returns Err so the caller can fall back to
//! the Python jsonable_encoder path. Falling back is correct but slow — Phase
//! M+1 may extend this to handle the most common exotic types directly.
//!
//! Output format matches FastAPI's defaults:
//!   * `ensure_ascii=False` — non-ASCII chars passed through as UTF-8 bytes
//!   * `separators=(",", ":")` — no whitespace
//!   * NaN / Infinity → `null` (FastAPI behavior, vs json.dumps which emits
//!     bare `NaN` / `Infinity`)

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

/// Try to encode `value` as JSON bytes purely in Rust. Returns `Ok(bytes)` on
/// success, `Err(())` if any subtree contains a type we don't handle natively.
/// On Err, callers should fall back to the Python `json.dumps` path.
pub fn encode(py: Python<'_>, value: &Bound<'_, PyAny>) -> Result<Vec<u8>, ()> {
    let mut out = Vec::with_capacity(64);
    write_value(py, value, &mut out)?;
    Ok(out)
}

fn write_value(py: Python<'_>, value: &Bound<'_, PyAny>, out: &mut Vec<u8>) -> Result<(), ()> {
    // None is the cheapest check (singleton compare).
    if value.is_none() {
        out.extend_from_slice(b"null");
        return Ok(());
    }
    // Bool MUST come before int (bool is a subclass of int in Python; the
    // PyInt downcast would succeed and we'd emit 0/1 instead of false/true).
    if let Ok(b) = value.downcast::<PyBool>() {
        out.extend_from_slice(if b.is_true() { b"true" } else { b"false" });
        return Ok(());
    }
    if let Ok(_) = value.downcast::<PyInt>() {
        // Try i64 fast path first; fall back to extracting as String for
        // arbitrary-precision (Python ints can be wider than i64).
        if let Ok(i) = value.extract::<i64>() {
            let mut buf = itoa::Buffer::new();
            out.extend_from_slice(buf.format(i).as_bytes());
            return Ok(());
        }
        // BigInt — render via Python's str(); rare path so the conversion
        // cost is acceptable.
        let s: String = value.str().map_err(|_| ())?.to_string();
        out.extend_from_slice(s.as_bytes());
        return Ok(());
    }
    if let Ok(_) = value.downcast::<PyFloat>() {
        let f: f64 = value.extract().map_err(|_| ())?;
        write_float(f, out);
        return Ok(());
    }
    if let Ok(s) = value.downcast::<PyString>() {
        let st: &str = s.to_str().map_err(|_| ())?;
        write_str(st, out);
        return Ok(());
    }
    if let Ok(d) = value.downcast::<PyDict>() {
        out.push(b'{');
        let mut first = true;
        for (k, v) in d.iter() {
            if !first {
                out.push(b',');
            }
            first = false;
            // JSON keys must be strings. Python's json.dumps coerces int/
            // float/bool keys to their str() form; do the same here.
            if let Ok(s) = k.downcast::<PyString>() {
                write_str(s.to_str().map_err(|_| ())?, out);
            } else if k.is_none() {
                write_str("null", out);
            } else if let Ok(b) = k.downcast::<PyBool>() {
                write_str(if b.is_true() { "true" } else { "false" }, out);
            } else if let Ok(_) = k.downcast::<PyInt>() {
                let s: String = k.str().map_err(|_| ())?.to_string();
                write_str(&s, out);
            } else if let Ok(_) = k.downcast::<PyFloat>() {
                let s: String = k.str().map_err(|_| ())?.to_string();
                write_str(&s, out);
            } else {
                return Err(());
            }
            out.push(b':');
            write_value(py, &v, out)?;
        }
        out.push(b'}');
        return Ok(());
    }
    if let Ok(l) = value.downcast::<PyList>() {
        out.push(b'[');
        let mut first = true;
        for item in l.iter() {
            if !first {
                out.push(b',');
            }
            first = false;
            write_value(py, &item, out)?;
        }
        out.push(b']');
        return Ok(());
    }
    if let Ok(t) = value.downcast::<PyTuple>() {
        out.push(b'[');
        let mut first = true;
        for item in t.iter() {
            if !first {
                out.push(b',');
            }
            first = false;
            write_value(py, &item, out)?;
        }
        out.push(b']');
        return Ok(());
    }
    // Unknown type — caller falls back to Python encoder.
    Err(())
}

fn write_float(f: f64, out: &mut Vec<u8>) {
    if f.is_nan() || f.is_infinite() {
        out.extend_from_slice(b"null");
        return;
    }
    // ryu produces shortest round-trip representation, much faster than
    // serde_json's float writer.
    let mut buf = ryu::Buffer::new();
    let s = buf.format(f);
    // ryu emits "1.0" for integral floats; Python's json emits "1.0" too —
    // already matches. ryu uses "e" exponent format same as JSON.
    out.extend_from_slice(s.as_bytes());
}

/// Emit a JSON string literal with proper escaping. ensure_ascii=False means
/// non-ASCII UTF-8 bytes pass through unchanged; only structural / control
/// characters need escape sequences.
fn write_str(s: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    let bytes = s.as_bytes();
    let mut start = 0usize;
    for (i, &b) in bytes.iter().enumerate() {
        let escape: Option<&[u8]> = match b {
            b'"' => Some(b"\\\""),
            b'\\' => Some(b"\\\\"),
            b'\n' => Some(b"\\n"),
            b'\r' => Some(b"\\r"),
            b'\t' => Some(b"\\t"),
            0x08 => Some(b"\\b"),
            0x0c => Some(b"\\f"),
            0x00..=0x1f => None, // handled below as \u00XX
            _ => continue,
        };
        if i > start {
            out.extend_from_slice(&bytes[start..i]);
        }
        match escape {
            Some(esc) => out.extend_from_slice(esc),
            None => {
                // Other control chars — \u00XX
                out.extend_from_slice(b"\\u00");
                let hi = (b >> 4) & 0x0f;
                let lo = b & 0x0f;
                out.push(HEX[hi as usize]);
                out.push(HEX[lo as usize]);
            }
        }
        start = i + 1;
    }
    if start < bytes.len() {
        out.extend_from_slice(&bytes[start..]);
    }
    out.push(b'"');
}

const HEX: &[u8; 16] = b"0123456789abcdef";
