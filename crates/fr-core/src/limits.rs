//! Pre-allocation and DoS-protection limits for the runtime.
//!
//! Every external input has a hard cap, every hot-path buffer has an explicit
//! capacity hint. Defaults are tuned for "typical web app" — large enough not
//! to surprise users, small enough to refuse abusive clients.

/// Default maximum request body size: 10 MiB.
pub const DEFAULT_MAX_BODY_SIZE: usize = 10 * 1024 * 1024;

/// Default maximum number of headers per request.
pub const DEFAULT_MAX_HEADERS: usize = 100;

/// Default maximum length of a single header name in bytes.
pub const DEFAULT_MAX_HEADER_NAME_LEN: usize = 256;

/// Default maximum length of a single header value in bytes.
pub const DEFAULT_MAX_HEADER_VALUE_LEN: usize = 16 * 1024;

/// Default maximum URL length in bytes (path + query string + scheme + authority).
pub const DEFAULT_MAX_URL_LEN: usize = 8 * 1024;

/// Default maximum query string length in bytes.
pub const DEFAULT_MAX_QUERY_STRING_LEN: usize = 4 * 1024;

/// Default maximum size of a single multipart part.
pub const DEFAULT_MAX_MULTIPART_PART_SIZE: usize = 10 * 1024 * 1024;

/// Default maximum WebSocket message size.
pub const DEFAULT_MAX_WS_MESSAGE_SIZE: usize = 1 * 1024 * 1024;

/// Default keepalive timeout for connections (seconds).
pub const DEFAULT_KEEPALIVE_TIMEOUT_SECS: u64 = 75;

/// Default per-request timeout (seconds).
pub const DEFAULT_REQUEST_TIMEOUT_SECS: u64 = 30;

#[derive(Clone, Debug)]
pub struct Limits {
    pub max_body_size: usize,
    pub max_headers: usize,
    pub max_header_name_len: usize,
    pub max_header_value_len: usize,
    pub max_url_len: usize,
    pub max_query_string_len: usize,
    pub max_multipart_part_size: usize,
    pub max_websocket_message_size: usize,
    pub keepalive_timeout_secs: u64,
    pub request_timeout_secs: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_body_size: DEFAULT_MAX_BODY_SIZE,
            max_headers: DEFAULT_MAX_HEADERS,
            max_header_name_len: DEFAULT_MAX_HEADER_NAME_LEN,
            max_header_value_len: DEFAULT_MAX_HEADER_VALUE_LEN,
            max_url_len: DEFAULT_MAX_URL_LEN,
            max_query_string_len: DEFAULT_MAX_QUERY_STRING_LEN,
            max_multipart_part_size: DEFAULT_MAX_MULTIPART_PART_SIZE,
            max_websocket_message_size: DEFAULT_MAX_WS_MESSAGE_SIZE,
            keepalive_timeout_secs: DEFAULT_KEEPALIVE_TIMEOUT_SECS,
            request_timeout_secs: DEFAULT_REQUEST_TIMEOUT_SECS,
        }
    }
}
