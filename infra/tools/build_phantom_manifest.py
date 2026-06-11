#!/usr/bin/env python3
"""Generate _brain_api/bid/<bid_id>/phantoms.json — the Phantom Files data layer.

Ghost rows for Obsidian's file explorer: artifacts a WINNING bid would have at
this stage but this bid is missing.

For each open bid in _brain_api/bid/_open.json (fallback: _brain_index/bids/_open.json):
  1. Resolve the bid folder from the bid record's `path` (vault-relative).
  2. List the folder's actual files (recursive; dotfiles and *.bak* excluded).
  3. Diff against the stage-conditioned expected-artifact skeleton at
     99_Meta/winning-skeleton.yaml. Stage conditioning is CUMULATIVE: a bid at
     stage S expects every artifact whose stage_due <= S in the order
     Discover < Qualify < Propose < Negotiate.
  4. Write _brain_api/bid/<bid_id>/phantoms.json.

Matching is deliberately GENEROUS — zero false ghosts is the acceptance bar.
An artifact counts as PRESENT if ANY file in the bid folder matches ANY of its
match_globs, case-insensitive and accent-insensitive (NFKD fold), tested against
both the basename and the folder-relative path. Artifacts flagged
`content_check: true` (the opportunity brief) additionally require a matching
.md to NOT be a scaffold (frontmatter `status: scaffold`, or fewer than
MIN_BODY_CHARS of meaningful body after stripping headings/quotes/empty bullets).

Provenance:
- "doctrine" — seeded from 99_Meta/winning-skeleton.yaml (Shipley-style discipline).
- "learned"  — ONLY when _agent_state/outcome-ledger.jsonl holds >= LEARN_MIN_BIDS
  closed bids carrying artifact data (records with outcome won|lost AND an
  `artifacts` list of filenames snapshotted at close). Evidence then reads
  "present in k/n won bids at <stage>". The current ledger (close_bid.py Phase 2)
  records canonical-block usage, not artifacts, so until the close hook snapshots
  bid-folder artifacts everything stays "doctrine". Learned evidence is NEVER
  fabricated.

Usage:
    python3 build/tools/build_phantom_manifest.py
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
API = VAULT / "_brain_api"
INDEX = VAULT / "_brain_index"
SKELETON = VAULT / "99_Meta" / "winning-skeleton.yaml"
LEDGER = VAULT / "_agent_state" / "outcome-ledger.jsonl"

STAGES = ["Discover", "Qualify", "Propose", "Negotiate"]
DOCTRINE_EVIDENCE = "bid doctrine — ledger learning"
LEARN_MIN_BIDS = 5      # closed bids with artifact data before "learned" kicks in
MIN_BODY_CHARS = 120    # content_check: meaningful body below this = scaffold


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def normalize(s: str) -> str:
    """Case-insensitive + accent-insensitive canonical form for glob matching."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).casefold()


def load_skeleton() -> dict:
    if yaml is None:
        print("FATAL: PyYAML not available — cannot parse 99_Meta/winning-skeleton.yaml", file=sys.stderr)
        sys.exit(1)
    if not SKELETON.exists():
        print(f"FATAL: skeleton not found at {SKELETON}", file=sys.stderr)
        sys.exit(1)
    data = yaml.safe_load(SKELETON.read_text())
    if not isinstance(data, dict) or "artifacts" not in data:
        print(f"FATAL: malformed skeleton at {SKELETON}", file=sys.stderr)
        sys.exit(1)
    return data


