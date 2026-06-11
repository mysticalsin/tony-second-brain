#!/usr/bin/env python3
"""portal_watcher.py — Procurement portal change detector (wired stub).

Watches opted-in procurement portals for page changes (addenda, Q&A updates,
deadline changes) and writes dated alert notes to Important/ on change.

STATUS: wired stub.  No portals are opted-in by default.  Credentials are
NEVER stored in config or code — they are fetched from macOS keychain only.
Playwright is imported lazily; the stub runs fine without it installed.

Config: 99_Meta/config/portal-watch.json
  { "portals": [...], "poll_hours": 6 }

Snapshots: _agent_state/portal-watcher/snapshots/<slug>.json
Alerts:    Important/<YYYY-MM-DD>-portal-<slug>-change.md

Usage (from vault root):
    python build/tools/portal_watcher.py
    python build/tools/portal_watcher.py --dry-run
    python build/tools/portal_watcher.py --config /path/to/override.json

Flags:
    --dry-run       Validate config, print per-portal plan, write nothing.
    --config PATH   Override default config path.
    --no-llm        Implied always (tool never calls LLM; flag accepted for
                    pipeline consistency).

Environment:
    NO_LLM=1        Same as --no-llm (accepted, no effect — no LLM used).
    VAULT           Override vault root (default: current working directory).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
CONFIG_PATH = VAULT / "99_Meta" / "config" / "portal-watch.json"
SNAPSHOT_DIR = VAULT / "_agent_state" / "portal-watcher" / "snapshots"
ALERT_DIR = VAULT / "Important"
ESCALATION_DIR = VAULT / "Important" / "escalations"

# Minimum seconds between polls per portal (1 hour) — enforced via snapshot mtime.
MIN_POLL_INTERVAL_S = 3600


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slugify(s: str) -> str:
    """Simple ASCII slug (mirrors rfp_pipeline.py convention)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48]


