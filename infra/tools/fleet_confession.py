#!/usr/bin/env python3
"""fleet_confession.py — the weekly "fleet confession" (composer only — SILENT).

Reads each agent's reputation.json (build/tools/reputation.py) and composes a short,
spoken-ready confession: each agent owns its worst signal of the week + what its trust
cost was. Writes it to _agent_state/_fleet-confession.md.

IT NEVER SPEAKS. Ultron's speech gate is sacred: Ultron talks ONLY after the mic
trigger. So this script just writes the note; to HEAR it you mic-trigger Ultron and
say "read me the fleet confession" — Ultron reads the note and speaks it (gate open
because you triggered). No autonomous voice, ever.

  python build/tools/fleet_confession.py          # compose + write the note
  python build/tools/fleet_confession.py --print   # also print the spoken paragraph

Cadence: run weekly via cron/launchd (composes silently). Speaking stays on-demand.
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
STATE = VAULT / "_agent_state"
OUT = STATE / "_fleet-confession.md"


def confession_line(sig: dict, R: float) -> str:
    """The agent's worst owned signal this week."""
    if sig.get("quarantined"):
        return f"I had {sig['quarantined']} write{'s' if sig['quarantined'] != 1 else ''} quarantined"
    if sig.get("conf_hold"):
        return f"I tripped the confidentiality gate {sig['conf_hold']} time{'s' if sig['conf_hold'] != 1 else ''}"
    total = sig.get("total", 0)
    if total and not sig.get("promoted"):
        return f"I wrote {total} time{'s' if total != 1 else ''} and got nothing promoted — all held"
    if sig.get("promoted"):
        return f"I promoted {sig['promoted']} clean — no complaints"
    return "I did nothing worth reporting"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true")
    args = ap.parse_args()
    if not STATE.is_dir():
        print(f"ERROR: {STATE} not found", file=sys.stderr); return 2

    agents = []
    tot = {"total": 0, "promoted": 0, "quarantined": 0, "conf_hold": 0}
    for d in sorted(STATE.iterdir()):
        rp = d / "reputation.json"
        if not (d.is_dir() and rp.exists()):
            continue
        try:
            r = json.loads(rp.read_text())
        except json.JSONDecodeError:
            continue
        sig = r.get("signals", {})
        for k in tot:
            tot[k] += sig.get(k, 0)
        agents.append((d.name, float(r.get("R", 0.5)), float(r.get("theta", 0.85)),
                       bool(r.get("pinned")), sig))

    if not agents:
        print("No reputation.json yet — run build/tools/reputation.py first.", file=sys.stderr)
        return 1

    # Worst first (lowest trust), but only agents that actually did something.
    active = [a for a in agents if a[4].get("total", 0) > 0]
    active.sort(key=lambda a: a[1])
    week = datetime.now(timezone.utc).strftime("week of %Y-%m-%d")

    # Spoken-ready paragraph (Ultron reads this on demand).
    spoken = [f"Fleet confession, {week}."]
    for name, R, theta, pinned, sig in active[:5]:
        bar = "bar pinned at point eight five" if pinned else f"bar now {theta:.2f}"
        spoken.append(f"{name} confesses: {confession_line(sig, R)}; trust {R:.2f}, {bar}.")
    spoken.append(
        f"Across the fleet: {tot['total']} writes, {tot['promoted']} promoted, "
        f"{tot['quarantined']} quarantined, {tot['conf_hold']} held for confidentiality.")
    spoken_text = " ".join(spoken)

    # The note (frontmatter + spoken block + full table).
    rows = "\n".join(
        f"| {n} | {R:.3f} | {theta:.2f}{' (pin)' if p else ''} | {s.get('total',0)} | "
        f"{s.get('promoted',0)} | {s.get('quarantined',0)} | {s.get('conf_hold',0)} | {confession_line(s,R)} |"
        for n, R, theta, p, s in sorted(agents, key=lambda a: a[1]))
    note = (
        "---\n"
        "type: fleet-confession\n"
        f"generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"week: \"{week}\"\n"
        "tags: [fleet, confession, reputation]\n"
        "---\n\n"
        f"# 🗣️ Fleet Confession — {week}\n\n"
        "> Composed silently by `fleet_confession.py`. To HEAR it: mic-trigger Ultron and say "
        "\"read me the fleet confession\". Ultron never speaks this unprompted.\n\n"
        "## Spoken brief\n\n"
        f"{spoken_text}\n\n"
        "## Ledger\n\n"
        "| agent | trust R | θ | writes | promoted | quarantined | conf-held | confession |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"{rows}\n"
    )
    OUT.write_text(note, encoding="utf-8")
    print(f"[fleet_confession] wrote {OUT.relative_to(VAULT)} ({len(active)} active / {len(agents)} agents).")
    if args.show:
        print("\n--- spoken ---\n" + spoken_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
