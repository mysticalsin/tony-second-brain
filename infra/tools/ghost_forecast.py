#!/usr/bin/env python3
"""ghost_forecast.py — the ghost vault, smallest scoreable loop.

Nightly (via brain-refresh.sh step 9c, once per day):
    1. --score : grade earlier ghost claims whose date has passed against the real
                 daily note + git log, append Brier scores to
                 _agent_state/ghost-forecaster/scores.jsonl
    2. (default): emit 5-10 schema-enforced FALSIFIABLE claims about tomorrow to
                 _brain_api/ghost/daily/<tomorrow>.json

Every claim must carry a binary resolution criterion grounded in an observable
vault artifact (daily-note mention, file existence, git change, dashboard number).
"Client may hesitate" style slop is rejected by schema validation — an
unfalsifiable forecast is theater, not a forecast.

Usage (from vault root):
    python3 build/tools/ghost_forecast.py            # predict tomorrow
    python3 build/tools/ghost_forecast.py --score    # score resolved claims
    python3 build/tools/ghost_forecast.py --dry-run  # print, don't write

Exit code is always 0 — never blocks brain-refresh.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))
MODEL = "claude-haiku-4-5"
MIN_CLAIMS, MAX_CLAIMS = 5, 10
CATEGORIES = {"bid_event", "client_touch", "agent_activity", "spend", "inbox", "journal"}


def vault_root() -> Path:
    v = Path(os.environ.get("CLAUDE_VAULT", VAULT_DEFAULT))
    return v if v.exists() else Path.cwd()


def claude_bin() -> str | None:
    for c in (str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", shutil.which("claude")):
        if c and Path(c).exists():
            return c
    return None


def ask_claude(prompt: str, vault: Path, timeout: int = 180) -> str:
    cb = claude_bin()
    if not cb:
        raise RuntimeError("claude CLI not found")
    result = subprocess.run(
        [cb, "-p", prompt, "--model", MODEL,
         "--setting-sources", "",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        cwd=str(vault), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed rc={result.returncode}: {result.stderr[:300]}")
    return result.stdout


def extract_json(text: str):
    """Pull the first JSON array/object out of model output (tolerates fences/prose)."""
    m = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"([\[{].*[\]}])", text, re.DOTALL)
        raw = m.group(1) if m else text
    return json.loads(raw)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def git_log_for(vault: Path, since: str, until: str) -> str:
    try:
        out = subprocess.run(
            ["git", "log", f"--since={since} 00:00", f"--until={until} 23:59",
             "--name-only", "--pretty=format:%h %s"],
            cwd=str(vault), capture_output=True, text=True, timeout=30,
        )
        return out.stdout[:4000]
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ──────────────────────────── forecast ────────────────────────────


def validate_claim(c: dict, target_date: str) -> dict | None:
    """Schema gate — drop anything unfalsifiable. Returns normalized claim or None."""
    if not isinstance(c, dict):
        return None
    event = str(c.get("event", "")).strip()
    crit = str(c.get("resolution_criterion", "")).strip()
    try:
        prob = float(c.get("probability"))
    except (TypeError, ValueError):
        return None
    cat = str(c.get("category", "")).strip()
    if not event or len(crit) < 20 or not (0.0 < prob < 1.0) or cat not in CATEGORIES:
        return None
    # The criterion must point at something checkable, not a vibe.
    checkable = ("daily note", "02_Areas/Daily", "git", "file", "00_Inbox", "_brain_api",
                 "dashboard", "commit", "exists", "frontmatter", "deadline", "stage")
    if not any(k in crit.lower() for k in checkable):
        return None
    return {"id": c.get("id") or f"g-{target_date}-{abs(hash(event)) % 10_000}",
            "event": event[:200], "date": target_date, "probability": round(prob, 2),
            "resolution_criterion": crit[:300], "category": cat}


def gather_context(vault: Path, today: str) -> str:
    bids = load_json(vault / "_brain_api" / "bid" / "_open.json", {}).get("bids") or []
    daily = ""
    p = vault / "02_Areas" / "Daily" / f"{today}.md"
    if p.exists():
        daily = p.read_text()[:3000]
    inbox = [f for f in (vault / "00_Inbox" / "from-dust").rglob("*.md")
             if f.name != "README.md"]
    prior_scores = (vault / "_agent_state" / "ghost-forecaster" / "scores.jsonl")
    recent_scores = ""
    if prior_scores.exists():
        recent_scores = "\n".join(prior_scores.read_text().splitlines()[-10:])
    return (f"OPEN BIDS:\n{json.dumps(bids, indent=1)}\n\n"
            f"TODAY'S DAILY NOTE ({today}):\n{daily}\n\n"
            f"INBOX: {len(inbox)} agent writes pending triage\n\n"
            f"GIT LOG (last 24h):\n{git_log_for(vault, today, today)}\n\n"
            f"YOUR RECENT FORECAST SCORES (learn from misses):\n{recent_scores}")


def run_forecast(vault: Path, dry_run: bool) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    out_path = vault / "_brain_api" / "ghost" / "daily" / f"{tomorrow}.json"
    if out_path.exists():
        print(f"ghost: forecast for {tomorrow} already exists — skipping")
        return 0

    prompt = f"""You are the ghost forecaster for Tony's bid-management vault. Predict TOMORROW ({tomorrow}).

