use std::io::Write;
use std::process::{Command, Stdio};

#[test]
fn search_uses_input_vector_and_rejects_invalid_bench() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("test.sqlite3");
    let conn = rusqlite::Connection::open(&path).unwrap();
    conn.execute_batch("CREATE TABLE collections(id INTEGER, name TEXT, dimension INTEGER);
        CREATE TABLE documents(id TEXT, embedding BLOB, wing TEXT, room TEXT, collection_id INTEGER);
        INSERT INTO collections VALUES(1, 'mempalace_drawers', 2);").unwrap();
    for (id, vector) in [("a", [1.0f32, 0.0]), ("b", [0.0f32, 1.0])] {
        let blob: Vec<u8> = vector.iter().flat_map(|v| v.to_le_bytes()).collect();
        conn.execute(
            "INSERT INTO documents VALUES(?1, ?2, NULL, NULL, 1)",
            rusqlite::params![id, blob],
        )
        .unwrap();
    }
    let binary = env!("CARGO_BIN_EXE_mempalace-native");
    for (vector, expected) in [("[1,0]", "a"), ("[0,1]", "b")] {
        let output = Command::new(binary)
            .args([
                "search",
                "--db",
                path.to_str().unwrap(),
                "--vector",
                vector,
                "-k",
                "1",
            ])
            .output()
            .unwrap();
        assert!(output.status.success(), "{:?}", output);
        let hits: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(hits[0]["id"], expected);
    }
    let mut child = Command::new(binary)
        .args([
            "search",
            "--db",
            path.to_str().unwrap(),
            "--vector",
            "-",
            "-k",
            "1",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(b"[0,1]").unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&output.stdout).unwrap()[0]["id"],
        "b"
    );
    for args in [["-k", "0"], ["--iterations", "0"]] {
        let output = Command::new(binary)
            .arg("bench")
            .args(args)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("requires k > 0"));
    }
}
