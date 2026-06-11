#!/usr/bin/env python3
"""reputation.py — per-agent trust ledger for the SBAP fleet.

Computes a reputation score R per Dust agent from the signals available in
_agent_state/<agent>/writes.jsonl, and a FLOATING auto-promote threshold theta
that replaces the static 0.85 cutoff: proven agents clear lower, sloppy agents
must clear higher.

HONEST LIMITATION (verified 2026-06-08): writes.jsonl records TRIAGE decisions
(promoted / quarantined / hold-*), NOT human accept/reject. So ground truth is
weak — most agents are all-holds (e.g. block-curator: 91 writes, 0 promoted).
We therefore: (a) treat only `promoted` (good) and `quarantined` /
`hold-needs-confidentiality-guard` (bad) as labels; holds are NEUTRAL (no
signal); (b) PIN theta to the static 0.85 until n>=10 labeled outcomes, so a
sparse ledger never swings the gate. The real human accept/reject signal should
be captured going forward (e.g. from /dust-resolve) — see record_resolution.py.

RECOVERY RAMP (added 2026-06-09):
  The trust system must be recoverable — punishment alone is a dead end.
  clean_streak: count of consecutive human ACCEPT decisions (from /dust-resolve).
  At clean_streak >= RECOVERY_STREAK_THRESHOLD (5): theta steps DOWN by THETA_STEP
  (0.05) toward STATIC floor (0.85), alpha nudged up by RECOVERY_ALPHA_NUDGE (1.0),
  and clean_streak resets to 0. Any REJECT/trip resets streak to 0 AND re-raises
  theta by THETA_STEP (minimum cap: STATIC, maximum cap: 0.95). Ramp state is
  persisted in reputation.json under a `recovery` key.

Run from vault root:  python build/tools/reputation.py [--all | <agent>]
Writes _agent_state/<agent>/reputation.json and prints a fleet table.

To record a clean accept (called by /dust-resolve):
  python build/tools/reputation.py --record-accept <agent>

To record a trip/reject (called by /dust-resolve):
  python build/tools/reputation.py --record-trip <agent>
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
STATE = VAULT / "_agent_state"
HALF_LIFE_DAYS = 30.0
PRIOR_A, PRIOR_B = 2.0, 2.0      # Beta(2,2) — neutral, weakly-informative
MIN_LABELED = 10                  # below this, theta is pinned to STATIC
STATIC = 0.85                     # floor — theta never decays below this
THETA_MAX = 0.95                  # ceiling — theta never rises above this

# Recovery ramp constants
RECOVERY_STREAK_THRESHOLD = 5    # consecutive clean accepts before theta steps down
THETA_STEP = 0.05                # theta decay / raise per streak completion / trip
RECOVERY_ALPHA_NUDGE = 1.0       # alpha bonus added when streak threshold is met

GOOD = {"promoted"}
BAD = {"quarantined": 1.0, "hold-needs-confidentiality-guard": 0.5}  # weight toward beta
# everything else (hold-review-only, hold-low-confidence, …) = NEUTRAL (no label)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load_reputation(agent: str) -> dict:
    """Load existing reputation.json for an agent, or return empty dict."""
    path = STATE / agent / "reputation.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_reputation(agent: str, rep: dict, dry_run: bool = False) -> None:
    path = STATE / agent / "reputation.json"
    if dry_run:
        print(f"DRY-RUN would write → {path}: R={rep['R']:.3f} theta={rep['theta']:.3f} streak={rep.get('recovery', {}).get('clean_streak', 0)}")
        return
    path.write_text(json.dumps(rep, indent=2), encoding="utf-8")


def compute_reputation(writes_path: Path, existing: dict | None = None) -> dict:
    """Compute R/theta from writes.jsonl. Preserves existing `recovery` block."""
    now = utcnow()
    a, b = PRIOR_A, PRIOR_B
    sig = {"promoted": 0, "quarantined": 0, "conf_hold": 0, "neutral_hold": 0, "total": 0}
    for line in writes_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        sig["total"] += 1
        action = rec.get("action", "")
        ts = parse_ts(rec.get("ts", ""))
        age_days = ((now - ts).total_seconds() / 86400.0) if ts else 0.0
        w = 0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS)   # recency half-life
        if action in GOOD:
            a += w; sig["promoted"] += 1
        elif action in BAD:
            b += w * BAD[action]
            sig["quarantined" if action == "quarantined" else "conf_hold"] += 1
        else:
            sig["neutral_hold"] += 1

    n = sig["promoted"] + sig["quarantined"] + sig["conf_hold"]   # labeled outcomes
    if n < MIN_LABELED:
        base_theta = STATIC                                       # not enough ground truth → static
    else:
        base_theta = min(THETA_MAX, max(STATIC, STATIC - 0.30 * (R_from_ab(a, b) - 0.5)))

    # Apply recovery ramp overlay: if existing rep has a recovery.theta_override, use it
    recovery = (existing or {}).get("recovery", {
        "clean_streak": 0,
        "theta_override": None,
        "last_streak_reset": None,
    })
    theta_override = recovery.get("theta_override")
    # theta = max of base_theta and override (recovery can only push theta down from trip-raised values)
    if theta_override is not None:
        theta = max(base_theta, theta_override)
    else:
        theta = base_theta

    # Apply the alpha nudge from recovery (accumulated from streak milestones)
    recovery_alpha_bonus = recovery.get("alpha_bonus", 0.0)
    a_final = a + recovery_alpha_bonus

    R = R_from_ab(a_final, b)

    result = {
        "alpha": round(a_final, 4), "beta": round(b, 4), "R": round(R, 4),
        "n_labeled": n, "theta": round(theta, 4), "pinned": n < MIN_LABELED,
        "signals": sig, "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recovery": recovery,
    }
    return result


def R_from_ab(a: float, b: float) -> float:
    return a / (a + b)


def record_accept(agent: str, dry_run: bool = False) -> dict:
    """Record one clean accept: increment clean_streak; if >= threshold, step theta down."""
    existing = load_reputation(agent)

    # Snapshot old recovery state BEFORE mutating anything
    old_recovery = existing.get("recovery", {})
    old_alpha_bonus = old_recovery.get("alpha_bonus", 0.0)

    # Build a fresh mutable copy of recovery
    recovery = {
        "clean_streak": old_recovery.get("clean_streak", 0),
        "theta_override": old_recovery.get("theta_override"),
        "last_streak_reset": old_recovery.get("last_streak_reset"),
        "alpha_bonus": old_alpha_bonus,
    }

    recovery["clean_streak"] += 1
    now_str = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Current effective theta: prefer the ramp override, else the stored theta, else STATIC
    theta_before = recovery["theta_override"] if recovery["theta_override"] is not None \
        else existing.get("theta", STATIC)
    streak_completed = False
    new_theta_value = theta_before  # updated only on milestone

    if recovery["clean_streak"] >= RECOVERY_STREAK_THRESHOLD:
        # Streak threshold met: step theta down toward STATIC floor
        new_theta_value = max(STATIC, theta_before - THETA_STEP)
        recovery["theta_override"] = round(new_theta_value, 4) if new_theta_value > STATIC else None
        # Nudge R by boosting alpha
        recovery["alpha_bonus"] = round(old_alpha_bonus + RECOVERY_ALPHA_NUDGE, 4)
        # Reset streak
        recovery["clean_streak"] = 0
        recovery["last_streak_reset"] = now_str
        streak_completed = True

    # Update existing with new recovery state
    existing["recovery"] = recovery
    existing["updated"] = now_str

    if streak_completed:
        # Apply alpha nudge delta and recompute R
        existing["alpha"] = round(existing.get("alpha", PRIOR_A) + RECOVERY_ALPHA_NUDGE, 4)
        existing["R"] = round(R_from_ab(existing["alpha"], existing.get("beta", PRIOR_B)), 4)
        # Update theta in the top-level field
        existing["theta"] = round(new_theta_value, 4)

    action = "streak_complete → theta_step_down" if streak_completed else f"streak {recovery['clean_streak']}/{RECOVERY_STREAK_THRESHOLD}"
    print(f"[record_accept] {agent}: clean_streak={recovery['clean_streak']} | theta={existing.get('theta', STATIC):.3f} | R={existing['R']:.3f} | {action}")

    save_reputation(agent, existing, dry_run=dry_run)

    # Sync reputation key to memory.json (ADD only — never touch global_patterns)
    sync_memory_reputation(agent, recovery, dry_run=dry_run)

    return existing


def record_trip(agent: str, dry_run: bool = False) -> dict:
    """Record a trip/reject: reset streak to 0 and re-raise theta by THETA_STEP."""
    existing = load_reputation(agent)
    recovery = existing.get("recovery", {
        "clean_streak": 0,
        "theta_override": None,
        "last_streak_reset": None,
        "alpha_bonus": 0.0,
    })
    recovery.setdefault("clean_streak", 0)
    recovery.setdefault("theta_override", None)
    recovery.setdefault("last_streak_reset", None)
    recovery.setdefault("alpha_bonus", 0.0)

    streak_before = recovery["clean_streak"]
    theta_before = recovery.get("theta_override") or existing.get("theta", STATIC)

    # Reset streak
    recovery["clean_streak"] = 0
    # Re-raise theta (capped at THETA_MAX)
    new_theta = min(THETA_MAX, theta_before + THETA_STEP)
    recovery["theta_override"] = round(new_theta, 4)

    now_str = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    existing["recovery"] = recovery
    existing["updated"] = now_str
    existing["theta"] = recovery["theta_override"]

    print(f"[record_trip] {agent}: streak reset (was {streak_before}) | theta {theta_before:.3f} → {new_theta:.3f}")

    save_reputation(agent, existing, dry_run=dry_run)
    sync_memory_reputation(agent, recovery, dry_run=dry_run)

    return existing


def sync_memory_reputation(agent: str, recovery: dict, dry_run: bool = False) -> None:
    """Write the `reputation` key into memory.json — ADD only, never touch other keys."""
    memory_path = STATE / agent / "memory.json"
    if not memory_path.exists():
        return
    try:
        mem = json.loads(memory_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    mem["reputation"] = {
        "clean_streak": recovery.get("clean_streak", 0),
        "streak_threshold": RECOVERY_STREAK_THRESHOLD,
        "theta_override": recovery.get("theta_override"),
        "alpha_bonus": recovery.get("alpha_bonus", 0.0),
        "last_updated": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if dry_run:
        print(f"DRY-RUN would update memory.json for {agent}: reputation={mem['reputation']}")
        return
    memory_path.write_text(json.dumps(mem, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("agent", nargs="?", help="single agent (default: all)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write reputation.json")
    ap.add_argument("--record-accept", metavar="AGENT",
                    help="record one clean /dust-resolve accept for AGENT (increments clean_streak)")
    ap.add_argument("--record-trip", metavar="AGENT",
                    help="record a trip/reject for AGENT (resets streak + raises theta)")
    args = ap.parse_args()

    if not STATE.is_dir():
        print(f"ERROR: {STATE} not found", file=sys.stderr); return 2

    # Sub-commands: record-accept / record-trip
    if args.record_accept:
        ag = args.record_accept
        if not (STATE / ag).is_dir():
            print(f"ERROR: unknown agent '{ag}' (no {STATE / ag})", file=sys.stderr); return 2
        record_accept(ag, dry_run=args.dry_run)
        return 0

    if args.record_trip:
        ag = args.record_trip
        if not (STATE / ag).is_dir():
            print(f"ERROR: unknown agent '{ag}' (no {STATE / ag})", file=sys.stderr); return 2
        record_trip(ag, dry_run=args.dry_run)
        return 0

    # Default: (re-)compute reputation from writes.jsonl for all or one agent
    agents = ([args.agent] if args.agent else
              sorted(d.name for d in STATE.iterdir()
                     if d.is_dir() and (d / "writes.jsonl").exists()))
    rows = []
    for ag in agents:
        wp = STATE / ag / "writes.jsonl"
        if not wp.exists():
            print(f"  (skip {ag}: no writes.jsonl)"); continue
        existing = load_reputation(ag)
        rep = compute_reputation(wp, existing=existing)
        rows.append((ag, rep))
        if not args.dry_run:
            save_reputation(ag, rep)

    rows.sort(key=lambda r: r[1]["R"], reverse=True)
    print(f"\n{'agent':<26} {'R':>6} {'theta':>6} {'n':>4}  {'prom':>4} {'quar':>4} {'conf':>4} {'hold':>4}  {'streak':>6}  pin")
    print("-" * 86)
    for ag, r in rows:
        s = r["signals"]
        streak = r.get("recovery", {}).get("clean_streak", 0)
        print(f"{ag:<26} {r['R']:>6.3f} {r['theta']:>6.3f} {r['n_labeled']:>4}  "
              f"{s['promoted']:>4} {s['quarantined']:>4} {s['conf_hold']:>4} {s['neutral_hold']:>4}  "
              f"{streak:>6}  {'PIN' if r['pinned'] else ''}")
    print(f"\n{len(rows)} agents. theta is the floating auto-promote threshold "
          f"(pinned to {STATIC} until {MIN_LABELED} labeled outcomes).\n"
          f"Recovery ramp: {RECOVERY_STREAK_THRESHOLD} consecutive /dust-resolve accepts "
          f"→ theta steps down {THETA_STEP:.2f} toward {STATIC} floor; "
          f"any trip resets streak + raises theta {THETA_STEP:.2f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
