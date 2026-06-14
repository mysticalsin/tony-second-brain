#!/usr/bin/env python3
"""
ingest_transcripts.py — Convert any transcript file to SBAP-compliant markdown
in 00_Inbox/from-meetings/<source>/.

Usage:
    python3 infra/tools/ingest_transcripts.py [SOURCE_PATH...]

If no path given: scans known locations configured via SCAN_LOCATIONS.

Supported input formats:
    .txt   — plain text (Cluely-style monologue, Whisper output)
    .vtt   — WebVTT (Teams subtitle format)
    .srt   — SubRip subtitle
    .docx  — Microsoft Word (Teams "Save transcript" output)

Output format: SBAP-compliant markdown at:
    00_Inbox/from-meetings/<source>/<YYYY-MM-DD>-<topic-slug>.md

Sources detected by path pattern:
    "cluely"     -> 00_Inbox/from-meetings/cluely/
    "teams"      -> 00_Inbox/from-meetings/teams/
    "whisper"    -> 00_Inbox/from-meetings/manual/
    otherwise    -> 00_Inbox/from-meetings/manual/

Idempotent: skips files whose content hash is already recorded in
99_Meta/transcript-ingest-log.jsonl.

Environment:
    VAULT_ROOT   (required) — absolute path to your vault root
"""

from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(
    SystemExit("Set VAULT_ROOT to your vault path")))
INBOX = VAULT / "00_Inbox" / "from-meetings"
LOG = VAULT / "99_Meta" / "transcript-ingest-log.jsonl"
ACCOUNT_ALIASES_FILE = VAULT / "99_Meta" / "account-aliases.json"
BY_CLIENT_DIR = VAULT / "Meetings" / "by-client"

META_DIR = VAULT / "99_Meta"
QUARANTINE_DIR = META_DIR / "sbap-quarantine"
VIOLATIONS_LOG = META_DIR / "conduct-violations.jsonl"
_SCANNER_MOD = None  # lazy-loaded once


def _load_scanner():
    """Load injection-scan.py via importlib (hyphenated name prevents normal import)."""
    global _SCANNER_MOD
    if _SCANNER_MOD is not None:
        return _SCANNER_MOD
    scanner_path = META_DIR / "injection-scan.py"
    if not scanner_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("injection_scan", scanner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SCANNER_MOD = mod
    return mod


def _scan_text_for_injection(content: str, source_path: Path) -> list[dict]:
    """
    Scan transcript text for prompt-injection patterns.
    Returns list of hit dicts (empty = clean).
    Writes the content to a temp file, calls scanner's scan_file(), then removes the temp.
    Safe default: returns [] (no hits) if the scanner cannot be loaded or errors.
    """
    try:
        mod = _load_scanner()
        if mod is None:
            return []
        # Write to a named temp file so scan_file() can read it
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            hits = mod.scan_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return hits
    except Exception as e:
        print(f"  injection-scan error ({e}); continuing ingest (safe default)", file=sys.stderr)
        return []


def _quarantine_and_log(source_path: Path, hits: list[dict]) -> None:
    """
    Move the source transcript file to sbap-quarantine/ and log each hit to
    conduct-violations.jsonl.  Non-destructive: uses rename-with-counter on collision.
    """
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    dest = QUARANTINE_DIR / (source_path.name + ".injection-flagged")
    counter = 1
    while dest.exists():
        dest = QUARANTINE_DIR / (f"{source_path.name}.{counter}.injection-flagged")
        counter += 1
    try:
        source_path.rename(dest)
    except OSError as e:
        print(f"  quarantine move failed ({e}); file left in place", file=sys.stderr)

    # Log each hit to conduct-violations.jsonl
    VIOLATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with VIOLATIONS_LOG.open("a", encoding="utf-8") as fh:
        for h in hits:
            record = {
                "ts":       ts,
                "source":   "injection-scan",
                "agent":    "meeting-ingest",
                "rule":     h.get("rule", "unknown"),
                "severity": h.get("severity", "unknown"),
                "detail":   f"line {h.get('line_no', '?')}: {h.get('matched_text', '')!r} — {h.get('snippet', '')[:120]}",
                "file":     str(source_path),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# Known input locations to scan when no args given.
# Each tuple is (path, max_depth) — keeps the scan fast on big folders.
# Populate SCAN_EXTRA_LOCATIONS env var (colon-separated paths) to add more.
_extra_raw = os.environ.get("SCAN_EXTRA_LOCATIONS", "")
_extra_locs = [(Path(p), 2) for p in _extra_raw.split(":") if p.strip()]

SCAN_LOCATIONS: list[tuple[Path, int]] = [
    (VAULT / "00_Inbox" / "from-meetings" / "manual" / "_dropbox", 2),
    (VAULT / "00_Inbox" / "from-meetings" / "_unprocessed", 2),
] + _extra_locs


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Account routing
# ---------------------------------------------------------------------------

def _load_account_aliases() -> dict[str, list[str]]:
    """Load account-aliases.json; returns {} on any error (safe default)."""
    try:
        with ACCOUNT_ALIASES_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        # Strip the _comment meta-key
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        print(f"  account-aliases.json load failed ({e}); routing disabled", file=sys.stderr)
        return {}


def detect_account(filename: str, text: str) -> Optional[str]:
    """
    Return the account folder name (e.g. 'acme-corp') if the transcript
    matches any alias, else None.

    Matching: case-insensitive; alias must appear as a word-boundary token
    (prevents partial-word matches).  filename and first 4000 chars of text
    are both searched.

    Separator normalization: hyphens/underscores/dots/slashes in BOTH the
    haystack and each alias are collapsed to spaces, so multi-word aliases
    like "dust partnership" match hyphenated filenames like
    "dust-partnership-2025.md" (routing-fix).
    """
    aliases = _load_account_aliases()
    if not aliases:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[-_./]+", " ", s.lower())

    haystack = _norm(filename + " " + text[:4000])

    for account, alias_list in aliases.items():
        for alias in alias_list:
            a = _norm(alias)
            # word-boundary around the alias
            pattern = r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])"
            if re.search(pattern, haystack):
                return account
    return None


