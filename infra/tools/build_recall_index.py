#!/usr/bin/env python3
"""build_recall_index.py — semantic recall index for the second brain.

Builds a local Qdrant index (NO server, NO API keys — fastembed ONNX on-device)
that the `qdrant` MCP server (uvx mcp-server-qdrant) and `recall.py --vector`
both query. Source of truth stays in the OneDrive vault; this index is a
derived, regenerable artifact OUTSIDE OneDrive (like graphify-out/).

CONTRACT with mcp-server-qdrant (verified against its source — do not drift):
  - named vector  : fast-all-minilm-l6-v2   (size 384, COSINE)
  - payload keys  : {"document": <text>, "metadata": {...}}
  - embedding     : fastembed sentence-transformers/all-MiniLM-L6-v2,
                    passage_embed for documents (query side uses query_embed)

Concurrency: Qdrant local mode is single-process (portalocker .lock). If a
Claude Code session holds the DB via the MCP server, this script logs and
exits 0 — BM25 in recall.py covers freshness. Never delete .lock blindly;
manual recovery after a crash only: rm ~/AI-Brain-build/qdrant/.lock

Run (Python 3.12 via uv; system python untouched):
    uv run --with qdrant-client --with fastembed python build_recall_index.py
    ... --rebuild        # drop collection + manifest, full re-embed
    ... --once-if-free   # opportunistic: silent skip if DB is locked
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

VAULT = Path(os.environ.get(
    "CLAUDE_VAULT",
    os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")),
))
BUILD_HOME = Path.home() / "AI-Brain-build"
QDRANT_PATH = BUILD_HOME / "qdrant"
MANIFEST_PATH = BUILD_HOME / "qdrant-manifest.json"

COLLECTION = "recall"
VECTOR_NAME = "fast-all-minilm-l6-v2"   # mcp-server-qdrant: f"fast-{model.split('/')[-1].lower()}"
VECTOR_SIZE = 384
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Corpus = the whole "second brain" so semantic recall truly knows everything.
# Mirrors graphify's include-set. Privacy exclusions enforced in EXCLUDE_FRAGMENTS below
# (Meetings/Confidential, HR Documents, LinkedIn — same guard as capture-session + graphify).
MARKDOWN_ROOTS = [
    "00_Inbox",
    # "01_Projects" removed 2026-06-09 (ULT-G2): vault folder renamed to RFPs (below) —
    # keeping it made the missing_roots gate abort EVERY direct-vault run.
    "02_Areas",
    "03_Resources",
    "04_Archives",
    "10_Intelligence",
    "99_Meta",
    "_wiki",
    "Preferences",
    "Use Cases",
    "People",
    "RFPs",
    "Reading",
    "Meetings",
    "Important",
    "Outbound",
    "Clients",
    "Document Library",
    "_External/MDM Memory/Firm Memory Obsidian",
    "_External/Hub AI",
    "_External/Clients",
]
MARKDOWN_FILES = ["CLAUDE.md", "99_Meta/vault-fusion.md"]
EXCLUDE_FRAGMENTS = (
    "/.obsidian/", "/Images/", "/.git/", "graphify-out/", "from-onenote/",
    "/Meetings/Confidential/", "/HR Documents/", "/HR Docs/", "/LinkedIn/",  # privacy guard
)

MAX_CHUNK_CHARS = 3500   # ~900 tokens
MIN_CHUNK_CHARS = 400    # merge smaller sections into the previous chunk
SNIPPET_CHARS = 300
ID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "ai-second-brain/recall")


def log(msg: str) -> None:
    print(f"[recall-index] {msg}", flush=True)


def point_id(relpath: str, chunk_i: int) -> str:
    return str(uuid.uuid5(ID_NS, f"{relpath}#{chunk_i}"))


def excluded(p: Path) -> bool:
    s = str(p)
    return any(frag in s for frag in EXCLUDE_FRAGMENTS)


# ── corpus collectors ────────────────────────────────────────────────────────
# Each yields (relpath, [(title, text), ...], source_type). One manifest entry
# per file; chunks are re-derived wholesale when the file's mtime changes.

def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split on headings into ~500-1000 token sections; merge tiny ones."""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = [("(intro)", [])]
    for ln in lines:
        if re.match(r"^#{1,4}\s+\S", ln):
            sections.append((ln.lstrip("# ").strip(), []))
        else:
            sections[-1][1].append(ln)
    chunks: list[tuple[str, str]] = []
    for title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        # split oversized sections on paragraph boundaries
        while len(body) > MAX_CHUNK_CHARS:
            cut = body.rfind("\n\n", 0, MAX_CHUNK_CHARS)
            cut = cut if cut > MIN_CHUNK_CHARS else MAX_CHUNK_CHARS
            chunks.append((title, body[:cut].strip()))
            body = body[cut:].strip()
        if len(body) < MIN_CHUNK_CHARS and chunks and len(chunks[-1][1]) < MAX_CHUNK_CHARS:
            prev_t, prev_b = chunks[-1]
            chunks[-1] = (prev_t, f"{prev_b}\n\n## {title}\n{body}")
        elif body:
            chunks.append((title, body))
    return chunks


