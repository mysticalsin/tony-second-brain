#!/usr/bin/env python3
"""deal_tape.py — Deal microstructure tape: per-meeting spread analysis for a bid.

Scans meeting files (Meetings/Confidential/ and Meetings/transcripts/) matching
a given client and/or keyword list, extracts a per-meeting spread object from the
Summary + Decisions + Action Items sections, computes a spread score per meeting,
and assembles a time-ordered tape that identifies whether the deal is compressing,
flat, or widening.

Spread score heuristics (documented below) are used in --no-llm mode.  LLM mode
(claude-haiku-4-5) sends only the Summary + Decisions + Action Items text (NOT the
full transcript) as a JSON-only prompt.

Output files (generated, never committed):
  _brain_api/bid/<bid_id>/spread_tape.json   -- structured tape
  _brain_api/bid/<bid_id>/spread_tape.md     -- human-readable table + sparkline

Escalation (real runs only, idempotent via _agent_state/deal-tape/memory.json):
  trend=widening AND bid stage in (Propose, Negotiate)
  -> Important/escalations/<date>-deal-tape-<bid_id>-widening.md

Heartbeat:
  _agent_state/deal-tape/memory.json         -- last_run, meetings_seen, errors

Weights and scoring rationale:
  Spread score per meeting = sum of weighted components:
    unresolved_objections: each objection token adds WEIGHT_OBJECTION = 0.30
    price_pushback:        price_signal == -1  adds WEIGHT_PRICE     = 0.40
    scope_wider:           scope_delta == wider adds WEIGHT_SCOPE     = 0.20
    (floored at 0.0, not capped — can exceed 1.0 for heavily troubled meetings)

  Trend is computed by fitting a line (least-squares) over the last N>=3 spread
  scores.  Slope > +SLOPE_THRESHOLD  -> widening
           Slope < -SLOPE_THRESHOLD  -> compressing
           otherwise                 -> flat
  SLOPE_THRESHOLD = 0.05 (per meeting step).

Keyword heuristics for --no-llm fallback:
  Objection keywords:  expensive, too high, not competitive, steep, overpriced,
                       budget, reduce, discount, lower price, high cost, too costly,
                       over budget, above budget, margin, cut cost, price concern
  Price warming (+1):  competitive, fair price, good value, aligned on price,
                       price is fine, acceptable, within range
  Scope widening:      add scope, additional scope, expand scope, include more,
                       wider coverage, plus services, more countries, extend to
  Scope narrowing:     reduce scope, narrower, fewer countries, exclude, cut scope,
                       remove from scope, limit to

Usage:
    python build/tools/deal_tape.py --bid globex-sap-ams --client "Globex" --no-llm
    python build/tools/deal_tape.py --bid globex-sap-ams --client "Globex"
    python build/tools/deal_tape.py --bid test-bid --client synthetic-corp \\
        --root /tmp/test-vault --no-llm --dry-run
    # Override vault root for testing:
    python build/tools/deal_tape.py --root /tmp/my-fixture --bid bid1 --client corp \\
        --no-llm --dry-run
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

WEIGHT_OBJECTION = 0.30   # per objection token in unresolved_objections list
WEIGHT_PRICE = 0.40       # flat add when price_signal == -1
WEIGHT_SCOPE = 0.20       # flat add when scope_delta == "wider"
SLOPE_THRESHOLD = 0.05    # per-meeting slope magnitude to classify trend

OBJECTION_KWS = [
    # English
    "expensive", "too high", "not competitive", "steep", "overpriced",
    "budget", "reduce", "discount", "lower price", "high cost", "too costly",
    "over budget", "above budget", "margin", "cut cost", "price concern",
    "above market", "reduce the price", "lower the price",
    # French
    "trop élevé", "trop cher", "pas compétitif", "hors budget", "prix jugé",
    "réduire le coût", "réduire les coûts", "trop de ressources", "surestim",
]
PRICE_WARMING_KWS = [
    "competitive", "fair price", "good value", "aligned on price",
    "price is fine", "acceptable", "within range", "looks good", "well positioned",
]
SCOPE_WIDER_KWS = [
    "add scope", "additional scope", "expand scope", "include more",
    "wider coverage", "plus services", "more countries", "extend to",
    "extra scope", "broader",
]
SCOPE_NARROWER_KWS = [
    "reduce scope", "narrower", "fewer countries", "exclude", "cut scope",
    "remove from scope", "limit to", "descope", "drop from scope",
]

# SBAP escalation frontmatter
ESCALATION_TEMPLATE = """\
---
sbap_version: "1.0"
source_agent: deal-tape
source_run_id: "{run_id}"
generated: "{generated}"
output_type: escalation_alert
target_path: "Important/escalations/{filename}"
confidence: 0.88
needs_review: true
reasoning_summary: "Deal spread widening on {bid_id} — {n_meetings} meetings analysed, trend=widening."
---