def log(msg: str) -> None:
    """Timestamped stderr log line."""
    print(f"[{utcnow()}] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict[str, Any]:
    """Load and validate portal-watch.json.  Raises ValueError on bad schema."""
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open() as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("Config must be a JSON object.")
    if "portals" not in cfg:
        raise ValueError("Config missing required key 'portals'.")
    if not isinstance(cfg["portals"], list):
        raise ValueError("'portals' must be a JSON array.")
    for i, portal in enumerate(cfg["portals"]):
        for key in ("name", "url", "opt_in"):
            if key not in portal:
                raise ValueError(f"Portal [{i}] missing required key '{key}'.")
    if "poll_hours" not in cfg:
        cfg["poll_hours"] = 6  # sensible default
    return cfg


# --------------------------------------------------------------------------- #
# Keychain credential lookup (macOS only)
# --------------------------------------------------------------------------- #

def fetch_keychain_credential(service_name: str) -> str | None:
    """Return password from macOS keychain for 'service_name', or None if missing."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except FileNotFoundError:
        # 'security' CLI not available (non-macOS or CI)
        return None
    except subprocess.TimeoutExpired:
        log(f"Keychain lookup timed out for service '{service_name}'.")
        return None


# --------------------------------------------------------------------------- #
# Snapshot diffing
# --------------------------------------------------------------------------- #

def normalize_text(text: str) -> str:
    """Strip excess whitespace for stable hashing."""
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


def unified_diff(old_text: str, new_text: str) -> str:
    """Return a unified diff string (no subprocess — pure Python difflib)."""
    import difflib
    old_lines = normalize_text(old_text).splitlines(keepends=True)
    new_lines = normalize_text(new_text).splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="previous", tofile="current",
        lineterm="",
    ))
    return "".join(diff_lines)


def detect_change(old_text: str, new_text: str) -> tuple[bool, str]:
    """Return (changed: bool, diff: str)."""
    if text_hash(old_text) == text_hash(new_text):
        return False, ""
    diff = unified_diff(old_text, new_text)
    return True, diff


# --------------------------------------------------------------------------- #
# Snapshot persistence
# --------------------------------------------------------------------------- #

def snapshot_path(portal_slug: str) -> Path:
    return SNAPSHOT_DIR / f"{portal_slug}.json"


def load_snapshot(portal_slug: str) -> dict[str, Any] | None:
    p = snapshot_path(portal_slug)
    if not p.exists():
        return None
    with p.open() as f:
        return json.load(f)


def save_snapshot(portal_slug: str, text: str, dry_run: bool) -> None:
    if dry_run:
        log(f"[dry-run] would write snapshot for '{portal_slug}'")
        return
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "portal_slug": portal_slug,
        "captured_at": utcnow(),
        "text_hash": text_hash(text),
        "text": normalize_text(text),
    }
    snapshot_path(portal_slug).write_text(json.dumps(data, indent=2))


def mtime_seconds(portal_slug: str) -> float | None:
    """Return mtime of the snapshot file, or None if absent."""
    p = snapshot_path(portal_slug)
    if not p.exists():
        return None
    return p.stat().st_mtime


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

def is_rate_limited(portal_slug: str) -> bool:
    """Return True if the portal was polled less than MIN_POLL_INTERVAL_S ago."""
    mt = mtime_seconds(portal_slug)
    if mt is None:
        return False
    elapsed = time.time() - mt
    return elapsed < MIN_POLL_INTERVAL_S


# --------------------------------------------------------------------------- #
# Playwright fetch (lazy import — guarded)
# --------------------------------------------------------------------------- #

def fetch_page_text(url: str, credential: str | None = None) -> str:
    """Fetch page text via Playwright.  Raises ImportError if not installed."""
    try:
        from playwright.sync_api import sync_playwright  # lazy import
    except ImportError:
        raise ImportError(
            "Playwright is not installed.  Install it with:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        # If a credential (cookie/token) was retrieved, set as a header.
        # Real auth logic would be portal-specific; the stub passes it as
        # a generic bearer token so the wiring is in place.
        if credential:
            context = browser.new_context(
                extra_http_headers={"Authorization": f"Bearer {credential}"}
            )
        page = context.new_page()
        page.goto(url, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        text = page.inner_text("body")
        browser.close()
    return text


# --------------------------------------------------------------------------- #
# Alert writing
# --------------------------------------------------------------------------- #

def write_alert(portal: dict[str, Any], diff: str, dry_run: bool) -> Path:
    """Write a dated alert note to Important/.  Returns the target path."""
    slug = slugify(portal["name"])
    filename = f"{today()}-portal-{slug}-change.md"
    target = ALERT_DIR / filename

    content = f"""---
sbap_version: "1.0"
source_agent: portal-watcher
generated: "{utcnow()}"
output_type: portal_change_alert
target_path: "Important/{filename}"
confidence: 0.90
needs_review: true
---

# Portal change: {portal['name']}

> **Detected:** {utcnow()}
> **URL:** {portal['url']}
> **Watch events:** {', '.join(portal.get('watch', []))}

## What changed

```diff
{diff[:4000]}
```

## Action required

Review the portal directly to confirm whether this is a material change
(addendum, deadline shift, Q&A update).  Acknowledge by deleting or archiving
this note.
"""

    if dry_run:
        log(f"[dry-run] would write alert: {target}")
        print(content)
        return target

    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    log(f"Alert written: {target}")
    return target


def write_escalation(portal: dict[str, Any], reason: str, dry_run: bool) -> None:
    """Write a keychain-missing escalation to Important/escalations/ (or stdout in dry-run)."""
    slug = slugify(portal["name"])
    filename = f"{today()}-portal-watcher-{slug}-credential-missing.md"
    content = (
        f"# Portal Watcher — credential missing: {portal['name']}\n\n"
        f"> **Generated:** {utcnow()}\n"
        f"> **Portal:** {portal['name']}\n"
        f"> **URL:** {portal['url']}\n\n"
        f"## Problem\n\n{reason}\n\n"
        "## Fix\n\n"
        f"Add a macOS keychain item with service name `{portal.get('credential_ref', '(not set)')}` "
        "containing the portal credential:\n\n"
        f"    security add-generic-password -s \"{portal.get('credential_ref', '')}\" "
        "-a portal-watcher -w\n"
    )
    if dry_run:
        log(f"[dry-run] escalation for '{portal['name']}': {reason}")
        print(content)
        return
    ESCALATION_DIR.mkdir(parents=True, exist_ok=True)
    target = ESCALATION_DIR / filename
    target.write_text(content)
    log(f"Escalation written: {target}")


# --------------------------------------------------------------------------- #
# Per-portal processing
# --------------------------------------------------------------------------- #

def process_portal(portal: dict[str, Any], dry_run: bool) -> None:
    """Run the watch cycle for a single opted-in portal."""
    name = portal["name"]
    url = portal["url"]
    slug = slugify(name)
    credential_ref = portal.get("credential_ref", "")

    log(f"Processing portal: {name} ({url})")

    # Rate limit check
    if is_rate_limited(slug):
        mt = mtime_seconds(slug)
        wait = int(MIN_POLL_INTERVAL_S - (time.time() - mt))  # type: ignore[operator]
        log(f"  Rate limited — {wait}s until next allowed poll. Skipping.")
        return

    # Credential lookup
    credential: str | None = None
    if credential_ref:
        credential = fetch_keychain_credential(credential_ref)
        if credential is None:
            reason = (
                f"Keychain item '{credential_ref}' not found. "
                "Portal is opted-in but credentials are missing."
            )
            log(f"  SKIP: {reason}")
            write_escalation(portal, reason, dry_run)
            return
    else:
        log("  No credential_ref set — attempting unauthenticated fetch.")

    # Fetch
    if dry_run:
        log(f"  [dry-run] would fetch {url}")
        return

    try:
        new_text = fetch_page_text(url, credential)
    except ImportError as exc:
        log(f"  SKIP: {exc}")
        return
    except Exception as exc:
        log(f"  Fetch failed for '{name}': {exc}")
        return

    # Diff against snapshot
    snapshot = load_snapshot(slug)
    if snapshot is None:
        log(f"  No prior snapshot — recording baseline.")
        save_snapshot(slug, new_text, dry_run=False)
        return

    changed, diff = detect_change(snapshot["text"], new_text)
    if not changed:
        log(f"  No change detected.")
        save_snapshot(slug, new_text, dry_run=False)
        return

    log(f"  Change detected — writing alert.")
    write_alert(portal, diff, dry_run)
    save_snapshot(slug, new_text, dry_run=False)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Procurement portal change detector (wired stub).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate config and print per-portal plan; write nothing.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"Override config path (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Accepted for pipeline consistency; this tool never calls an LLM.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Load config
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        log(f"Config error: {exc}")
        return 1

    portals = cfg["portals"]
    opted_in = [p for p in portals if p.get("opt_in") is True]

    # Hard gate: no opted-in portals → validate only, exit 0
    if not opted_in:
        total = len(portals)
        log(f"0 portals opted in ({total} defined). Nothing to watch.")
        print(f"0 portals opted in — portal_watcher is in stub mode. "
              f"Set opt_in: true in {args.config} to activate watching.")
        return 0

    log(f"{len(opted_in)}/{len(portals)} portal(s) opted in.")

    if args.dry_run:
        for portal in opted_in:
            slug = slugify(portal["name"])
            cref = portal.get("credential_ref") or "(none)"
            watch = ", ".join(portal.get("watch", [])) or "(none)"
            rate_ok = "ready" if not is_rate_limited(slug) else "rate-limited"
            print(
                f"  Portal: {portal['name']}\n"
                f"    URL:            {portal['url']}\n"
                f"    credential_ref: {cref}\n"
                f"    watch:          {watch}\n"
                f"    poll status:    {rate_ok}\n"
            )
        print(f"[dry-run] {len(opted_in)} portal(s) would be polled. No writes.")
        return 0

    for portal in opted_in:
        process_portal(portal, dry_run=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
