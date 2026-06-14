#!/usr/bin/env python3
"""competence-finalize.py -- inject runtime fields into a JSON_RECORD dict.

Usage (called by competence-eval.sh):
    echo '<json>' | python3 competence-finalize.py <ts> <model> <sample_file> <elapsed_s>

Reads a JSON object from stdin.
Injects ts, model, sample_file, elapsed_s fields.
Prints the enriched JSON to stdout (one line).
"""
from __future__ import annotations
import json
import sys


def main() -> None:
    if len(sys.argv) < 5:
        print("usage: competence-finalize.py <ts> <model> <sample_file> <elapsed_s>")
        sys.exit(1)

    ts = sys.argv[1]
    model = sys.argv[2]
    sample_file = sys.argv[3]
    elapsed_s = int(sys.argv[4])

    raw = sys.stdin.read().strip()
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Emit a minimal record rather than crashing; shell will still append it
        rec = {
            "ts": ts,
            "source": "llm-judge",
            "model": model,
            "output_type": "unknown",
            "sample_file": sample_file,
            "elapsed_s": elapsed_s,
            "avg_score": 0.0,
            "overall": "ERROR",
            "dimensions": {},
            "critical_failures": [f"finalize JSON parse error: {exc}"],
        }
        print(json.dumps(rec))
        return

    rec["ts"] = ts
    rec["model"] = model
    rec["sample_file"] = sample_file
    rec["elapsed_s"] = elapsed_s
    print(json.dumps(rec))


if __name__ == "__main__":
    main()
