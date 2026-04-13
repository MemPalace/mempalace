"""Capture stdio responses for parity queries. Writes JSONL."""
import json
import subprocess
import sys
from pathlib import Path

QUERIES = json.loads(Path("tests/fixtures/parity_queries.json").read_text())
OUT = Path("tests/fixtures/parity_stdio_responses.jsonl")


def main() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "mempalace.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    with OUT.open("w") as f:
        for i, q in enumerate(QUERIES, start=1):
            req = {
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": q,
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp = proc.stdout.readline()
            f.write(resp)
    proc.stdin.close()
    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