def list_bid_files(bid_dir: Path) -> list[Path]:
    """All real files in the bid folder, recursive. Dotfiles and *.bak* excluded."""
    files = []
    for p in bid_dir.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(bid_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if ".bak" in p.name:
            continue
        files.append(p)
    return files


def glob_match(globs: list[str], bid_dir: Path, files: list[Path]) -> list[Path]:
    """Files matching any glob — against basename AND folder-relative path."""
    norm_globs = [normalize(g) for g in globs]
    hits = []
    for f in files:
        name = normalize(f.name)
        rel = normalize(f.relative_to(bid_dir).as_posix())
        if any(fnmatch.fnmatchcase(name, g) or fnmatch.fnmatchcase(rel, g)
               for g in norm_globs):
            hits.append(f)
    return hits


def is_scaffold_md(path: Path) -> bool:
    """A .md counts as a scaffold if frontmatter says `status: scaffold`,
    or its body holds < MIN_BODY_CHARS of meaningful content
    (headings, blockquotes, empty bullets and blanks stripped)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False  # unreadable → err toward present (no false ghosts)
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    if k.strip().lower() == "status" and \
                       v.strip().strip("\"'").lower() == "scaffold":
                        return True
            body = text[end + 4:]
    meaningful = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">") or s in ("-", "*"):
            continue
        meaningful.append(s)
    return len(" ".join(meaningful)) < MIN_BODY_CHARS


def artifact_present(entry: dict, bid_dir: Path, files: list[Path]) -> bool:
    hits = glob_match(entry.get("match_globs", []), bid_dir, files)
    if not hits:
        return False
    if not entry.get("content_check"):
        return True
    # content_check: at least one matching file must be a real (non-scaffold) doc.
    # Non-.md matches (e.g. a PDF) can't be inspected → count as present.
    for f in hits:
        if f.suffix.lower() != ".md" or not is_scaffold_md(f):
            return True
    return False


def verify_template(template_path) -> "str | None":
    """Only emit template_path if the file really exists in the vault."""
    if not template_path:
        return None
    return template_path if (VAULT / template_path).is_file() else None


def urgency_days(deadline: str) -> "int | None":
    """Days until the bid deadline (negative = overdue). None if no deadline."""
    if not deadline:
        return None
    try:
        d = datetime.strptime(str(deadline).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - date.today()).days


def load_ledger_artifact_bids() -> dict:
    """Closed bids in the outcome ledger that carry artifact data.

    Returns {bid_id: {"outcome": "won"|"lost", "artifacts": set[str]}}.
    A record contributes only if it has bid_id, outcome won|lost, and a
    non-empty `artifacts` list (filenames snapshotted at close). The current
    close_bid.py schema records block usage without artifacts → empty dict.
    """
    bids: dict = {}
    if not LEDGER.exists():
        return bids
    try:
        lines = LEDGER.read_text().splitlines()
    except OSError:
        return bids
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        bid_id = rec.get("bid_id")
        outcome = str(rec.get("outcome", "")).lower()
        arts = rec.get("artifacts")
        if not bid_id or outcome not in ("won", "lost"):
            continue
        if not isinstance(arts, list) or not arts:
            continue
        entry = bids.setdefault(bid_id, {"outcome": outcome, "artifacts": set()})
        entry["outcome"] = outcome
        entry["artifacts"].update(str(a) for a in arts)
    return bids


def learned_evidence(entry: dict, stage_due: str, ledger_bids: dict) -> "tuple[str, str]":
    """(provenance, evidence) for one skeleton artifact.

    "learned" ONLY when >= LEARN_MIN_BIDS closed bids carry artifact data AND
    at least one of them was won. Otherwise doctrine. Never fabricated.
    """
    if len(ledger_bids) < LEARN_MIN_BIDS:
        return "doctrine", DOCTRINE_EVIDENCE
    won = {b: d for b, d in ledger_bids.items() if d["outcome"] == "won"}
    if not won:
        return "doctrine", DOCTRINE_EVIDENCE
    norm_globs = [normalize(g) for g in entry.get("match_globs", [])]
    k = sum(
        1 for d in won.values()
        if any(fnmatch.fnmatchcase(normalize(a), g)
               for a in d["artifacts"] for g in norm_globs)
    )
    return "learned", f"present in {k}/{len(won)} won bids at {stage_due}"


def build_phantoms_for_bid(bid: dict, skeleton: dict, ledger_bids: dict) -> "dict | None":
    bid_id = bid.get("bid_id", "")
    bid_path = bid.get("path", "")
    stage = bid.get("stage", "")
    if not bid_id or stage not in STAGES:
        print(f"  ⚠ {bid_id or '?'}: stage {stage!r} not in {STAGES} — skipping")
        return None
    bid_dir = VAULT / bid_path
    if not bid_path or not bid_dir.is_dir():
        print(f"  ⚠ {bid_id}: bid folder not found at {bid_path!r} — skipping")
        return None

    files = list_bid_files(bid_dir)
    stage_idx = STAGES.index(stage)
    days = urgency_days(bid.get("deadline", ""))

    phantoms = []
    n_expected = 0
    for stage_due in STAGES[: stage_idx + 1]:
        for entry in skeleton.get("artifacts", {}).get(stage_due, []) or []:
            n_expected += 1
            if artifact_present(entry, bid_dir, files):
                continue
            provenance, evidence = learned_evidence(entry, stage_due, ledger_bids)
            phantoms.append({
                "artifact": entry.get("artifact", ""),
                "filename_pattern": entry.get("filename_pattern")
                                    or (entry.get("match_globs") or [""])[0],
                "match_globs": entry.get("match_globs", []),
                "evidence": evidence,
                "provenance": provenance,
                "template_path": verify_template(entry.get("template_path")),
                "stage_due": stage_due,
                "urgency_days": days,
            })

    out = {
        "bid_id": bid_id,
        "bid_path": bid_path,
        "stage": stage,
        "generated": utcnow(),
        "phantoms": phantoms,
    }
    out_dir = API / "bid" / bid_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phantoms.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"  {bid_id} ({stage}): {len(phantoms)} phantoms / {n_expected} expected"
          f" / {len(files)} files on disk")
    return out


def main() -> int:
    skeleton = load_skeleton()
    ledger_bids = load_ledger_artifact_bids()

    open_data = safe_load(API / "bid" / "_open.json", None) \
        or safe_load(INDEX / "bids" / "_open.json", {"bids": []})
    bids = open_data.get("bids", [])

    print(f"Phantom manifest — {len(bids)} open bids, "
          f"ledger artifact data: {len(ledger_bids)} closed bids "
          f"({'learned mode' if len(ledger_bids) >= LEARN_MIN_BIDS else 'doctrine mode'})")
    n = sum(1 for b in bids if build_phantoms_for_bid(b, skeleton, ledger_bids))
    print(f"Built {n} phantoms.json under {API / 'bid'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
