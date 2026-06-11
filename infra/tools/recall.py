#!/usr/bin/env python3
"""recall.py — Phase 4.1 — semantic search across the second brain.

Two retrieval paths:
  Default : ripgrep-based keyword + BM25-ish scoring (always works, no deps,
            always fresh — searches live files)
  --vector: semantic search against the local Qdrant index built nightly by
            build_recall_index.py (same index the `qdrant` MCP server queries).
            Runs via `uv run` (qdrant-client + fastembed; system python stays
            untouched). Falls back to BM25 with a note when the index is
            locked by an open Claude Code session or not yet built.

Corpus searched:
  _agent_state/*/memory.json:recent_learnings + global_patterns
  _agent_state/claude-code/sessions.jsonl summaries
  _External/MDM Memory/Firm Memory Obsidian/**/*.md  (fused old vault)
  _External/claude-transcripts/*.jsonl  (past conversations)
  _brain_api/canonical/**/*.json
  02_Areas/Accounts/**/*.md
  01_Projects/**/*.md
  03_Resources/**/*.md

Usage:
    python3 build/tools/recall.py "compliance evidence healthcare"
    python3 build/tools/recall.py "pricing patterns" --top 20 --no-rerank
    python3 build/tools/recall.py "RFP for Globex" --json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))

INCLUDE_GLOBS = [
    "_agent_state",
    "_brain_api/canonical",
    "_External/MDM Memory/Firm Memory Obsidian",
    "02_Areas",
    "01_Projects",
    "03_Resources",
    "CLAUDE.md",
    "99_Meta/vault-fusion.md",
]
EXCLUDE_FRAGMENTS = ("/.obsidian/", "/Images/", "/.git/", "graphify-out/", "from-onenote/")


def tokenize(s: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", s)]


def gather_candidates(vault: Path, query: str, max_files: int = 200) -> list[Path]:
    """Use ripgrep (or fallback grep) to find files mentioning any query term.
    Returns the top max_files results ordered roughly by match count."""
    terms = list(set(tokenize(query)))[:6]
    if not terms:
        return []
    rg = "/opt/homebrew/bin/rg" if Path("/opt/homebrew/bin/rg").exists() else \
         "/usr/local/bin/rg" if Path("/usr/local/bin/rg").exists() else None

    files_seen: Counter = Counter()
    for term in terms:
        if rg:
            cmd = [rg, "-l", "-i", "--no-messages", "-S", term]
            cmd += [str(vault / g) for g in INCLUDE_GLOBS if (vault / g).exists()]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                for line in out.stdout.splitlines():
                    if not line.strip():
                        continue
                    if any(frag in line for frag in EXCLUDE_FRAGMENTS):
                        continue
                    files_seen[line] += 1
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
        else:
            # Fallback: simple recursive grep using Python
            for g in INCLUDE_GLOBS:
                root = vault / g
                if not root.exists():
                    continue
                if root.is_file():
                    try:
                        if term.lower() in root.read_text(errors="ignore").lower():
                            files_seen[str(root)] += 1
                    except OSError:
                        pass
                else:
                    for p in root.rglob("*"):
                        if not p.is_file():
                            continue
                        if any(frag in str(p) for frag in EXCLUDE_FRAGMENTS):
                            continue
                        if p.suffix not in (".md", ".json", ".jsonl", ".txt"):
                            continue
                        try:
                            if term.lower() in p.read_text(errors="ignore").lower():
                                files_seen[str(p)] += 1
                        except OSError:
                            continue

    ranked = [p for p, _ in files_seen.most_common(max_files)]
    return [Path(p) for p in ranked]


def extract_snippet(path: Path, query: str, ctx: int = 2) -> str:
    """Return a short snippet around the best match."""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return ""

    terms = tokenize(query)
    lines = text.splitlines()
    best_idx = 0
    best_score = 0
    for i, ln in enumerate(lines):
        s = sum(1 for t in terms if t in ln.lower())
        if s > best_score:
            best_score, best_idx = s, i
        if best_score == len(terms):
            break

    start = max(0, best_idx - ctx)
    end = min(len(lines), best_idx + ctx + 1)
    snippet = "\n".join(lines[start:end])[:400]
    return snippet


def bm25_score(path: Path, query: str) -> float:
    """Crude BM25-ish: term frequency × inverse document length penalty."""
    try:
        text = path.read_text(errors="ignore").lower()
    except OSError:
        return 0.0
    terms = tokenize(query)
    if not terms or not text:
        return 0.0
    score = 0.0
    for t in terms:
        tf = text.count(t)
        if tf == 0:
            continue
        # crude length norm — long files penalized lightly
        score += math.log(1 + tf) / (1 + math.log(1 + len(text) / 1000))
    return score


def vector_search(query: str, top: int = 10) -> list[dict] | None:
    """Semantic search via the local Qdrant index (recall_vector.py under uv).
    Returns None when unavailable (locked / unbuilt / uv missing) — caller
    falls back to BM25."""
    helper = Path(__file__).parent / "recall_vector.py"
    uv = Path.home() / ".local/bin/uv"
    if not uv.exists() or not helper.exists():
        print("[recall] uv or recall_vector.py missing — using BM25", file=sys.stderr)
        return None
    cmd = [str(uv), "run", "--with", "qdrant-client", "--with", "fastembed",
           "python", str(helper), query, "--top", str(top)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("[recall] semantic query timed out — using BM25", file=sys.stderr)
        return None
    if out.returncode == 3:
        print("[recall] semantic unavailable — index locked by an open session; using BM25", file=sys.stderr)
        return None
    if out.returncode == 4:
        print("[recall] semantic index not built yet (build_recall_index.py --rebuild) — using BM25", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[recall] semantic query failed ({out.stderr.strip()[:200]}) — using BM25", file=sys.stderr)
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        print("[recall] semantic returned bad JSON — using BM25", file=sys.stderr)
        return None


def search(vault: Path, query: str, top: int = 10) -> list[dict]:
    candidates = gather_candidates(vault, query, max_files=200)
    if not candidates:
        return []

    scored = []
    for p in candidates[:50]:  # BM25 over top 50
        b = bm25_score(p, query)
        if b > 0:
            snip = extract_snippet(p, query)
            scored.append((p, b, snip))

    scored.sort(key=lambda x: -x[1])

    return [
        {
            "path": str(p.relative_to(vault)) if p.is_absolute() and str(p).startswith(str(vault)) else str(p),
            "score": round(s, 4),
            "snippet": snip,
        }
        for p, s, snip in scored[:top]
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+", help="search query (multiple words = phrase)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--vector", action="store_true",
                    help="semantic search via local Qdrant index (falls back to BM25 if locked/unbuilt)")
    ap.add_argument("--no-rerank", action="store_true", help="(deprecated, no-op)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--vault", default=os.environ.get("CLAUDE_VAULT", VAULT_DEFAULT))
    args = ap.parse_args()

    query = " ".join(args.query)
    results = None
    if args.vector:
        results = vector_search(query, top=args.top)
    if results is None:
        results = search(Path(args.vault), query, top=args.top)

    if args.json:
        print(json.dumps({"query": query, "results": results}, indent=2))
        return 0

    if not results:
        print(f"No matches for: {query!r}")
        return 0

    print(f"\n══ /recall '{query}' — {len(results)} results ══\n")
    for i, r in enumerate(results, 1):
        print(f"{i:>2}. [{r['score']}] {r['path']}")
        for line in r["snippet"].splitlines()[:3]:
            print(f"      {line[:140]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