def collect_markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in MARKDOWN_ROOTS:
        r = VAULT / root
        if r.exists():
            files += [p for p in r.rglob("*.md") if p.is_file() and not excluded(p)]
    for f in MARKDOWN_FILES:
        p = VAULT / f
        if p.exists():
            files.append(p)
    return files


def docs_for_markdown(p: Path) -> list[tuple[str, str]]:
    try:
        return chunk_markdown(p.read_text(errors="ignore"))
    except OSError:
        return []


def docs_for_memory(p: Path) -> list[tuple[str, str]]:
    """_agent_state/<agent>/memory.json → one doc per learning/pattern/observation."""
    try:
        m = json.loads(p.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return []
    agent = m.get("agent") or p.parent.name
    docs: list[tuple[str, str]] = []
    for e in m.get("recent_learnings") or []:
        txt = e.get("text", "") if isinstance(e, dict) else str(e)
        if txt.strip():
            date = e.get("date", "") if isinstance(e, dict) else ""
            docs.append((f"{agent} learning", f"[{agent} {date}] {txt}"))
    for e in m.get("global_patterns") or []:
        txt = e.get("pattern", "") if isinstance(e, dict) else str(e)
        if txt.strip():
            n = e.get("n_observations", "") if isinstance(e, dict) else ""
            docs.append((f"{agent} pattern", f"[{agent} pattern x{n}] {txt}"))
    for e in m.get("self_observations") or []:
        txt = e.get("text", e.get("observation", "")) if isinstance(e, dict) else str(e)
        if txt.strip():
            docs.append((f"{agent} observation", f"[{agent}] {txt}"))
    return docs


def docs_for_sessions(p: Path) -> list[tuple[str, str]]:
    """sessions.jsonl → one doc per session with a non-empty summary/learnings."""
    docs: list[tuple[str, str]] = []
    try:
        lines = p.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    for ln in lines:
        try:
            s = json.loads(ln)
        except json.JSONDecodeError:
            continue
        parts = []
        if s.get("summary"):
            parts.append(str(s["summary"]))
        for learning in s.get("learnings") or []:
            parts.append(f"learning: {learning}")
        if s.get("topics"):
            parts.append("topics: " + ", ".join(map(str, s["topics"])))
        if not parts:
            continue
        ts = str(s.get("ts", ""))[:10]
        docs.append((f"session {ts}", "\n".join(parts)[:MAX_CHUNK_CHARS]))
    return docs


def docs_for_canonical(p: Path) -> list[tuple[str, str]]:
    try:
        d = json.loads(p.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return []
    title = d.get("title") or d.get("key") or p.stem
    body = d.get("body") or json.dumps(d, ensure_ascii=False)
    return [(title, f"{title}\n\n{body}"[:MAX_CHUNK_CHARS])]


def gather_corpus() -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """relpath → (source_type, docs). Docs computed lazily only for changed files
    — here we just enumerate (path, source_type, mtime); embedding happens later."""
    corpus: dict[str, str] = {}
    for p in collect_markdown_files():
        corpus[str(p.relative_to(VAULT))] = "markdown"
    state = VAULT / "_agent_state"
    if state.exists():
        for p in state.glob("*/memory.json"):
            corpus[str(p.relative_to(VAULT))] = "memory"
    sessions = VAULT / "_agent_state/claude-code/sessions.jsonl"
    if sessions.exists():
        corpus[str(sessions.relative_to(VAULT))] = "session"
    canonical = VAULT / "_brain_api/canonical"
    if canonical.exists():
        for p in canonical.rglob("*.json"):
            corpus[str(p.relative_to(VAULT))] = "canonical"
    return corpus


DOC_FNS = {
    "markdown": docs_for_markdown,
    "memory": docs_for_memory,
    "session": docs_for_sessions,
    "canonical": docs_for_canonical,
}


# ── index build ──────────────────────────────────────────────────────────────

def open_client(once_if_free: bool):
    from qdrant_client import QdrantClient
    QDRANT_PATH.mkdir(parents=True, exist_ok=True)
    try:
        return QdrantClient(path=str(QDRANT_PATH))
    except RuntimeError as e:
        if "already accessed" in str(e):
            level = "skip (opportunistic)" if once_if_free else "skip"
            log(f"DB locked by another process (open Claude session?) — {level}. "
                "BM25 recall stays fresh; next nightly run catches up.")
            sys.exit(0)
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="drop collection + manifest, full re-embed")
    ap.add_argument("--once-if-free", action="store_true", help="non-blocking: silent skip if DB locked")
    args = ap.parse_args()

    from qdrant_client import models
    from fastembed import TextEmbedding

    client = open_client(args.once_if_free)

    manifest: dict = {}
    if MANIFEST_PATH.exists() and not args.rebuild:
        try:
            manifest = json.loads(MANIFEST_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            log("manifest unreadable — falling back to full rebuild")
            args.rebuild = True

    if args.rebuild and client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
        manifest = {}
        log("dropped existing collection (--rebuild)")

    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config={VECTOR_NAME: models.VectorParams(
                size=VECTOR_SIZE, distance=models.Distance.COSINE)},
        )
        log(f"created collection '{COLLECTION}' vector '{VECTOR_NAME}' ({VECTOR_SIZE}, COSINE)")

    # sanity-assert the contract every run — a drifted vector name = silent zero results
    info = client.get_collection(COLLECTION)
    vectors = info.config.params.vectors
    assert VECTOR_NAME in vectors and vectors[VECTOR_NAME].size == VECTOR_SIZE, (
        f"collection vector config drifted: {vectors!r} — expected {VECTOR_NAME}/{VECTOR_SIZE}. "
        "Rebuild with --rebuild after verifying mcp-server-qdrant's get_vector_name().")

    # Safety gate BEFORE any diffing: if the vault looks partially or fully
    # invisible (OneDrive offline, TCC denial under launchd, eviction), abort
    # rather than interpret missing files as deletions. A wrong "removed"
    # sweep nukes the index (happened 2026-06-05: launchd ran uv without
    # /bin/bash's TCC grant — 240 'removed', 1755 points deleted).
    missing_roots = [r for r in MARKDOWN_ROOTS if not (VAULT / r).exists()]
    if missing_roots:
        log(f"ABORT: vault roots not visible: {missing_roots} — "
            "vault offline or process lacks access (TCC?). No changes made.")
        return 1

    corpus = gather_corpus()

    if manifest:
        gone = sum(1 for rel in manifest if rel not in corpus)
        if gone > max(10, len(manifest) * 0.3):
            log(f"ABORT: {gone}/{len(manifest)} manifest files vanished (>30%) — "
                "refusing to mass-delete. Run with --rebuild if this is intentional.")
            return 1

    # diff vs manifest
    changed, removed = [], []
    for rel, source_type in corpus.items():
        mtime = (VAULT / rel).stat().st_mtime
        entry = manifest.get(rel)
        if not entry or entry["mtime"] != mtime:
            changed.append((rel, source_type, mtime))
    for rel in list(manifest):
        if rel not in corpus:
            removed.append(rel)

    log(f"corpus {len(corpus)} files — {len(changed)} changed, {len(removed)} removed")
    if not changed and not removed:
        log("index up to date")
        return 0

    # ~90MB ONNX. Persistent cache (NOT the temp-dir default — macOS purges it; see recall_daemon._load_model)
    _fe_cache = BUILD_HOME / "fastembed-cache"; _fe_cache.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(MODEL_NAME, cache_dir=str(_fe_cache))

    # deletions first (removed files + stale points of changed files)
    stale_ids = [pid for rel in removed for pid in manifest[rel]["ids"]]
    for rel, _, _ in changed:
        if rel in manifest:
            stale_ids += manifest[rel]["ids"]
    if stale_ids:
        client.delete(collection_name=COLLECTION,
                      points_selector=models.PointIdsList(points=stale_ids))
        log(f"deleted {len(stale_ids)} stale points")
    for rel in removed:
        del manifest[rel]

    total_points = 0
    batch: list[models.PointStruct] = []
    for rel, source_type, mtime in changed:
        docs = DOC_FNS[source_type](VAULT / rel)
        ids = []
        if docs:
            embeddings = list(model.passage_embed([t for _, t in docs]))  # passage_embed = mcp-server-qdrant's doc path
            for i, ((title, text), emb) in enumerate(zip(docs, embeddings)):
                pid = point_id(rel, i)
                ids.append(pid)
                batch.append(models.PointStruct(
                    id=pid,
                    vector={VECTOR_NAME: emb.tolist()},
                    payload={
                        "document": text,
                        "metadata": {
                            "path": rel,
                            "title": title,
                            "snippet": text[:SNIPPET_CHARS],
                            "mtime": mtime,
                            "source_type": source_type,
                        },
                    },
                ))
        manifest[rel] = {"mtime": mtime, "ids": ids}
        total_points += len(ids)
        if len(batch) >= 256:
            client.upsert(collection_name=COLLECTION, points=batch)
            batch = []
    if batch:
        client.upsert(collection_name=COLLECTION, points=batch)

    MANIFEST_PATH.write_text(json.dumps(manifest))
    count = client.count(COLLECTION).count
    log(f"upserted {total_points} points from {len(changed)} files — collection total {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
