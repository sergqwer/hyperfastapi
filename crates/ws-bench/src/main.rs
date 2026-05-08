//! WebSocket throughput benchmark — Rust client to bypass the Python
//! asyncio.gather scheduling pathology that artificially caps multi-conn
//! throughput in `tests/perf/ws_bench.py`.
//!
//! Each connection runs as an independent tokio task; tokio scheduling is
//! pre-emptive across worker threads, so N connections actually run in
//! parallel without GIL contention.
//!
//! Usage:
//!   cargo run --release -p ws-bench -- --url ws://127.0.0.1:8765/echo \
//!         --connections 16 --messages 2000 --payload-size 64

use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;
use tokio_tungstenite::{connect_async, tungstenite::Message};

#[derive(Parser, Debug)]
struct Cli {
    /// WebSocket URL (e.g. ws://127.0.0.1:8765/echo or wss://host/path)
    #[arg(long, default_value = "ws://127.0.0.1:8765/echo")]
    url: String,

    /// Number of concurrent connections
    #[arg(long, short = 'c', default_value_t = 16)]
    connections: u32,

    /// Number of echo round-trips per connection
    #[arg(long, short = 'm', default_value_t = 2000)]
    messages: u32,

    /// Payload size in bytes
    #[arg(long, default_value_t = 64)]
    payload_size: usize,

    /// Tokio worker threads (0 = num_cpus)
    #[arg(long, default_value_t = 0)]
    workers: usize,

    /// Optional warmup message count per connection (excluded from timing).
    #[arg(long, default_value_t = 1)]
    warmup: u32,
}

fn main() {
    let cli = Cli::parse();
    let workers = if cli.workers == 0 {
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1)
    } else {
        cli.workers
    };
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(workers)
        .enable_all()
        .build()
        .expect("tokio runtime");
    rt.block_on(run(cli));
}

async fn run(cli: Cli) {
    println!(
        "ws-bench  url={}  conns={}  msgs/conn={}  payload={}B  workers={}",
        cli.url,
        cli.connections,
        cli.messages,
        cli.payload_size,
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1)
    );

    let payload: Arc<String> = Arc::new("x".repeat(cli.payload_size));
    let total_msgs = Arc::new(AtomicU64::new(0));
    let total_lat_us = Arc::new(AtomicU64::new(0));
    let max_lat_us = Arc::new(AtomicU64::new(0));

    let mut handles = Vec::with_capacity(cli.connections as usize);
    let start = Instant::now();
    for cid in 0..cli.connections {
        let url = cli.url.clone();
        let payload = Arc::clone(&payload);
        let total_msgs = Arc::clone(&total_msgs);
        let total_lat_us = Arc::clone(&total_lat_us);
        let max_lat_us = Arc::clone(&max_lat_us);
        let n_msgs = cli.messages;
        let warmup = cli.warmup;
        handles.push(tokio::spawn(async move {
            let (ws, _resp) = match connect_async(&url).await {
                Ok(p) => p,
                Err(e) => {
                    eprintln!("  conn {cid}: connect failed: {e}");
                    return;
                }
            };
            let (mut tx, mut rx) = ws.split();

            // Warmup so handshake / first-allocation latency doesn't poison
            // the measured window.
            for _ in 0..warmup {
                if tx.send(Message::Text((*payload).clone().into())).await.is_err() {
                    return;
                }
                if rx.next().await.is_none() {
                    return;
                }
            }

            let mut local_max = 0u64;
            let mut local_sum = 0u64;
            for _ in 0..n_msgs {
                let t0 = Instant::now();
                if tx.send(Message::Text((*payload).clone().into())).await.is_err() {
                    break;
                }
                if rx.next().await.is_none() {
                    break;
                }
                let lat_us = t0.elapsed().as_micros() as u64;
                local_sum += lat_us;
                if lat_us > local_max {
                    local_max = lat_us;
                }
            }
            total_msgs.fetch_add(n_msgs as u64, Ordering::Relaxed);
            total_lat_us.fetch_add(local_sum, Ordering::Relaxed);
            max_lat_us.fetch_max(local_max, Ordering::Relaxed);

            let _ = tx.close().await;
        }));
    }

    for h in handles {
        let _ = h.await;
    }
    let elapsed = start.elapsed();

    let total = total_msgs.load(Ordering::Relaxed);
    let lat_sum = total_lat_us.load(Ordering::Relaxed);
    let lat_max = max_lat_us.load(Ordering::Relaxed);
    let elapsed_secs = elapsed.as_secs_f64();
    let rps = if elapsed_secs > 0.0 {
        total as f64 / elapsed_secs
    } else {
        0.0
    };
    let mean_lat_us = if total > 0 { lat_sum / total } else { 0 };

    println!();
    println!("  total msgs   = {total}");
    println!("  elapsed      = {:.3} s", elapsed_secs);
    println!("  throughput   = {:>10.0} msg/sec", rps);
    println!("  per-conn     = {:>10.0} msg/sec", rps / cli.connections as f64);
    println!("  mean latency = {:.3} ms", mean_lat_us as f64 / 1000.0);
    println!("  max  latency = {:.3} ms", lat_max as f64 / 1000.0);
}
