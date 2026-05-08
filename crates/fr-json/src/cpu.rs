//! Runtime CPU feature detection for SIMD dispatch.

use std::sync::atomic::{AtomicBool, Ordering};

static AVX2: AtomicBool = AtomicBool::new(false);
static INITIALIZED: AtomicBool = AtomicBool::new(false);

/// Detect available CPU features. Idempotent; safe to call multiple times.
/// Should be called once at runtime startup (e.g., when the FastAPI() class
/// is first constructed) so the dispatch flag is set before any request.
pub fn init() {
    if INITIALIZED.swap(true, Ordering::SeqCst) {
        return;
    }
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    {
        AVX2.store(is_x86_feature_detected!("avx2"), Ordering::Relaxed);
    }
}

/// Returns true if AVX2 is available on this CPU.
pub fn has_avx2() -> bool {
    AVX2.load(Ordering::Relaxed)
}
