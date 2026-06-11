#!/usr/bin/env python3
"""
self_promote.py — auto-draft /promote-pattern and /skill-update proposals.

Scans all _agent_state/*/memory.json global_patterns for patterns that:
  - confidence >= CONF_THRESHOLD (default 1.0)
  - n_observations >= OBS_THRESHOLD (default 3)
  - promoted_to is null

...and all _agent_state/skill-registry.json skills where:
  - user_corrections >= CORRECTION_THRESHOLD (default 3)
  - last proposal < 7 days ago (1-update/week hard limit)

Emits HELD draft proposals to 00_Inbox/from-dust/self-promote/ with valid
SBAP frontmatter. NEVER auto-promotes. Tony reviews and approves via the
normal /promote-pattern and /skill-update flows.

Usage:
  python3 build/tools/self_promote.py                  # defaults
  python3 build/tools/self_promote.py --obs-threshold 5
  python3 build/tools/self_promote.py --conf-threshold 0.9
  python3 build/tools/self_promote.py --dry-run        # print proposals only, no writes
  python3 build/tools/self_promote.py --quiet          # suppress info, only errors+summary
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAULT = Path(os.environ.get(
    "VAULT",
    os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))
))
AGENT_STATE = VAULT / "_agent_state"
INBOX = VAULT / "00_Inbox" / "from-dust" / "self-promote"
SKILL_REGISTRY = AGENT_STATE / "skill-registry.json"

DEFAULT_OBS_THRESHOLD = 3
DEFAULT_CONF_THRESHOLD = 1.0
DEFAULT_CORRECTION_THRESHOLD = 3

SBAP_VERSION = "1.0"
SOURCE_AGENT = "self-promote"

# Hard limit: never propose a skill update if the last proposal was < 7 days ago.
SKILL_UPDATE_COOLDOWN_DAYS = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def short_run_id() -> str:
    return "self-promote-" + str(uuid.uuid4())[:8]

def sbap_frontmatter(
    output_type: str,
    target_path: str,
    confidence: float,
    extra: Optional[dict] = None,
) -> str:
    """Return SBAP YAML frontmatter block for a held draft."""
    lines = [
        "---",
        f'sbap_version: "{SBAP_VERSION}"',
        f'source_agent: "{SOURCE_AGENT}"',
        f'source_run_id: "{short_run_id()}"',
        f'generated: "{now_iso()}"',
        f'output_type: "{output_type}"',
        f'target_path: "{target_path}"',
        f'confidence: {confidence:.2f}',
        'status: "held"',
        'review_required: true',
        'auto_applied: false',
    ]
    if extra:
        for k, v in extra.items():
            if isinstance(v, str):
                lines.append(f'{k}: "{v}"')
            else:
                lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def write_proposal(path: Path, content: str, dry_run: bool, quiet: bool) -> bool:
    """Write a proposal file. Returns True if written (or would write in dry-run)."""
    if dry_run:
        if not quiet:
            print(f"  [DRY-RUN] would write → {path.relative_to(VAULT)}")
            print("  --- content preview (first 30 lines) ---")
            for line in content.splitlines()[:30]:
                print(f"  {line}")
            print("  ---")
        return True

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not quiet:
            print(f"  [SKIP] proposal already exists: {path.name}")
        return False
    path.write_text(content, encoding="utf-8")
    if not quiet:
        print(f"  [WRITTEN] {path.relative_to(VAULT)}")
    return True


# ---------------------------------------------------------------------------
# Pattern scan
# ---------------------------------------------------------------------------

def scan_patterns(
    obs_threshold: int,
    conf_threshold: float,
    dry_run: bool,
    quiet: bool,
) -> list[dict]:
    """
    Scan all agent memory.json files for unpromoted patterns meeting
    the threshold criteria.

    Returns list of proposal metadata dicts (for the summary report).
    """
    proposals = []

    if not AGENT_STATE.exists():
        print(f"ERROR: _agent_state not found at {AGENT_STATE}", file=sys.stderr)
        return proposals

    for agent_dir in sorted(AGENT_STATE.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        if agent_name.startswith("_"):
            continue

        memory_file = agent_dir / "memory.json"
        if not memory_file.exists():
            continue

        try:
            with open(memory_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [WARN] cannot read {memory_file}: {e}", file=sys.stderr)
            continue

        global_patterns = data.get("global_patterns", [])
        for idx, pattern in enumerate(global_patterns):
            conf = pattern.get("confidence", 0.0)
            n_obs = pattern.get("n_observations", 0)
            promoted_to = pattern.get("promoted_to")
            pattern_text = pattern.get("pattern", "")
            first_seen = pattern.get("first_seen", "unknown")

            # Skip already-promoted entries
            if promoted_to is not None:
                continue

            # Check thresholds
            if conf < conf_threshold or n_obs < obs_threshold:
                continue

            if not quiet:
                print(
                    f"  CANDIDATE: {agent_name}[{idx}] "
                    f"n={n_obs} conf={conf:.2f} "
                    f"pattern={pattern_text[:70]!r}"
                )

            # Build the proposal filename: date-agent-idx.md
            filename = f"{today_str()}-promote-pattern-{agent_name}-{idx}.md"
            target_path = f"_agent_state/canonical/[TYPE_REQUIRED]/{agent_name}_pattern_{idx}.json"

            frontmatter = sbap_frontmatter(
                output_type="promote-pattern-proposal",
                target_path=target_path,
                confidence=conf,
                extra={
                    "source_pattern_agent": agent_name,
                    "source_pattern_index": idx,
                    "n_observations": n_obs,
                    "first_seen": first_seen,
                },
            )

            body = f"""
