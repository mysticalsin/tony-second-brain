#!/usr/bin/env python3
"""prerfp_radar.py — Pre-RFP radar: detect RFP-likelihood signals per client 60-180 days out.

Scans intel-agent writes.jsonl files, account notes, wiki account pages, and meeting
transcripts for RFP-likelihood keywords.  When a client's score crosses the threshold,
emits a capture plan (02_Areas/Accounts/<client>/capture-<client>.md) and an Important/
nudge with SBAP frontmatter.

Signal sources (checked in order, defensive parse throughout):
  1. _agent_state/<agent>/writes.jsonl  — agents: deal-intel, intel-deep, intel-vertical,
                                          competitive-intel, meeting-intel
  2. 02_Areas/Accounts/<client>/        — all .md files
  3. _wiki/accounts/<client>.md
  4. Meetings/ subtree                  — transcripts/, by-client/, recaps/

Signal weights (all additive; total forms a score 0.0-1.0 after capping at 1.0):
  - TIER_A_KEYWORDS (each match): +0.15  — renewal, contract end, vendor review, tender,
                                            RFI, budget cycle, S/4 migration, go-to-market
  - TIER_B_KEYWORDS (each match): +0.08  — dissatisfaction, incumbent, procurement,
                                            shortlist, evaluation, request for proposal,
                                            statement of work, sow, rfp
  - TIER_C_KEYWORDS (each match): +0.04  — roadmap, initiative, strategic, 2027, 2026,
                                            transformation, competitive, re-evaluation
  - RECENCY_BONUS per signal ≤7d old:   +0.10
  - RECENCY_BONUS per signal 8-30d old: +0.04
  - MULTI_SOURCE_BONUS (≥2 distinct sources contain ≥1 match in last 30d): +0.12

Threshold: 0.40 → trigger capture plan + nudge.

Usage:
    python build/tools/prerfp_radar.py [--client <name>] [--dry-run] [--no-llm] [--force]
    python build/tools/prerfp_radar.py --dry-run                   # score all, write nothing
    python build/tools/prerfp_radar.py --client globex --no-llm     # skeleton capture plan
    # Override vault root for testing:
    python build/tools/prerfp_radar.py --root /tmp/test-vault --no-llm --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

TRIGGER_THRESHOLD = 0.40

TIER_A_KEYWORDS = [
    "renewal", "contract end", "contract renewal", "vendor review",
    "rfi", "request for information", "budget cycle", "budget planning",
    "s/4 migration", "s4 migration", "s/4hana", "s4hana",
    "tender", "go-to-market", "go to market",
]
TIER_B_KEYWORDS = [
    "dissatisfaction", "incumbent", "procurement", "shortlist", "evaluation",
    "request for proposal", "rfp", "statement of work", "sow", "re-evaluate",
]
TIER_C_KEYWORDS = [
    "roadmap", "initiative", "strategic", "2027", "transformation",
    "competitive", "re-evaluation", "landscape review",
]

WEIGHT_TIER_A = 0.15
WEIGHT_TIER_B = 0.08
WEIGHT_TIER_C = 0.04
RECENCY_BONUS_FRESH = 0.10    # signal <= 7 days
RECENCY_BONUS_RECENT = 0.04   # signal 8-30 days
MULTI_SOURCE_BONUS = 0.12     # >= 2 distinct sources within 30d

INTEL_AGENTS = [
    "deal-intel", "intel-deep", "intel-vertical", "competitive-intel", "meeting-intel",
]

CAPTURE_PLAN_TTL_DAYS = 14   # Idempotency window


# ── Helpers ───────────────────────────────────────────────────────────────────

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def run_id() -> str:
    return f"{utcnow()}-prerfp-radar-{uuid.uuid4().hex[:8]}"


def slugify(name: str) -> str:
    """Convert client name to slug (lowercase, hyphens)."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


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


def synthesize(prompt: str, model: str = "claude-haiku-4-5") -> str:
    """Bounded claude -p call.  Raises RuntimeError on failure."""
    cb = claude_bin()
    if not cb:
        raise RuntimeError("claude CLI not found")
    result = subprocess.run(
        [
            cb, "-p", prompt, "--model", model,
            "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1",
             "ULTRON_VOICE": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (rc={result.returncode}): {result.stderr[:400]}"
        )
    return result.stdout.strip()


def parse_ts(ts_str: str) -> datetime | None:
    """Parse ISO timestamp string to UTC datetime, returns None on failure."""
    if not ts_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f+00:00", "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts_str[:26], fmt[:len(ts_str[:26])])
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def age_days(ts_str: str) -> float | None:
    """Return age in days from now, or None if unparseable."""
    dt = parse_ts(ts_str)
    if dt is None:
        return None
    return (utcnow_dt() - dt).total_seconds() / 86400


