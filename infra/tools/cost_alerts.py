#!/usr/bin/env python3
"""cost_alerts.py — Phase 2.2 + Phase 5 (#13)
Check whether 7d-extrapolated monthly burn exceeds configured thresholds.
Per-agent cost breakdown + per-agent runaway kill (flip status:paused in _registry.json).
If thresholds are breached, write SBAP-frontmatter'd alert files into 00_Inbox/from-system/.
Throttled so we don't spam the inbox.

Called from build/tools/build_dashboard.py after the aggregate KPIs are computed.
Can also be run standalone:
    python3 build/tools/cost_alerts.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_alert(vault: Path, severity: str, monthly_proj: float, daily: float,
                threshold: float, agg: dict) -> Path:
    today = datetime.now(timezone.utc)
    name = today.strftime("%Y-%m-%d-cost-alert-") + severity + ".md"
    out_dir = vault / "00_Inbox" / "from-system"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / name

    top_tool = ""
    try:
        tt = agg["windows"]["last_7d"]["top_tools"]
        top_tool = tt[0][0] if tt else "?"
    except (KeyError, IndexError, TypeError):
        top_tool = "?"

    body = f"""---
sbap_version: "1.0"
source_agent: claude-code
source_run_id: cost-alert-{today.isoformat().replace(':', '').replace('-', '')[:15]}
generated: "{today.isoformat()}"
input_context_refs:
  - "_brain_api/claude_usage/aggregate.json"
  - "99_Meta/config/cost-thresholds.json"
output_type: escalation_alert
target_path: ""
confidence: 1.0
needs_review: true
reasoning_summary: |
  Cost burn-rate threshold breach. 7d-extrapolated monthly cost = ${monthly_proj:.2f},
  exceeding ${threshold:.0f} (severity: {severity}). Surfacing for awareness.
---

# ⚠ Cost alert — {severity.upper()}

**Today's date:** {today.strftime('%Y-%m-%d %H:%M')}

## Numbers

| Metric | Value |
|---|---:|
| Last 7d cost | ${agg['windows']['last_7d']['cost_usd']:.4f} |
| Daily rate (extrapolated) | ${daily:.4f}/day |
| Monthly projection | **${monthly_proj:.2f}** |
| Threshold ({severity}) | ${threshold:.0f} |
| Top tool last 7d | `{top_tool}` |

## What to check

- Open the [Dashboard](../../02_Areas/Dashboard.md) §6 "Optimization signal" to see which recurring questions are driving spend.
- Run `/recall "<topic>"` against any pattern repeating 3+ times — those are candidates for `/promote-pattern` so future sessions hit canonical blocks instead of regenerating.
- If an agent is misbehaving, check `_agent_state/<agent>/stats.json` to see write volume.

## Throttle