def append_to_client_index(account: str, date_str: str, topic: str, out_path: Path) -> None:
    """
    Ensure Meetings/by-client/<account>/_index.md exists and append a dated link.
    Creates the file with a header if missing.  Never overwrites existing content.
    """
    index_dir = BY_CLIENT_DIR / account
    index_dir.mkdir(parents=True, exist_ok=True)
    index_file = index_dir / "_index.md"

    link_line = f"- [{date_str} — {topic}]({out_path.relative_to(VAULT)})\n"

    if not index_file.exists():
        header = f"""---
type: client-meeting-index
account: {account}
generated_by: ingest_transcripts
---

# Meetings — {account}

> Auto-maintained by `ingest_transcripts.py`. One entry per ingested transcript routed to this account.

```dataview
TABLE WITHOUT ID file.link AS "Meeting", date AS "Date", source AS "Source"
FROM "Meetings/by-client/{account}"
WHERE file.name != "_index"
SORT date DESC
```

## Transcript links

"""
        index_file.write_text(header + link_line, encoding="utf-8")
    else:
        with index_file.open("a", encoding="utf-8") as f:
            f.write(link_line)


def already_processed(content_hash: str) -> bool:
    if not LOG.exists():
        return False
    with LOG.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("hash") == content_hash:
                    return True
            except json.JSONDecodeError:
                continue
    return False


def detect_source(path: Path, content: str) -> str:
    """
    Detect source: cluely, teams, manual.
    Strict — false positives end up mis-tagged.
    Only trust strong signals (specific path segments, not generic substrings).
    """
    p = str(path).lower()
    c = content.lower()[:1000]

    # CLUELY: path segment '/cluely/' or filename starts with 'cluely'
    if "/cluely/" in p or path.name.lower().startswith("cluely"):
        return "cluely"

    # TEAMS: only path segment '/from-meetings/teams/' or filename starts with 'teams_' or 'teams-transcript'
    if "/from-meetings/teams/" in p:
        return "teams"
    if path.name.lower().startswith(("teams_", "teams-transcript", "teams-meeting")):
        return "teams"
    # Microsoft Teams transcript header phrases (multi-language)
    if any(s in c for s in ["transcripcion de la reunion", "transcricao da reuniao", "meeting transcript"]):
        if c.count(". ") + c.count("? ") > 5:
            return "teams"

    # Fallback: manual archive
    return "manual"


def slugify(text: str, maxlen: int = 50) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:maxlen].strip("-")


def extract_date(path: Path, content: str) -> str:
    """Extract date from filename or default to file mtime."""
    name = path.name
    for pattern in [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{4})(\d{2})(\d{2})",
        r"(\d{4}-\d{2}-\d{2})T",
    ]:
        m = re.search(pattern, name)
        if m:
            grp = m.groups()
            if len(grp) == 1:
                return grp[0]
            elif len(grp) == 3:
                return f"{grp[0]}-{grp[1]}-{grp[2]}"
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime.strftime("%Y-%m-%d")