def find_keywords(text: str) -> list[tuple[str, str]]:
    """
    Return list of (tier, keyword) matches found in text (case-insensitive).
    Each keyword matched at most once per call.
    """
    text_lower = text.lower()
    hits = []
    for kw in TIER_A_KEYWORDS:
        if kw in text_lower:
            hits.append(("A", kw))
    for kw in TIER_B_KEYWORDS:
        if kw in text_lower:
            hits.append(("B", kw))
    for kw in TIER_C_KEYWORDS:
        if kw in text_lower:
            hits.append(("C", kw))
    return hits


# ── Signal collection ─────────────────────────────────────────────────────────

class Signal:
    """A single scored evidence item for one client."""

    __slots__ = ("source_type", "source_path", "ts_str", "text_snippet",
                 "keyword_hits", "age_d")

    def __init__(
        self,
        source_type: str,
        source_path: str,
        ts_str: str,
        text_snippet: str,
        keyword_hits: list[tuple[str, str]],
        age_d: float | None,
    ) -> None:
        self.source_type = source_type
        self.source_path = source_path
        self.ts_str = ts_str
        self.text_snippet = text_snippet
        self.keyword_hits = keyword_hits
        self.age_d = age_d


def collect_signals_from_writes_jsonl(
    path: Path, client_name: str
) -> list[Signal]:
    """
    Parse a writes.jsonl and return signals that mention the client.
    client_name match is case-insensitive substring of source_file or target fields.
    """
    signals: list[Signal] = []
    if not path.exists():
        return signals
    client_lower = client_name.lower()
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return signals
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # check if this entry concerns our client
        combined = " ".join(str(v) for v in entry.values()).lower()
        if client_lower not in combined:
            continue
        ts_str = entry.get("ts", "")
        text_snippet = json.dumps(entry)[:400]
        hits = find_keywords(combined)
        if not hits:
            continue
        signals.append(Signal(
            source_type="writes_jsonl",
            source_path=str(path),
            ts_str=ts_str,
            text_snippet=text_snippet,
            keyword_hits=hits,
            age_d=age_days(ts_str),
        ))
    return signals


def collect_signals_from_md_files(
    directory: Path, label: str
) -> list[Signal]:
    """
    Scan all .md files in a directory tree for RFP-likelihood keywords.
    Uses file mtime as a proxy age when no frontmatter date exists.
    """
    signals: list[Signal] = []
    if not directory.exists():
        return signals
    for md_file in sorted(directory.rglob("*.md")):
        try:
            text = md_file.read_text(errors="replace")
        except OSError:
            continue
        hits = find_keywords(text)
        if not hits:
            continue
        # Attempt to get date from frontmatter
        ts_str = ""
        fm_match = re.search(r"(?:date|last_updated|generated|created):\s*[\"']?(\d{4}-\d{2}-\d{2}[T\d:Z.+\-]*)[\"']?", text)
        if fm_match:
            ts_str = fm_match.group(1)
        if not ts_str:
            mtime = md_file.stat().st_mtime
            ts_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        snippet = text[:300].replace("\n", " ")
        signals.append(Signal(
            source_type=label,
            source_path=str(md_file),
            ts_str=ts_str,
            text_snippet=snippet,
            keyword_hits=hits,
            age_d=age_days(ts_str),
        ))
    return signals


