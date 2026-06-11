#!/usr/bin/env python3
"""promise_ledger.py — Track every commitment made in every meeting.

Two promise classes:
  owner-owes  : Tony/owner commits to do something ("I'll send the revised P&L by Friday").
  owed-to-owner : Another party commits to Tony ("we'll share the resolution-time data").

Extraction strategy (in order of preference):
  1. LLM mode  — claude -p haiku, strict JSON-only, conservative (confidence 0-1).
     Extract only explicit commitment language; due_date_inferred null unless strongly stated.
  2. --no-llm  — regex over Action-items bullets ("**Name**: action" pattern) plus
     modal-verb heuristics over Summary / Decisions text.

Records are written to:
  _brain_api/promises/ledger.json  — confidence >= CONFIDENCE_THRESHOLD
  _brain_api/promises/held.json    — confidence < CONFIDENCE_THRESHOLD (needs review)
  _brain_api/promises/summary.json — {due_48h, overdue_to_owner, oldest_unresolved, keep_rate_30d}
  _agent_state/promise-ledger/memory.json — seen-set + heartbeat (idempotency)

Reconciler: on each run, scan meetings newer than a promise for resolution signals.
  Past-due +3d  -> stale
  Past-due +10d -> broken

Signal weights / heuristics (--no-llm path):
  ACTION_ITEM_BULLET     : Action-items section "**Name**: action" -> confidence 0.82
  MODAL_VERB_WILL        : "will <verb>" in Summary/Decisions   -> confidence 0.72
  MODAL_VERB_SHALL       : "shall <verb>"                       -> confidence 0.75
  MODAL_VERB_GOING_TO    : "going to <verb>"                    -> confidence 0.68
  SEND_SHARE_DELIVER     : strong delivery verbs in context     -> +0.05 boost
  EXPLICIT_DATE          : date/day mentioned within 10 words   -> due_date_inferred set

Owner mapping: if actor name contains "Owner", "Speaker 2", or is blank on an owner-led
meeting  -> owner-owes.  Otherwise -> owed-to-owner.  LLM path uses full context.

Idempotency: _agent_state/promise-ledger/memory.json stores seen_files set (filename-based).
  On each run, only unseen files are extracted. --reconcile-only skips extraction entirely.

Usage:
    python build/tools/promise_ledger.py [--root VAULT] [--dry-run] [--no-llm]
    python build/tools/promise_ledger.py --reconcile-only
    python build/tools/promise_ledger.py --limit 12
    # Override vault root for testing:
    python build/tools/promise_ledger.py --root /tmp/test-vault --no-llm --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAULT_DEFAULT = Path(
    os.environ.get(
        "VAULT",
        os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")),
    )
).resolve()

MEETING_SUBDIR = "Meetings/Confidential"
LEDGER_DIR = "_brain_api/promises"
AGENT_STATE_DIR = "_agent_state/promise-ledger"

CONFIDENCE_THRESHOLD = 0.80   # below this -> held.json for review

# Days past due-date before status upgrade
STALE_DAYS = 3
BROKEN_DAYS = 10

# Heuristic confidence levels (--no-llm path)
CONF_ACTION_BULLET = 0.82
CONF_WILL = 0.72
CONF_SHALL = 0.75
CONF_GOING_TO = 0.68
DATE_BOOST = 0.05
DELIVERY_VERB_BOOST = 0.05

# Words that suggest delivery / commitment
DELIVERY_VERBS = [
    "send", "share", "deliver", "provide", "submit", "upload",
    "forward", "prepare", "create", "write", "draft", "review",
    "schedule", "book", "confirm", "contact", "reach out", "follow up",
    "update", "complete", "finish", "present",
]

# Phrases that signal resolution (promise kept)
RESOLUTION_PATTERNS = [
    r"\bsent\b", r"\bshared\b", r"\bdelivered\b", r"\bprovided\b",
    r"\bsubmitted\b", r"\buploaded\b", r"\bforwarded\b", r"\bconfirmed\b",
    r"\bcompleted\b", r"\bfinished\b", r"\bdone\b", r"\bfinalized\b",
    r"\breviewed\b", r"\bscheduled\b", r"\bbooked\b",
]

# Speaker-to-owner mappings (--no-llm path)
OWNER_NAMES = {"owner", "speaker 2", "speaker2"}

LLM_MODEL = "claude-haiku-4-5"
LLM_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def promise_id(meeting_ref: str, text: str) -> str:
    """Deterministic hash for a (meeting, text) pair."""
    raw = f"{meeting_ref}|{text[:100]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def parse_date(s: str) -> datetime | None:
    """Try several ISO/short date patterns; return UTC datetime or None."""
    if not s:
        return None
    # Normalise: strip trailing Z, keep first 26 chars max
    s = s.rstrip("Z").strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Try prefix match for datetime strings with extra data
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        width = len(datetime.now().strftime(fmt))
        try:
            dt = datetime.strptime(s[:width], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def days_overdue(due: str | None) -> int | None:
    """Return integer days past due-date, or None if no due date."""
    if not due:
        return None
    dt = parse_date(due)
    if dt is None:
        return None
    delta = (utcnow_dt() - dt).days
    return delta if delta > 0 else None


def infer_date_from_text(text: str, meeting_date: str) -> str | None:
    """
    Look for explicit date hints (Monday, Friday, tomorrow, YYYY-MM-DD, next week, etc.)
    within text. Returns ISO date string or None.
    """
    t = text.lower()
    # Explicit ISO date
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)
    # Day names
    base = parse_date(meeting_date)
    if base is None:
        return None
    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    }
    for day, weekday in day_map.items():
        if day in t:
            diff = (weekday - base.weekday()) % 7
            if diff == 0:
                diff = 7
            target = base + timedelta(days=diff)
            return target.strftime("%Y-%m-%d")
    if "tomorrow" in t:
        return (base + timedelta(days=1)).strftime("%Y-%m-%d")
    if "next week" in t or "next week" in t:
        return (base + timedelta(days=7)).strftime("%Y-%m-%d")
    if "end of week" in t or "eow" in t:
        diff = (4 - base.weekday()) % 7  # Friday
        if diff == 0:
            diff = 7
        return (base + timedelta(days=diff)).strftime("%Y-%m-%d")
    return None


def has_delivery_verb(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in DELIVERY_VERBS)


def is_owner_actor(actor: str) -> bool:
    return actor.strip().lower() in OWNER_NAMES


def truncate(text: str, max_chars: int = 200) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1] + "…"


# ---------------------------------------------------------------------------
# Meeting parsing helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(content: str) -> dict[str, Any]:
    """Extract YAML frontmatter (simple key:value, no nested) from --- blocks."""
    fm: dict[str, Any] = {}
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return fm
    for line in m.group(1).splitlines():
        kv = line.split(":", 1)
        if len(kv) == 2:
            k = kv[0].strip()
            v = kv[1].strip().strip('"').strip("'")
            fm[k] = v
    return fm


def extract_section(content: str, heading: str) -> str:
    """
    Return text under a ## heading until the next ## heading (or EOF).
    Returns empty string if not found.
    """
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    if not m:
        return ""
    start = m.end()
    # next ## heading
    nxt = re.search(r"^##\s+", content[start:], re.MULTILINE)
    end = start + nxt.start() if nxt else len(content)
    return content[start:end].strip()


def list_meeting_files(root: Path, limit: int | None = None) -> list[Path]:
    """
    Return meeting .md files sorted newest-first by filename.
    Skips README.md and _index.md.
    """
    meeting_dir = root / MEETING_SUBDIR
    if not meeting_dir.exists():
        return []
    files = sorted(
        [
            f for f in meeting_dir.glob("*.md")
            if f.name not in ("README.md", "_index.md")
        ],
        key=lambda f: f.name,
        reverse=True,
    )
    if limit:
        files = files[:limit]
    return files


# ---------------------------------------------------------------------------
# Memory / idempotency
# ---------------------------------------------------------------------------

def load_memory(root: Path) -> dict[str, Any]:
    state_dir = root / AGENT_STATE_DIR
    mem_file = state_dir / "memory.json"
    if mem_file.exists():
        try:
            return json.loads(mem_file.read_text())
        except Exception:
            pass
    return {"seen_files": [], "last_run": None, "run_count": 0}


def save_memory(root: Path, memory: dict[str, Any], dry_run: bool) -> None:
    state_dir = root / AGENT_STATE_DIR
    if not dry_run:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "memory.json").write_text(
            json.dumps(memory, indent=2, ensure_ascii=False)
        )


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def load_ledger(root: Path) -> tuple[list[dict], list[dict]]:
    """Return (ledger, held) lists."""
    ledger_dir = root / LEDGER_DIR
    ledger: list[dict] = []
    held: list[dict] = []
    for fname, container in (("ledger.json", ledger), ("held.json", held)):
        f = ledger_dir / fname
        if f.exists():
            try:
                data = json.loads(f.read_text())
                if isinstance(data, list):
                    container.extend(data)
            except Exception:
                pass
    return ledger, held


def save_ledger(
    root: Path, ledger: list[dict], held: list[dict], dry_run: bool
) -> None:
    if dry_run:
        return
    ledger_dir = root / LEDGER_DIR
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "ledger.json").write_text(
        json.dumps(ledger, indent=2, ensure_ascii=False)
    )
    (ledger_dir / "held.json").write_text(
        json.dumps(held, indent=2, ensure_ascii=False)
    )


def build_summary(ledger: list[dict]) -> dict[str, Any]:
    """Compute summary.json content from the current ledger."""
    now = utcnow_dt()
    now_48h = now + timedelta(hours=48)
    due_48h = []
    overdue_to_owner = []
    oldest_unresolved: dict | None = None
    oldest_dt: datetime | None = None

    # keep_rate_30d
    cutoff_30d = now - timedelta(days=30)
    kept_30d = 0
    resolved_30d = 0

    for p in ledger:
        if p.get("status") in ("kept",):
            ext_dt = parse_date(p.get("extracted_at", ""))
            if ext_dt and ext_dt >= cutoff_30d:
                kept_30d += 1
                resolved_30d += 1
            continue
        if p.get("status") in ("stale", "broken"):
            ext_dt = parse_date(p.get("extracted_at", ""))
            if ext_dt and ext_dt >= cutoff_30d:
                resolved_30d += 1

        if p.get("status") != "pending":
            continue

        # Due in 48h?
        due = p.get("due_date_inferred")
        if due:
            due_dt = parse_date(due)
            if due_dt and now <= due_dt <= now_48h:
                due_48h.append({"id": p["promise_id"], "text": p["text"], "due": due})

        # Overdue to owner?
        if p.get("promise_type") == "owed-to-owner" and due:
            due_dt = parse_date(due)
            if due_dt and due_dt < now:
                overdue_to_owner.append(
                    {"id": p["promise_id"], "text": p["text"], "due": due}
                )

        # Oldest unresolved?
        ext_dt = parse_date(p.get("extracted_at", ""))
        if ext_dt:
            if oldest_dt is None or ext_dt < oldest_dt:
                oldest_dt = ext_dt
                oldest_unresolved = {
                    "id": p["promise_id"],
                    "text": p["text"],
                    "extracted_at": p["extracted_at"],
                    "promise_type": p["promise_type"],
                }

    keep_rate = (kept_30d / resolved_30d) if resolved_30d > 0 else None

    return {
        "generated": utcnow(),
        "due_48h": due_48h,
        "overdue_to_owner": overdue_to_owner,
        "oldest_unresolved": oldest_unresolved,
        "keep_rate_30d": keep_rate,
        "total_pending": sum(1 for p in ledger if p.get("status") == "pending"),
        "total_ledger": len(ledger),
    }


def save_summary(root: Path, summary: dict, dry_run: bool) -> None:
    if dry_run:
        return
    ledger_dir = root / LEDGER_DIR
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# No-LLM extraction (regex + heuristics)
# ---------------------------------------------------------------------------

def _action_item_promises(
    action_text: str, meeting_ref: str, meeting_date: str
) -> list[dict]:
    """
    Parse Action items section for "**Actor**: action text" bullets.
    Returns raw promise dicts (pre-ID assignment).
    """
    promises: list[dict] = []
    # Pattern: "**Name**: rest of line" (Markdown bold actor)
    for m in re.finditer(
        r"^\s*[-*]?\s*\*\*([^*]+)\*\*\s*[:–—-]\s*(.+)$",
        action_text,
        re.MULTILINE,
    ):
        actor = m.group(1).strip()
        action = m.group(2).strip()
        if not action or len(action) < 5:
            continue
        conf = CONF_ACTION_BULLET
        if has_delivery_verb(action):
            conf = min(conf + DELIVERY_VERB_BOOST, 0.95)
        due = infer_date_from_text(action, meeting_date)
        if due:
            conf = min(conf + DATE_BOOST, 0.95)
        ptype = "owner-owes" if is_owner_actor(actor) else "owed-to-owner"
        promises.append(
            {
                "actor": actor,
                "text": truncate(action, 200),
                "due_date_inferred": due,
                "confidence": round(conf, 3),
                "promise_type": ptype,
                "source_section": "action_items",
                "meeting_ref": meeting_ref,
            }
        )
    return promises


def _modal_verb_promises(
    text: str, section_name: str, meeting_ref: str, meeting_date: str
) -> list[dict]:
    """
    Scan free text for modal-verb commitment patterns.
    Returns raw promise dicts.
    """
    promises: list[dict] = []
    # Sentences containing will/shall/going to
    sentence_split = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentence_split:
        sl = sentence.lower()
        conf: float | None = None
        if re.search(r"\bwill\b", sl):
            conf = CONF_WILL
        elif re.search(r"\bshall\b", sl):
            conf = CONF_SHALL
        elif re.search(r"\bgoing to\b", sl):
            conf = CONF_GOING_TO

        if conf is None:
            continue
        if not has_delivery_verb(sentence):
            continue
        if has_delivery_verb(sentence):
            conf = min(conf + DELIVERY_VERB_BOOST, 0.95)
        due = infer_date_from_text(sentence, meeting_date)
        if due:
            conf = min(conf + DATE_BOOST, 0.95)

        # Infer actor from "Speaker X will" pattern
        actor_match = re.search(
            r"\b(speaker\s*\d+|owner|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:will|shall|is going to)\b",
            sentence,
            re.IGNORECASE,
        )
        actor = actor_match.group(1).strip() if actor_match else "Unknown"
        ptype = "owner-owes" if is_owner_actor(actor) else "owed-to-owner"

        promises.append(
            {
                "actor": actor,
                "text": truncate(sentence.strip(), 200),
                "due_date_inferred": due,
                "confidence": round(conf, 3),
                "promise_type": ptype,
                "source_section": section_name,
                "meeting_ref": meeting_ref,
            }
        )
    return promises


def extract_no_llm(content: str, file_path: Path) -> list[dict]:
    """
    Extract promises from a meeting note without LLM.
    Prefers Action items section; falls back to modal-verb scan over
    Summary and Decisions.
    """
    fm = parse_frontmatter(content)
    meeting_date = fm.get("start", "")[:10]
    meeting_ref = file_path.name

    raw: list[dict] = []

    action_text = extract_section(content, "Action items")
    if action_text:
        raw.extend(_action_item_promises(action_text, meeting_ref, meeting_date))

    # Modal-verb fallback over Summary + Decisions (only if no action items found)
    if not raw:
        for section in ("Summary", "Decisions"):
            sec_text = extract_section(content, section)
            if sec_text:
                raw.extend(
                    _modal_verb_promises(sec_text, section, meeting_ref, meeting_date)
                )

    return raw


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def claude_bin() -> str | None:
    for c in (
        str(Path.home() / ".local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        shutil.which("claude"),
    ):
        if c and Path(c).exists():
            return c
    return None


def build_llm_prompt(content: str, file_path: Path) -> str:
    fm = parse_frontmatter(content)
    meeting_name = fm.get("meeting_name", file_path.stem)
    meeting_date = fm.get("start", "")[:10]

    # Feed only distilled sections to minimize tokens; append short transcript excerpt
    summary = extract_section(content, "Summary")[:600]
    decisions = extract_section(content, "Decisions")[:400]
    action_items = extract_section(content, "Action items")[:600]

    prompt = f"""You are a strict promise-extraction system. Extract ONLY explicit commitments from this meeting note.

