#!/usr/bin/env python3
"""competence-parse.py -- parse LLM judge JSON reply, print scores, emit JSON_RECORD line.

Usage (called by competence-eval.sh):
    python3 competence-parse.py <output_type> <passing_avg> "<critical_spec>" < reply.txt

Arguments:
    output_type   -- bid | intel | meeting
    passing_avg   -- float, e.g. 3.5
    critical_spec -- space-separated "dimension:min_score" pairs, e.g. "source_quality:3 recency:3"

Reads LLM reply from stdin.
Prints display lines to stdout.
Prints one "JSON_RECORD:<json>" line to stdout (consumed by competence-eval.sh).
Exits 0 always (parse errors are printed but do not crash the caller).
"""
from __future__ import annotations
import json
import sys


def main() -> None:
    if len(sys.argv) < 4:
        print("usage: competence-parse.py <output_type> <passing_avg> <critical_spec>")
        sys.exit(1)

    output_type = sys.argv[1]
    passing_avg = float(sys.argv[2])
    critical_spec = sys.argv[3]  # e.g. "source_quality:3 recency:3"

    raw = sys.stdin.read().strip()

    # Strip markdown code fences if the model wrapped its JSON
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(ln for ln in lines if not ln.startswith("```"))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"PARSE_ERROR: {exc}")
        print(f"RAW (first 400 chars): {raw[:400]}")
        # Emit a minimal error record so the history log still gets an entry
        record = {
            "ts": "",
            "source": "llm-judge",
            "model": "",
            "output_type": output_type,
            "sample_file": "",
            "elapsed_s": 0,
            "avg_score": 0.0,
            "overall": "ERROR",
            "dimensions": {},
            "critical_failures": [f"JSON parse error: {exc}"],
        }
        print("JSON_RECORD:" + json.dumps(record))
        return

    # Collect dimension entries (skip the output_type string key)
    dims: dict[str, dict] = {
        k: v
        for k, v in data.items()
        if k != "output_type" and isinstance(v, dict)
    }

    if not dims:
        print("PARSE_ERROR: no dimension keys found in JSON")
        record = {
            "ts": "",
            "source": "llm-judge",
            "model": "",
            "output_type": output_type,
            "sample_file": "",
            "elapsed_s": 0,
            "avg_score": 0.0,
            "overall": "ERROR",
            "dimensions": {},
            "critical_failures": ["no dimension keys found"],
        }
        print("JSON_RECORD:" + json.dumps(record))
        return

    scores: list[float] = []
    for dim, val in dims.items():
        score = int(val.get("score", 0))
        rationale = val.get("rationale", "")
        scores.append(score)
        print(f"  {dim:<24} {score}/5   {rationale}")

    avg = sum(scores) / len(scores) if scores else 0.0

    # Check critical floors
    critical_failures: list[str] = []
    for spec in critical_spec.split():
        if ":" not in spec:
            continue
        dim_name, min_s_str = spec.split(":", 1)
        min_s = int(min_s_str)
        actual = data.get(dim_name, {}).get("score", 0)
        if actual < min_s:
            critical_failures.append(f"{dim_name} scored {actual} (min {min_s})")

    print()
    print(f"  Average score : {avg:.2f} (pass bar: {passing_avg})")

    if critical_failures:
        print("  CRITICAL FLOOR BREACHES:")
        for cf in critical_failures:
            print(f"    - {cf}")

    overall = "PASS" if avg >= passing_avg and not critical_failures else "WARN"
    print(f"  Aggregate     : {overall} (SCAFFOLD -- advisory only, no gate)")

    # Emit JSON_RECORD for the shell to parse and enrich with runtime fields
    record = {
        "ts": "",           # filled by competence-finalize.py
        "source": "llm-judge",
        "model": "",        # filled by competence-finalize.py
        "output_type": output_type,
        "sample_file": "",  # filled by competence-finalize.py
        "elapsed_s": 0,     # filled by competence-finalize.py
        "avg_score": round(avg, 3),
        "overall": overall,
        "dimensions": {dim: data[dim]["score"] for dim in dims},
        "critical_failures": critical_failures,
    }
    print("JSON_RECORD:" + json.dumps(record))


if __name__ == "__main__":
    main()