def collect_signals_for_client(
    vault: Path, client_name: str
) -> list[Signal]:
    """Gather all signals for a given client from all configured sources."""
    signals: list[Signal] = []
    slug = slugify(client_name)

    # Source 1: intel-agent writes.jsonl files
    for agent in INTEL_AGENTS:
        jsonl = vault / "_agent_state" / agent / "writes.jsonl"
        signals.extend(collect_signals_from_writes_jsonl(jsonl, client_name))
        # Also check slugified form
        if slug != client_name.lower():
            signals.extend(collect_signals_from_writes_jsonl(jsonl, slug))

    # Source 2: 02_Areas/Accounts/<client>/ notes
    accounts_dir = vault / "02_Areas" / "Accounts" / client_name
    if not accounts_dir.exists():
        accounts_dir = vault / "02_Areas" / "Accounts" / slug
    signals.extend(collect_signals_from_md_files(accounts_dir, "account_notes"))

    # Source 3: _wiki/accounts/<client>.md
    wiki_file = vault / "_wiki" / "accounts" / f"{client_name}.md"
    if not wiki_file.exists():
        wiki_file = vault / "_wiki" / "accounts" / f"{slug}.md"
    if wiki_file.exists():
        try:
            text = wiki_file.read_text(errors="replace")
        except OSError:
            text = ""
        hits = find_keywords(text)
        if hits:
            ts_str = ""
            fm_match = re.search(r"last_distilled:\s*(\d{4}-\d{2}-\d{2})", text)
            if fm_match:
                ts_str = fm_match.group(1)
            if not ts_str:
                mtime = wiki_file.stat().st_mtime
                ts_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            signals.append(Signal(
                source_type="wiki_accounts",
                source_path=str(wiki_file),
                ts_str=ts_str,
                text_snippet=text[:300].replace("\n", " "),
                keyword_hits=hits,
                age_d=age_days(ts_str),
            ))

    # Source 4: Meetings/ subtree — filter to client mentions
    meetings_root = vault / "Meetings"
    if meetings_root.exists():
        for md_file in sorted(meetings_root.rglob("*.md")):
            try:
                text = md_file.read_text(errors="replace")
            except OSError:
                continue
            # Only include if client name appears in the file
            if client_name.lower() not in text.lower() and slug not in text.lower():
                continue
            hits = find_keywords(text)
            if not hits:
                continue
            ts_str = ""
            fm_match = re.search(r"date:\s*[\"']?(\d{4}-\d{2}-\d{2}[T\d:Z.+\-]*)[\"']?", text)
            if fm_match:
                ts_str = fm_match.group(1)
            if not ts_str:
                mtime = md_file.stat().st_mtime
                ts_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            signals.append(Signal(
                source_type="meetings",
                source_path=str(md_file),
                ts_str=ts_str,
                text_snippet=text[:300].replace("\n", " "),
                keyword_hits=hits,
                age_d=age_days(ts_str),
            ))

    return signals


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_client(signals: list[Signal]) -> tuple[float, dict]:
    """
    Compute a 0.0-1.0 RFP-likelihood score from collected signals.
    Returns (score, breakdown_dict).
    """
    if not signals:
        return 0.0, {"tier_a": 0, "tier_b": 0, "tier_c": 0, "recency": 0.0,
                     "multi_source": False, "raw_total": 0.0}

    score = 0.0
    tier_a_count = 0
    tier_b_count = 0
    tier_c_count = 0
    recency_bonus = 0.0
    now = utcnow_dt()
    cutoff_30d = now - timedelta(days=30)

    distinct_sources_30d: set[str] = set()

    for sig in signals:
        for tier, kw in sig.keyword_hits:
            if tier == "A":
                score += WEIGHT_TIER_A
                tier_a_count += 1
            elif tier == "B":
                score += WEIGHT_TIER_B
                tier_b_count += 1
            else:
                score += WEIGHT_TIER_C
                tier_c_count += 1

        # Recency bonus (per-signal, not per-keyword)
        if sig.age_d is not None:
            if sig.age_d <= 7:
                recency_bonus += RECENCY_BONUS_FRESH
                score += RECENCY_BONUS_FRESH
            elif sig.age_d <= 30:
                recency_bonus += RECENCY_BONUS_RECENT
                score += RECENCY_BONUS_RECENT
            if sig.age_d <= 30:
                distinct_sources_30d.add(sig.source_type)

    multi_source = len(distinct_sources_30d) >= 2
    if multi_source:
        score += MULTI_SOURCE_BONUS

    raw_total = score
    score = min(score, 1.0)

    return score, {
        "tier_a": tier_a_count,
        "tier_b": tier_b_count,
        "tier_c": tier_c_count,
        "recency_bonus": round(recency_bonus, 3),
        "multi_source": multi_source,
        "distinct_sources_30d": sorted(distinct_sources_30d),
        "raw_total": round(raw_total, 3),
        "n_signals": len(signals),
    }


