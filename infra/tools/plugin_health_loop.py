#!/usr/bin/env python3
"""Close the Command Center self-healing + self-learning loop into the vault's
nightly cadence (run from brain-refresh.sh).

SELF-HEALING: read the plugin's health log; if it reported degraded status in the
last 24h, surface an escalation note in Important/escalations/ (and clear it when
healthy again) — so persistent plugin issues reach Tony, not just the console.

SELF-LEARNING: read the plugin's usage counters (which tabs/actions Tony actually
uses) and write a rollup to _agent_state/claude-code/usage-rollup.json for the
nightly agent-dreaming / skill loops to mine (and to adapt the UI over time).
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
HEALTH_LOG = VAULT / "_agent_state/claude-code/plugin-health.log"
PLUGIN_DATA = VAULT / ".obsidian/plugins/claude-command-center/data.json"
ESC_DIR = VAULT / "Important/escalations"
ROLLUP = VAULT / "_agent_state/claude-code/usage-rollup.json"
ESC = ESC_DIR / "command-center-health.md"
NOW = dt.datetime.now().astimezone()


def heal_loop() -> list[str]:
    """Surface persistent degradation; clear the escalation when healthy."""
    if not HEALTH_LOG.exists():
        return []
    cutoff = NOW - dt.timedelta(hours=24)
    degraded = []
    for line in HEALTH_LOG.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            ts = dt.datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff and (e.get("level") in ("warn", "error")) and "status=degraded" in e.get("msg", ""):
            degraded.append(e)
    if degraded:
        ESC_DIR.mkdir(parents=True, exist_ok=True)
        body = (
            f"---\ntype: escalation\nsource: command-center-plugin\ngenerated: {NOW.isoformat(timespec='seconds')}\n"
            f"tags: [escalation, self-heal]\n---\n\n# ⚠ Command Center health — {len(degraded)} degraded event(s) in 24h\n\n"
            "The dashboard's self-heal watchdog reported issues it could not fully auto-recover:\n\n"
            + "\n".join(f"- `{e['ts']}` — {e['msg']}" for e in degraded[-10:])
            + "\n\n> Auto-surfaced by `plugin_health_loop.py`. Clears automatically once healthy.\n"
        )
        ESC.write_text(body, encoding="utf-8")
        return [f"escalated {len(degraded)} degraded events → {ESC.relative_to(VAULT)}"]
    if ESC.exists():
        ESC.unlink()  # healthy again → clear stale escalation
        return ["cleared stale health escalation (healthy)"]
    return []


def learn_loop() -> list[str]:
    """Roll up usage counters for the nightly learning loop."""
    if not PLUGIN_DATA.exists():
        return []
    try:
        usage = (json.loads(PLUGIN_DATA.read_text(encoding="utf-8")) or {}).get("usage", {})
    except Exception:
        return []
    if not usage:
        return []
    top = sorted(usage.items(), key=lambda kv: kv[1], reverse=True)
    rollup = {
        "updated": NOW.isoformat(timespec="seconds"),
        "total_interactions": sum(usage.values()),
        "top_surfaces": [{"surface": k, "count": v} for k, v in top[:12]],
        "raw": usage,
    }
    ROLLUP.parent.mkdir(parents=True, exist_ok=True)
    tmp = ROLLUP.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rollup, indent=1), encoding="utf-8")
    os.replace(tmp, ROLLUP)
    most = top[0][0] if top else "n/a"
    return [f"usage rollup: {sum(usage.values())} interactions, most-used '{most}' → {ROLLUP.relative_to(VAULT)}"]


def main() -> int:
    notes = heal_loop() + learn_loop()
    print("[plugin-health-loop] " + ("; ".join(notes) if notes else "nothing to do (healthy, no usage yet)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
