use clap::{Parser, Subcommand};
use mempalace_core::VectorIndex;
use std::path::{Path, PathBuf};
use std::time::Instant;

const DEFAULT_DB_FILENAME: &str = "sqlite_exact.sqlite3";

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
        /// Palace database file. Defaults to the active config directory's
        /// palace, resolved like the Python tools (XDG-aware, see #2520).
        #[arg(short, long)]
        db: Option<String>,

        #[arg(short, long)]
        collection: Option<String>,
    },
    /// Benchmark query latency and memory usage on live data
    Bench {
        /// Palace database file. Defaults to the active config directory's
        /// palace, resolved like the Python tools (XDG-aware, see #2520).
        #[arg(short, long)]
        db: Option<String>,

        #[arg(short, long)]
        collection: Option<String>,

        #[arg(short, long, default_value_t = 10)]
        k: usize,

        #[arg(short, long, default_value_t = 25)]
        iterations: usize,
    },
    /// Search palace using an input vector
    Search {
        /// JSON array of embedding floats, or '-' to read the array from stdin
        #[arg(long)]
        vector: String,
        /// Palace database file. Defaults to the active config directory's
        /// palace, resolved like the Python tools (XDG-aware, see #2520).
        #[arg(short, long)]
        db: Option<String>,

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

fn env_var(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

struct ProcessEnv {
    home: Option<PathBuf>,
    config_dir: Option<String>,
    xdg_config_home: Option<String>,
}

impl ProcessEnv {
    fn from_process() -> Self {
        Self {
            home: dirs_or_home(),
            config_dir: env_var("MEMPALACE_CONFIG_DIR"),
            xdg_config_home: env_var("XDG_CONFIG_HOME"),
        }
    }
}

fn has_legacy_install(legacy: &Path) -> bool {
    if legacy.join("config.json").is_file() || legacy.join("people_map.json").is_file() {
        return true;
    }
    legacy.join("palace").join("chroma.sqlite3").is_file()
}

/// Resolve the default config directory with the same precedence as
/// `mempalace.config._default_config_dir()` (#2520):
/// 1. $MEMPALACE_CONFIG_DIR  2. a real legacy ~/.mempalace  3. $XDG_CONFIG_HOME/mempalace
/// 4. ~/.config/mempalace
fn default_config_dir_from(env: &ProcessEnv) -> Option<PathBuf> {
    if let Some(dir) = &env.config_dir {
        return Some(resolve_path(dir));
    }
    let home = env.home.clone()?;
    let legacy = home.join(".mempalace");
    if legacy.is_dir() && has_legacy_install(&legacy) {
        return Some(legacy);
    }
    if let Some(xdg) = &env.xdg_config_home {
        let xdg_path = resolve_path(xdg);
        // Per XDG spec, relative paths must be ignored as invalid.
        if xdg_path.is_absolute() {
            return Some(xdg_path.join("mempalace"));
        }
    }
    Some(home.join(".config").join("mempalace"))
}

fn palace_path_from_config(config: &Path) -> Option<PathBuf> {
    let raw = std::fs::read_to_string(config).ok()?;
    let value: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let palace_path = value.get("palace_path")?.as_str()?;
    Some(resolve_path(palace_path.trim()))
}

/// Default `--db` target: `<palace>/sqlite_exact.sqlite3` for the resolved
/// config directory, honouring a configured `palace_path` in config.json.
fn default_db_path_from(env: &ProcessEnv) -> Option<PathBuf> {
    let config_dir = default_config_dir_from(env)?;
    let palace = match palace_path_from_config(&config_dir.join("config.json")) {
        Some(palace_path) => palace_path,
        None => config_dir.join("palace"),
    };
    Some(palace.join(DEFAULT_DB_FILENAME))
}

fn default_db_path() -> Option<PathBuf> {
    default_db_path_from(&ProcessEnv::from_process())
}

fn db_arg_or_default(db: Option<&str>) -> Result<PathBuf, &'static str> {
    match db {
        Some(p) => Ok(resolve_path(p)),
        None => default_db_path()
            .ok_or("cannot resolve a default palace database; pass --db (see #2520)"),
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Stats { db, collection } => {
            let path = db_arg_or_default(db.as_deref())?;
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
        Commands::Bench {
            db,
            collection,
            k,
            iterations,
        } => {
            if k == 0 || iterations == 0 {
                return Err("bench requires k > 0 and iterations > 0".into());
            }
            let path = db_arg_or_default(db.as_deref())?;
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
            println!(
                "Cold 1st Query: {:.2} ms (top hit: {} dist: {:.6})",
                cold_ms, hits[0].id, hits[0].distance
            );

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
            println!(
                "Warm Query (Single-thread): p50={:.2} ms, p95={:.2} ms, p99={:.2} ms",
                p50, p95, p99
            );

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
            println!(
                "Warm Query (Multi-thread):   p50={:.2} ms, p95={:.2} ms",
                par_p50, par_p95
            );
            println!("==================================================");
        }
        Commands::Search {
            db,
            collection,
            k,
            wing,
            vector,
        } => {
            let path = db_arg_or_default(db.as_deref())?;
            let index = VectorIndex::load_from_sqlite(&path, collection.as_deref())?;
            let input = if vector == "-" {
                std::io::read_to_string(std::io::stdin())?
            } else {
                vector
            };
            let q: Vec<f32> = serde_json::from_str(&input)?;
            let hits = index.query_parallel(&q, k, wing.as_deref())?;
            let json = serde_json::to_string_pretty(&hits)?;
            println!("{}", json);
        }
    }

    Ok(())
}

#[cfg(test)]
mod default_db_tests {
    use super::*;

    fn touch(path: &Path) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, b"").unwrap();
    }

    fn env(home: &Path, config_dir: Option<&str>, xdg: Option<&str>) -> ProcessEnv {
        ProcessEnv {
            home: Some(home.to_path_buf()),
            config_dir: config_dir.map(str::to_string),
            xdg_config_home: xdg.map(str::to_string),
        }
    }

    #[test]
    fn fresh_install_resolves_under_the_xdg_config_dir() {
        let home = tempfile::tempdir().unwrap();
        let db = default_db_path_from(&env(home.path(), None, None)).unwrap();
        assert_eq!(
            db,
            home.path()
                .join(".config/mempalace/palace/sqlite_exact.sqlite3")
        );
    }

    #[test]
    fn real_legacy_install_keeps_priority() {
        let home = tempfile::tempdir().unwrap();
        touch(&home.path().join(".mempalace/config.json"));
        let db = default_db_path_from(&env(home.path(), None, None)).unwrap();
        assert_eq!(
            db,
            home.path().join(".mempalace/palace/sqlite_exact.sqlite3")
        );
    }

    #[test]
    fn bare_legacy_directory_does_not_hijack_the_xdg_path() {
        let home = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(home.path().join(".mempalace/palace")).unwrap();
        let db = default_db_path_from(&env(home.path(), None, None)).unwrap();
        assert_eq!(
            db,
            home.path()
                .join(".config/mempalace/palace/sqlite_exact.sqlite3")
        );
    }

    #[test]
    fn chroma_store_in_legacy_palace_counts_as_an_install() {
        let home = tempfile::tempdir().unwrap();
        touch(&home.path().join(".mempalace/palace/chroma.sqlite3"));
        let db = default_db_path_from(&env(home.path(), None, None)).unwrap();
        assert_eq!(
            db,
            home.path().join(".mempalace/palace/sqlite_exact.sqlite3")
        );
    }

    #[test]
    fn explicit_env_dir_wins_over_everything() {
        let home = tempfile::tempdir().unwrap();
        touch(&home.path().join(".mempalace/config.json"));
        let db = default_db_path_from(&env(home.path(), Some("/tmp/mp-xdg"), None)).unwrap();
        assert_eq!(db, PathBuf::from("/tmp/mp-xdg/palace/sqlite_exact.sqlite3"));
    }

    #[test]
    fn xdg_config_home_is_honoured_and_relative_values_are_ignored() {
        let home = tempfile::tempdir().unwrap();
        let db = default_db_path_from(&env(home.path(), None, Some("/tmp/xdg-base"))).unwrap();
        assert_eq!(
            db,
            PathBuf::from("/tmp/xdg-base/mempalace/palace/sqlite_exact.sqlite3")
        );
        let fallback =
            default_db_path_from(&env(home.path(), None, Some("relative/path"))).unwrap();
        assert_eq!(
            fallback,
            home.path()
                .join(".config/mempalace/palace/sqlite_exact.sqlite3")
        );
    }

    #[test]
    fn configured_palace_path_in_config_json_is_used() {
        let home = tempfile::tempdir().unwrap();
        let config_dir = home.path().join(".config/mempalace");
        std::fs::create_dir_all(&config_dir).unwrap();
        let palace = home.path().join("palaces/work");
        let config = format!(r#"{{"palace_path": "{}"}}"#, palace.display());
        std::fs::write(config_dir.join("config.json"), config).unwrap();
        let db = default_db_path_from(&env(home.path(), None, None)).unwrap();
        assert_eq!(db, palace.join("sqlite_exact.sqlite3"));
    }

    #[test]
    fn tilde_prefixes_expand_to_the_home_directory() {
        assert!(resolve_path("~/palace/a.db").starts_with(dirs_or_home().unwrap()));
        assert_eq!(resolve_path("/abs/a.db"), PathBuf::from("/abs/a.db"));
    }
}