# DEAL SPREAD WIDENING — {bid_id}

> **Generated:** {generated}
> **Bid:** `{bid_id}`
> **Client:** {client}
> **Stage:** {stage}
> **Trend:** widening (slope={slope:+.3f} per meeting over last {window} meetings)

## What this means

The spread score has increased across recent meetings, indicating accumulating
unresolved objections, price pushback, or scope creep.  In a Propose/Negotiate
stage, widening spread is a material risk signal.

## Per-meeting score summary

{score_table}

## Recommended actions

1. Review `_brain_api/bid/{bid_id}/spread_tape.md` for the full tape.
2. Identify which meetings contain unresolved price objections or scope widening.
3. Schedule an internal alignment call before the next client touchpoint.
4. Consider a proactive concession or scope-clarification communication.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id() -> str:
    return f"{utcnow()}-deal-tape-{uuid.uuid4().hex[:8]}"


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"['’‘]", "", s)  # strip apostrophes
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def claude_bin() -> Optional[str]:
    for c in (
        str(Path.home() / ".local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        shutil.which("claude"),
    ):
        if c and Path(c).exists():
            return c
    return None


def parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp to aware UTC datetime."""
    if not ts_str:
        return None
    # Handle numeric timezone offsets
    ts_clean = ts_str.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts_clean, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return None


def ls_str(slope: float) -> str:
    """Least-squares slope to trend label."""
    if slope > SLOPE_THRESHOLD:
        return "widening"
    if slope < -SLOPE_THRESHOLD:
        return "compressing"
    return "flat"


def least_squares_slope(scores: list[float]) -> float:
    """Simple least-squares slope over evenly-spaced points."""
    n = len(scores)
    if n < 2:
        return 0.0
    xs = list(range(n))
    xm = sum(xs) / n
    ym = sum(scores) / n
    num = sum((xs[i] - xm) * (scores[i] - ym) for i in range(n))
    den = sum((xs[i] - xm) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


def sparkline(scores: list[float]) -> str:
    """ASCII sparkline from float scores (0–2+ range mapped to 8 blocks)."""
    if not scores:
        return ""
    blocks = " _.-+*=#@"  # 9 chars, ascending density
    mn = min(scores)
    mx = max(scores)
    rng = mx - mn if mx != mn else 1.0
    result = []
    for s in scores:
        idx = int((s - mn) / rng * (len(blocks) - 1))
        result.append(blocks[min(idx, len(blocks) - 1)])
    return "".join(result)


# ── YAML frontmatter parser ───────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Extract YAML-like frontmatter from a markdown file.
    Returns (frontmatter_dict, body_text).
    Only handles simple key: value pairs and lists (no nested maps).
    """
    fm: dict = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return fm, body
    raw = m.group(1)
    body = text[m.end():]
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kv = re.match(r'^([A-Za-z_][A-Za-z_0-9]*):\s*(.*)', line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        # Unquote strings
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        # Parse lists like [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            items = [i.strip().strip('"\'') for i in val[1:-1].split(",") if i.strip()]
            fm[key] = items
        else:
            fm[key] = val
    return fm, body


def extract_sections(body: str) -> dict[str, str]:
    """
    Extract named sections from markdown body.
    Returns dict with keys like 'Summary', 'Decisions', 'Action items', etc.
    Values are the section text (stripped).
    """
    sections: dict[str, str] = {}
    current_title: Optional[str] = None
    current_lines: list[str] = []

    for line in body.splitlines():
        h = re.match(r'^(#{1,4})\s+(.*)', line)
        if h:
            if current_title is not None:
                sections[current_title] = "\n".join(current_lines).strip()
            current_title = h.group(2).strip()
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        sections[current_title] = "\n".join(current_lines).strip()

    return sections


def build_extract_text(sections: dict[str, str]) -> str:
    """Compose the text sent to LLM or scanned by heuristics."""
    parts = []
    for key in ("Summary", "Decisions", "Action items"):
        # Case-insensitive lookup
        for k, v in sections.items():
            if k.lower() == key.lower() and v:
                parts.append(f"## {k}\n{v}")
                break
    return "\n\n".join(parts)


# ── Meeting file discovery ────────────────────────────────────────────────────

def discover_meetings(
    vault: Path,
    client: str,
    keywords: list[str],
    limit: Optional[int],
) -> list[Path]:
    """
    Walk Meetings/Confidential/ and Meetings/transcripts/ for files whose
    meeting_name frontmatter field or filename body matches client or keywords.
    Returns files sorted by start time (ascending).
    """
    search_dirs = [
        vault / "Meetings" / "Confidential",
        vault / "Meetings" / "transcripts",
    ]
    client_lower = client.lower() if client else ""
    kws_lower = [k.lower() for k in keywords] if keywords else []

    # Normalise client for matching: strip accents / apostrophes loosely
    def norm(s: str) -> str:
        return re.sub(r"['’‘éèê]", "", s.lower())

    client_norm = norm(client_lower)

    candidates: list[tuple[Optional[datetime], Path]] = []

    for d in search_dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name.startswith("_") or p.name == "README.md":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue

            fm, body = parse_frontmatter(text)
            name = fm.get("meeting_name", "") or ""
            name_norm = norm(name)
            filename_norm = norm(p.name)

            # Match client
            matched = False
            if client_norm and (client_norm in name_norm or client_norm in filename_norm):
                matched = True

            # Match keywords
            if not matched and kws_lower:
                combined = (name_norm + " " + filename_norm + " " + norm(body[:2000])).lower()
                if any(kw in combined for kw in kws_lower):
                    matched = True

            if not matched:
                continue

            start_str = fm.get("start", "")
            ts = parse_ts(start_str) if start_str else None
            candidates.append((ts, p))

    # Sort by start time, None timestamps go last
    candidates.sort(key=lambda x: (x[0] is None, x[0] or datetime.min.replace(tzinfo=timezone.utc)))

    paths = [p for _, p in candidates]
    if limit:
        paths = paths[:limit]
    return paths


# ── Spread extraction — heuristic fallback ────────────────────────────────────

def heuristic_extract(text: str) -> dict:
    """
    Keyword-based spread extraction.  Returns the same shape as LLM output:
    {
        client_ask: [...],
        our_offer: [...],
        unresolved_objections: [...],
        price_signal: -1 | 0 | +1,
        scope_delta: "wider" | "same" | "narrower"
    }
    """
    lower = text.lower()

    # Collect objection snippets (short phrases around the keyword hit)
    objections = []
    for kw in OBJECTION_KWS:
        pos = lower.find(kw)
        if pos != -1:
            start = max(0, pos - 30)
            end = min(len(text), pos + len(kw) + 50)
            snippet = text[start:end].strip().replace("\n", " ")
            objections.append(snippet[:80])

    # Price signal: -1 = pushback, +1 = warming, 0 = neutral
    # Warming check uses word-boundary search preceded by non-negation context
    # to avoid "not competitive" falsely matching the warming keyword "competitive".
    NEGATION_PAT = re.compile(r"\b(not|no|n't|never|non|pas|sans)\s+", re.IGNORECASE)

    def _warming_match(text_lower: str) -> bool:
        for kw in PRICE_WARMING_KWS:
            pos = text_lower.find(kw)
            if pos == -1:
                continue
            # Check the 15 chars preceding the keyword for a negation word
            prefix = text_lower[max(0, pos - 15):pos]
            if NEGATION_PAT.search(prefix):
                continue  # skip: negated instance
            return True
        return False

    has_objection = any(kw in lower for kw in OBJECTION_KWS)
    has_warming = _warming_match(lower)
    if has_objection and not has_warming:
        price_signal = -1
    elif has_warming and not has_objection:
        price_signal = 1
    else:
        price_signal = 0

    # Scope delta
    has_wider = any(kw in lower for kw in SCOPE_WIDER_KWS)
    has_narrower = any(kw in lower for kw in SCOPE_NARROWER_KWS)
    if has_wider and not has_narrower:
        scope_delta = "wider"
    elif has_narrower and not has_wider:
        scope_delta = "narrower"
    else:
        scope_delta = "same"

    # client_ask / our_offer — heuristic sentence extraction
    # Look for lines containing price/scope signals
    client_ask = []
    our_offer = []
    for line in text.splitlines():
        ll = line.lower()
        if any(kw in ll for kw in OBJECTION_KWS[:5]):
            client_ask.append(line.strip()[:100])
        if "we propose" in ll or "our offer" in ll or "your firm" in ll:
            our_offer.append(line.strip()[:100])

    return {
        "client_ask": client_ask[:3],
        "our_offer": our_offer[:3],
        "unresolved_objections": list(dict.fromkeys(objections))[:5],
        "price_signal": price_signal,
        "scope_delta": scope_delta,
    }


# ── Spread extraction — LLM mode ─────────────────────────────────────────────

LLM_PROMPT_TEMPLATE = """\
You are a deal-microstructure analyst.  Read the meeting notes below and return a JSON object with EXACTLY these fields (no extra keys, no markdown):

{{
  "client_ask": ["<concise ask 1>", ...],
  "our_offer": ["<concise offer element 1>", ...],
  "unresolved_objections": ["<objection 1>", ...],
  "price_signal": -1,
  "scope_delta": "wider"
}}

Rules:
- client_ask: up to 3 strings, each under 80 chars, describing what the client demands or challenges.
- our_offer: up to 3 strings, what your firm has offered or proposed.
- unresolved_objections: list of strings, objections raised that were NOT resolved in this meeting.  Empty list [] if none.
- price_signal: integer -1 (client pushback on price / too expensive), 0 (neutral / no price discussion), or +1 (client warming / price acceptable).
- scope_delta: exactly one of "wider" (client wants more than proposed), "same", or "narrower" (client accepts less).

Output ONLY valid JSON.  No explanation.  No markdown code fences.

--- MEETING NOTES ---
{text}
"""


def llm_extract(text: str, model: str = "claude-haiku-4-5") -> dict:
    """Call claude -p to extract spread object.  Falls back to heuristic on failure."""
    cb = claude_bin()
    if not cb:
        print("  [deal-tape] claude CLI not found — falling back to heuristic", file=sys.stderr)
        return heuristic_extract(text)

    prompt = LLM_PROMPT_TEMPLATE.format(text=text[:6000])

    try:
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
    except subprocess.TimeoutExpired:
        print("  [deal-tape] claude -p timed out — falling back to heuristic", file=sys.stderr)
        return heuristic_extract(text)

    if result.returncode != 0:
        print(
            f"  [deal-tape] claude -p failed (rc={result.returncode}) — "
            f"falling back to heuristic",
            file=sys.stderr,
        )
        return heuristic_extract(text)

    raw = result.stdout.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find JSON object inside the response
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                print("  [deal-tape] LLM returned unparseable JSON — falling back to heuristic",
                      file=sys.stderr)
                return heuristic_extract(text)
        else:
            print("  [deal-tape] LLM returned no JSON object — falling back to heuristic",
                  file=sys.stderr)
            return heuristic_extract(text)

    # Validate and coerce
    def _coerce(obj: dict) -> dict:
        return {
            "client_ask": list(obj.get("client_ask") or [])[:3],
            "our_offer": list(obj.get("our_offer") or [])[:3],
            "unresolved_objections": list(obj.get("unresolved_objections") or [])[:5],
            "price_signal": int(obj.get("price_signal", 0))
                if obj.get("price_signal") in (-1, 0, 1) else 0,
            "scope_delta": str(obj.get("scope_delta", "same"))
                if obj.get("scope_delta") in ("wider", "same", "narrower") else "same",
        }

    return _coerce(parsed)


# ── Spread score ──────────────────────────────────────────────────────────────

def compute_spread_score(spread: dict) -> tuple[float, dict]:
    """
    Compute numeric spread score and a components breakdown dict.

    Returns (score, components) where:
      components = {
          "objections": float,
          "price_pushback": float,
          "scope_wider": float,
      }
    """
    n_obj = len(spread.get("unresolved_objections") or [])
    obj_component = n_obj * WEIGHT_OBJECTION
    price_component = WEIGHT_PRICE if spread.get("price_signal", 0) == -1 else 0.0
    scope_component = WEIGHT_SCOPE if spread.get("scope_delta") == "wider" else 0.0
    score = obj_component + price_component + scope_component
    return score, {
        "objections": round(obj_component, 3),
        "price_pushback": round(price_component, 3),
        "scope_wider": round(scope_component, 3),
    }


# ── One-line summary ──────────────────────────────────────────────────────────

def one_line(meeting_name: str, spread: dict, score: float) -> str:
    """Generate a concise one-line description of the meeting spread state."""
    parts = []
    if spread.get("price_signal") == -1:
        parts.append("price pushback")
    elif spread.get("price_signal") == 1:
        parts.append("price warming")
    n_obj = len(spread.get("unresolved_objections") or [])
    if n_obj:
        parts.append(f"{n_obj} unresolved objection{'s' if n_obj > 1 else ''}")
    if spread.get("scope_delta") == "wider":
        parts.append("scope widening")
    elif spread.get("scope_delta") == "narrower":
        parts.append("scope narrowing")
    if not parts:
        return f"{meeting_name}: spread neutral (score={score:.2f})"
    return f"{meeting_name}: {', '.join(parts)} (score={score:.2f})"


# ── Tape assembly ─────────────────────────────────────────────────────────────

def assemble_tape(
    bid_id: str,
    client: str,
    meeting_files: list[Path],
    no_llm: bool,
    verbose: bool = True,
) -> dict:
    """
    Process each meeting file, extract spreads, compute scores, and return
    the tape dict.
    """
    entries = []

    for p in meeting_files:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  [deal-tape] warning: cannot read {p.name}: {e}", file=sys.stderr)
            continue

        fm, body = parse_frontmatter(text)
        sections = extract_sections(body)
        extract_text = build_extract_text(sections)

        if not extract_text.strip():
            if verbose:
                print(f"  [deal-tape] skip (no extractable sections): {p.name}", file=sys.stderr)
            continue

        meeting_name = fm.get("meeting_name", p.stem)
        start_str = fm.get("start", "")
        date_str = start_str[:10] if start_str else "unknown"

        if verbose:
            mode_label = "heuristic" if no_llm else "llm"
            print(f"  [deal-tape] extracting [{mode_label}]: {p.name[:60]}", file=sys.stderr)

        if no_llm:
            spread = heuristic_extract(extract_text)
        else:
            spread = llm_extract(extract_text)

        score, components = compute_spread_score(spread)
        entry = {
            "ref": p.name,
            "date": date_str,
            "meeting_name": meeting_name,
            "spread": spread,
            "score": round(score, 3),
            "components": components,
            "one_line": one_line(meeting_name, spread, score),
        }
        entries.append(entry)

    # Compute trend over last N >= 3 entries
    scores = [e["score"] for e in entries]
    window = min(len(scores), max(3, len(scores)))
    tail_scores = scores[-window:] if len(scores) >= 3 else scores
    slope = least_squares_slope(tail_scores)
    trend = ls_str(slope)

    tape = {
        "bid_id": bid_id,
        "client": client,
        "generated": utcnow(),
        "meetings": entries,
        "trend": trend,
        "slope": round(slope, 4),
        "window": window,
        "scores": scores,
    }
    return tape


# ── Markdown report ───────────────────────────────────────────────────────────

def render_md(tape: dict) -> str:
    """Render the human-readable markdown tape."""
    lines = [
        f"# Deal Spread Tape — {tape['bid_id']}",
        "",
        f"> **Client:** {tape['client']}  ",
        f"> **Generated:** {tape['generated']}  ",
        f"> **Trend:** {tape['trend'].upper()}  (slope={tape['slope']:+.4f} over last {tape['window']} meetings)",
        "",
        "## Per-Meeting Spread Table",
        "",
        "| Date | Meeting | Score | Price | Scope | Objections |",
        "|------|---------|-------|-------|-------|------------|",
    ]

    for e in tape["meetings"]:
        spread = e["spread"]
        ps = {-1: "pushback", 0: "neutral", 1: "warming"}.get(spread.get("price_signal", 0), "?")
        sc = spread.get("scope_delta", "same")
        n_obj = len(spread.get("unresolved_objections") or [])
        name_short = e["meeting_name"][:55]
        lines.append(
            f"| {e['date']} | {name_short} | {e['score']:.2f} | {ps} | {sc} | {n_obj} |"
        )

    lines += ["", "## Spread Score Sparkline", ""]
    spark = sparkline(tape["scores"])
    lines.append(f"```\n{spark}\n```")
    lines += [
        "",
        f"Scores (low = healthy, high = stressed): {tape['scores']}",
        "",
        "## Trend Verdict",
        "",
    ]

    verdict_map = {
        "widening":     "WIDENING — spread is growing. Unresolved objections, price resistance, or scope creep are accumulating.",
        "compressing":  "COMPRESSING — spread is narrowing. Deal health is improving.",
        "flat":         "FLAT — spread is stable. No significant change in deal tension.",
    }
    lines.append(f"**{tape['trend'].upper()}**: {verdict_map.get(tape['trend'], '')}")
    lines += ["", "## Meeting Summaries"]

    for e in tape["meetings"]:
        lines += [
            "",
            f"### {e['date']} — {e['meeting_name'][:70]}",
            f"*{e['one_line']}*",
            "",
        ]
        spread = e["spread"]
        if spread.get("unresolved_objections"):
            lines.append("**Unresolved objections:**")
            for obj in spread["unresolved_objections"]:
                lines.append(f"- {obj}")
            lines.append("")
        if spread.get("client_ask"):
            lines.append("**Client asks:**")
            for ask in spread["client_ask"]:
                lines.append(f"- {ask}")
            lines.append("")

    return "\n".join(lines)


# ── Escalation ────────────────────────────────────────────────────────────────

def maybe_escalate(
    tape: dict,
    vault: Path,
    bid_id: str,
    client: str,
    stage: str,
    dry_run: bool,
    memory: dict,
) -> Optional[str]:
    """
    Emit an escalation file if trend=widening and stage in (Propose, Negotiate).
    Deduplicates via memory['escalations_emitted'].
    Returns the escalation filename if emitted, else None.
    """
    if tape["trend"] != "widening":
        return None
    if stage not in ("Propose", "Negotiate"):
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{today}-deal-tape-{bid_id}-widening.md"

    emitted = memory.get("escalations_emitted", [])
    if filename in emitted:
        print(f"  [deal-tape] escalation already emitted: {filename}", file=sys.stderr)
        return None

    # Build score table
    score_rows = []
    for e in tape["meetings"]:
        score_rows.append(f"  {e['date']}  score={e['score']:.2f}  {e['one_line'][:60]}")
    score_table = "\n".join(score_rows) if score_rows else "(no meetings)"

    window = tape.get("window", len(tape["scores"]))
    content = ESCALATION_TEMPLATE.format(
        run_id=run_id(),
        generated=tape["generated"],
        filename=filename,
        bid_id=bid_id,
        client=client,
        stage=stage,
        slope=tape["slope"],
        window=window,
        n_meetings=len(tape["meetings"]),
        score_table=score_table,
    )

    esc_dir = vault / "Important" / "escalations"
    esc_path = esc_dir / filename

    if dry_run:
        print(f"  [deal-tape] DRY-RUN would write escalation: {esc_path}", file=sys.stderr)
    else:
        esc_dir.mkdir(parents=True, exist_ok=True)
        esc_path.write_text(content, encoding="utf-8")
        print(f"  [deal-tape] escalation written: {esc_path}", file=sys.stderr)
        emitted.append(filename)
        memory["escalations_emitted"] = emitted

    return filename


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def load_memory(agent_state_dir: Path) -> dict:
    mem_path = agent_state_dir / "memory.json"
    if mem_path.exists():
        try:
            return json.loads(mem_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_run": None, "meetings_seen": [], "escalations_emitted": [], "errors": []}


def save_memory(agent_state_dir: Path, memory: dict) -> None:
    agent_state_dir.mkdir(parents=True, exist_ok=True)
    mem_path = agent_state_dir / "memory.json"
    mem_path.write_text(json.dumps(memory, indent=2), encoding="utf-8")


# ── Bid stage lookup ──────────────────────────────────────────────────────────

def get_bid_stage(vault: Path, bid_id: str) -> str:
    """Read stage from _brain_api/bid/<bid_id>/status.json, default 'Unknown'."""
    status_path = vault / "_brain_api" / "bid" / bid_id / "status.json"
    if status_path.exists():
        try:
            obj = json.loads(status_path.read_text(encoding="utf-8"))
            return obj.get("stage", "Unknown")
        except Exception:
            pass
    return "Unknown"


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deal microstructure tape — per-meeting spread analysis for a bid.",
    )
    parser.add_argument("--bid", required=True,
                        help="Bid ID (used for output paths, e.g. globex-cloud-migration)")
    parser.add_argument("--client", default="",
                        help="Client name substring to match meeting files")
    parser.add_argument("--match", default="",
                        help="Comma-separated keyword list for additional meeting matching")
    parser.add_argument("--root", default="",
                        help="Override vault root (for testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print outputs, do not write any files")
    parser.add_argument("--no-llm", action="store_true",
                        help="Use keyword heuristics only (no LLM call)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N meeting files")
    args = parser.parse_args(argv)

    # Resolve vault
    no_llm = args.no_llm or os.environ.get("NO_LLM", "").lower() in ("1", "true", "yes")

    if args.root:
        vault = Path(args.root).resolve()
    else:
        vault = Path(os.environ.get(
            "VAULT",
            os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")),
        )).resolve()

    print(f"[deal-tape] vault={vault}", file=sys.stderr)
    print(f"[deal-tape] bid={args.bid}  client={args.client!r}  no-llm={no_llm}", file=sys.stderr)

    # Load memory
    agent_state_dir = vault / "_agent_state" / "deal-tape"
    memory = load_memory(agent_state_dir)

    # Keywords
    keywords = [k.strip() for k in args.match.split(",") if k.strip()] if args.match else []

    # Discover meetings
    meeting_files = discover_meetings(vault, args.client, keywords, args.limit)
    if not meeting_files:
        print(f"[deal-tape] WARNING: no matching meetings found for client={args.client!r} "
              f"keywords={keywords}", file=sys.stderr)
        memory["last_run"] = utcnow()
        memory["errors"].append(
            {"ts": utcnow(), "error": f"no meetings found for {args.client}"}
        )
        if not args.dry_run:
            save_memory(agent_state_dir, memory)
        return 1

    print(f"[deal-tape] found {len(meeting_files)} matching meeting(s)", file=sys.stderr)

    # Assemble tape
    tape = assemble_tape(
        bid_id=args.bid,
        client=args.client,
        meeting_files=meeting_files,
        no_llm=no_llm,
    )

    print(
        f"[deal-tape] trend={tape['trend']}  slope={tape['slope']:+.4f}  "
        f"meetings={len(tape['meetings'])}",
        file=sys.stderr,
    )

    # Render markdown
    md_content = render_md(tape)

    # Determine output paths
    brain_api_dir = vault / "_brain_api" / "bid" / args.bid
    json_out = brain_api_dir / "spread_tape.json"
    md_out = brain_api_dir / "spread_tape.md"

    if args.dry_run:
        print("\n--- spread_tape.json (dry-run) ---", file=sys.stderr)
        print(json.dumps(tape, indent=2), file=sys.stderr)
        print("\n--- spread_tape.md (dry-run) ---", file=sys.stderr)
        print(md_content, file=sys.stderr)
    else:
        brain_api_dir.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(tape, indent=2), encoding="utf-8")
        md_out.write_text(md_content, encoding="utf-8")
        print(f"[deal-tape] wrote {json_out}", file=sys.stderr)
        print(f"[deal-tape] wrote {md_out}", file=sys.stderr)

    # Escalation check
    stage = get_bid_stage(vault, args.bid)
    print(f"[deal-tape] bid stage={stage}", file=sys.stderr)

    esc_file = maybe_escalate(
        tape=tape,
        vault=vault,
        bid_id=args.bid,
        client=args.client,
        stage=stage,
        dry_run=args.dry_run,
        memory=memory,
    )

    # Update memory
    memory["last_run"] = utcnow()
    memory["meetings_seen"] = [p.name for p in meeting_files]
    if not args.dry_run:
        save_memory(agent_state_dir, memory)

    # Print summary to stdout
    print(f"\n[deal-tape] SUMMARY")
    print(f"  bid_id  : {args.bid}")
    print(f"  client  : {args.client}")
    print(f"  meetings: {len(tape['meetings'])}")
    print(f"  scores  : {tape['scores']}")
    print(f"  trend   : {tape['trend']}  (slope={tape['slope']:+.4f})")
    if esc_file:
        print(f"  escalation emitted: {esc_file}")

    return 0


# ── Self-tests ────────────────────────────────────────────────────────────────

def _make_fixture_meeting(
    root: Path,
    name: str,
    start: str,
    meeting_name: str,
    summary: str,
    decisions: str,
    action_items: str,
) -> None:
    """Write a synthetic fixture meeting file."""
    content = f"""\
---
type: meeting-transcript
sensitivity: confidential
source: fixture
plaud_file_id: fixture-{name}
meeting_name: "{meeting_name}"
start: "{start}"
duration_min: 30
pulled: "2026-01-01"
tags: [meeting, fixture]
---

# {meeting_name}

## Summary

{summary}

## Decisions

{decisions}

## Action items

{action_items}
"""
    out_dir = root / "Meetings" / "Confidential"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.md").write_text(content, encoding="utf-8")


def _make_status_json(root: Path, bid_id: str, stage: str) -> None:
    d = root / "_brain_api" / "bid" / bid_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(
        json.dumps({"bid_id": bid_id, "stage": stage, "client": "Synthetic Corp"}),
        encoding="utf-8",
    )


def run_self_tests() -> None:
    """
    Self-contained offline tests.  Uses $TMPDIR fixture root; never touches the
    live vault.  Exits non-zero on any assertion failure.
    """
    import tempfile

    tmpdir = Path(os.environ.get("TMPDIR", "/tmp")) / f"deal-tape-test-{uuid.uuid4().hex[:8]}"
    print(f"[self-test] fixture root: {tmpdir}", file=sys.stderr)

    # ── Fixture A: objections resolve → trend=compressing ────────────────────
    fix_a = tmpdir / "fixture-a"
    fix_a.mkdir(parents=True)
    _make_status_json(fix_a, "bid-a", "Propose")

    _make_fixture_meeting(
        fix_a, "meet-a1", "2026-01-01T10:00:00Z",
        "synthetic-corp meeting 1",
        summary=(
            "Client raised concerns: price is too high, not competitive, expensive. "
            "Scope widening requested: include more countries, expand scope."
        ),
        decisions="No resolution reached.",
        action_items="- Recalibrate pricing\n- Add countries to scope",
    )
    _make_fixture_meeting(
        fix_a, "meet-a2", "2026-01-08T10:00:00Z",
        "synthetic-corp meeting 2",
        summary=(
            "Client still thinks price is too high. "
            "The firm proposes discount. Scope widening: add scope for Mexico."
        ),
        decisions="Discount under consideration.",
        action_items="- Prepare revised commercial proposal",
    )
    _make_fixture_meeting(
        fix_a, "meet-a3", "2026-01-15T10:00:00Z",
        "synthetic-corp meeting 3",
        summary=(
            "Agreement reached on pricing. Client accepted our firm's proposal. "
            "Price aligned, scope confirmed as final. No remaining objections."
        ),
        decisions="Price and scope locked. Contract draft to follow.",
        action_items="- Draft contract\n- Confirm start date",
    )

    tape_a = assemble_tape("bid-a", "synthetic-corp", [
        fix_a / "Meetings" / "Confidential" / f"{n}.md"
        for n in ("meet-a1", "meet-a2", "meet-a3")
    ], no_llm=True)

    print(f"  [fixture-A] scores={tape_a['scores']}  trend={tape_a['trend']}  slope={tape_a['slope']:+.4f}")
    assert len(tape_a["meetings"]) == 3, f"Expected 3 meetings, got {len(tape_a['meetings'])}"
    assert tape_a["trend"] == "compressing", (
        f"Expected trend=compressing (objections resolved), got {tape_a['trend']}  "
        f"slope={tape_a['slope']:+.4f}  scores={tape_a['scores']}"
    )
    print("  [fixture-A] PASS: trend=compressing")

    # No escalation for fixture-A (compressing)
    mem_a: dict = {"escalations_emitted": []}
    esc_a = maybe_escalate(tape_a, fix_a, "bid-a", "synthetic-corp", "Propose",
                           dry_run=True, memory=mem_a)
    assert esc_a is None, f"Expected no escalation for compressing trend, got {esc_a}"
    print("  [fixture-A] PASS: no escalation for compressing")

    # ── Fixture B: objections accumulate → trend=widening + escalation ───────
    fix_b = tmpdir / "fixture-b"
    fix_b.mkdir(parents=True)
    _make_status_json(fix_b, "bid-b", "Negotiate")

    _make_fixture_meeting(
        fix_b, "meet-b1", "2026-02-01T10:00:00Z",
        "synthetic-corp negotiation 1",
        summary="Initial discussion. Client is open. Price seems acceptable.",
        decisions="Proceed to detailed scoping.",
        action_items="- Prepare detailed scope",
    )
    _make_fixture_meeting(
        fix_b, "meet-b2", "2026-02-08T10:00:00Z",
        "synthetic-corp negotiation 2",
        summary=(
            "Client raised budget concerns. Price is too high. "
            "Need to reduce cost. Discount requested."
        ),
        decisions="No agreement on pricing.",
        action_items="- Revisit commercial model",
    )
    _make_fixture_meeting(
        fix_b, "meet-b3", "2026-02-15T10:00:00Z",
        "synthetic-corp negotiation 3",
        summary=(
            "Client says price is very expensive, above market. "
            "Also requesting expand scope, add scope for additional countries. "
            "Multiple unresolved objections: pricing, scope, SLA not competitive."
        ),
        decisions="No decisions — meeting ended without alignment.",
        action_items="- Escalate internally\n- Recalibrate pricing model",
    )

    tape_b = assemble_tape("bid-b", "synthetic-corp", [
        fix_b / "Meetings" / "Confidential" / f"{n}.md"
        for n in ("meet-b1", "meet-b2", "meet-b3")
    ], no_llm=True)

    print(f"  [fixture-B] scores={tape_b['scores']}  trend={tape_b['trend']}  slope={tape_b['slope']:+.4f}")
    assert len(tape_b["meetings"]) == 3, f"Expected 3 meetings, got {len(tape_b['meetings'])}"
    assert tape_b["trend"] == "widening", (
        f"Expected trend=widening (objections accumulate), got {tape_b['trend']}  "
        f"slope={tape_b['slope']:+.4f}  scores={tape_b['scores']}"
    )
    print("  [fixture-B] PASS: trend=widening")

    # Escalation for fixture-B (widening + Negotiate stage)
    mem_b: dict = {"escalations_emitted": []}
    esc_b = maybe_escalate(tape_b, fix_b, "bid-b", "synthetic-corp", "Negotiate",
                           dry_run=True, memory=mem_b)
    assert esc_b is not None, "Expected escalation for widening+Negotiate"
    print(f"  [fixture-B] PASS: escalation produced: {esc_b}")

    # Dedup: second call should not re-emit
    mem_b2: dict = {"escalations_emitted": [esc_b]}
    esc_b2 = maybe_escalate(tape_b, fix_b, "bid-b", "synthetic-corp", "Negotiate",
                            dry_run=True, memory=mem_b2)
    assert esc_b2 is None, f"Expected no re-escalation (dedup), got {esc_b2}"
    print("  [fixture-B] PASS: escalation dedup works")

    # ── Validate slope math ───────────────────────────────────────────────────
    assert abs(least_squares_slope([1.0, 1.0, 1.0]) - 0.0) < 1e-9
    assert least_squares_slope([0.0, 0.5, 1.0]) > 0.0
    assert least_squares_slope([1.0, 0.5, 0.0]) < 0.0
    print("  [slope math] PASS")

    # ── Validate sparkline ────────────────────────────────────────────────────
    sp = sparkline([0.0, 0.5, 1.0])
    assert len(sp) == 3
    print(f"  [sparkline] PASS: {sp!r}")

    # ── Validate frontmatter / section parser ─────────────────────────────────
    sample = """\
---
meeting_name: "Test meeting"
start: "2026-03-01T09:00:00Z"
duration_min: 45
---

# Test meeting

## Summary

This is a summary.

## Decisions

Decision 1.

## Action items

- Action A
"""
    fm, body = parse_frontmatter(sample)
    assert fm.get("meeting_name") == "Test meeting", f"FM parse failed: {fm}"
    secs = extract_sections(body)
    assert "Summary" in secs and secs["Summary"] == "This is a summary."
    assert "Decisions" in secs
    print("  [frontmatter/sections] PASS")

    print("\n[self-test] ALL TESTS PASSED")

    # Cleanup
    import shutil as _shutil
    _shutil.rmtree(tmpdir, ignore_errors=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--self-test":
        run_self_tests()
        sys.exit(0)
    sys.exit(main())