# Draft /promote-pattern Proposal — {agent_name}[{idx}]

**Status:** HELD — requires Tony approval via `/promote-pattern {agent_name} {idx}`

## Pattern

> {pattern_text}

## Signal

| Field            | Value               |
|------------------|---------------------|
| Agent            | `{agent_name}`      |
| Pattern index    | {idx}               |
| n_observations   | {n_obs}             |
| confidence       | {conf:.2f}          |
| first_seen       | {first_seen}        |
| promoted_to      | null (unpromoted)   |

## Threshold gate

- obs_threshold applied: {obs_threshold} (n={n_obs} >= {obs_threshold} ✓)
- conf_threshold applied: {conf_threshold:.2f} (conf={conf:.2f} >= {conf_threshold:.2f} ✓)
- promoted_to: null ✓ (not yet promoted)

## What to do next

1. Run `/promote-pattern {agent_name} {idx}` in Claude Code.
2. Claude will ask for canonical type + key (see promote-pattern.md for valid types).
3. Approve the resulting JSON block.
4. The pattern will be written to `_agent_state/canonical/<type>/<key>.json` (durable).
5. `promoted_to` on the source pattern will be updated.

## Safety notes

- This proposal was auto-drafted by `self_promote.py` during a nightly brain-refresh run.
- **Nothing has been auto-applied.** `_agent_state/canonical/` and `_Skills/` are unchanged.
- Tony must approve and run `/promote-pattern` explicitly to complete the promotion.
- Auto-applied: false
""".lstrip()

            content = frontmatter + "\n" + body
            proposal_path = INBOX / filename

            written = write_proposal(proposal_path, content, dry_run, quiet)
            if written:
                proposals.append({
                    "type": "promote-pattern",
                    "agent": agent_name,
                    "idx": idx,
                    "n_observations": n_obs,
                    "confidence": conf,
                    "pattern": pattern_text,
                    "filename": filename,
                })

    return proposals


# ---------------------------------------------------------------------------
# Skill-update scan
# ---------------------------------------------------------------------------

def scan_skills(
    correction_threshold: int,
    dry_run: bool,
    quiet: bool,
) -> list[dict]:
    """
    Scan skill-registry.json for skills with user_corrections >= threshold
    and not already proposed within the cooldown window.

    Returns list of proposal metadata dicts.
    """
    proposals = []

    if not SKILL_REGISTRY.exists():
        if not quiet:
            print(f"  [SKIP] skill-registry.json not found at {SKILL_REGISTRY}")
        return proposals

    try:
        with open(SKILL_REGISTRY, encoding="utf-8") as fh:
            registry = json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [WARN] cannot read skill-registry.json: {e}", file=sys.stderr)
        return proposals

    cutoff = datetime.now(timezone.utc) - timedelta(days=SKILL_UPDATE_COOLDOWN_DAYS)

    for skill in registry.get("skills", []):
        slug = skill.get("slug", "")
        corrections = skill.get("user_corrections", 0)
        last_validated = skill.get("last_validated", "")
        version = skill.get("version", "unknown")
        health = skill.get("health_status", "UNKNOWN")

        # Skip library pointers — only slash-commands / claude-skills get updates
        skill_type = skill.get("type", "")
        if skill_type == "skill-library-pointer":
            continue

        if corrections < correction_threshold:
            continue

        # Check cooldown: look for an existing recent proposal in the inbox
        existing_proposals = list(INBOX.glob(f"*skill-update-{slug}.md")) if INBOX.exists() else []
        recent_proposal = False
        for ep in existing_proposals:
            try:
                ep_mtime = datetime.fromtimestamp(ep.stat().st_mtime, tz=timezone.utc)
                if ep_mtime > cutoff:
                    recent_proposal = True
                    if not quiet:
                        print(
                            f"  [SKIP] {slug}: proposal exists within {SKILL_UPDATE_COOLDOWN_DAYS}d "
                            f"cooldown ({ep.name})"
                        )
                    break
            except OSError:
                pass
        if recent_proposal:
            continue

        if not quiet:
            print(
                f"  CANDIDATE: skill/{slug} "
                f"user_corrections={corrections} "
                f"version={version} health={health}"
            )

        filename = f"{today_str()}-skill-update-{slug}.md"
        target_path = f"~/.claude/commands/{slug}.md"

        frontmatter = sbap_frontmatter(
            output_type="skill-update-proposal",
            target_path=target_path,
            confidence=0.85,
            extra={
                "skill_slug": slug,
                "user_corrections": corrections,
                "current_version": version,
                "health_status": health,
            },
        )

        # Gather the per_skill_observations from skill-curator memory if available
        skill_curator_obs = _get_skill_curator_observations(slug)
        obs_block = ""
        if skill_curator_obs:
            obs_block = "\n## Accumulated observations from skill-curator\n\n"
            for o in skill_curator_obs:
                ts = o.get("ts", "unknown")
                text = o.get("observation", "")
                obs_block += f"- [{ts}] {text}\n"

        body = f"""