def extract_topic(content: str, fallback: str = "untitled") -> str:
    """Pull first meaningful line as the topic."""
    for line in content.split("\n")[:10]:
        line = line.strip()
        if 15 < len(line) < 100:
            return line
    cleaned = " ".join(content.split())[:60]
    return cleaned if cleaned else fallback


def read_vtt(path: Path) -> str:
    """Strip WebVTT/SRT timing + cue identifiers; keep speaker voice tags."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    uuid_cue = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(/[\w.-]+)?$"
    )
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            continue
        if re.match(r"^\d+$", line):
            continue
        if uuid_cue.match(line):
            continue
        m = re.match(r"^<v\s+([^>]+)>(.*)$", line)
        if m:
            speaker = m.group(1).strip()
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            line = f"{speaker}: {text}" if text else ""
        else:
            line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def read_srt(path: Path) -> str:
    """Strip SRT timing, return clean text. Same logic as VTT."""
    return read_vtt(path)


def read_docx(path: Path) -> Optional[str]:
    """Try to read docx via python-docx if installed, else None."""
    try:
        from docx import Document  # pip install python-docx
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        return None
    except Exception:
        return None


def read_file(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    try:
        if ext == ".txt":
            return path.read_text(encoding="utf-8", errors="replace")
        if ext == ".vtt":
            return read_vtt(path)
        if ext == ".srt":
            return read_srt(path)
        if ext == ".docx":
            return read_docx(path)
    except Exception as e:
        print(f"  read failed: {e}", file=sys.stderr)
        return None
    return None


def build_markdown(
    path: Path,
    content: str,
    source: str,
    date_str: str,
    topic: str,
    account: Optional[str] = None,
) -> str:
    """Build SBAP-compliant markdown."""
    run_id = f"ingest-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    content_hash = hash_content(content)
    word_count = len(content.split())
    char_count = len(content)

    # Determine target_path: routed to Clients/<account>/ when detected,
    # otherwise the original inbox path (backward-compatible fallback).
    if account:
        target_path = f"Clients/{account}/{date_str}-{slugify(topic)}.md"
    else:
        target_path = f"00_Inbox/from-meetings/{source}/{date_str}-{slugify(topic)}.md"

    account_fm_lines = ""
    if account:
        account_fm_lines = f'account: "{account}"\n'

    tag_parts = f"meeting, transcript, {source}"
    if account:
        tag_parts += f", {account}"

    frontmatter = f"""---
sbap_version: "1.0"
source_agent: ingest_transcripts
source_run_id: "{run_id}"
generated: "{datetime.now().isoformat()}"
input_context_refs:
  - "{path}"
output_type: transcript
target_path: "{target_path}"
confidence: 0.95
source: "{source}"
ingested_from: "{path}"
content_hash: "{content_hash}"
date: "{date_str}"
word_count: {word_count}
char_count: {char_count}
{account_fm_lines}tags: [{tag_parts}]
---

# {topic}

> **Source:** `{source}` · **Date:** {date_str} · **Words:** {word_count} · **Ingested:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
> **Original file:** `{path.name}`

---