# ── Client discovery ──────────────────────────────────────────────────────────

def discover_clients(vault: Path) -> list[str]:
    """
    Return all known client names from:
      - 02_Areas/Accounts/<name>/ subdirectories
      - _wiki/accounts/*.md filestems
    """
    clients: set[str] = set()
    accounts_dir = vault / "02_Areas" / "Accounts"
    if accounts_dir.exists():
        for p in accounts_dir.iterdir():
            if p.is_dir() and not p.name.startswith("_"):
                clients.add(p.name)
    wiki_accounts = vault / "_wiki" / "accounts"
    if wiki_accounts.exists():
        for md in wiki_accounts.glob("*.md"):
            if not md.stem.startswith("_"):
                clients.add(md.stem)
    return sorted(clients)


# ── Capture plan generation ───────────────────────────────────────────────────

def build_capture_plan_prompt(
    client: str, score: float, breakdown: dict, signals: list[Signal]
) -> str:
    """Build the prompt for the LLM capture plan."""
    signal_lines = "\n".join(
        f"- [{s.source_type}] {s.ts_str}: {', '.join(kw for _, kw in s.keyword_hits[:5])} "
        f"| snippet: {s.text_snippet[:120]}"
        for s in signals[:12]
    )
    return f"""You are a pre-sales expert at a consulting firm preparing a capture plan.

Client: {client}
Pre-RFP likelihood score: {score:.2f} / 1.00 (threshold: {TRIGGER_THRESHOLD})
Breakdown: {json.dumps(breakdown)}

Detected signals (most recent first):
{signal_lines}

Write a capture plan in Obsidian Markdown following a capture-plan structure:
- Win Themes (3 bullets max, grounded in signals above)
- Hot Buttons (what the client cares about, based on keywords found)
- Ghosting Moves (2-3 specific moves to shape the requirements before the RFP lands)
- Relationship Moves (who to meet, what to ask, timeline)
- 60-180 Day Timeline (milestones from now to expected RFP submission)

Be specific and grounded in the signals. No generic filler. Tone: senior sales leader.
Keep it under 500 words. Start directly with the ## Win Themes heading."""


def build_capture_plan_skeleton(
    client: str, score: float, breakdown: dict, signals: list[Signal]
) -> str:
    """Deterministic skeleton capture plan (no-LLM fallback)."""
    keyword_list = sorted({kw for s in signals for _, kw in s.keyword_hits})
    signal_lines = "\n".join(
        f"- [{s.source_type}] {s.ts_str}: {', '.join(kw for _, kw in s.keyword_hits[:5])}"
        for s in signals[:10]
    )
    return f"""## Win Themes

- Detected RFP-likelihood signals: {", ".join(keyword_list[:6]) or "see signals below"}
- Score {score:.2f} / 1.00 — above trigger threshold of {TRIGGER_THRESHOLD}
- [Add win themes once account team reviews signals]

## Hot Buttons

- [Derive from signal keywords: {", ".join(keyword_list[:4])}]
- [Confirm with account manager before RFP lands]

## Ghosting Moves

- Request an informal briefing session to understand upcoming priorities
- Position your firm's relevant capability aligned to detected themes
- [Specific ghosting action TBD based on account context]

## Relationship Moves

- Identify and engage economic buyer + procurement contact within 30 days
- [Complete stakeholder map based on account intelligence]

## 60-180 Day Timeline

| Days out | Milestone |
|---|---|
| 150-180 | Initial outreach / pulse check |
| 90-120 | Discovery call + capability positioning |
| 60-90 | Pre-RFP brief (if available) |
| 30-60 | Final positioning; ready to respond in 48h |
| 0 | RFP receipt — activate bid team |

## Detected Signals

{signal_lines}
"""


