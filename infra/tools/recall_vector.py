#!/usr/bin/env python3
"""recall_vector.py — query the local recall Qdrant index (semantic path).

Invoked by recall.py --vector via uv (needs qdrant-client + fastembed,
which system python 3.9 doesn't have):
    uv run --with qdrant-client --with fastembed python recall_vector.py "<query>" --top 10

Exit codes: 0 ok (JSON on stdout) · 3 DB locked (caller falls back to BM25)
            4 collection missing (index never built).

Same contract as build_recall_index.py / mcp-server-qdrant:
vector 'fast-all-minilm-l6-v2', query_embed, payload {document, metadata}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

QDRANT_PATH = Path.home() / "AI-Brain-build" / "qdrant"
COLLECTION = "recall"
VECTOR_NAME = "fast-all-minilm-l6-v2"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    from qdrant_client import QdrantClient

    try:
        client = QdrantClient(path=str(QDRANT_PATH))
    except RuntimeError as e:
        if "already accessed" in str(e):
            print("locked", file=sys.stderr)
            return 3
        raise

    if not client.collection_exists(COLLECTION):
        print("collection missing — run build_recall_index.py --rebuild", file=sys.stderr)
        return 4

    from fastembed import TextEmbedding
    from pathlib import Path
    # Persistent cache (NOT the temp-dir default — macOS purges it; see recall_daemon._load_model)
    _fe_cache = Path.home() / "AI-Brain-build" / "fastembed-cache"; _fe_cache.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(MODEL_NAME, cache_dir=str(_fe_cache))
    q_vec = list(model.query_embed([args.query]))[0].tolist()  # query_embed = mcp-server-qdrant's query path

    hits = client.query_points(
        collection_name=COLLECTION,
        query=q_vec,
        using=VECTOR_NAME,
        limit=args.top,
        with_payload=True,
    ).points

    results = []
    for h in hits:
        meta = (h.payload or {}).get("metadata") or {}
        results.append({
            "path": meta.get("path", ""),
            "score": round(float(h.score), 4),
            "snippet": meta.get("snippet", ""),
            "title": meta.get("title", ""),
            "source_type": meta.get("source_type", ""),
        })
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