# Draft /skill-update Proposal — {slug}

**Status:** HELD — requires Tony approval via `/skill-update {slug}`

## Signal

| Field              | Value               |
|--------------------|---------------------|
| Skill slug         | `{slug}`            |
| Type               | `{skill_type}`      |
| Current version    | `{version}`         |
| user_corrections   | {corrections}       |
| health_status      | `{health}`          |
| last_validated     | {last_validated}    |

## Threshold gate

- correction_threshold applied: {correction_threshold} (user_corrections={corrections} >= {correction_threshold} ✓)
- 1-update/week cooldown: no recent proposal found ✓
{obs_block}
## What to do next

1. Run `/skill-update {slug}` in Claude Code.
2. Claude will show the accumulated corrections and propose a diff.
3. Approve / edit / reject the proposed changes.
4. On approval, the skill is atomically updated and archived.

## Safety notes

- This proposal was auto-drafted by `self_promote.py` during a nightly brain-refresh run.
- **Nothing has been auto-applied.** `~/.claude/commands/{slug}.md` and `_Skills/` are unchanged.
- Tony must approve and run `/skill-update` explicitly to complete the update.
- The 1-update/week hard limit is enforced; no proposal will be re-emitted for this skill
  within {SKILL_UPDATE_COOLDOWN_DAYS} days of this proposal.