{gather_context(vault, today)}

Emit {MIN_CLAIMS}-{MAX_CLAIMS} FALSIFIABLE claims about tomorrow as a JSON array. Each claim:
{{"id": "<slug>", "event": "<concrete event>", "probability": <0.05-0.95>,
  "resolution_criterion": "<HOW to check it in the vault: which daily note section, file, git change, or dashboard number proves it true/false>",
  "category": "bid_event|client_touch|agent_activity|spend|inbox|journal"}}

Rules:
- Every claim must be checkable against a vault artifact tomorrow night. No vibes ("client may hesitate" = invalid).
- Spread probabilities — an honest forecaster is rarely at 0.5 on everything.
- Include at least one claim about each open bid and one about spend or inbox.
- Output ONLY the JSON array."""

    try:
        raw = ask_claude(prompt, vault, timeout=220)
        claims = [v for c in extract_json(raw) if (v := validate_claim(c, tomorrow))]
    except Exception as e:  # noqa: BLE001
        print(f"ghost: forecast failed soft — {e}", file=sys.stderr)
        return 0
    if len(claims) < MIN_CLAIMS:
        print(f"ghost: only {len(claims)} valid claims after schema gate — not writing "
              f"(model produced unfalsifiable slop; will retry next refresh)", file=sys.stderr)
        return 0

    doc = {"generated": datetime.now(timezone.utc).isoformat(), "target_date": tomorrow,
           "model": MODEL, "claims": claims[:MAX_CLAIMS]}
    if dry_run:
        print(json.dumps(doc, indent=2))
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2))
    print(f"ghost: {len(doc['claims'])} claims for {tomorrow} → {out_path}")
    return 0


# ──────────────────────────── score ────────────────────────────


def run_score(vault: Path, dry_run: bool) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    ghost_dir = vault / "_brain_api" / "ghost" / "daily"
    scores_path = vault / "_agent_state" / "ghost-forecaster" / "scores.jsonl"
    if not ghost_dir.exists():
        return 0

    scored_dates = set()
    if scores_path.exists():
        for line in scores_path.read_text().splitlines():
            try:
                scored_dates.add(json.loads(line).get("date"))
            except (json.JSONDecodeError, AttributeError):
                continue

    pending = [p for p in sorted(ghost_dir.glob("*.json"))
               if p.stem <= today and p.stem not in scored_dates]
    if not pending:
        print("ghost: nothing to score")
        return 0

    for ghost_file in pending[-3:]:  # at most 3 backlog days per run
        doc = load_json(ghost_file, {})
        claims = doc.get("claims") or []
        if not claims:
            continue
        date = doc.get("target_date") or ghost_file.stem
        daily_note = ""
        p = vault / "02_Areas" / "Daily" / f"{date}.md"
        if p.exists():
            daily_note = p.read_text()[:5000]

        prompt = f"""Grade these forecasts about {date} against the evidence. For each claim, decide if it HAPPENED.

CLAIMS:
{json.dumps(claims, indent=1)}

EVIDENCE — real daily note for {date}:
{daily_note or "(no daily note written)"}

EVIDENCE — git log for {date}:
{git_log_for(vault, date, date)}

Output ONLY a JSON array: [{{"id": "<claim id>", "outcome": 1 or 0 or null, "evidence": "<one line>"}}]
outcome=1 happened, 0 did not happen, null only if genuinely unresolvable from the evidence."""

        try:
            verdicts = {v.get("id"): v for v in extract_json(ask_claude(prompt, vault, timeout=180))
                        if isinstance(v, dict)}
        except Exception as e:  # noqa: BLE001
            print(f"ghost: scoring {date} failed soft — {e}", file=sys.stderr)
            continue

        rows, briers, unresolved = [], [], 0
        for c in claims:
            v = verdicts.get(c["id"]) or {}
            outcome = v.get("outcome")
            if outcome in (0, 1):
                brier = round((c["probability"] - outcome) ** 2, 4)
                briers.append(brier)
            else:
                brier, unresolved = None, unresolved + 1
            rows.append({"date": date, "id": c["id"], "event": c["event"],
                         "probability": c["probability"], "outcome": outcome,
                         "brier": brier, "evidence": str(v.get("evidence", ""))[:200],
                         "scored": datetime.now(timezone.utc).isoformat()})

        mean_brier = round(sum(briers) / len(briers), 4) if briers else None
        summary = {"date": date, "type": "day_summary", "n_claims": len(claims),
                   "n_resolved": len(briers), "n_unresolved": unresolved,
                   "mean_brier": mean_brier,
                   "scored": datetime.now(timezone.utc).isoformat()}
        if dry_run:
            print(json.dumps([*rows, summary], indent=2))
            continue
        scores_path.parent.mkdir(parents=True, exist_ok=True)
        with scores_path.open("a") as f:
            for r in [*rows, summary]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"ghost: scored {date} — mean Brier {mean_brier} "
              f"({len(briers)} resolved, {unresolved} unresolvable; lower is better, 0.25 = coin-flip)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true", help="grade past claims instead of forecasting")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()
    vault = Path(args.vault) if args.vault else vault_root()
    try:
        return run_score(vault, args.dry_run) if args.score else run_forecast(vault, args.dry_run)
    except Exception as e:  # noqa: BLE001
        print(f"ghost: hard fail (soft exit) — {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