def write_capture_plan(
    vault: Path,
    client: str,
    score: float,
    breakdown: dict,
    signals: list[Signal],
    no_llm: bool,
    force: bool,
) -> Path | None:
    """
    Write capture-<client>.md to 02_Areas/Accounts/<client>/.
    Returns the written path, or None if skipped (idempotency).
    """
    slug = slugify(client)
    accounts_dir = vault / "02_Areas" / "Accounts" / client
    if not accounts_dir.exists():
        accounts_dir = vault / "02_Areas" / "Accounts" / slug
    if not accounts_dir.exists():
        # Create it
        accounts_dir = vault / "02_Areas" / "Accounts" / (client if client else slug)
        accounts_dir.mkdir(parents=True, exist_ok=True)

    plan_path = accounts_dir / f"capture-{slug}.md"

    # Idempotency check
    if plan_path.exists() and not force:
        mtime = plan_path.stat().st_mtime
        age = (utcnow_dt() - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds() / 86400
        if age < CAPTURE_PLAN_TTL_DAYS:
            print(
                f"  [radar] SKIP {plan_path.name} — written {age:.1f}d ago (< {CAPTURE_PLAN_TTL_DAYS}d). "
                f"Use --force to overwrite.",
                file=sys.stderr,
            )
            return None

    # Generate content
    if no_llm:
        body = build_capture_plan_skeleton(client, score, breakdown, signals)
    else:
        prompt = build_capture_plan_prompt(client, score, breakdown, signals)
        try:
            body = synthesize(prompt)
        except RuntimeError as exc:
            print(f"  [radar] LLM failed ({exc}) — falling back to skeleton", file=sys.stderr)
            body = build_capture_plan_skeleton(client, score, breakdown, signals)

    now_str = utcnow()
    breakdown_yaml = "\n".join(f"  {k}: {v}" for k, v in breakdown.items())
    content = f"""---
sbap_version: "1.0"
source_agent: prerfp-radar
source_run_id: "{run_id()}"
generated: "{now_str}"
output_type: capture_plan
client: "{client}"
rfp_likelihood_score: {score:.3f}
trigger_threshold: {TRIGGER_THRESHOLD}
n_signals: {breakdown.get("n_signals", 0)}
score_breakdown:
{breakdown_yaml}
target_path: "02_Areas/Accounts/{client}/capture-{slug}.md"
confidence: {min(score, 0.84):.2f}
---

# Capture Plan — {client}

> **Pre-RFP likelihood:** {score:.2f} / 1.00  |  Generated: {now_str}  |  Threshold: {TRIGGER_THRESHOLD}

{body}
"""
    plan_path.write_text(content)
    return plan_path


def write_important_nudge(
    vault: Path,
    client: str,
    score: float,
    breakdown: dict,
    plan_path: Path | None,
) -> Path:
    """Write a nudge file to Important/ with SBAP frontmatter."""
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = slugify(client)
    nudge_name = f"{date_prefix}-prerfp-radar-{slug}.md"
    nudge_path = vault / "Important" / nudge_name

    plan_ref = str(plan_path.relative_to(vault)) if plan_path else "(capture plan not written)"
    score_pct = int(score * 100)
    now_str = utcnow()
    rid = run_id()

    content = f"""---
sbap_version: "1.0"
source_agent: prerfp-radar
source_run_id: "{rid}"
generated: "{now_str}"
output_type: escalation_alert
target_path: "Important/{nudge_name}"
confidence: {min(score, 0.84):.2f}
needs_review: true
reasoning_summary: "Pre-RFP radar detected likelihood {score:.2f} for {client} — {breakdown.get('n_signals', 0)} signals, {breakdown.get('tier_a', 0)} tier-A keywords"
---

# Pre-RFP radar: {client} — likelihood {score_pct}%

**Date:** {date_prefix}

| Field | Value |
|---|---|
| Client | `{client}` |
| RFP Likelihood Score | **{score:.2f}** / 1.00 ({score_pct}%) |
| Trigger Threshold | {TRIGGER_THRESHOLD} |
| Signals Found | {breakdown.get("n_signals", 0)} |
| Tier-A Keywords | {breakdown.get("tier_a", 0)} |
| Tier-B Keywords | {breakdown.get("tier_b", 0)} |
| Tier-C Keywords | {breakdown.get("tier_c", 0)} |
| Multi-source (30d) | {breakdown.get("multi_source", False)} |
| Capture Plan | `{plan_ref}` |

## Actions

- [ ] Review capture plan at `{plan_ref}`
- [ ] Confirm decision-maker contacts
- [ ] Schedule pre-RFP outreach within 30 days
- [ ] Update account notes with latest intelligence
"""
    nudge_path.write_text(content)
    return nudge_path


# ── State persistence ─────────────────────────────────────────────────────────

def load_state(vault: Path) -> dict:
    """Load prerfp-radar memory.json, or return a fresh state."""
    state_dir = vault / "_agent_state" / "prerfp-radar"
    state_file = state_dir / "memory.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "agent": "prerfp-radar",
        "memory_version": 1,
        "last_updated": "",
        "last_run": "",
        "per_client": {},
    }


