use clap::{Parser, Subcommand};
use mempalace_core::VectorIndex;
use std::path::PathBuf;
use std::time::Instant;

#[derive(Parser)]
#[command(name = "mempalace-native")]
#[command(about = "MemPalace Native High-Performance Engine (Rust)", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Show statistics and taxonomy for a palace database
    Stats {
        #[arg(short, long, default_value = "~/.mempalace/palace/sqlite_exact.sqlite3")]
        db: String,

        #[arg(short, long)]
        collection: Option<String>,
    },
    /// Benchmark query latency and memory usage on live data
    Bench {
        #[arg(short, long, default_value = "~/.mempalace/palace/sqlite_exact.sqlite3")]
        db: String,

        #[arg(short, long)]
        collection: Option<String>,

        #[arg(short, long, default_value_t = 10)]
        k: usize,

        #[arg(short, long, default_value_t = 25)]
        iterations: usize,
    },
    /// Search palace using an input vector
    Search {
        #[arg(short, long, default_value = "~/.mempalace/palace/sqlite_exact.sqlite3")]
        db: String,

        #[arg(short, long)]
        collection: Option<String>,

        #[arg(short, long, default_value_t = 10)]
        k: usize,

        #[arg(short, long)]
        wing: Option<String>,
    },
}

fn resolve_path(p: &str) -> PathBuf {
    if p.starts_with("~/") || p.starts_with("~\\") {
        if let Some(home) = dirs_or_home() {
            return home.join(&p[2..]);
        }
    }
    PathBuf::from(p)
}

fn dirs_or_home() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Stats { db, collection } => {
            let path = resolve_path(&db);
            println!("Opening MemPalace database: {}", path.display());
            let t0 = Instant::now();
            let index = VectorIndex::load_from_sqlite(&path, collection.as_deref())?;
            let load_dur = t0.elapsed();

            println!("Loaded {} documents in {:?}", index.len(), load_dur);
            println!("Embedding dimension: {}", index.dim());

            let wings = index.wing_counts();
            println!("\nTaxonomy by Wing ({} total wings):", wings.len());
            let mut sorted_wings: Vec<_> = wings.into_iter().collect();
            sorted_wings.sort_by(|a, b| b.1.cmp(&a.1));
            for (wing, count) in sorted_wings.iter().take(15) {
                println!("  - {:<30} : {:>6} docs", wing, count);
            }
            if sorted_wings.len() > 15 {
                println!("  ... and {} more wings", sorted_wings.len() - 15);
            }
        }
        Commands::Bench { db, collection, k, iterations } => {
            let path = resolve_path(&db);
            println!("==================================================");
            println!(" MemPalace Native Rust Benchmark");
            println!(" Database: {}", path.display());
            println!(" Target k: {}, Iterations: {}", k, iterations);
            println!("==================================================");

            let t0 = Instant::now();
            let index = VectorIndex::load_from_sqlite(&path, collection.as_deref())?;
            let load_ms = t0.elapsed().as_secs_f64() * 1000.0;
            println!("Loaded {} rows in {:.2} ms", index.len(), load_ms);

            if index.is_empty() {
                println!("Database is empty, nothing to benchmark.");
                return Ok(());
            }

            // Create test query vector
            let mut q = vec![0.0f32; index.dim()];
            q[0] = 1.0;

            // Cold query
            let t_cold = Instant::now();
            let hits = index.query(&q, k, None)?;
            let cold_ms = t_cold.elapsed().as_secs_f64() * 1000.0;
            println!("Cold 1st Query: {:.2} ms (top hit: {} dist: {:.6})", cold_ms, hits[0].id, hits[0].distance);

            // Warm single-thread queries
            let mut warm_times = Vec::with_capacity(iterations);
            for _ in 0..iterations {
                let t = Instant::now();
                let _ = index.query(&q, k, None)?;
                warm_times.push(t.elapsed().as_secs_f64() * 1000.0);
            }
            warm_times.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let p50 = warm_times[warm_times.len() / 2];
            let p95 = warm_times[(warm_times.len() as f64 * 0.95) as usize];
            let p99 = warm_times[(warm_times.len() as f64 * 0.99) as usize];
            println!("Warm Query (Single-thread): p50={:.2} ms, p95={:.2} ms, p99={:.2} ms", p50, p95, p99);

            // Warm multi-thread (parallel) queries
            let mut par_times = Vec::with_capacity(iterations);
            for _ in 0..iterations {
                let t = Instant::now();
                let _ = index.query_parallel(&q, k, None)?;
                par_times.push(t.elapsed().as_secs_f64() * 1000.0);
            }
            par_times.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let par_p50 = par_times[par_times.len() / 2];
            let par_p95 = par_times[(par_times.len() as f64 * 0.95) as usize];
            println!("Warm Query (Multi-thread):   p50={:.2} ms, p95={:.2} ms", par_p50, par_p95);
            println!("==================================================");
        }
        Commands::Search { db, collection, k, wing } => {
            let path = resolve_path(&db);
            let index = VectorIndex::load_from_sqlite(&path, collection.as_deref())?;
            let mut q = vec![0.0f32; index.dim()];
            q[0] = 1.0;

            let hits = index.query_parallel(&q, k, wing.as_deref())?;
            let json = serde_json::to_string_pretty(&hits)?;
            println!("{}", json);
        }
    }

    Ok(())
}