MEETING: {meeting_name}
DATE: {meeting_date}
FILE: {file_path.name}

--- SUMMARY ---
{summary}

--- DECISIONS ---
{decisions}

--- ACTION ITEMS ---
{action_items}

OUTPUT: Return valid JSON only. No markdown, no prose. Schema:
{{
  "promises": [
    {{
      "actor": "string — speaker name or role",
      "promise_type": "owner-owes | owed-to-owner",
      "text": "string — verbatim or close paraphrase, max 200 chars",
      "due_date_inferred": "YYYY-MM-DD or null",
      "confidence": 0.0-1.0
    }}
  ]
}}

Rules:
- "owner-owes" = the meeting owner / Speaker 2 owes something to others.
- "owed-to-owner" = another party owes something to the owner.
- ONLY extract explicit commitments ("I will...", "we'll send...", "**Name**: action...").
- Vague intentions ("we should...") get confidence <= 0.60.
- Explicit commitments with delivery verb get confidence >= 0.80.
- due_date_inferred: set ONLY if a day/date is explicitly or strongly implied in the text.
- Return empty promises array if nothing qualifies.
- RETURN JSON ONLY."""
    return prompt


def call_llm(prompt: str) -> str:
    cb = claude_bin()
    if not cb:
        raise RuntimeError("claude CLI not found")
    result = subprocess.run(
        [
            cb, "-p", prompt,
            "--model", LLM_MODEL,
            "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ],
        capture_output=True,
        text=True,
        timeout=LLM_TIMEOUT,
        env={**os.environ, "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1",
             "ULTRON_VOICE": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (rc={result.returncode}): {result.stderr[:400]}"
        )
    return result.stdout.strip()


def parse_llm_response(raw: str) -> list[dict]:
    """
    Robustly parse JSON from LLM output.
    Tries: raw, json block extraction, greedy { ... } match.
    """
    raw = raw.strip()
    # Try direct parse
    try:
        data = json.loads(raw)
        return data.get("promises", [])
    except json.JSONDecodeError:
        pass
    # Extract ```json ... ``` block
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if m:
        try:
            data = json.loads(m.group(1))
            return data.get("promises", [])
        except json.JSONDecodeError:
            pass
    # Greedy: find outermost { ... }
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            data = json.loads(m.group(0))
            return data.get("promises", [])
        except json.JSONDecodeError:
            pass
    return []


def extract_with_llm(content: str, file_path: Path) -> tuple[list[dict], bool]:
    """
    Extract promises via LLM. Returns (raw_promises, used_llm).
    Falls back to no-llm path on any error.
    """
    try:
        prompt = build_llm_prompt(content, file_path)
        raw_output = call_llm(prompt)
        raw_promises = parse_llm_response(raw_output)
        fm = parse_frontmatter(content)
        for p in raw_promises:
            p["meeting_ref"] = file_path.name
            p["source_section"] = "llm"
        return raw_promises, True
    except Exception as e:
        print(
            f"  [promise-ledger] LLM failed for {file_path.name}: {e} — falling back to regex",
            file=sys.stderr,
        )
        return extract_no_llm(content, file_path), False


# ---------------------------------------------------------------------------
# Finalise raw promises into ledger records
# ---------------------------------------------------------------------------

def finalise_promises(
    raw: list[dict],
    meeting_file: Path,
    fm: dict[str, Any],
) -> tuple[list[dict], list[dict]]:
    """
    Convert raw extraction dicts into full ledger records.
    Returns (to_ledger, to_held) based on confidence threshold.
    """
    to_ledger: list[dict] = []
    to_held: list[dict] = []
    for p in raw:
        text = str(p.get("text", "")).strip()
        if not text or len(text) < 8:
            continue
        conf = float(p.get("confidence", 0.0))
        pid = promise_id(meeting_file.name, text)
        record: dict[str, Any] = {
            "promise_id": pid,
            "promise_type": p.get("promise_type", "unknown"),
            "actor": p.get("actor", "Unknown"),
            "owner": "owner",
            "beneficiary": "other" if p.get("promise_type") == "owner-owes" else "owner",
            "text": truncate(text, 200),
            "due_date_inferred": p.get("due_date_inferred"),
            "meeting_ref": meeting_file.name,
            "extracted_at": utcnow(),
            "confidence": round(conf, 3),
            "status": "pending",
            "days_overdue": None,
            "source_section": p.get("source_section", "unknown"),
        }
        if conf >= CONFIDENCE_THRESHOLD:
            to_ledger.append(record)
        else:
            to_held.append(record)
    return to_ledger, to_held


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------

def resolution_signal_in_text(text: str) -> bool:
    """Return True if text contains a resolution phrase."""
    tl = text.lower()
    for pat in RESOLUTION_PATTERNS:
        if re.search(pat, tl):
            return True
    return False


def reconcile(ledger: list[dict], all_meeting_files: list[Path]) -> list[dict]:
    """
    For each pending promise, check:
    1. Resolution: scan meetings NEWER than extraction for the promise text / actor.
    2. Staleness: past due by >= STALE_DAYS -> stale; >= BROKEN_DAYS -> broken.
    Mutates and returns ledger.
    """
    now = utcnow_dt()

    # Build a cache of (filename -> (mtime, content)) for recently added meetings
    # We only scan files that might be newer than the promise
    file_cache: dict[str, tuple[str, str]] = {}
    for f in all_meeting_files:
        try:
            content = f.read_text(errors="replace")
            fm = parse_frontmatter(content)
            date_str = fm.get("start", "")[:10]
            file_cache[f.name] = (date_str, content)
        except Exception:
            continue

    # Build a meeting-date lookup by filename (for promise source-meeting date)
    meeting_date_by_ref: dict[str, str] = {
        fname: date_str for fname, (date_str, _) in file_cache.items()
    }

    for promise in ledger:
        if promise.get("status") != "pending":
            continue

        # Use the source meeting's date as the baseline, not the extraction run timestamp.
        # Only scan meetings that are on or after the promise source meeting.
        source_ref = promise.get("meeting_ref", "")
        source_date_str = meeting_date_by_ref.get(source_ref, "")
        source_dt = parse_date(source_date_str)

        promise_text_lower = promise.get("text", "").lower()
        actor_lower = promise.get("actor", "").lower()

        # Check for resolution in meetings on or after the source meeting
        kept = False
        for fname, (meeting_date, content) in file_cache.items():
            if fname == source_ref:
                continue
            m_dt = parse_date(meeting_date)
            # Skip meetings that predate the promise's source meeting
            if source_dt and m_dt and m_dt < source_dt:
                continue
            # Look for actor + resolution phrase in action items / summary
            action_text = extract_section(content, "Action items").lower()
            summary_text = extract_section(content, "Summary").lower()
            combined = action_text + " " + summary_text

            # Check for actor context + resolution signal
            if actor_lower and actor_lower != "unknown":
                actor_window = 200
                idx = combined.find(actor_lower)
                while idx != -1:
                    window = combined[max(0, idx - 50): idx + actor_window]
                    if resolution_signal_in_text(window):
                        kept = True
                        break
                    idx = combined.find(actor_lower, idx + 1)
            else:
                # Just check if resolution phrase near key words from promise text
                words = promise_text_lower.split()[:5]
                for word in words:
                    if len(word) < 4:
                        continue
                    idx = combined.find(word)
                    if idx != -1:
                        window = combined[max(0, idx - 50): idx + 200]
                        if resolution_signal_in_text(window):
                            kept = True
                            break
            if kept:
                break

        if kept:
            promise["status"] = "kept"
            promise["days_overdue"] = None
            continue

        # Staleness check
        due = promise.get("due_date_inferred")
        if due:
            due_dt = parse_date(due)
            if due_dt:
                delta_days = (now - due_dt).days
                if delta_days >= BROKEN_DAYS:
                    promise["status"] = "broken"
                    promise["days_overdue"] = delta_days
                elif delta_days >= STALE_DAYS:
                    promise["status"] = "stale"
                    promise["days_overdue"] = delta_days

    return ledger


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(
    root: Path,
    no_llm: bool = False,
    dry_run: bool = False,
    reconcile_only: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    Full promise-ledger pipeline. Returns stats dict.
    """
    print(f"[promise-ledger] root={root}  no_llm={no_llm}  dry_run={dry_run}  "
          f"reconcile_only={reconcile_only}  limit={limit}")

    memory = load_memory(root)
    seen_files: set[str] = set(memory.get("seen_files", []))

    ledger, held = load_ledger(root)
    all_meeting_files = list_meeting_files(root, limit=None)

    new_ledger_count = 0
    new_held_count = 0
    llm_used = False

    if not reconcile_only:
        # Build list of files to process (newest first, up to limit, unseen only)
        candidate_files = list_meeting_files(root, limit=limit)
        to_process = [f for f in candidate_files if f.name not in seen_files]

        print(f"[promise-ledger] {len(candidate_files)} candidate(s), "
              f"{len(to_process)} new to extract")

        existing_ids = {p["promise_id"] for p in ledger + held}

        for meeting_file in to_process:
            print(f"  extracting: {meeting_file.name}")
            try:
                content = meeting_file.read_text(errors="replace")
            except Exception as e:
                print(f"  [skip] cannot read {meeting_file.name}: {e}", file=sys.stderr)
                seen_files.add(meeting_file.name)
                continue

            fm = parse_frontmatter(content)

            if no_llm:
                raw = extract_no_llm(content, meeting_file)
                used = False
            else:
                raw, used = extract_with_llm(content, meeting_file)
                if used:
                    llm_used = True

            new_l, new_h = finalise_promises(raw, meeting_file, fm)

            # Dedup against existing
            for p in new_l:
                if p["promise_id"] not in existing_ids:
                    ledger.append(p)
                    existing_ids.add(p["promise_id"])
                    new_ledger_count += 1
            for p in new_h:
                if p["promise_id"] not in existing_ids:
                    held.append(p)
                    existing_ids.add(p["promise_id"])
                    new_held_count += 1

            seen_files.add(meeting_file.name)
            print(f"    -> {len(new_l)} ledger + {len(new_h)} held promises")

    # Reconcile
    print(f"[promise-ledger] reconciling {len(ledger)} ledger promises ...")
    ledger = reconcile(ledger, all_meeting_files)

    # Compute summary
    summary = build_summary(ledger)

    # Save
    save_ledger(root, ledger, held, dry_run)
    save_summary(root, summary, dry_run)

    memory["seen_files"] = sorted(seen_files)
    memory["last_run"] = utcnow()
    memory["run_count"] = memory.get("run_count", 0) + 1
    save_memory(root, memory, dry_run)

    stats: dict[str, Any] = {
        "new_ledger": new_ledger_count,
        "new_held": new_held_count,
        "total_ledger": len(ledger),
        "total_held": len(held),
        "llm_used": llm_used,
        "summary": summary,
    }

    # Print sample promises
    pending = [p for p in ledger if p.get("status") == "pending"]
    if pending:
        print(f"\n[promise-ledger] Sample pending promises:")
        for p in pending[:3]:
            print(
                f"  [{p.get('promise_type','?')}] {p.get('actor','?')}: "
                f"{p['text'][:120]} (conf={p['confidence']}, status={p['status']})"
            )

    print(f"\n[promise-ledger] Done. "
          f"new_ledger={new_ledger_count} new_held={new_held_count} "
          f"total={len(ledger)} llm={llm_used}")

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="promise_ledger — extract and track meeting commitments"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=VAULT_DEFAULT,
        help="Vault root directory (default: env VAULT or OneDrive vault path)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and reconcile, but write nothing to disk",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=bool(os.environ.get("NO_LLM")),
        help="Disable LLM; use regex/heuristic extraction only (also set NO_LLM=1)",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Skip extraction; only reconcile existing ledger entries",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of most-recent meetings to process per run",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.exists():
        print(f"[promise-ledger] ERROR: root does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    run(
        root=root,
        no_llm=args.no_llm,
        dry_run=args.dry_run,
        reconcile_only=args.reconcile_only,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