def save_state(vault: Path, state: dict) -> None:
    """Persist prerfp-radar memory.json."""
    state_dir = vault / "_agent_state" / "prerfp-radar"
    state_dir.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = utcnow()
    state_file = state_dir / "memory.json"
    state_file.write_text(json.dumps(state, indent=2))


# ── Main radar run ─────────────────────────────────────────────────────────────

def run_radar(
    vault: Path,
    client_filter: str | None,
    dry_run: bool,
    no_llm: bool,
    force: bool,
) -> int:
    """
    Main entry point.  Returns exit code (0 = ok, 1 = error).
    """
    state = load_state(vault)
    state["last_run"] = utcnow()

    # Discover clients
    if client_filter:
        clients = [client_filter]
    else:
        clients = discover_clients(vault)
        if not clients:
            print("[radar] No clients discovered. Add accounts to 02_Areas/Accounts/ or _wiki/accounts/.")
            return 0

    print(f"[radar] Scanning {len(clients)} client(s): {', '.join(clients)}")

    results: list[tuple[str, float, dict]] = []

    for client in clients:
        signals = collect_signals_for_client(vault, client)
        score, breakdown = score_client(signals)
        results.append((client, score, breakdown))
        if not dry_run:
            state.setdefault("per_client", {})[client] = {
                "last_scored": utcnow(),
                "last_score": round(score, 3),
                "n_signals": breakdown.get("n_signals", 0),
            }

    # Sort by score descending
    results.sort(key=lambda x: x[1], reverse=True)

    # Print scoring table
    print(f"\n{'Client':<30} {'Score':>7}  {'n_sig':>6}  {'A':>4}  {'B':>4}  {'C':>4}  {'Multi':>6}  {'Trigger':>8}")
    print("-" * 85)
    for client, score, bd in results:
        trigger = ">= threshold" if score >= TRIGGER_THRESHOLD else "below"
        print(
            f"{client:<30} {score:>7.3f}  {bd.get('n_signals', 0):>6}  "
            f"{bd.get('tier_a', 0):>4}  {bd.get('tier_b', 0):>4}  "
            f"{bd.get('tier_c', 0):>4}  {str(bd.get('multi_source', False)):>6}  {trigger:>12}"
        )
    print()

    if dry_run:
        print("[radar] --dry-run: no files written.")
        return 0

    # Act on triggers
    triggered = [(c, s, bd) for c, s, bd in results if s >= TRIGGER_THRESHOLD]
    if not triggered:
        print("[radar] No clients above trigger threshold — nothing written.")
        save_state(vault, state)
        return 0

    for client, score, breakdown in triggered:
        signals = collect_signals_for_client(vault, client)
        print(f"[radar] TRIGGER: {client} score={score:.3f} — writing capture plan + nudge")
        plan_path = write_capture_plan(vault, client, score, breakdown, signals, no_llm, force)
        if plan_path:
            print(f"  -> capture plan: {plan_path}")
        nudge_path = write_important_nudge(vault, client, score, breakdown, plan_path)
        print(f"  -> nudge: {nudge_path}")

    save_state(vault, state)
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-RFP radar — detect RFP-likelihood signals per client 60-180d out."
    )
    parser.add_argument("--client", metavar="NAME",
                        help="Scan only this client (else all discovered clients)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print scoring table; write nothing")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM call; use deterministic skeleton capture plan")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite capture plan even if written < 14 days ago")
    parser.add_argument("--root", metavar="PATH",
                        help="Override vault root (for testing; default: $VAULT or cwd)")
    args = parser.parse_args()

    # Resolve vault
    if args.root:
        vault = Path(args.root).resolve()
    else:
        vault = Path(os.environ.get("VAULT", Path.cwd())).resolve()

    if not vault.exists():
        print(f"[radar] ERROR: vault root does not exist: {vault}", file=sys.stderr)
        sys.exit(1)

    no_llm = args.no_llm or bool(os.environ.get("NO_LLM", ""))

    rc = run_radar(
        vault=vault,
        client_filter=args.client,
        dry_run=args.dry_run,
        no_llm=no_llm,
        force=args.force,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