{content}
"""
    return frontmatter


def write_output(source: str, date_str: str, topic: str, markdown: str) -> Path:
    out_dir = INBOX / source
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{date_str}-{slugify(topic)}.md"
    out_path = out_dir / fname
    counter = 2
    while out_path.exists():
        out_path = out_dir / f"{date_str}-{slugify(topic)}-{counter}.md"
        counter += 1
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


def log_ingest(path: Path, content_hash: str, source: str, out_path: Path) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "source_file": str(path),
        "hash": content_hash,
        "source": source,
        "output": str(out_path.relative_to(VAULT)),
    }
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


EXCLUDE_FILENAMES = {
    "requirements.txt", "requirements-dev.txt", "constraints.txt",
    "FILE_MANIFEST.txt", "robots.txt", "llms.txt", "llms-full.txt",
    "CMakeLists.txt", "package.json", "package-lock.json",
    "sample_meeting.txt",
}

EXCLUDE_NAME_PATTERNS = [
    r"^license",
    r"^copying",
    r"^changelog",
    r"^readme",
    r"\.min\.",
    r"node_modules",
    r"site-packages",
    r"dist-info",
]

CODE_INDICATORS = [
    "import ", "from __future__", "def ", "function ",
    "#!/usr/bin/env", "#!/bin/bash", "<?xml", "<html",
    "// Copyright", "/* Copyright",
    "Permission is hereby granted",
    "Licensed under the Apache",
    "User-Agent:",
]


def looks_like_transcript(content: str) -> bool:
    """Heuristic: real transcripts have sentences, not code/config."""
    head = content[:2000]
    code_hits = sum(1 for ind in CODE_INDICATORS if ind in head)
    if code_hits >= 2:
        return False
    sentences = head.count(". ") + head.count("? ") + head.count("! ")
    if sentences < 3:
        return False
    lines = [l for l in head.split("\n") if l.strip()]
    if not lines:
        return False
    avg_line = sum(len(l) for l in lines) / len(lines)
    if avg_line < 25:
        return False
    return True


def process_file(path: Path) -> bool:
    """Process a single transcript file. Returns True if ingested."""
    if not path.is_file():
        return False
    if path.suffix.lower() not in {".txt", ".vtt", ".srt", ".docx"}:
        return False
    fname_lower = path.name.lower()
    if fname_lower in EXCLUDE_FILENAMES:
        return False
    for pattern in EXCLUDE_NAME_PATTERNS:
        if re.search(pattern, fname_lower):
            return False
    content = read_file(path)
    if not content or len(content) < 100:
        print(f"  skipped (too short): {path.name}")
        return False
    if path.suffix.lower() not in {".vtt", ".srt"} and not looks_like_transcript(content):
        print(f"  skipped (doesn't look like transcript): {path.name}")
        return False
    content_hash = hash_content(content)
    if already_processed(content_hash):
        print(f"  skipped (already ingested): {path.name}")
        return False

    # ── Injection scan ──────────────────────────────────────────────────────
    # Fetched transcripts are untrusted; scan BEFORE writing anything.
    inj_hits = _scan_text_for_injection(content, path)
    if inj_hits:
        worst = max(inj_hits, key=lambda h: {"low": 0, "med": 1, "high": 2}.get(h.get("severity", ""), 0))
        print(
            f"  QUARANTINED (injection detected — {len(inj_hits)} hit(s), "
            f"worst: {worst.get('severity','?').upper()} {worst.get('rule','')}): {path.name}",
            file=sys.stderr,
        )
        _quarantine_and_log(path, inj_hits)
        return False
    # ── End injection scan ───────────────────────────────────────────────────

    source = detect_source(path, content)
    date_str = extract_date(path, content)
    topic = extract_topic(content, fallback=path.stem)
    account = detect_account(path.name, content)
    md = build_markdown(path, content, source, date_str, topic, account=account)
    out_path = write_output(source, date_str, topic, md)
    log_ingest(path, content_hash, source, out_path)
    if account:
        append_to_client_index(account, date_str, topic, out_path)
        print(f"  ingested -> {out_path.relative_to(VAULT)}  ({source}, {len(content.split())} words) [account: {account}]")
    else:
        print(f"  ingested -> {out_path.relative_to(VAULT)}  ({source}, {len(content.split())} words)")
    return True


def scan_locations() -> list[Path]:
    """Scan known locations with bounded depth (avoids hanging on huge dirs)."""
    paths = []
    for loc, max_depth in SCAN_LOCATIONS:
        if not loc.exists():
            continue
        for f in _bounded_walk(loc, max_depth):
            if f.suffix.lower() in {".txt", ".vtt", ".srt", ".docx"}:
                paths.append(f)
    return paths


def _bounded_walk(root: Path, max_depth: int):
    """Walk a directory tree with a max depth limit."""
    if max_depth < 0:
        return
    try:
        for entry in root.iterdir():
            if entry.is_file():
                yield entry
            elif entry.is_dir() and max_depth > 0 and not entry.name.startswith("."):
                yield from _bounded_walk(entry, max_depth - 1)
    except (PermissionError, OSError):
        pass


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        targets = [Path(a) for a in argv[1:]]
    else:
        print("No args given — scanning known locations:")
        for loc, depth in SCAN_LOCATIONS:
            print(f"  - {loc} (max depth {depth})")
        targets = scan_locations()

    print(f"\nFound {len(targets)} candidate file(s).\n")

    ingested = 0
    for path in targets:
        try:
            if process_file(path):
                ingested += 1
        except Exception as e:
            print(f"  error on {path.name}: {e}", file=sys.stderr)

    print(f"\nIngested {ingested} new transcript(s).")
    print(f"  Log: {LOG.relative_to(VAULT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
