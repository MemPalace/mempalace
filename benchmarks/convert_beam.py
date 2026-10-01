#!/usr/bin/env python3
"""
Convert BEAM 100K dataset from HuggingFace parquet to JSON for the benchmark runner.

Dataset: https://huggingface.co/datasets/Mohammadta/BEAM
Paper: Tavakoli et al., "BEAM: Benchmark for Evaluating AI Memory" (2024)

This is a thin wrapper around `_beam_utils.convert_parquet_to_json()`. The
benchmark runner (`beam_100k_bench.py`) auto-downloads and converts on first
run, so this script is only needed if you want to convert manually or
generate the JSON ahead of time.

Usage:
    pip install pandas pyarrow
    python benchmarks/convert_beam.py data/beam-100k.parquet data/beam-100k.json
"""

import sys
from pathlib import Path

# Allow importing _beam_utils from the same directory when called as a script
sys.path.insert(0, str(Path(__file__).parent))

from _beam_utils import convert_parquet_to_json


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else "data/beam-100k.parquet"
    output_file = sys.argv[2] if len(sys.argv) > 2 else "data/beam-100k.json"

    print(f"Reading {input_file}...")
    convert_parquet_to_json(input_file, output_file)
    print(f"Wrote {output_file}")
    print("Done.")


if __name__ == "__main__":
    main()
