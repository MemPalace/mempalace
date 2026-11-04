"""Record a synthetic CLI invocation and emit a fixed answer."""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--exit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0)
    parser.add_argument("--empty", action="store_true")
    args = parser.parse_args()
    record = {
        "argv": sys.argv[1:],
        "cwd": sorted(os.listdir()),
        "env": dict(os.environ),
        "stdin": sys.stdin.read(),
    }
    Path(args.record).write_text(json.dumps(record), encoding="utf-8")
    if args.sleep:
        time.sleep(args.sleep)
    if not args.empty:
        print("Fake answer from the runner.")
    return args.exit


if __name__ == "__main__":
    sys.exit(main())