This alert won't fire again for 24h unless severity escalates. To suppress entirely (e.g. you know about a one-off heavy build), `touch 99_Meta/config/cost-alerts-paused` — the script honors that file.
"""
    p.write_text(body)
    return p


# ──────────────────────────────────────────────────────────────────
# Per-agent cost computation (Phase 5 #13)
# ──────────────────────────────────────────────────────────────────

def compute_per_agent_costs(vault: Path) -> dict[str, dict]:
    """Return a per-agent cost breakdown.

    Cost attribution strategy:
    - claude-code: reads _agent_state/claude-code/stats.json (accurate token/cost data).
    - codex / gemini: read their stats.json last_ingest.json for activity but NO cost data
      → marked unattributed with reason.
    - All other SBAP Dust agents: no token data exists (they run inside Dust's infra,
      costs billed directly to Dust's account) → marked unattributed with reason.

    Returns dict keyed by agent_name → {
        cost_7d_usd: float | None,
        cost_monthly_proj_usd: float | None,
        attributed: bool,
        unattributed_reason: str | None,
        last_active: str | None,
    }
    """
    results: dict[str, dict] = {}

    # ── claude-code: real token data ──────────────────────────────
    cc_stats = load_json(vault / "_agent_state" / "claude-code" / "stats.json", {})
    by_day = cc_stats.get("by_day") or {}
    today_dt = datetime.now(timezone.utc).date()
    cutoff = today_dt - timedelta(days=7)
    cost_7d = sum(
        float(v.get("cost_usd", 0))
        for d, v in by_day.items()
        if _parse_date_safe(d) and _parse_date_safe(d) >= cutoff
    )
    last_active_cc = cc_stats.get("all_time", {})  # no last_active in cc stats.json
    # Derive last active from by_day max date
    last_day = max(by_day.keys()) if by_day else None
    results["claude-code"] = {
        "cost_7d_usd": round(cost_7d, 4),
        "cost_monthly_proj_usd": round(cost_7d / 7.0 * 30.0, 2) if cost_7d > 0 else 0.0,
        "attributed": True,
        "unattributed_reason": None,
        "last_active": last_day,
    }

    # ── codex / gemini: activity tracked but NO cost data ────────
    for agent_name in ("codex", "gemini"):
        agent_dir = vault / "_agent_state" / agent_name
        last_ingest = load_json(agent_dir / "last_ingest.json", {})
        last_active = last_ingest.get("last_ingest") or last_ingest.get("last_run")
        results[agent_name] = {
            "cost_7d_usd": None,
            "cost_monthly_proj_usd": None,
            "attributed": False,
            "unattributed_reason": (
                "External CLI tool — costs billed directly to OpenAI/Google account, "
                "not captured in vault stats."
            ),
            "last_active": last_active,
        }

    # ── All other registered agents: Dust-hosted, no cost data ───
    registry = load_json(vault / "_agent_state" / "_registry.json", {})
    for agent in registry.get("agents", []):
        name = agent["agent_name"]
        if name in results:
            continue
        # Try to pull last_active from stats.json
        stats = load_json(vault / "_agent_state" / name / "stats.json", {})
        last_active = stats.get("last_active")
        results[name] = {
            "cost_7d_usd": None,
            "cost_monthly_proj_usd": None,
            "attributed": False,
            "unattributed_reason": (
                "Dust-hosted agent — LLM costs billed to Dust subscription, "
                "not captured in vault stats."
            ),
            "last_active": last_active,
        }

    return results


def _parse_date_safe(s: str):
    try:
        from datetime import date
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def write_agent_alert(vault: Path, agent_name: str, monthly_proj: float,
                      cap: float, cost_7d: float) -> Path:
    """Write a per-agent runaway cost alert to 00_Inbox/from-system/."""
    today = datetime.now(timezone.utc)
    name = today.strftime(f"%Y-%m-%d-cost-alert-agent-{agent_name}.md")
    out_dir = vault / "00_Inbox" / "from-system"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / name

    body = f"""---
sbap_version: "1.0"
source_agent: claude-code
source_run_id: per-agent-cost-alert-{agent_name}-{today.isoformat().replace(':', '').replace('-', '')[:15]}
generated: "{today.isoformat()}"
input_context_refs:
  - "_agent_state/claude-code/stats.json"
  - "99_Meta/config/cost-thresholds.json"
output_type: escalation_alert
target_path: ""
confidence: 1.0
needs_review: true
reasoning_summary: |
  Per-agent runaway kill triggered for {agent_name}.
  7d-extrapolated monthly cost = ${monthly_proj:.2f}, exceeding per-agent cap ${cap:.0f}.
  Agent status flipped to paused in _registry.json.
---

# 🚨 Per-agent cost runaway — {agent_name}

**Today's date:** {today.strftime('%Y-%m-%d %H:%M')}

Agent **`{agent_name}`** has breached its per-agent monthly cost cap.
Its status has been automatically set to **`paused`** in `_agent_state/_registry.json`.

## Numbers

| Metric | Value |
|---|---:|
| Agent | `{agent_name}` |
| Last 7d cost | **${cost_7d:.4f}** |
| Monthly projection | **${monthly_proj:.2f}** |
| Per-agent cap | ${cap:.0f} |
| Overage | ${monthly_proj - cap:.2f} |

## Actions required

1. Review `_agent_state/{agent_name}/stats.json` for write volume anomalies.
2. Check recent sessions in `_agent_state/claude-code/sessions.jsonl` for runaway loops.
3. When satisfied, manually set `status: active` back in `_agent_state/_registry.json`.

## Throttle

This alert throttles once per agent per 24h. To suppress, `touch 99_Meta/config/cost-alerts-paused`.
"""
    p.write_text(body)
    return p


def pause_agent_in_registry(vault: Path, agent_name: str) -> bool:
    """Flip agent status to 'paused' in _registry.json. Returns True on success."""
    registry_path = vault / "_agent_state" / "_registry.json"
    registry = load_json(registry_path, {})
    if not registry:
        return False
    changed = False
    for agent in registry.get("agents", []):
        if agent["agent_name"] == agent_name and agent.get("status") not in ("paused",):
            agent["status"] = "paused"
            changed = True
    if changed:
        try:
            registry_path.write_text(json.dumps(registry, indent=1, ensure_ascii=False))
        except OSError:
            return False
    return changed


def check_per_agent_costs(vault: Path, cfg: dict) -> list[dict]:
    """Check per-agent costs against caps. Returns list of alert result dicts."""
    per_agent_caps = cfg.get("per_agent_monthly_cap") or {}
    default_cap = float(per_agent_caps.get("_default", 200))
    throttle_h = float(cfg.get("throttle_hours", 24))

    agent_costs = compute_per_agent_costs(vault)
    alerts_fired = []

    for agent_name, cost_info in agent_costs.items():
        if not cost_info["attributed"]:
            continue  # Can't enforce cap on unattributed agents
        monthly_proj = cost_info.get("cost_monthly_proj_usd") or 0.0
        if monthly_proj <= 0:
            continue

        cap_key = agent_name if agent_name in per_agent_caps else "_default"
        cap = float(per_agent_caps.get(cap_key, default_cap))

        if monthly_proj < cap:
            continue

        # Throttle check — per-agent key in cfg
        ts_key = f"_last_per_agent_alert_ts_{agent_name}"
        last_ts = cfg.get(ts_key)
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_h < throttle_h:
                    alerts_fired.append({
                        "agent": agent_name, "status": "throttled",
                        "monthly_proj": monthly_proj, "cap": cap, "age_h": age_h,
                    })
                    continue
            except ValueError:
                pass

        cost_7d = cost_info.get("cost_7d_usd") or 0.0
        alert_path = write_agent_alert(vault, agent_name, monthly_proj, cap, cost_7d)
        paused = pause_agent_in_registry(vault, agent_name)

        # Record throttle timestamp
        cfg[ts_key] = datetime.now(timezone.utc).isoformat()

        alerts_fired.append({
            "agent": agent_name,
            "status": "alerted",
            "monthly_proj": monthly_proj,
            "cap": cap,
            "cost_7d_usd": cost_7d,
            "agent_paused": paused,
            "alert_path": str(alert_path.relative_to(vault)),
        })

    return alerts_fired


def check_and_write(vault: Path) -> dict:
    """Returns a dict describing what (if anything) was alerted."""
    cfg_path = vault / "99_Meta" / "config" / "cost-thresholds.json"
    cfg = load_json(cfg_path, {})
    if not cfg:
        return {"status": "no-config", "alerted": False}

    # Pause flag — kill switch
    if (vault / "99_Meta" / "config" / "cost-alerts-paused").exists():
        return {"status": "paused", "alerted": False}

    agg = load_json(vault / "_brain_api" / "claude_usage" / "aggregate.json", {})
    last_7d = (agg.get("windows") or {}).get("last_7d") or {}
    cost_7d = float(last_7d.get("cost_usd", 0))

    result: dict = {
        "global": {"status": "no-data", "alerted": False},
        "per_agent": [],
        "alerted": False,
    }

    # ── Global alert ─────────────────────────────────────────────
    if cost_7d > 0:
        daily = cost_7d / 7.0
        monthly = daily * 30.0

        warn_thresh = float(cfg.get("monthly_warn_usd", 200))
        crit_thresh = float(cfg.get("monthly_critical_usd", 500))
        throttle_h = float(cfg.get("throttle_hours", 24))

        severity = None
        threshold_hit = None
        if monthly >= crit_thresh:
            severity, threshold_hit = "critical", crit_thresh
            last_key = "_last_alert_ts_critical"
        elif monthly >= warn_thresh:
            severity, threshold_hit = "warn", warn_thresh
            last_key = "_last_alert_ts_warn"

        if severity:
            # Throttle check
            last_ts = cfg.get(last_key)
            throttled = False
            if last_ts:
                try:
                    last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                    age_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                    if age_h < throttle_h:
                        throttled = True
                        result["global"] = {
                            "status": "throttled", "alerted": False,
                            "severity": severity, "age_h": age_h,
                        }
                except ValueError:
                    pass

            if not throttled:
                alert_path = write_alert(vault, severity, monthly, daily, threshold_hit, agg)
                cfg[last_key] = datetime.now(timezone.utc).isoformat()
                result["global"] = {
                    "status": "alerted", "severity": severity,
                    "monthly_proj": monthly, "alert_path": str(alert_path.relative_to(vault)),
                    "alerted": True,
                }
                result["alerted"] = True
        else:
            result["global"] = {"status": "ok", "monthly_proj": monthly, "alerted": False}

    # ── Per-agent alerts ─────────────────────────────────────────
    try:
        per_agent_results = check_per_agent_costs(vault, cfg)
        result["per_agent"] = per_agent_results
        if any(r.get("status") == "alerted" for r in per_agent_results):
            result["alerted"] = True
    except Exception as e:  # noqa: BLE001
        result["per_agent_error"] = str(e)

    # ── Save updated config (throttle timestamps) ────────────────
    try:
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    except OSError:
        pass

    # Backward-compat: expose top-level fields for callers that expect the old shape
    global_info = result["global"]
    if global_info.get("alerted"):
        result["severity"] = global_info.get("severity")
        result["monthly_proj"] = global_info.get("monthly_proj")
        result["alert_path"] = global_info.get("alert_path")

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("CLAUDE_VAULT", VAULT_DEFAULT))
    args = ap.parse_args()

    result = check_and_write(Path(args.vault))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
