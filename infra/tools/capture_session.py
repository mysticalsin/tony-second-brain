#!/usr/bin/env python3
"""capture_session.py — extract per-session learnings + token usage and write to the vault.

Invoked async by 99_Meta/capture-session.sh after Claude Code's SessionEnd hook.

Reads the session transcript .jsonl, tallies token usage, calls Haiku 4.5 to extract
structured learnings, and writes atomic updates to:
  - _agent_state/claude-code/sessions.jsonl   (append)
  - _agent_state/claude-code/memory.json      (merge)
  - _agent_state/claude-code/stats.json       (rolling totals)
  - 02_Areas/Daily/<YYYY-MM-DD>.md            (append "## AI Sessions" entry)

Env vars (set by the detacher script):
  CLAUDE_TRANSCRIPT_PATH   path to <session-id>.jsonl
  CLAUDE_SESSION_ID        session id
  CLAUDE_CWD               working dir at SessionEnd
  CLAUDE_MODEL             model name (best-effort; detected from transcript if absent)
  CLAUDE_VAULT             vault root (override)
  ANTHROPIC_API_KEY        required for learning extraction (stats still capture without it)

Flags:
  --dry-run         write to /tmp/capture-dryrun/ instead of vault; print summary
  --transcript P    override transcript path (for testing)
  --no-api          skip Haiku call; record stats + raw summary only

Exit code is always 0 — failures log to ~/AI-Brain-build/logs/capture-<date>.log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))
LOG_DIR = Path.home() / "AI-Brain-build" / "logs"

# Pricing per million tokens (USD), Jun 2026 baseline (claude-api skill model table).
# Cache multipliers: read = 0.1x input, write(5m) = 1.25x input.
# Legacy Opus 4.5/4.1 keep their era pricing as EXACT entries so history stays true.
PRICING = {
    "claude-fable-5":       {"in": 10.00, "out": 50.00, "cache_read": 1.00,  "cache_write": 12.50},
    "claude-opus-4-8":      {"in":  5.00, "out": 25.00, "cache_read": 0.50,  "cache_write":  6.25},
    "claude-opus-4-7":      {"in":  5.00, "out": 25.00, "cache_read": 0.50,  "cache_write":  6.25},
    "claude-opus-4-6":      {"in":  5.00, "out": 25.00, "cache_read": 0.50,  "cache_write":  6.25},
    "claude-opus-4-5":      {"in": 15.00, "out": 75.00, "cache_read": 1.50,  "cache_write": 18.75},
    "claude-opus-4-1":      {"in": 15.00, "out": 75.00, "cache_read": 1.50,  "cache_write": 18.75},
    "claude-sonnet-4-6":    {"in":  3.00, "out": 15.00, "cache_read": 0.30,  "cache_write":  3.75},
    "claude-sonnet-4-5":    {"in":  3.00, "out": 15.00, "cache_read": 0.30,  "cache_write":  3.75},
    "claude-haiku-4-5":     {"in":  1.00, "out":  5.00, "cache_read": 0.10,  "cache_write":  1.25},
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}
# Family rates — used when an exact id isn't listed (a new variant, or a suffixed id
# like "claude-opus-4-8[1m]"). Prefer family over a blanket Sonnet fallback so a new
# Opus model is NEVER silently priced as Sonnet (the bug that undercounted Opus 4.8).
_FAMILY = {
    "fable":  {"in": 10.00, "out": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "opus":   {"in":  5.00, "out": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "sonnet": {"in":  3.00, "out": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "haiku":  {"in":  1.00, "out":  5.00, "cache_read": 0.10, "cache_write":  1.25},
}
FALLBACK_PRICING = _FAMILY["sonnet"]  # truly-unknown model → Sonnet


def price_for(model):
    """Per-MTok pricing for a model id: exact match → family (opus/sonnet/haiku
    substring) → Sonnet fallback. Tolerates date/'[1m]' suffixes. NOTE: the
    1M-context premium tier is not modelled (see _relay/ISSUES.md)."""
    m = (model or "").lower()
    if m in PRICING:
        return PRICING[m]
    for fam, rate in _FAMILY.items():
        if fam in m:
            return rate
    return None  # unknown/synthetic (e.g. "<synthetic>") → not billed; never guess-price

HAIKU_MODEL = "claude-haiku-4-5"
EXTRACTION_INPUT_TOKEN_CAP = 4000  # ≈16K chars; we truncate longer transcripts
MAX_LEARNINGS_RING = 50            # recent_learnings ring buffer size
EXCLUDED_CWD_PATTERNS = ("/HR Documents", "/LinkedIn", "/Meetings/Confidential")


# ────────────────────────────── logging ──────────────────────────────


def _log_path() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"capture-{datetime.now().strftime('%Y-%m-%d')}.log"


def log(msg: str, *, level: str = "INFO") -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] [{level}] {msg}\n"
    try:
        with _log_path().open("a") as f:
            f.write(line)
    except OSError:
        pass
    # Also print to stderr when running interactively (dry-run prints to stdout itself).
    if sys.stderr.isatty():
        sys.stderr.write(line)


# ────────────────────────────── helpers ──────────────────────────────


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=str(path.parent), delete=False, prefix=".tmp-", suffix=path.suffix
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def atomic_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSONL append is idempotent enough that we use direct append for performance,
    # but only after we've checked the line doesn't duplicate an existing session_id.
    with path.open("a") as f:
        f.write(line)
        if not line.endswith("\n"):
            f.write("\n")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 4 chars. Conservative for budget planning."""
    return max(1, len(text) // 4)


def cwd_is_excluded(cwd: str) -> bool:
    return any(p in cwd for p in EXCLUDED_CWD_PATTERNS)


# ────────────────────────── transcript parsing ───────────────────────


def parse_transcript(path: Path) -> dict:
    """Walk the .jsonl, return aggregated session facts.

    The Claude Code transcript format puts each message as one JSON line.
    Usage is on the `message.usage` object of assistant messages.
    Tool calls are content blocks inside the assistant message content list.
    """
    facts = {
        "model": None,
        "n_turns_user": 0,
        "n_turns_assistant": 0,
        "tool_uses": Counter(),
        "files_touched": set(),
        "first_user_text": None,
        "last_assistant_text": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "cost_usd": 0.0,  # accurate sum: each assistant turn priced by ITS OWN model
        "by_model": {},   # per-model usage+cost (accurate attribution, not first-seen)
        "first_ts": None,
        "last_ts": None,
    }

    if not path.exists():
        return facts

    seen_msg_ids = set()  # transcript-dedupe-fix: one usage per assistant message id

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue

            ts = rec.get("timestamp") or rec.get("ts")
            if ts:
                facts["first_ts"] = facts["first_ts"] or ts
                facts["last_ts"] = ts

            msg = rec.get("message") or {}
            role = (msg.get("role") or rec.get("role") or rec.get("type") or "").lower()

            # Capture model if present (assistant messages carry it on `message.model`).
            if not facts["model"]:
                facts["model"] = msg.get("model") or rec.get("model")

            if role == "user":
                facts["n_turns_user"] += 1
                if not facts["first_user_text"]:
                    facts["first_user_text"] = _extract_text(msg.get("content") or rec.get("content"))

            elif role == "assistant":
                text = _extract_text(msg.get("content"))
                if text:
                    facts["last_assistant_text"] = text
                # transcript-dedupe-fix (2026-06-10): streaming writes ONE LINE PER
                # CONTENT BLOCK, each carrying the SAME full usage snapshot for the
                # message. Summing every line multiplied tokens/cost 2-3x. Dedupe by
                # message.id (verified identical snapshots; mirrors the plugin's
                # parseTranscriptText dedupe).
                _mid = msg.get("id")
                if _mid:
                    if _mid in seen_msg_ids:
                        continue
                    seen_msg_ids.add(_mid)
                facts["n_turns_assistant"] += 1
                # Usage on this turn:
                u = msg.get("usage") or {}
                mm = msg.get("model") or facts["model"]
                tt = 0
                for k in facts["usage"]:
                    n = int(u.get(k, 0) or 0); facts["usage"][k] += n; tt += n
                # Price THIS turn by its own model — sessions mix Opus + Haiku, so a
                # single session-level model would mis-price part of the spend.
                c = compute_cost(u, mm)
                facts["cost_usd"] += c
                if tt:  # accurate per-model attribution (first-seen model is unreliable)
                    bm = facts["by_model"].setdefault(mm or "unknown",
                        {"input_tokens": 0, "output_tokens": 0,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.0})
                    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                        bm[k] += int(u.get(k, 0) or 0)
                    bm["cost_usd"] = round(bm["cost_usd"] + c, 6)
                # Tool uses inside content blocks:
                for block in (msg.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tname = block.get("name", "?")
                        facts["tool_uses"][tname] += 1
                        # Capture file_path inputs (Read/Edit/Write tools)
                        ti = block.get("input") or {}
                        fp = ti.get("file_path") or ti.get("notebook_path")
                        if fp:
                            facts["files_touched"].add(fp)

    # Primary model = the real model with the most output tokens (first-seen is
    # unreliable and may be "<synthetic>"). Keeps by_model labels honest.
    real = {m: b for m, b in facts["by_model"].items() if m and m != "<synthetic>"}
    if real:
        facts["model"] = max(real, key=lambda m: real[m]["output_tokens"])
    facts["files_touched"] = sorted(facts["files_touched"])
    facts["tool_uses"] = dict(facts["tool_uses"])
    return facts


def _extract_text(content) -> str:
    """Content can be a string or a list of typed blocks. Concatenate text blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
                elif block.get("type") == "tool_result":
                    # Skip tool result bodies — too noisy
                    pass
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts).strip()
    return str(content).strip()


def duration_seconds(facts: dict) -> int:
    try:
        a = datetime.fromisoformat(facts["first_ts"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(facts["last_ts"].replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds()))
    except (TypeError, ValueError, AttributeError):
        return 0


def compute_cost(usage: dict, model: str | None) -> float:
    p = price_for(model)
    if not p:
        return 0.0  # synthetic / unrecognized model → no API charge
    return (
        usage.get("input_tokens", 0)              * p["in"] / 1_000_000
        + usage.get("output_tokens", 0)             * p["out"] / 1_000_000
        + usage.get("cache_read_input_tokens", 0)   * p["cache_read"] / 1_000_000
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write"] / 1_000_000
    )


# ────────────────────────── extraction prompt ────────────────────────


SYSTEM_PROMPT = """You extract structured learnings from a Claude Code session transcript so future sessions can be smarter.

Hard rules — never violate:
- NEVER include credentials, API keys, secrets, NDA text, signed contract terms, client-confidential data, or personal identifiers (emails, phone numbers, addresses) verbatim. If such material appears in the transcript, only mention that it was discussed — never the content.
- Output ONLY valid JSON matching the schema below — no preamble, no markdown fences, no explanation.
- Keep every string short and concrete. The summary is capped at 60 words. Each learning, pattern, and mistake item is capped at 25 words.

Focus on signal that will make the NEXT session better:
- learnings = preferences expressed or implied ("user prefers X over Y", "always run lint before commit", "this repo uses pnpm not npm")
- patterns = recurring topics, tool sequences, file areas the user works in
- mistakes_to_avoid = anything the user corrected, told you to stop doing, or that failed and required workaround

Output schema:
{
  "summary": "≤60 word natural-language description of what this session was about and what got done",
  "learnings": ["≤25 words each; preferences/do-don't rules; empty array if nothing actionable"],
  "patterns": ["≤25 words each; recurring topics or tool sequences; empty array if none"],
  "mistakes_to_avoid": ["≤25 words each; things to NOT repeat; empty array if none"],
  "topics": ["1-3 short tags like 'pptx', 'sbap', 'react'"]
}"""


def build_user_message(facts: dict, transcript_excerpt: str) -> str:
    tool_summary = ", ".join(f"{k}×{v}" for k, v in sorted(facts["tool_uses"].items(), key=lambda x: -x[1])[:10]) or "(none)"
    files = ", ".join(facts["files_touched"][:15]) or "(none)"
    return f"""Session metadata:
- model: {facts.get('model') or 'unknown'}
- user turns: {facts['n_turns_user']}, assistant turns: {facts['n_turns_assistant']}
- tools used: {tool_summary}
- files touched: {files}
- usage: in={facts['usage']['input_tokens']}, out={facts['usage']['output_tokens']}, cache_read={facts['usage']['cache_read_input_tokens']}, cache_write={facts['usage']['cache_creation_input_tokens']}

Transcript excerpt (first user prompt + last assistant text, possibly truncated):
---
{transcript_excerpt}
---

Extract per the schema. JSON only."""


def truncate_for_extraction(text: str, cap_tokens: int) -> str:
    cap_chars = cap_tokens * 4
    if len(text) <= cap_chars:
        return text
    half = cap_chars // 2 - 50
    return text[:half] + f"\n\n[…truncated {len(text) - cap_chars} chars from middle…]\n\n" + text[-half:]


def call_haiku(system_prompt: str, user_msg: str, api_key: str) -> tuple[dict, dict]:
    """Returns (parsed_json, usage_dict). Raises on API failure."""
    body = {
        "model": HAIKU_MODEL,
        "max_tokens": 1024,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    # Concatenate any text content blocks from the response
    text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text").strip()
    # Strip accidental markdown fences just in case
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)

    usage = raw.get("usage", {})
    usage_dict = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0)),
    }
    return parsed, usage_dict


# ──────────────────────────── vault writes ───────────────────────────


def is_already_captured(sessions_jsonl: Path, session_id: str) -> bool:
    if not sessions_jsonl.exists():
        return False
    needle = f'"session_id": "{session_id}"'
    try:
        with sessions_jsonl.open("r") as f:
            for line in f:
                if needle in line:
                    return True
    except OSError:
        return False
    return False


def detect_account(cwd: str, topics: list) -> str | None:
    """Return a normalized account/client slug if the session was clearly about one.
    Heuristics (in order):
      1. cwd is under /Clients/<name>/ → <name>  (e.g. /Clients/Globex/ → 'globex')
      2. cwd is under /02_Areas/Accounts/<name>/ → <name>
      3. cwd is under /01_Projects/<bid-folder>/ → first segment of bid folder
      4. None of the above → return None (write to global_patterns only)
    The Haiku-extracted topics are not used for account detection — too noisy.
    """
    import re
    norm = lambda s: re.sub(r"[^a-z0-9_-]+", "-", s.strip().lower()).strip("-") or None
    if not cwd:
        return None
    parts = cwd.rstrip("/").split("/")
    for marker in ("Clients", "Accounts"):
        if marker in parts:
            i = parts.index(marker)
            if i + 1 < len(parts):
                return norm(parts[i + 1])
    # 01_Projects/<Client>-<Opp>/ → take prefix before first hyphen
    if "01_Projects" in parts:
        i = parts.index("01_Projects")
        if i + 1 < len(parts):
            bid_folder = parts[i + 1]
            client_part = bid_folder.split("-")[0] if "-" in bid_folder else bid_folder
            return norm(client_part)
    return None


def merge_into_memory(memory_path: Path, learnings: list, patterns: list, mistakes: list,
                      today: str, account: str | None = None) -> None:
    mem = load_json(memory_path, {})
    if not mem:
        mem = {"agent": "claude-code", "memory_version": 1, "last_updated": None,
               "global_patterns": [], "per_account_knowledge": {}, "self_observations": [],
               "recent_learnings": []}

    # recent_learnings: prepend (newest first), cap to MAX_LEARNINGS_RING.
    # Each entry now carries the detected account so retrieval can be scoped.
    new_entries = [{"date": today, "text": l, "account": account} for l in learnings if l]
    mem["recent_learnings"] = (new_entries + mem.get("recent_learnings", []))[:MAX_LEARNINGS_RING]

    # global_patterns: merge with bump on duplicates
    existing = {p["pattern"].lower(): p for p in mem.get("global_patterns", [])}
    for p in patterns + mistakes:  # mistakes are also patterns to recognize
        if not p:
            continue
        key = p.lower().strip()
        if key in existing:
            existing[key]["n_observations"] = existing[key].get("n_observations", 1) + 1
            existing[key]["confidence"] = round(min(1.0, existing[key]["n_observations"] / 5.0), 2)
        else:
            existing[key] = {
                "pattern": p,
                "confidence": 0.2,
                "n_observations": 1,
                "first_seen": today,
            }
    mem["global_patterns"] = sorted(existing.values(), key=lambda x: -x.get("n_observations", 0))[:200]

    # per_account_knowledge: scoped pool that survives the 50-learning ring cap.
    # An account's bucket keeps every learning/pattern ever attached to it.
    if account:
        bucket = mem.setdefault("per_account_knowledge", {}).setdefault(account, {
            "first_seen": today, "sessions": 0, "learnings": [], "patterns": [], "mistakes": [],
        })
        bucket["sessions"] = bucket.get("sessions", 0) + 1
        bucket["last_seen"] = today
        for l in learnings:
            if l and l not in bucket["learnings"]:
                bucket["learnings"].append({"date": today, "text": l})
        for p in patterns:
            if p and p not in [x.get("text") if isinstance(x, dict) else x for x in bucket["patterns"]]:
                bucket["patterns"].append({"date": today, "text": p})
        for m in mistakes:
            if m and m not in [x.get("text") if isinstance(x, dict) else x for x in bucket["mistakes"]]:
                bucket["mistakes"].append({"date": today, "text": m})
        # Cap per-account lists to keep file size bounded.
        for key in ("learnings", "patterns", "mistakes"):
            bucket[key] = bucket[key][-100:]

    mem["last_updated"] = datetime.now(timezone.utc).isoformat()
    atomic_write(memory_path, json.dumps(mem, indent=2) + "\n")


def update_stats(stats_path: Path, usage: dict, cost_usd: float, capture_cost: float, model: str | None, today: str) -> None:
    stats = load_json(stats_path, {})
    if not stats or "all_time" not in stats:
        stats = {
            "all_time": {"sessions": 0, "input_tokens": 0, "output_tokens": 0,
                         "cache_read_tokens": 0, "cache_creation_tokens": 0,
                         "cost_usd": 0.0, "capture_overhead_usd": 0.0},
            "by_day": {},
            "by_model": {},
        }

    def bump(bucket: dict) -> None:
        bucket["sessions"] = bucket.get("sessions", 0) + 1
        bucket["input_tokens"] = bucket.get("input_tokens", 0) + usage.get("input_tokens", 0)
        bucket["output_tokens"] = bucket.get("output_tokens", 0) + usage.get("output_tokens", 0)
        bucket["cache_read_tokens"] = bucket.get("cache_read_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        bucket["cache_creation_tokens"] = bucket.get("cache_creation_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        bucket["cost_usd"] = round(bucket.get("cost_usd", 0.0) + cost_usd, 6)
        bucket["capture_overhead_usd"] = round(bucket.get("capture_overhead_usd", 0.0) + capture_cost, 6)

    bump(stats["all_time"])
    stats["by_day"].setdefault(today, {})
    bump(stats["by_day"][today])
    if model:
        stats["by_model"].setdefault(model, {})
        bump(stats["by_model"][model])

    atomic_write(stats_path, json.dumps(stats, indent=2) + "\n")


# ──────────────────────────── daily note ─────────────────────────────


DAILY_FALLBACK_TEMPLATE = """---
type: daily
date: {date}
tags: [daily]
---

# {pretty_date}

## Notes
-

## AI Sessions
"""


def render_daily_from_template(template_path: Path, date: str, pretty_date: str) -> str:
    if not template_path.exists():
        return DAILY_FALLBACK_TEMPLATE.format(date=date, pretty_date=pretty_date)
    raw = template_path.read_text()
    # Substitute the two Templater expressions our template uses.
    raw = re.sub(r'<%\s*tp\.date\.now\("YYYY-MM-DD"\)\s*%>', date, raw)
    raw = re.sub(r'<%\s*tp\.date\.now\("dddd, MMMM D, YYYY"\)\s*%>', pretty_date, raw)
    # Strip any leftover Templater tags so the file is valid markdown.
    raw = re.sub(r"<%.*?%>", "", raw)
    return raw


def append_daily_section(daily_path: Path, template_path: Path, date: str, entry: str) -> None:
    pretty_date = datetime.fromisoformat(date).strftime("%A, %B %-d, %Y")
    if daily_path.exists():
        content = daily_path.read_text()
    else:
        content = render_daily_from_template(template_path, date, pretty_date)

    if "## AI Sessions" in content:
        # Append inside existing section, just before the next H2 (or EOF).
        lines = content.split("\n")
        out: list[str] = []
        i = 0
        inserted = False
        while i < len(lines):
            out.append(lines[i])
            if not inserted and lines[i].strip() == "## AI Sessions":
                # Walk forward to find end of section (next ## or EOF)
                j = i + 1
                while j < len(lines) and not lines[j].startswith("## "):
                    out.append(lines[j])
                    j += 1
                # Trim trailing blank lines inside the section before our insert
                while out and out[-1].strip() == "":
                    out.pop()
                out.append("")
                out.append(entry.rstrip())
                out.append("")
                i = j
                inserted = True
                continue
            i += 1
        new_content = "\n".join(out)
    else:
        if not content.endswith("\n"):
            content += "\n"
        new_content = content + "\n## AI Sessions\n\n" + entry.rstrip() + "\n"

    atomic_write(daily_path, new_content)


def write_ai_session_note(vault: Path, record: dict) -> "Path | None":
    """Mirror this session as a type: ai-session note for the Command Center fleet counts.
    Same schema as build/tools/ingest_ai_sessions.py write_session_note."""
    import re as _re
    # record keys: ts (ISO), session_id, summary (str|None), cost_usd, n_turns_user, cwd
    date = (record.get("ts") or "")[:10]
    sid = str(record.get("session_id") or "")
    if not date or not sid:
        return None
    summary_raw = record.get("summary") or ""
    summary = summary_raw.replace('"', "'")[:140]
    slug = _re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:40].rstrip("-") or "session"
    # Use the LAST 8 alphanumeric chars of the session_id (the random tail).
    # UUIDv4 prefix collisions are rare but fix for consistency with ingest_ai_sessions.py.
    short_id = _re.sub(r"[^A-Za-z0-9]", "", sid)[-8:] or "noid"
    target = vault / "02_Areas" / "AI Sessions" / "claude" / f"{date}-{slug}-{short_id}.md"
    if target.exists():
        return None
    cost = record.get("cost_usd")
    turns = record.get("n_turns_user")
    cwd = record.get("cwd", "")
    lines = [
        "---", "type: ai-session", "tool: claude", f"date: {date}", f"session_id: {sid}",
        f'summary: "{summary}"',
    ]
    if turns is not None:
        lines.append(f"turns: {turns}")
    if cost is not None:
        lines.append(f"cost_usd: {round(float(cost), 4)}")
    if cwd:
        lines.append(f'cwd: "{cwd}"')
    lines += ["tags: [ai-session]", "---", "", f"# claude · {date} · {summary or short_id}", ""]
    if summary:
        lines.append(f"- **What**: {summary}")
    if cost is not None:
        lines.append(f"- **Cost**: ${round(float(cost), 4)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, "\n".join(lines) + "\n")
    return target


def daily_entry(session: dict) -> str:
    cost = session["cost_usd"]
    usage = session["usage"]
    duration = session.get("duration_s", 0)
    duration_str = f"{duration // 60}m{duration % 60}s" if duration else "?"
    tools = ", ".join(f"{k}×{v}" for k, v in sorted(session.get("tool_uses", {}).items(), key=lambda x: -x[1])[:5]) or "(none)"

    learnings = session.get("learnings") or []
    patterns = session.get("patterns") or []
    mistakes = session.get("mistakes_to_avoid") or []

    lines = [
        f"### {session['ts'][11:16]} · {session['model'] or '?'} · ${cost:.4f}",
        f"- **What**: {session.get('summary') or '(no summary)'}",
        f"- **Where**: `{session.get('cwd', '?')}`",
        f"- **Cost/tokens**: ${cost:.4f}  (in {usage['input_tokens']:,} · out {usage['output_tokens']:,} · cache-read {usage['cache_read_input_tokens']:,}) · {duration_str} · {session.get('n_turns_user', 0)} turns",
        f"- **Tools**: {tools}",
    ]
    if learnings:
        lines.append("- **Learnings**:")
        for l in learnings:
            lines.append(f"  - {l}")
    if patterns:
        lines.append("- **Patterns**: " + " · ".join(patterns))
    if mistakes:
        lines.append("- **Avoid**:")
        for m in mistakes:
            lines.append(f"  - {m}")
    return "\n".join(lines)


# ──────────────────────────── main ───────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write to /tmp/capture-dryrun/")
    ap.add_argument("--transcript", help="override transcript path")
    ap.add_argument("--no-api", action="store_true", help="skip Haiku call")
    args = ap.parse_args()

    vault = Path(os.environ.get("CLAUDE_VAULT") or VAULT_DEFAULT)
    transcript_path = Path(args.transcript or os.environ.get("CLAUDE_TRANSCRIPT_PATH", ""))
    session_id = os.environ.get("CLAUDE_SESSION_ID") or (transcript_path.stem if transcript_path.name else "unknown")
    cwd = os.environ.get("CLAUDE_CWD") or os.getcwd()

    if not transcript_path or not transcript_path.exists():
        log(f"transcript not found: {transcript_path!s}", level="WARN")
        return 0

    if cwd_is_excluded(cwd):
        log(f"cwd excluded by privacy rule: {cwd}", level="INFO")
        return 0

    # Output roots
    if args.dry_run:
        out_root = Path("/tmp/capture-dryrun")
        out_root.mkdir(parents=True, exist_ok=True)
        agent_dir = out_root / "_agent_state" / "claude-code"
        daily_dir = out_root / "02_Areas" / "Daily"
        daily_template = vault / "02_Areas" / "Daily" / "_template.md"
        notes_root = out_root  # ai-session notes go to /tmp in dry-run, not live vault
    else:
        agent_dir = vault / "_agent_state" / "claude-code"
        daily_dir = vault / "02_Areas" / "Daily"
        daily_template = vault / "02_Areas" / "Daily" / "_template.md"
        notes_root = vault

    sessions_jsonl = agent_dir / "sessions.jsonl"
    memory_path = agent_dir / "memory.json"
    stats_path = agent_dir / "stats.json"

    # Idempotency: skip if this session_id is already recorded.
    if not args.dry_run and is_already_captured(sessions_jsonl, session_id):
        log(f"session {session_id} already captured — skipping", level="INFO")
        return 0

    # Parse transcript
    facts = parse_transcript(transcript_path)
    model = os.environ.get("CLAUDE_MODEL") or facts.get("model")
    # Per-message-model sum (accurate) when available; else session-model estimate.
    cost_usd = facts.get("cost_usd") or compute_cost(facts["usage"], model)

    # Build extraction input
    excerpt_parts = []
    if facts["first_user_text"]:
        excerpt_parts.append("USER (first prompt):\n" + facts["first_user_text"])
    if facts["last_assistant_text"]:
        excerpt_parts.append("ASSISTANT (last reply):\n" + facts["last_assistant_text"])
    excerpt = truncate_for_extraction("\n\n".join(excerpt_parts), EXTRACTION_INPUT_TOKEN_CAP)

    # Haiku call
    extraction = {"summary": None, "learnings": [], "patterns": [], "mistakes_to_avoid": [], "topics": []}
    capture_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    capture_cost = 0.0
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    do_api = (not args.no_api) and bool(api_key) and (facts["n_turns_user"] >= 1)
    if do_api:
        try:
            user_msg = build_user_message(facts, excerpt)
            extraction, capture_usage = call_haiku(SYSTEM_PROMPT, user_msg, api_key)
            capture_cost = compute_cost(capture_usage, HAIKU_MODEL)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as e:
            log(f"Haiku call failed: {e!r}", level="WARN")
    else:
        if not api_key:
            log("ANTHROPIC_API_KEY not set — recording stats only", level="WARN")

    # Compose session record
    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now().strftime("%Y-%m-%d")
    session = {
        "ts": now_iso,
        "session_id": session_id,
        "model": model,
        "cwd": cwd,
        "transcript": str(transcript_path),
        "duration_s": min(duration_seconds(facts), 14400),  # 4h hard cap; multi-day deltas = Mac sleep/wake artifact
        "n_turns_user": facts["n_turns_user"],
        "n_turns_assistant": facts["n_turns_assistant"],
        "tool_uses": facts["tool_uses"],
        "files_touched_count": len(facts["files_touched"]),
        "usage": facts["usage"],
        "cost_usd": round(cost_usd, 6),
        "capture": {
            "model": HAIKU_MODEL if do_api else None,
            "usage": capture_usage,
            "cost_usd": round(capture_cost, 6),
        },
        "summary": (extraction.get("summary") or "")[:400] or None,
        "learnings": extraction.get("learnings") or [],
        "patterns": extraction.get("patterns") or [],
        "mistakes_to_avoid": extraction.get("mistakes_to_avoid") or [],
        "topics": extraction.get("topics") or [],
    }

    # Detect account scope from cwd (e.g. /Clients/Globex/ → 'globex') so the
    # per_account_knowledge bucket survives the 50-learning ring buffer cap.
    account = detect_account(cwd, session.get("topics") or [])
    session["account"] = account

    # Persist
    atomic_append(sessions_jsonl, json.dumps(session))
    try:
        write_ai_session_note(notes_root, session)
    except Exception as e:  # noqa: BLE001
        log(f"ai-session note failed (non-fatal): {e}", level="WARN")
    merge_into_memory(memory_path, session["learnings"], session["patterns"],
                      session["mistakes_to_avoid"], today, account=account)
    update_stats(stats_path, facts["usage"], cost_usd, capture_cost, model, today)

    # Daily note append
    daily_path = daily_dir / f"{today}.md"
    try:
        append_daily_section(daily_path, daily_template, today, daily_entry(session))
    except Exception as e:  # noqa: BLE001
        log(f"daily note append failed: {e!r}", level="WARN")

    log(f"captured session {session_id} (cost ${cost_usd:.4f}, capture overhead ${capture_cost:.5f}) → {agent_dir}", level="INFO")

    if args.dry_run:
        print(json.dumps(session, indent=2))
        print(f"\nDRY-RUN: wrote to {out_root}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
