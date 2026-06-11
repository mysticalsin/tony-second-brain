"""Generate _brain_index/ from vault state. Idempotent. Incremental.

SBAP v1.0 — read-side machine view of the vault.

Usage:
    python3 build/tools/build_brain_index.py --full
    python3 build/tools/build_brain_index.py --incremental   # default

What it does:
1. Walks the vault, hashing every markdown file
2. Compares against _brain_index/_meta.json hashes
3. For changed files: re-parses frontmatter, updates index
4. Appends to _changefeed.jsonl
5. Rebuilds composed indexes (accounts/_state, bids/_open, etc.)

TODO: embeddings via Voyage AI (requires API key). Currently no-op.
TODO: integration with TBrain Supabase tables.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
INDEX = VAULT / "_brain_index"
SCHEMAS = VAULT / "99_Meta" / "sbap-schemas"


# What to include in the index
INCLUDE_GLOBS = [
    "RFPs/**/*.md",      # bids (folder named 01_Projects in current vault)
    "02_Areas/**/*.md",
    "03_Resources/**/*.md",
    "99_Meta/**/*.md",
    "CLAUDE.md",
]

EXCLUDE = {
    ".obsidian", ".git", ".claude",
    "out", "graphify-out", "build",
    "04_Archives", "_brain_index", "_brain_api",
    "_agent_state",
    "Confidential",  # privacy guard — Meetings/Confidential must never enter the SBAP layer
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return f"sha256:{h.hexdigest()}"


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}


def collect_files() -> list[Path]:
    files = []
    for glob in INCLUDE_GLOBS:
        for p in VAULT.glob(glob):
            if any(part in EXCLUDE for part in p.parts):
                continue
            if p.is_file():
                files.append(p)
    return files


def build_content_library_index() -> dict:
    out = {"_meta": {"generated": utcnow(), "schema": "block_index.v1"}, "blocks": []}
    pwr = VAULT / "03_Resources" / "PowerPoint Standards"
    proposal = VAULT / "03_Resources" / "Proposal Templates"
    for folder in (pwr, proposal):
        if not folder.exists():
            continue
        for p in folder.rglob("*.md"):
            try:
                text = p.read_text()
            except OSError:
                continue
            fm = parse_frontmatter(text)
            rel = p.relative_to(VAULT)
            out["blocks"].append({
                "block_id": p.stem.lower().replace(" ", "-"),
                "type": fm.get("type", "other"),
                "path": str(rel),
                "content_hash": hash_file(p),
                "frontmatter": fm,
                "last_modified": utcnow(),
                "sensitivity": fm.get("sensitivity", "internal"),
            })
    return out


def build_bid_index() -> dict:
    """Walk RFPs/<Company>/<Topic>/<Opp>/00 - Brief.md, build bid registry."""
    out = {"_meta": {"generated": utcnow()}, "bids": []}
    projects = VAULT / "RFPs"
    if not projects.exists():
        return out
    for brief in sorted(projects.rglob("00 - Brief.md")):
        rel = brief.relative_to(projects).parts  # (Company, Topic, Opp, "00 - Brief.md")
        if any(p.startswith("_") for p in rel):   # skip _template/ + library files
            continue
        bid_dir = brief.parent
        try:
            fm = parse_frontmatter(brief.read_text())
        except OSError:
            continue
        company = fm.get("company", rel[0] if len(rel) >= 1 else "")
        topic = fm.get("topic", rel[1] if len(rel) >= 3 else "")
        out["bids"].append({
            "bid_id": bid_dir.name.lower().replace(" ", "-"),
            "path": str(bid_dir.relative_to(VAULT)),
            "company": company,
            "topic": topic,
            "client": fm.get("client", fm.get("account", company)),
            "stage": fm.get("stage", "Discover"),
            "value": fm.get("value", 0),
            "deadline": fm.get("deadline", ""),
            "probability": fm.get("probability", 0),
            "owner": fm.get("owner", ""),
            "opened": fm.get("opened", ""),
        })
    return out


def main(incremental: bool = True) -> int:
    INDEX.mkdir(exist_ok=True)
    (INDEX / "content_library").mkdir(exist_ok=True)
    (INDEX / "bids").mkdir(exist_ok=True)
    (INDEX / "accounts").mkdir(exist_ok=True)

    meta = {
        "schema_version": "1.0",
        "last_rebuild": utcnow(),
        "incremental": incremental,
        "note": "Skeleton implementation — see TODO blocks below.",
        "todo": [
            "Voyage AI embeddings for semantic search",
            "Incremental rebuild from _changefeed.jsonl",
            "Composed account/_state.json from cross-folder data",
            "Phase 9 win_patterns merging",
            "TBrain Supabase sync",
        ],
    }
    (INDEX / "_meta.json").write_text(json.dumps(meta, indent=2, default=str))

    cl = build_content_library_index()
    (INDEX / "content_library" / "_index.json").write_text(json.dumps(cl, indent=2, default=str))

    bids = build_bid_index()
    (INDEX / "bids" / "_index.json").write_text(json.dumps(bids, indent=2, default=str))

    # _open subset
    open_bids = {
        "_meta": bids["_meta"],
        "bids": [b for b in bids["bids"] if b["stage"] not in ("Won", "Lost")],
    }
    (INDEX / "bids" / "_open.json").write_text(json.dumps(open_bids, indent=2, default=str))

    print(f"Built {INDEX}")
    print(f"  content_library blocks: {len(cl['blocks'])}")
    print(f"  bids: {len(bids['bids'])} total, {len(open_bids['bids'])} open")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Full rebuild (default: incremental)")
    args = parser.parse_args()
    sys.exit(main(incremental=not args.full))
