#!/usr/bin/env python3
"""ingest_ai_sessions.py - distill Codex / Gemini-Antigravity / Dust sessions into vault notes.

One markdown note per conversation lands in 02_Areas/AI Sessions/<tool>/, schema
`type: ai-session` (counted by 02_Areas/Bases/AI Sessions.base on the Command Center).
Claude notes are written by capture_session.py at SessionEnd, NOT here (no double count).

Sources:
  codex   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (+ ~/.codex/session_index.jsonl for titles)
  gemini  ~/.gemini/antigravity/conversations/  (format unconfirmed; unknown files are logged, never guessed)
  dust    _agent_state/<agent>/writes.jsonl (one note per write row = one agent run)

Checkpoints: _agent_state/<tool>/last_ingest.json {"last_ts": iso-string-or-null}.
Idempotent: a note whose target path already exists is never rewritten.
Privacy: sessions whose cwd contains an excluded fragment are skipped entirely.

Run by 99_Meta/brain-refresh.sh hourly. Manual: python3 build/tools/ingest_ai_sessions.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
SESSIONS_DIR = VAULT / "02_Areas" / "AI Sessions"
AGENT_STATE = VAULT / "_agent_state"
CODEX_HOME = Path.home() / ".codex"
GEMINI_CONVERSATIONS = Path.home() / ".gemini" / "antigravity" / "conversations"
EXCLUDED_CWD_FRAGMENTS = ("HR Documents/", "LinkedIn/", "Meetings/Confidential/")
SUMMARY_MAX = 140


def log(msg: str) -> None:
    print(f"[ingest_ai_sessions] {msg}")


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")


def cwd_is_excluded(cwd: str) -> bool:
    return any(frag in cwd for frag in EXCLUDED_CWD_FRAGMENTS)


def load_checkpoint(tool: str) -> str | None:
    p = AGENT_STATE / tool / "last_ingest.json"
    try:
        return json.loads(p.read_text()).get("last_ts")
    except (OSError, json.JSONDecodeError):
        return None


def save_checkpoint(tool: str, ts: str) -> None:
    p = AGENT_STATE / tool / "last_ingest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"last_ts": ts}) + "\n")
    tmp.replace(p)


def write_session_note(sessions_dir: Path, tool: str, session: dict) -> Path | None:
    """Write one ai-session note. Returns the path, or None if it already exists."""
    # Use the LAST 8 alphanumeric chars of the session_id (the random tail) as the
    # discriminator.  UUIDv7 ids share a common 8-char timestamp *prefix* across all
    # sessions from the same time window, so [:8] was a collision factory.  The tail
    # is the random portion and is collision-safe.
    short_id = re.sub(r"[^A-Za-z0-9]", "", str(session["session_id"]))[-8:] or "noid"
    slug = slugify(session.get("summary", "")) or "session"
    target = sessions_dir / tool / f"{session['date']}-{slug[:40].rstrip('-')}-{short_id}.md"
    if target.exists():
        return None
    summary = session.get("summary", "").replace('"', "'")[:SUMMARY_MAX]
    lines = [
        "---",
        "type: ai-session",
        f"tool: {tool}",
        f"date: {session['date']}",
        f"session_id: {session['session_id']}",
        f'summary: "{summary}"',
    ]
    if session.get("turns") is not None:
        lines.append(f"turns: {session['turns']}")
    if session.get("cost_usd") is not None:
        lines.append(f"cost_usd: {session['cost_usd']}")
    if session.get("cwd"):
        lines.append(f'cwd: "{session["cwd"]}"')
    if session.get("agent"):
        lines.append(f"agent: {session['agent']}")
    lines += ["tags: [ai-session]", "---", "", f"# {tool} · {session['date']} · {summary or short_id}", ""]
    if session.get("summary"):
        lines.append(f"- **Ask**: {session['summary']}")
    if session.get("last_message"):
        last = session["last_message"].replace("\n", " ")[:400]
        lines.append(f"- **Outcome**: {last}")
    if session.get("turns") is not None:
        lines.append(f"- **Turns**: {session['turns']}")
    if session.get("detail"):
        lines.append(f"- **Detail**: {session['detail']}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(target)
    return target


# ------------------------------- codex --------------------------------------

def codex_thread_names(index_path: Path) -> dict:
    names = {}
    if not index_path.exists():
        return names
    for line in index_path.read_text().splitlines():
        try:
            row = json.loads(line)
            names[row["id"]] = row.get("thread_name", "")
        except (json.JSONDecodeError, KeyError):
            continue
    return names


def parse_codex_rollout(path: Path, thread_names: dict | None = None) -> dict | None:
    """Parse one rollout jsonl. Returns a session dict, or None (empty/excluded)."""
    meta, first_user, last_agent, turns = None, None, None, 0
    try:
        with path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t, p = d.get("type"), d.get("payload") or {}
                if t == "session_meta" and meta is None:
                    meta = p
                elif t == "event_msg" and p.get("type") == "user_message":
                    turns += 1
                    if first_user is None:
                        first_user = p.get("message", "")
                elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                    turns += 1
                    if first_user is None:
                        texts = [c.get("text", "") for c in p.get("content", []) if isinstance(c, dict)]
                        first_user = " ".join(texts)
                elif t == "event_msg" and p.get("type") == "task_complete":
                    last_agent = p.get("last_agent_message") or last_agent
    except OSError as e:
        log(f"codex: cannot read {path.name}: {e}")
        return None
    if meta is None:
        return None
    cwd = meta.get("cwd", "")
    if cwd_is_excluded(cwd):
        return None
    sid = meta.get("id", path.stem)
    title = (thread_names or {}).get(sid) or (first_user or "").strip()
    title = re.sub(r"\s+", " ", title)[:SUMMARY_MAX]
    date = (meta.get("timestamp") or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"session_id": sid, "date": date, "cwd": cwd, "summary": title,
            "turns": turns, "last_message": (last_agent or "").strip()}


def ingest_codex(codex_home: Path, sessions_dir: Path, since_ts: str | None) -> int:
    root = codex_home / "sessions"
    if not root.is_dir():
        log("codex: no sessions dir, skipping")
        return 0
    since = datetime.fromisoformat(since_ts.replace("Z", "+00:00")).timestamp() if since_ts else 0.0
    names = codex_thread_names(codex_home / "session_index.jsonl")
    written = 0
    for path in sorted(root.rglob("rollout-*.jsonl")):
        if path.stat().st_mtime <= since:
            continue
        session = parse_codex_rollout(path, names)
        if session and write_session_note(sessions_dir, "codex", session):
            written += 1
    return written


# ------------------------------- gemini -------------------------------------

def ingest_gemini(conversations_dir: Path, sessions_dir: Path, since_ts: str | None) -> int:
    """Antigravity conversation format is unconfirmed (dir was empty on 2026-06-04).
    Parse JSON files defensively; log anything else visibly instead of guessing."""
    if not conversations_dir.is_dir():
        log("gemini: no conversations dir, skipping")
        return 0
    since = datetime.fromisoformat(since_ts.replace("Z", "+00:00")).timestamp() if since_ts else 0.0
    written, unknown = 0, 0
    for path in sorted(conversations_dir.rglob("*")):
        if not path.is_file() or path.stat().st_mtime <= since:
            continue
        if path.suffix == ".json":
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                unknown += 1
                continue
            title = ""
            if isinstance(data, dict):
                title = str(data.get("title") or data.get("name") or data.get("summary") or "")
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            session = {"session_id": path.stem, "date": mtime.strftime("%Y-%m-%d"),
                       "summary": title[:SUMMARY_MAX] or path.stem,
                       "detail": f"raw file: {path}"}
            if write_session_note(sessions_dir, "gemini", session):
                written += 1
        else:
            unknown += 1
    if unknown:
        log(f"gemini: {unknown} file(s) in an unrecognized format were NOT ingested "
            f"(probe {conversations_dir} and extend ingest_gemini)")
    return written


# -------------------------------- dust --------------------------------------

def ingest_dust(agent_state_dir: Path, sessions_dir: Path, since_ts: str | None) -> int:
    written = 0
    for writes in sorted(agent_state_dir.glob("*/writes.jsonl")):
        agent = writes.parent.name
        if agent in ("codex", "gemini", "claude-code"):
            continue
        try:
            text = writes.read_text()
        except OSError as e:
            log(f"dust: cannot read {writes}: {e}")
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts", "")
            if since_ts and ts <= since_ts:
                continue
            run_id = row.get("source_run_id") or ts
            session = {
                "session_id": str(run_id),
                "date": ts[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "summary": f"{agent}: {row.get('action', '?')} {row.get('output_type', '')} -> {row.get('target', '')}".strip(),
                "agent": agent,
                "detail": f"confidence {row.get('confidence')}",
            }
            if write_session_note(sessions_dir, "dust", session):
                written += 1
    return written


# -------------------------------- main --------------------------------------

def main() -> int:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = 0
    for tool, fn, args in (
        ("codex", ingest_codex, (CODEX_HOME, SESSIONS_DIR)),
        ("gemini", ingest_gemini, (GEMINI_CONVERSATIONS, SESSIONS_DIR)),
        ("dust", ingest_dust, (AGENT_STATE, SESSIONS_DIR)),
    ):
        since = load_checkpoint(tool)
        try:
            n = fn(*args, since_ts=since)
        except Exception as e:  # fail visibly, never silently
            log(f"{tool}: INGEST FAILED: {e!r}")
            continue
        log(f"{tool}: {n} new session note(s) (since {since or 'beginning'})")
        save_checkpoint(tool, now_iso)
        total += n
    log(f"done: {total} note(s) written to {SESSIONS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
