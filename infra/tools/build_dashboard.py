#!/usr/bin/env python3
"""build_dashboard.py — render the AI Command Center dashboard from captured data.

Reads (all under VAULT):
    _agent_state/claude-code/sessions.jsonl    per-session records from capture_session.py
    _agent_state/claude-code/memory.json       recent_learnings + global_patterns
    _agent_state/claude-code/stats.json        rolling totals (by_day, by_model, all_time)
    _brain_api/bid/_open.json                  open bids
    02_Areas/Daily/<today>.md                  today's plan
    02_Areas/Daily/<tomorrow>.md               tomorrow's plan (if exists)

Writes:
    _brain_api/claude_usage/aggregate.json     machine-readable KPI rollups (7d / 30d / all-time)
    _brain_api/claude_usage/optimization_candidates.json  patterns repeated ≥ THRESHOLD
    02_Areas/Dashboard.md                      live dashboard (Dataview + Charts + static tables)
    02_Areas/Daily_Brief.md                    daily snapshot — only when --daily is passed

Invocation:
    python3 build/tools/build_dashboard.py            # update Dashboard.md + aggregate JSON
    python3 build/tools/build_dashboard.py --daily    # also regenerate Daily_Brief.md

Exit code is always 0 — the dashboard should never block the refresh job.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))
LOG_DIR = Path.home() / "AI-Brain-build" / "logs"
OPTIMIZATION_OBS_THRESHOLD = 3  # patterns observed ≥ this many times surface as candidates


def log(msg: str, *, level: str = "INFO") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] [{level}] dashboard: {msg}\n"
    with (LOG_DIR / f"dashboard-{datetime.now().strftime('%Y-%m-%d')}.log").open("a") as f:
        f.write(line)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), delete=False,
                                     prefix=".tmp-", suffix=path.suffix) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


# ──────────────────────────── loaders ────────────────────────────


def load_sessions(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    # Fail SOFT on OSError (PermissionError) too — under launchd, OneDrive CloudStorage can
    # intermittently deny the read, and an uncaught OSError here aborts main() BEFORE the
    # dashboard is ever written (the real "static dashboard" cause). Mirror load_json().
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def read_daily_plan(daily_path: Path, max_lines: int = 50) -> str:
    """Return the bullet list under '## Plan' or '## TODO' or top of file."""
    if not daily_path.exists():
        return ""
    text = daily_path.read_text()
    # Strip frontmatter
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5:]
    # Look for a planning section
    lines = text.split("\n")
    plan_keys = ("## Focus today", "## Plan", "## TODO", "## Tasks", "## Today", "## Notes")
    capture = False
    out: list[str] = []
    for ln in lines:
        if any(ln.strip().startswith(k) for k in plan_keys):
            capture = True
            out.append(ln)
            continue
        if capture and ln.startswith("## ") and ln.strip() not in plan_keys:
            break
        if capture:
            out.append(ln)
            if len(out) >= max_lines:
                break
    if not out:
        # Fallback: first N non-empty lines after frontmatter
        out = [ln for ln in lines if ln.strip()][:15]
    return "\n".join(out).rstrip()


# ──────────────────────────── compute ────────────────────────────


def session_date(s: dict) -> str:
    """Return YYYY-MM-DD from session timestamp."""
    ts = s.get("ts") or ""
    return ts[:10]


def parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def window_filter(sessions: list[dict], days: int) -> list[dict]:
    """Return sessions within the last `days` days (None = all-time)."""
    if days is None:
        return sessions
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for s in sessions:
        ts = parse_iso(s.get("ts") or "")
        if ts and ts >= cutoff:
            out.append(s)
    return out


def kpi_block(sessions: list[dict]) -> dict:
    """Aggregate KPIs for a session list."""
    n = len(sessions)
    cost_total = round(sum(float(s.get("cost_usd", 0)) for s in sessions), 4)
    cap_cost = round(sum(float((s.get("capture") or {}).get("cost_usd", 0)) for s in sessions), 5)
    tokens_in = sum(int((s.get("usage") or {}).get("input_tokens", 0)) for s in sessions)
    tokens_out = sum(int((s.get("usage") or {}).get("output_tokens", 0)) for s in sessions)
    cache_read = sum(int((s.get("usage") or {}).get("cache_read_input_tokens", 0)) for s in sessions)
    duration_total = sum(int(s.get("duration_s", 0)) for s in sessions)
    avg_cost = round(cost_total / n, 4) if n else 0.0
    avg_duration_min = round((duration_total / n) / 60, 1) if n else 0.0
    avg_turns = round(sum(int(s.get("n_turns_user", 0)) for s in sessions) / n, 1) if n else 0.0

    tools = Counter()
    topics = Counter()
    cwds = Counter()
    models = Counter()
    for s in sessions:
        for tname, ct in (s.get("tool_uses") or {}).items():
            tools[tname] += int(ct)
        for tag in (s.get("topics") or []):
            topics[tag] += 1
        cwd = s.get("cwd") or ""
        if cwd:
            # Last 2 segments as project label
            parts = cwd.rstrip("/").split("/")
            cwds["/".join(parts[-2:])] += 1
        if s.get("model"):
            models[s["model"]] += 1

    return {
        "sessions": n,
        "cost_usd": cost_total,
        "capture_overhead_usd": cap_cost,
        "avg_cost_usd": avg_cost,
        "avg_duration_min": avg_duration_min,
        "avg_turns": avg_turns,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "tokens_cache_read": cache_read,
        "top_tools": tools.most_common(10),
        "top_topics": topics.most_common(10),
        "top_cwds": cwds.most_common(10),
        "top_models": models.most_common(5),
    }


def daily_cost_series(sessions: list[dict], days: int = 30) -> list[dict]:
    """Per-day cost + session count for the last `days` days (date-ascending)."""
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    bucket: dict[str, dict] = {}
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        bucket[d] = {"date": d, "sessions": 0, "cost_usd": 0.0}
    for s in sessions:
        d = session_date(s)
        if d in bucket:
            bucket[d]["sessions"] += 1
            bucket[d]["cost_usd"] += float(s.get("cost_usd", 0))
    rows = [{"date": d, "sessions": v["sessions"], "cost_usd": round(v["cost_usd"], 4)} for d, v in bucket.items()]
    return rows


def recurring_first_prompts(sessions: list[dict], min_repeats: int = 2) -> list[dict]:
    """Cluster session summaries/topics to find repeated questions worth turning into skills.

    Strategy: take each session's `summary` (Haiku-extracted), normalise, and shingle
    on word-trigrams. Group by Jaccard ≥ 0.5. Score = group size × avg cost.
    """
    items = []
    for s in sessions:
        text = (s.get("summary") or "").strip()
        if not text:
            continue
        # Crude shingle
        tokens = [t.lower() for t in text.split() if len(t) > 3]
        shingles = set()
        for i in range(len(tokens) - 2):
            shingles.add(" ".join(tokens[i:i+3]))
        if shingles:
            items.append({"text": text, "shingles": shingles, "cost": float(s.get("cost_usd", 0))})

    groups: list[list[dict]] = []
    for it in items:
        placed = False
        for g in groups:
            ref = g[0]["shingles"]
            inter = len(ref & it["shingles"])
            union = len(ref | it["shingles"])
            if union and inter / union >= 0.5:
                g.append(it)
                placed = True
                break
        if not placed:
            groups.append([it])

    out = []
    for g in groups:
        if len(g) < min_repeats:
            continue
        avg_cost = round(sum(x["cost"] for x in g) / len(g), 4)
        total_cost = round(sum(x["cost"] for x in g), 4)
        out.append({
            "n_observations": len(g),
            "representative": g[0]["text"][:200],
            "examples": [x["text"][:120] for x in g[:3]],
            "avg_cost_usd": avg_cost,
            "total_cost_usd": total_cost,
            "optimization_potential": "Convert to a skill or template — would save ~{} per future request.".format(fmt_money(avg_cost)),
        })
    return sorted(out, key=lambda x: -x["total_cost_usd"])[:20]


def optimization_from_memory(memory: dict) -> list[dict]:
    """Patterns from memory.json with n_observations ≥ THRESHOLD = optimization signal."""
    out = []
    for p in (memory.get("global_patterns") or []):
        n = int(p.get("n_observations", 0))
        if n >= OPTIMIZATION_OBS_THRESHOLD:
            out.append({
                "pattern": p.get("pattern", ""),
                "n_observations": n,
                "confidence": p.get("confidence", 0),
                "first_seen": p.get("first_seen", ""),
            })
    return sorted(out, key=lambda x: -x["n_observations"])[:20]


def aggregate_agent_fleet(vault: Path) -> dict:
    """Walk _agent_state/*/ and produce a unified view across the agent fleet.

    The vault IS the shared agent memory pool. Each Dust agent maintains its own
    memory.json + writes.jsonl + stats.json. The dashboard surfaces the union so
    Tony can see what every agent has learned without opening each one.
    """
    fleet = {"agents": {}, "all_learnings": [], "all_patterns": [], "active_today": 0}
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    agent_root = vault / "_agent_state"
    if not agent_root.exists():
        return fleet

    for agent_dir in sorted(agent_root.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith("_") or agent_dir.name == "claude-code":
            continue
        name = agent_dir.name
        mem = load_json(agent_dir / "memory.json", {})
        stats = load_json(agent_dir / "stats.json", {})

        # Recent writes tail
        writes_path = agent_dir / "writes.jsonl"
        recent_writes = []
        if writes_path.exists():
            try:
                with writes_path.open() as f:
                    for line in f:
                        if line.strip():
                            try:
                                recent_writes.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            except OSError:
                pass

        # Tally last-7d activity
        last_7d = {}
        for ev in recent_writes:
            d = ev.get("ts", "")[:10]
            if d >= week_ago:
                a = ev.get("action", "unknown")
                last_7d[a] = last_7d.get(a, 0) + 1

        calib = agent_calibration(agent_dir)
        fleet["agents"][name] = {
            "memory_learnings": len(mem.get("recent_learnings") or []),
            "memory_patterns": len(mem.get("global_patterns") or []),
            "accounts_known": len(mem.get("per_account_knowledge") or {}),
            "all_time": stats.get("all_time", {}),
            "last_7d": last_7d,
            "last_active": stats.get("last_active") or mem.get("last_updated"),
            "recent_writes": recent_writes[-5:],
            "calibration": calib,
        }
        # Feed calibration back into committed agent state so the agent-dreaming loop /
        # future runs can consume it (memory.json already anticipates a calibration block).
        if calib.get("n"):
            try:
                stats["calibration"] = calib
                atomic_write(agent_dir / "stats.json", json.dumps(stats, indent=2, ensure_ascii=False))
            except OSError:
                pass

        # Union of learnings and patterns, tagged with source agent
        # recent_learnings may be dicts {text,date} (new) or bare strings (old format)
        for l in (mem.get("recent_learnings") or [])[:20]:
            if isinstance(l, dict):
                fleet["all_learnings"].append({"agent": name, **l})
            else:
                fleet["all_learnings"].append({"agent": name, "text": str(l), "date": ""})
        for p in (mem.get("global_patterns") or [])[:20]:
            if isinstance(p, dict):
                fleet["all_patterns"].append({"agent": name, **p})
            else:
                fleet["all_patterns"].append({"agent": name, "pattern": str(p), "n_observations": 0})

        if stats.get("last_active", "")[:10] == today:
            fleet["active_today"] += 1

    fleet["all_learnings"].sort(key=lambda x: x.get("date", ""), reverse=True)
    fleet["all_patterns"].sort(key=lambda x: -int(x.get("n_observations", 0)))
    return fleet


# ── Confidence calibration ────────────────────────────────────────────────
# Joins the agent's stated confidence (writes.jsonl) with the human outcome
# (resolutions.jsonl, written by record_resolution.py via /dust-resolve). Answers:
# was the agent's confidence warranted? An over-confident agent (high confidence,
# low accept-rate) is the dangerous one to auto-promote.
CALIBRATION_MIN_SAMPLES = 5


def _outcome_score(human_action: str):
    """accept=1 (right), edit=0.5 (partial), reject=0 (wrong), defer=None (no outcome)."""
    return {"accept": 1.0, "edit": 0.5, "reject": 0.0}.get(human_action)


def agent_calibration(agent_dir: Path) -> dict:
    """Per-agent calibration from resolutions.jsonl. Returns {n, ...} — n=0 when no data."""
    res_path = agent_dir / "resolutions.jsonl"
    rows = []
    if res_path.exists():
        for line in res_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    scored = [(float(r["confidence"]), _outcome_score(r.get("human_action", "")))
              for r in rows
              if r.get("confidence") is not None and _outcome_score(r.get("human_action", "")) is not None]
    n = len(scored)
    if n == 0:
        return {"n": 0}
    mean_conf = round(sum(c for c, _ in scored) / n, 3)
    accept_rate = round(sum(s for _, s in scored) / n, 3)
    high = [(c, s) for c, s in scored if c >= 0.85]
    overconfident = len(high) >= 3 and (sum(s for _, s in high) / len(high)) < 0.8
    return {
        "n": n,
        "mean_confidence": mean_conf,
        "accept_rate": accept_rate,
        "gap": round(mean_conf - accept_rate, 3),
        "overconfident": overconfident,
        "high_conf_n": len(high),
        "insufficient": n < CALIBRATION_MIN_SAMPLES,
        "computed": datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────── render ────────────────────────────


def kpi_card_row(label: str, value, sub: str = "") -> str:
    """One row of a 4-card KPI grid (markdown-table-style)."""
    sub_str = f"<br/><sub>{sub}</sub>" if sub else ""
    return f"| **{label}** | {value}{sub_str} |"


def fmt_int(n) -> str:
    return f"{int(n):,}"


def fmt_calibration(calib) -> str:
    """Render an agent_calibration() dict for the fleet table."""
    if not calib or not calib.get("n"):
        return "—"
    if calib.get("insufficient"):
        return f"n={calib['n']} (need {CALIBRATION_MIN_SAMPLES})"
    s = f"{calib['mean_confidence']:.2f}→{calib['accept_rate']:.2f} (n={calib['n']})"
    return s + " ⚠" if calib.get("overconfident") else s


_USD_TO_CAD = None


def usd_to_cad() -> float:
    """USD→CAD display rate. Mirrors the command-center plugin's roi.usdToCad
    (default 1.37). Tunable via 'usd_to_cad' in 99_Meta/config/cost-thresholds.json.
    Source of truth stays USD (cost_usd in state); only the display is converted —
    so cost_alerts.py thresholds (USD) are unaffected."""
    global _USD_TO_CAD
    if _USD_TO_CAD is None:
        _USD_TO_CAD = 1.37
        try:
            cfg = json.loads((Path(VAULT_DEFAULT) / "99_Meta" / "config"
                              / "cost-thresholds.json").read_text())
            r = float(cfg.get("usd_to_cad") or 0)
            if r > 0:
                _USD_TO_CAD = r
        except Exception:
            pass
    return _USD_TO_CAD


def fmt_money(n) -> str:
    return f"C${float(n) * usd_to_cad():.2f}"


def fmt_tokens(n) -> str:
    """1234567 → 1.2M"""
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def render_kpi_block(title: str, k: dict, *, projection_basis_days: int | None = None,
                     true_cost_usd: float | None = None,
                     true_sessions: int | None = None) -> str:
    # Spend + session counts come from stats.json (transcript-recomputed truth) when
    # supplied; sessions.jsonl is an incomplete incremental capture and was the cause
    # of three disagreeing spend numbers on one page. Averages/tokens stay capture-based.
    cost = true_cost_usd if true_cost_usd is not None else k["cost_usd"]
    n_sessions = true_sessions if true_sessions is not None else k["sessions"]
    rows = [
        f"### {title}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Sessions | **{n_sessions}** |",
        f"| Cost (CAD) | **{fmt_money(cost)}** |",
        f"| Avg cost/session | {fmt_money(cost / n_sessions) if n_sessions else fmt_money(0)} |",
        f"| Avg duration *(captured sessions)* | {k['avg_duration_min']} min |",
        f"| Avg user turns *(captured sessions)* | {k['avg_turns']} |",
        f"| Tokens in / out *(captured)* | {fmt_tokens(k['tokens_input'])} / {fmt_tokens(k['tokens_output'])} |",
        f"| Cache read (tokens saved) | {fmt_tokens(k['tokens_cache_read'])} |",
    ]
    # If a projection basis is supplied, extrapolate to monthly burn rate.
    if projection_basis_days and cost > 0:
        daily = cost / projection_basis_days
        monthly = daily * 30
        rows.append(f"| Burn rate (extrapolated) | **{fmt_money(daily)}/day → {fmt_money(monthly)}/month** |")
    rows.append("")
    return "\n".join(rows)


def render_counter_table(title: str, items: list[tuple], col_label: str) -> str:
    if not items:
        return f"### {title}\n\n_(no data yet)_\n"
    rows = [f"### {title}", "", f"| {col_label} | Count |", "|---|---:|"]
    for name, count in items:
        rows.append(f"| {name} | {count} |")
    return "\n".join(rows) + "\n"


def render_chart(label: str, kind: str, chart_data: dict) -> str:
    """Charts plugin code block. Renders a line/bar chart from JSON."""
    code = json.dumps(chart_data, indent=2)
    return f"### {label}\n\n```chart\n{code}\n```\n"


def render_per_agent_cost(agent_costs: dict) -> str:
    """Render a per-agent cost breakdown section for the dashboard.

    Only claude-code has attributed cost data today. Dust-hosted and external CLI
    agents are listed as unattributed with a reason — honest, not fabricated.
    """
    rows = [
        "### Per-agent cost breakdown",
        "",
        "> Only agents with captured token data are costed. Dust agents run inside Dust's "
        "infrastructure — their LLM costs are billed directly to Dust's account and are not "
        "visible in the vault. External CLIs (codex, gemini) bill to their own provider accounts.",
        "",
        "| Agent | 7d cost | Monthly proj. | Cap (USD) | Status |",
        "|---|---:|---:|---:|---|",
    ]
    try:
        cfg = load_json(
            Path(VAULT_DEFAULT) / "99_Meta" / "config" / "cost-thresholds.json", {}
        )
        caps = cfg.get("per_agent_monthly_cap") or {}
        default_cap = float(caps.get("_default", 200))
    except Exception:  # noqa: BLE001
        caps = {}
        default_cap = 200.0

    usd_cad = usd_to_cad()
    unattributed: dict[str, list[str]] = defaultdict(list)
    for agent_name in sorted(agent_costs.keys()):
        info = agent_costs[agent_name]
        if info.get("attributed"):
            cost_7d = float(info.get("cost_7d_usd") or 0)
            proj = float(info.get("cost_monthly_proj_usd") or 0)
            cap = float(caps.get(agent_name, default_cap))
            over = "🔴 OVER CAP" if proj >= cap else ("🟡 warn" if proj >= cap * 0.75 else "✅ ok")
            rows.append(
                f"| `{agent_name}` | C${cost_7d * usd_cad:.2f} | **C${proj * usd_cad:.2f}** "
                f"| ${cap:.0f} | {over} |"
            )
        else:
            # Collapse the wall of identical "unattributed" rows into one summary line
            # per reason — 28 boilerplate rows buried the one agent that has real data.
            reason = (info.get("unattributed_reason") or "no cost data")[:80]
            unattributed[reason].append(agent_name)
    for reason, names in unattributed.items():
        rows.append(f"| _{len(names)} agents_ | — | — | — | _unattributed: {reason}_ |")

    if not agent_costs:
        rows.append("| _no agent cost data yet_ | | | | |")

    rows.append("")
    return "\n".join(rows)


def render_optimization(opts: list[dict], recurring: list[dict]) -> str:
    # Empty-state collapse: two empty tables were pure noise. One line each instead.
    rows = []
    if opts:
        rows += ["### Optimization candidates", "",
                 "Patterns repeated ≥ 3× — strong candidates for a custom skill, template, or snippet.",
                 "",
                 "| # obs | Pattern | Confidence | First seen |",
                 "|---:|---|---:|---|"]
        for o in opts[:10]:
            rows.append(f"| {o['n_observations']} | {o['pattern']} | {o['confidence']} | {o['first_seen']} |")
        rows.append("")
    else:
        rows.append("_No optimization candidates yet — patterns surface after repeating 3+ times._\n")
    if recurring:
        rows += ["### Recurring questions (from session summaries)", "",
                 "Questions you've asked variants of — turning them into a slash command or skill pays back fast.",
                 "",
                 "| # obs | Total cost | Avg cost | Representative summary |",
                 "|---:|---:|---:|---|"]
        for r in recurring[:10]:
            rows.append(f"| {r['n_observations']} | {fmt_money(r['total_cost_usd'])} | {fmt_money(r['avg_cost_usd'])} | {r['representative']} |")
    else:
        rows.append("_No recurring-question clusters yet — needs session summaries, which the capture "
                    "pipeline is currently not writing (every recent session shows \"(no summary)\")._")
    return "\n".join(rows) + "\n"


def _stats_spend_header(stats: dict, today: str) -> tuple[float, float, float, int]:
    """Return (today_cost_usd, last7d_cost_usd, alltime_cost_usd, alltime_sessions) from stats.json.

    stats.json is the single source of truth for spend — it is written by
    recompute_claude_stats.py which walks ALL ~/.claude/projects transcripts with
    accurate per-model pricing.  sessions.jsonl is used for tool/topic/chart sections
    (fine-grained per-session metadata) but must NOT drive the spend header numbers
    (it is an incomplete incremental capture).
    """
    by_day = stats.get("by_day") or {}
    all_time = stats.get("all_time") or {}

    today_cost = float((by_day.get(today) or {}).get("cost_usd", 0.0))

    # Last 7 days: sum by_day entries whose date is within 7 days of today.
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    last7_cost = 0.0
    for d, v in by_day.items():
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        if 0 <= (today_dt - dt).days < 7:
            last7_cost += float(v.get("cost_usd", 0.0))

    alltime_cost = float(all_time.get("cost_usd", 0.0))
    alltime_sessions = int(all_time.get("sessions", 0))
    return today_cost, last7_cost, alltime_cost, alltime_sessions


def _stats_window(stats: dict, days: int | None) -> tuple[float, int]:
    """(cost_usd, sessions) for a window from stats.json by_day — the transcript-recomputed
    source of truth. days=None → all_time block. Used so KPI tables agree with the header
    and §12 instead of the incomplete sessions.jsonl capture."""
    if days is None:
        at = stats.get("all_time") or {}
        return float(at.get("cost_usd", 0.0)), int(at.get("sessions", 0))
    by_day = stats.get("by_day") or {}
    today_dt = datetime.now().date()
    cost, n = 0.0, 0
    for d, v in by_day.items():
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        if 0 <= (today_dt - dt).days < days:
            cost += float(v.get("cost_usd", 0.0))
            n += int(v.get("sessions", 0))
    return cost, n


def stats_daily_series(stats: dict, days: int = 30) -> list[dict]:
    """Per-day cost+sessions from stats.json by_day (truth), date-ascending, zero-filled."""
    by_day = stats.get("by_day") or {}
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    rows = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        v = by_day.get(d) or {}
        rows.append({"date": d, "sessions": int(v.get("sessions", 0)),
                     "cost_usd": round(float(v.get("cost_usd", 0.0)), 4)})
    return rows


def moving_average(values: list[float], window: int = 7) -> list[float]:
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1):i + 1]
        out.append(round(sum(chunk) / len(chunk), 4))
    return out


def bid_age_days(bid: dict) -> int | None:
    """Days since the bid was opened (proxy for days-in-stage; status.json has no stage_since)."""
    opened = (bid.get("opened") or "").strip()
    if not opened:
        return None
    try:
        return (datetime.now().date() - datetime.strptime(opened[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return None


STALL_AGE_DAYS = 21  # fleet historical data: bids stuck past 21 days in Propose run ~78% loss


def render_trust_leaderboard(vault: Path) -> str:
    """Trust ledger from reputation.py output (_agent_state/<agent>/reputation.json).
    R = Beta-posterior trust score from triage outcomes; θ = floating auto-promote threshold."""
    rows = []
    agent_root = vault / "_agent_state"
    if agent_root.exists():
        for agent_dir in sorted(agent_root.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
                continue
            rep = load_json(agent_dir / "reputation.json", {})
            sig = rep.get("signals") or {}
            if not sig.get("total"):
                continue
            rows.append((agent_dir.name, rep, sig))
    out = ["### Trust ledger — who has earned auto-promote\n",
           "R = trust score from triage outcomes (Beta posterior, 30d half-life). "
           "θ = that agent's floating auto-promote confidence threshold — drops as trust builds. "
           "Recomputed by `build/tools/reputation.py` from `writes.jsonl`.\n"]
    if not rows:
        out.append("_no reputation data yet — run `python3 build/tools/reputation.py --all`_")
        return "\n".join(out) + "\n"
    rows.sort(key=lambda r: -float(r[1].get("R", 0)))
    out.append("| Agent | R (trust) | θ (auto-promote) | Promoted | Held | Quarantined | n labeled |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, rep, sig in rows:
        held = int(sig.get("conf_hold", 0)) + int(sig.get("neutral_hold", 0))
        theta = f"{float(rep.get('theta', 0.85)):.2f}" + (" (pinned)" if rep.get("pinned") else "")
        out.append(f"| `{name}` | {float(rep.get('R', 0)):.2f} | {theta} | "
                   f"{sig.get('promoted', 0)} | {held} | {sig.get('quarantined', 0)} | "
                   f"{rep.get('n_labeled', 0)} |")
    return "\n".join(out) + "\n"


def stale_agents(vault: Path) -> list[dict]:
    """Registry agents with expected_cadence_hours whose stats.last_active is older than
    the cadence. Mirrors check_stale_agents() in 99_Meta/verify-brain.sh."""
    reg = load_json(vault / "_agent_state" / "_registry.json", {})
    now = datetime.now(timezone.utc)
    out = []
    for a in (reg.get("agents") or []):
        if a.get("status") != "active" or not a.get("expected_cadence_hours"):
            continue
        name = a.get("agent_name", "?")
        cadence = float(a["expected_cadence_hours"])
        stats = load_json(vault / "_agent_state" / name / "stats.json", {})
        last = parse_iso(stats.get("last_active") or "")
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        silent_h = (now - last).total_seconds() / 3600 if last else float("inf")
        if silent_h > cadence:
            out.append({"agent": name, "silent_h": silent_h, "cadence_h": cadence,
                        "last_active": stats.get("last_active") or "never"})
    return sorted(out, key=lambda x: -x["silent_h"])


def _render_tomorrow(tomorrow_plan: str, bids: list[dict], pending_count: int,
                     ghost: dict | None = None) -> str:
    """Tomorrow's journal if it exists; otherwise something useful instead of a
    permanently-empty '(no daily journal for tomorrow yet)' placeholder."""
    if tomorrow_plan:
        return f"```\n{tomorrow_plan}\n```"
    lines = ["_No journal for tomorrow yet. Standing items:_", ""]
    # Ghost forecaster claims (ghost_forecast.py) — what the vault predicts will happen
    for c in ((ghost or {}).get("claims") or [])[:5]:
        lines.append(f"- 👻 {int(float(c.get('probability', 0)) * 100)}% — {c.get('event', '')}")
    if (ghost or {}).get("claims"):
        lines.append("")
    horizon = datetime.now().date() + timedelta(days=7)
    for b in bids:
        dl = (b.get("deadline") or "").strip()
        name = b.get("name") or b.get("bid_id", "?")
        if dl:
            try:
                if datetime.strptime(dl[:10], "%Y-%m-%d").date() <= horizon:
                    lines.append(f"- ⏰ **{name}** due {dl}")
            except ValueError:
                pass
        else:
            lines.append(f"- 📋 **{name}** ({b.get('stage', '?')}) — still has no deadline; set one")
    if pending_count:
        lines.append(f"- 📥 {pending_count} agent writes pending triage (`/dust-resolve`)")
    return "\n".join(lines)


def render_dashboard(*, vault: Path, sessions: list[dict], memory: dict, stats: dict,
                     open_bids: dict, today_plan: str, tomorrow_plan: str,
                     today: str, tomorrow: str) -> str:
    k_7 = kpi_block(window_filter(sessions, 7))
    k_30 = kpi_block(window_filter(sessions, 30))
    k_all = kpi_block(window_filter(sessions, None))
    cost_7, n_7 = _stats_window(stats, 7)
    cost_30, n_30 = _stats_window(stats, 30)
    cost_all, n_all = _stats_window(stats, None)
    daily_series = stats_daily_series(stats, days=30)
    # Spend header: read from stats.json (single source of truth — recomputed from transcripts).
    # sessions.jsonl is left for tools/topics/charts/recent-sessions sections only.
    today_cost_usd, last7_cost_usd, alltime_cost_usd, alltime_sessions = _stats_spend_header(stats, today)
    recurring = recurring_first_prompts(sessions, min_repeats=2)
    opts = optimization_from_memory(memory)
    recent_learnings = (memory.get("recent_learnings") or [])[:10]
    recent_sessions = sorted(sessions, key=lambda s: s.get("ts", ""), reverse=True)[:10]
    pending_writes_dir = vault / "00_Inbox" / "from-dust"
    pending_writes = []
    if pending_writes_dir.exists():
        pending_writes = [p for p in sorted(pending_writes_dir.rglob("*.md"))
                          if p.name != "README.md"]
    audit_log = vault / "99_Meta" / "dust-write-log.md"
    audit_tail = []
    if audit_log.exists():
        try:
            # REPLAY_SUPPRESSED entries are hourly-refresh noise (same files re-suppressed
            # every run) — filter them so the tail shows actual triage decisions.
            lines = [ln for ln in audit_log.read_text().splitlines()
                     if "REPLAY_SUPPRESSED" not in ln and ln.strip()]
            audit_tail = lines[-15:]
        except OSError:
            pass

    sbap_bids_early = (open_bids.get("bids") if isinstance(open_bids, dict) else []) or []
    pending_count = len(pending_writes)
    ghost_tomorrow = load_json(vault / "_brain_api" / "ghost" / "daily" / f"{tomorrow}.json", {})

    refreshed = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    parts: list[str] = []

    parts.append(f"""---
type: dashboard
refreshed: {refreshed}
tags: [dashboard, claude-code, kpis]
---

# AI Command Center

> Live operations dashboard. Regenerated by `build/tools/build_dashboard.py` each hour (and at SessionEnd, via `99_Meta/brain-refresh.sh`).
> Snapshot version: [Daily_Brief](Daily_Brief.md) — frozen at 06:30 each morning.

**Last refresh:** {refreshed}
**Spend today:** {fmt_money(today_cost_usd)}  ·  **Spend last 7 days:** {fmt_money(last7_cost_usd)}  ·  **Total spend (all time):** {fmt_money(alltime_cost_usd)}  ·  **Sessions (all time):** {alltime_sessions}

---

## 1. Today's plan — {today}

```
{today_plan or "(no daily journal for today yet — `02_Areas/Daily/" + today + ".md`)"}
```

## 2. Tomorrow — {tomorrow}

{_render_tomorrow(tomorrow_plan, sbap_bids_early, pending_count, ghost_tomorrow)}
""")

    # KPIs — last 7d shows burn-rate projection extrapolated to monthly.
    # Spend + session counts from stats.json (same source as the header) so the page
    # agrees with itself; duration/turn/token averages remain capture-based.
    parts.append("---\n\n## 3. KPIs — at a glance\n")
    parts.append(render_kpi_block("Last 7 days", k_7, projection_basis_days=7,
                                  true_cost_usd=cost_7, true_sessions=n_7))
    parts.append(render_kpi_block("Last 30 days", k_30, projection_basis_days=30,
                                  true_cost_usd=cost_30, true_sessions=n_30))
    parts.append(render_kpi_block("All time", k_all,
                                  true_cost_usd=cost_all, true_sessions=n_all))

    # Cost chart (Charts plugin)
    chart_labels = [r["date"][5:] for r in daily_series]  # MM-DD
    chart_costs = [r["cost_usd"] for r in daily_series]
    chart_sessions = [r["sessions"] for r in daily_series]
    parts.append("---\n\n## 4. Cost & activity (last 30 days)\n")
    # Phase 4.2: embed graph snapshot if present
    snap = vault / "graphify-out" / "graph-snapshot.png"
    if snap.exists():
        parts.append(f"\n![Knowledge graph snapshot](../graphify-out/graph-snapshot.png)\n")
        parts.append(f"*Auto-captured nightly. Live interactive view: open `graphify-out/graph.html` in a browser.*\n")
    cad_costs = [round(c * usd_to_cad(), 4) for c in chart_costs]
    parts.append(render_chart("Daily cost (CAD)", "line", {
        "type": "line",
        "labels": chart_labels,
        "series": [
            {"title": "Cost (CAD)", "data": cad_costs},
            # 7d moving average — readable trend even when one C$1,200 spike crushes the scale
            {"title": "7d avg", "data": moving_average(cad_costs, 7)},
        ],
        "tension": 0.2, "width": "80%", "fill": False, "labelColors": True,
    }))
    parts.append(render_chart("Sessions per day", "bar", {
        "type": "bar",
        "labels": chart_labels,
        "series": [{"title": "Sessions", "data": chart_sessions}],
        "width": "80%", "labelColors": True,
    }))

    # Top tools / topics / projects
    parts.append("---\n\n## 5. Where the time goes (last 30 days)\n")
    parts.append(render_counter_table("Top tools", k_30["top_tools"], "Tool"))
    parts.append(render_counter_table("Top topics", k_30["top_topics"], "Topic"))
    parts.append(render_counter_table("Top working dirs", k_30["top_cwds"], "Project"))
    parts.append(render_counter_table("Top models", k_30["top_models"], "Model"))

    # Optimization candidates
    parts.append("---\n\n## 6. Optimization signal — questions you ask a lot\n")
    parts.append(render_optimization(opts, recurring))

    # Recent learnings + sessions
    parts.append("---\n\n## 7. Recent learnings (carry-over to next session)\n")
    if recent_learnings:
        # recent_learnings is a mixed list: some entries are dicts {text,date}, some are
        # bare strings (older format). Tolerate both so the dashboard never crashes.
        def _ltext(l):
            if isinstance(l, dict):
                return f"- {l.get('text', '')}  *({l.get('date', '')})*"
            return f"- {l}"
        parts.append("\n".join(_ltext(l) for l in recent_learnings))
    else:
        parts.append("_no learnings captured yet — they accumulate as Haiku extracts from each session_")

    parts.append("\n\n---\n\n## 8. Recent sessions\n")
    if recent_sessions:
        parts.append("| When | Model | Cost | Turns | Summary |")
        parts.append("|---|---|---:|---:|---|")
        for s in recent_sessions:
            ts = (s.get("ts") or "")[:16].replace("T", " ")
            summary = (s.get("summary") or "")[:90].replace("|", "\\|")
            parts.append(f"| {ts} | {s.get('model', '?')} | {fmt_money(s.get('cost_usd', 0))} | "
                         f"{s.get('n_turns_user', 0)} | {summary} |")
    else:
        parts.append("_no sessions captured yet_")

    # Pipeline (Dataview — live)
    parts.append("\n\n---\n\n## 9. Pipeline — open bids (live via Dataview)\n")
    parts.append("```dataview\n"
                 "TABLE without id\n"
                 "  file.link as Bid,\n"
                 "  stage as Stage,\n"
                 "  client as Client,\n"
                 "  deadline as \"Due\"\n"
                 "FROM \"01_Projects\"\n"
                 "WHERE stage AND stage != \"Won\" AND stage != \"Lost\"\n"
                 "SORT deadline ASC\n"
                 "LIMIT 20\n"
                 "```\n")

    # Open bids from SBAP (in case daily-note frontmatter isn't there yet)
    sbap_bids = sbap_bids_early
    parts.append("### From SBAP `_brain_api/bid/_open.json`\n")
    if sbap_bids:
        parts.append("| Bid | Stage | Client | Deadline | Age | Health |")
        parts.append("|---|---|---|---|---:|---|")
        for b in sbap_bids[:20]:
            age = bid_age_days(b)
            age_str = f"{age}d" if age is not None else "?"
            # Stall heuristic: fleet history puts bids stuck past 21 days at ~78% loss.
            if age is not None and age > STALL_AGE_DAYS and b.get("stage") in ("Propose", "Negotiate"):
                health = f"🔥 STALLED — {age}d in {b.get('stage')}, no recorded touchpoint kills bids"
            elif not (b.get("deadline") or "").strip():
                health = "⚠ no deadline set"
            else:
                health = "✅"
            parts.append(f"| {b.get('name') or b.get('bid_id', '?')} | {b.get('stage', '?')} | "
                         f"{b.get('client', '?')} | {b.get('deadline') or '—'} | {age_str} | {health} |")
    else:
        parts.append("_no open bids in SBAP yet — populate `01_Projects/<bid>/00 - Brief.md` with frontmatter `stage: Discover|Qualify|Propose|Negotiate`_")

    # Dust agent fleet — the heart of the "vault as shared memory" architecture
    fleet = aggregate_agent_fleet(vault)
    parts.append("\n\n---\n\n## 10. Agent fleet — the vault IS the shared memory\n")
    parts.append("Every Dust agent reads + writes through OneDrive. The vault is the persistent memory; "
                 "SBAP frontmatter is the protocol. No API key needed locally — agents bring their own LLM via Dust.\n")
    parts.append("**Write protocol:** [`00_Inbox/from-dust/README.md`](../00_Inbox/from-dust/README.md) — paste into each Dust agent's instructions.\n")
    parts.append(f"\n**Fleet snapshot:** {len(fleet['agents'])} agent state dirs · "
                 f"{fleet['active_today']} active today · {sum(a['memory_learnings'] for a in fleet['agents'].values())} learnings stored across all agents\n")

    # Stale agents — registry cadence vs last_active (was only visible in hook output)
    stale = stale_agents(vault)
    if stale:
        parts.append(f"\n### ⚠ Stale agents ({len(stale)}) — silent past their expected cadence\n")
        parts.append("| Agent | Silent | Expected every | Last active |")
        parts.append("|---|---:|---:|---|")
        for s in stale[:12]:
            silent = "never ran" if s["silent_h"] == float("inf") else f"{int(s['silent_h'])}h"
            parts.append(f"| `{s['agent']}` | {silent} | {int(s['cadence_h'])}h | {s['last_active'][:16]} |")

    # Trust ledger — reputation.py R/θ surfaced (the earned-autonomy leaderboard)
    parts.append("\n" + render_trust_leaderboard(vault))

    # Per-agent state — writes, memory size, last active
    parts.append("\n### Per-agent state\n")
    parts.append("Calibration = stated confidence vs human accept-rate (from `/dust-resolve`). "
                 "⚠ over-confident = high-confidence drafts Tony still rejects — the risky ones to auto-promote.\n")
    parts.append("| Agent | Inbox | Mem. learnings | Mem. patterns | Accounts known | Writes (7d) | Calibration | Last active |")
    parts.append("|---|---:|---:|---:|---:|---:|---|---|")
    agents_dir = vault / "00_Inbox" / "from-dust"
    for name in sorted(fleet["agents"].keys()):
        a = fleet["agents"][name]
        inbox_files = []
        if agents_dir.exists() and (agents_dir / name).exists():
            inbox_files = [f for f in (agents_dir / name).glob("*.md") if f.name != "README.md"]
        writes_7d = sum(a["last_7d"].values())
        last = (a.get("last_active") or "")[:16].replace("T", " ") or "never"
        parts.append(f"| `{name}` | {len(inbox_files)} | {a['memory_learnings']} | {a['memory_patterns']} | "
                     f"{a['accounts_known']} | {writes_7d} | {fmt_calibration(a.get('calibration'))} | {last} |")

    # Union of all agent learnings — the cross-agent intelligence layer
    parts.append("\n### Top learnings across all agents (latest 15)\n")
    if fleet["all_learnings"]:
        parts.append("| Agent | Learning | Date |")
        parts.append("|---|---|---|")
        for l in fleet["all_learnings"][:15]:
            if not isinstance(l, dict):
                continue
            text = l.get("text", "").replace("|", "\\|")[:140]
            parts.append(f"| `{l['agent']}` | {text} | {l.get('date', '?')} |")
    else:
        parts.append("_no learnings stored yet across the fleet — each Dust agent should include "
                     "`learnings: []`, `patterns: []`, `mistakes_to_avoid: []` in its frontmatter when "
                     "writing to `00_Inbox/from-dust/<agent>/`, and triage will merge them into "
                     "`_agent_state/<agent>/memory.json` automatically_")

    # Union of patterns ≥ THRESHOLD obs across the fleet — strongest signal
    parts.append("\n### Recurring patterns across the fleet (≥ 3 observations)\n")
    strong = [p for p in fleet["all_patterns"] if isinstance(p, dict) and int(p.get("n_observations", 0)) >= OPTIMIZATION_OBS_THRESHOLD]
    if strong:
        parts.append("| Agent | Pattern | # obs | Confidence |")
        parts.append("|---|---|---:|---:|")
        for p in strong[:10]:
            if not isinstance(p, dict):
                continue
            parts.append(f"| `{p['agent']}` | {p.get('pattern','')} | {p['n_observations']} | {p.get('confidence', 0)} |")
    else:
        parts.append("_no patterns have repeated 3+ times yet across any agent_")

    parts.append("\n### Pending writes (awaiting triage or low-confidence hold)\n")
    if pending_writes:
        now_ts = datetime.now().timestamp()
        aged = []
        for f in pending_writes:
            try:
                age_d = int((now_ts - f.stat().st_mtime) / 86400)
            except OSError:
                age_d = 0
            aged.append((age_d, f))
        aged.sort(key=lambda x: -x[0])  # oldest first — those are the ones rotting
        n_old = sum(1 for d, _ in aged if d > 7)
        old_note = f" — **{n_old} older than 7 days**" if n_old else ""
        parts.append(f"_{len(pending_writes)} files awaiting Tony's review{old_note} (oldest first):_\n")
        for age_d, f in aged[:20]:
            rel = f.relative_to(vault)
            flag = " ⚠" if age_d > 7 else ""
            parts.append(f"- [[{rel}]] · {age_d}d{flag}")
    else:
        parts.append("_no pending agent writes — all triaged or inbox empty_")

    parts.append("\n### Recent audit trail (last 15 triage decisions from `99_Meta/dust-write-log.md`, replay noise filtered)\n")
    if audit_tail:
        parts.append("```")
        parts.extend(audit_tail)
        parts.append("```")
    else:
        parts.append("_no Dust writes promoted yet — log lives at `99_Meta/dust-write-log.md`_")

    parts.append("\n\n---\n\n## 11. Today's daily journal (preview)\n\nDataview pull of the live note:\n")
    parts.append("```dataview\n"
                 "LIST WITHOUT ID file.link\n"
                 "FROM \"02_Areas/Daily\"\n"
                 f"WHERE file.name = \"{today}\"\n"
                 "```\n")

    # Per-agent cost breakdown (Phase 5 #13)
    parts.append("\n\n---\n\n## 12. Per-agent cost breakdown\n")
    try:
        import importlib.util as _ilu
        _ca_path = Path(__file__).parent / "cost_alerts.py"
        _spec = _ilu.spec_from_file_location("cost_alerts_fresh", _ca_path)
        _ca_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
        _spec.loader.exec_module(_ca_mod)  # type: ignore[union-attr]
        agent_costs = _ca_mod.compute_per_agent_costs(vault)
        parts.append(render_per_agent_cost(agent_costs))
    except Exception as _e:  # noqa: BLE001
        parts.append(f"_Per-agent cost data unavailable: {_e}_\n")

    parts.append(f"\n\n---\n\n*Generated by* `build/tools/build_dashboard.py` *at {refreshed}.*\n")
    return "\n".join(parts)


def render_daily_brief(*, vault: Path, sessions: list[dict], memory: dict,
                       open_bids: dict, today_plan: str, today: str) -> str:
    k_yesterday = kpi_block(window_filter(sessions, 1))
    k_7 = kpi_block(window_filter(sessions, 7))
    learnings = (memory.get("recent_learnings") or [])[:5]
    opts = optimization_from_memory(memory)
    sbap_bids = (open_bids.get("bids") if isinstance(open_bids, dict) else []) or []

    refreshed = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    parts = [
        f"""---
type: daily-brief
date: {today}
refreshed: {refreshed}
tags: [daily-brief]
---

# Daily Brief — {today}

*Snapshot frozen at 06:30. Live view: [[Dashboard]].*

## Yesterday in numbers
| Metric | Value |
|---|---:|
| Sessions | {k_yesterday['sessions']} |
| Cost | {fmt_money(k_yesterday['cost_usd'])} |
| Avg duration | {k_yesterday['avg_duration_min']} min |
| Top tool | {k_yesterday['top_tools'][0][0] if k_yesterday['top_tools'] else '—'} |
| Top topic | {k_yesterday['top_topics'][0][0] if k_yesterday['top_topics'] else '—'} |

## This week so far
| Metric | Value |
|---|---:|
| Sessions | {k_7['sessions']} |
| Cost | {fmt_money(k_7['cost_usd'])} |
| Cache savings | {fmt_tokens(k_7['tokens_cache_read'])} cached input tokens (free reads) |

## Today's plan ({today})

```
{today_plan or "(no daily journal yet — create `02_Areas/Daily/" + today + ".md`)"}
```

## Carry-over learnings

"""
    ]
    if learnings:
        # recent_learnings is a mixed list: dicts {text,date} (new) or bare strings
        # (older format). Tolerate both so the daily brief never crashes (was: l.get
        # on a str → AttributeError → step 5 of brain-refresh.sh died, dashboard froze).
        parts.append("\n".join(f"- {l.get('text', '') if isinstance(l, dict) else l}" for l in learnings))
    else:
        parts.append("_(none captured yet)_")

    parts.append("\n\n## Optimization candidates\n")
    if opts:
        for o in opts[:5]:
            parts.append(f"- **{o['n_observations']}× **{o['pattern']} (conf {o['confidence']})")
    else:
        parts.append("_(none yet — patterns surface after they repeat 3+ times)_")

    parts.append("\n\n## Bids needing attention\n")
    if sbap_bids:
        for b in sbap_bids[:10]:
            parts.append(f"- {b.get('name') or b.get('bid_id', '?')} — stage {b.get('stage','?')} — due {b.get('deadline','?')}")
    else:
        parts.append("_(no open bids in SBAP)_")

    parts.append(f"\n\n---\n*Frozen at {refreshed}. Next snapshot tomorrow 06:30.*\n")
    return "\n".join(parts)


# ──────────────────────────── main ───────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true", help="also regenerate Daily_Brief.md")
    ap.add_argument("--vault", default=os.environ.get("CLAUDE_VAULT", VAULT_DEFAULT))
    args = ap.parse_args()

    vault = Path(args.vault)
    if not vault.exists():
        log(f"vault not found: {vault}", level="ERROR")
        return 0

    agent_dir = vault / "_agent_state" / "claude-code"
    sessions = load_sessions(agent_dir / "sessions.jsonl")
    memory = load_json(agent_dir / "memory.json", {})
    stats = load_json(agent_dir / "stats.json", {})
    open_bids = load_json(vault / "_brain_api" / "bid" / "_open.json", {})

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    today_plan = read_daily_plan(vault / "02_Areas" / "Daily" / f"{today}.md")
    tomorrow_plan = read_daily_plan(vault / "02_Areas" / "Daily" / f"{tomorrow}.md")

    # Render the live dashboard
    dashboard_md = render_dashboard(
        vault=vault, sessions=sessions, memory=memory, stats=stats,
        open_bids=open_bids, today_plan=today_plan, tomorrow_plan=tomorrow_plan,
        today=today, tomorrow=tomorrow,
    )
    atomic_write(vault / "02_Areas" / "Dashboard.md", dashboard_md)

    # Write machine-readable rollups
    aggregate = {
        "refreshed": datetime.now(timezone.utc).isoformat(),
        "windows": {
            "last_7d": kpi_block(window_filter(sessions, 7)),
            "last_30d": kpi_block(window_filter(sessions, 30)),
            "all_time": kpi_block(window_filter(sessions, None)),
        },
        "daily_cost_series_30d": daily_cost_series(sessions, days=30),
        "session_count": len(sessions),
    }
    atomic_write(vault / "_brain_api" / "claude_usage" / "aggregate.json",
                 json.dumps(aggregate, indent=2))

    optimizations = {
        "refreshed": datetime.now(timezone.utc).isoformat(),
        "from_memory_patterns": optimization_from_memory(memory),
        "recurring_summaries": recurring_first_prompts(sessions, min_repeats=2),
        "threshold_obs": OPTIMIZATION_OBS_THRESHOLD,
    }
    atomic_write(vault / "_brain_api" / "claude_usage" / "optimization_candidates.json",
                 json.dumps(optimizations, indent=2))

    if args.daily:
        brief = render_daily_brief(vault=vault, sessions=sessions, memory=memory,
                                   open_bids=open_bids, today_plan=today_plan, today=today)
        atomic_write(vault / "02_Areas" / "Daily_Brief.md", brief)
        log(f"daily brief regenerated for {today}", level="INFO")

    # Phase 2.2 + Phase 5 #13: cost alerts — global + per-agent runaway kill
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from cost_alerts import check_and_write as _cost_alert
        ca = _cost_alert(vault)
        if ca.get("alerted"):
            g = ca.get("global") or {}
            if g.get("alerted"):
                log(f"COST ALERT fired: {g.get('severity')} "
                    f"(monthly_proj=${g.get('monthly_proj', 0):.2f}) → {g.get('alert_path')}", level="WARN")
            for pr in (ca.get("per_agent") or []):
                if pr.get("status") == "alerted":
                    log(f"PER-AGENT COST ALERT: {pr['agent']} monthly_proj=${pr['monthly_proj']:.2f} "
                        f"cap=${pr['cap']:.0f} paused={pr.get('agent_paused')} → {pr.get('alert_path')}", level="WARN")
    except Exception as e:  # noqa: BLE001
        log(f"cost_alerts check failed: {e!r}", level="WARN")

    log(f"dashboard refreshed: {len(sessions)} sessions, {len(memory.get('global_patterns', []))} patterns, "
        f"{len(optimizations['from_memory_patterns'])} optimization candidates", level="INFO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