- Auto-applied: false
""".lstrip()

        content = frontmatter + "\n" + body
        proposal_path = INBOX / filename

        written = write_proposal(proposal_path, content, dry_run, quiet)
        if written:
            proposals.append({
                "type": "skill-update",
                "slug": slug,
                "user_corrections": corrections,
                "version": version,
                "filename": filename,
            })

    return proposals


def _get_skill_curator_observations(slug: str) -> list[dict]:
    """Return per_skill_observations for a slug from skill-curator memory, if available."""
    sc_memory = AGENT_STATE / "skill-curator" / "memory.json"
    if not sc_memory.exists():
        return []
    try:
        with open(sc_memory, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("per_skill_observations", {}).get(slug, [])
    except (json.JSONDecodeError, OSError):
        return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-draft /promote-pattern and /skill-update proposals (HELD, never auto-applied)."
    )
    parser.add_argument(
        "--obs-threshold", type=int, default=DEFAULT_OBS_THRESHOLD,
        help=f"Min n_observations to emit a promote-pattern proposal (default {DEFAULT_OBS_THRESHOLD})"
    )
    parser.add_argument(
        "--conf-threshold", type=float, default=DEFAULT_CONF_THRESHOLD,
        help=f"Min confidence to emit a promote-pattern proposal (default {DEFAULT_CONF_THRESHOLD:.1f})"
    )
    parser.add_argument(
        "--correction-threshold", type=int, default=DEFAULT_CORRECTION_THRESHOLD,
        help=f"Min user_corrections to emit a skill-update proposal (default {DEFAULT_CORRECTION_THRESHOLD})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print proposals to stdout without writing files"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress info output; only errors and final summary"
    )
    args = parser.parse_args()

    if not args.quiet:
        print(f"self_promote.py — {now_iso()}")
        print(f"  vault:              {VAULT}")
        print(f"  obs_threshold:      {args.obs_threshold}")
        print(f"  conf_threshold:     {args.conf_threshold:.2f}")
        print(f"  correction_thresh:  {args.correction_threshold}")
        print(f"  dry_run:            {args.dry_run}")
        print()

    # Ensure inbox exists (unless dry-run)
    if not args.dry_run:
        INBOX.mkdir(parents=True, exist_ok=True)

    # --- Pattern proposals ---
    if not args.quiet:
        print("=== Scanning agent patterns (promote-pattern candidates) ===")
    pattern_proposals = scan_patterns(
        obs_threshold=args.obs_threshold,
        conf_threshold=args.conf_threshold,
        dry_run=args.dry_run,
        quiet=args.quiet,
    )

    # --- Skill-update proposals ---
    if not args.quiet:
        print()
        print("=== Scanning skill registry (skill-update candidates) ===")
    skill_proposals = scan_skills(
        correction_threshold=args.correction_threshold,
        dry_run=args.dry_run,
        quiet=args.quiet,
    )

    # --- Summary ---
    total = len(pattern_proposals) + len(skill_proposals)
    print()
    print(f"=== self_promote.py summary ===")
    print(f"  promote-pattern proposals:  {len(pattern_proposals)}")
    for p in pattern_proposals:
        print(f"    - {p['agent']}[{p['idx']}]  n={p['n_observations']}  conf={p['confidence']:.2f}  {p['pattern'][:60]!r}")
    print(f"  skill-update proposals:     {len(skill_proposals)}")
    for s in skill_proposals:
        print(f"    - {s['slug']}  corrections={s['user_corrections']}  v{s['version']}")
    print(f"  total proposals emitted:    {total}")
    if args.dry_run:
        print("  mode: DRY-RUN (no files written)")
    else:
        print(f"  proposals inbox:            {INBOX.relative_to(VAULT)}")
        print("  status: HELD — no auto-application; Tony must approve")
    print()
    print("  SAFETY CHECK:")
    print("    _agent_state/canonical/ — NOT modified")
    print("    _Skills/ — NOT modified")
    print("    agent memory.json files — NOT modified")
    print("    promoted_to fields — NOT modified")
    print("    skill-registry.json — NOT modified")

    return 0 if True else 1


if __name__ == "__main__":
    sys.exit(main())
