#!/usr/bin/env python3
"""graph_sync.py — Phase 3.1
Pull email + calendar from Microsoft Graph into the vault.

Auth: device-code flow via msal. First run prompts Tony to open a URL on his
phone and type a code; token caches at ~/AI-Brain-build/.graph_token.json with
silent refresh on subsequent runs.

Writes:
  00_Inbox/from-graph/email/<msg-id>.md     unread emails (SBAP frontmatter)
  _brain_api/calendar/today.json            today's events
  _brain_api/calendar/tomorrow.json         tomorrow's events
  _brain_api/calendar/week.json             next 7 days

Graceful no-op if msal is not installed (logs the pip install command).

Usage:
    python3 build/tools/graph_sync.py            # full sync
    python3 build/tools/graph_sync.py --auth     # interactive device-code auth
    python3 build/tools/graph_sync.py --dry-run  # print, don't write
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))
TOKEN_CACHE = Path.home() / "AI-Brain-build" / ".graph_token.json"
LOG_DIR = Path.home() / "AI-Brain-build" / "logs"

try:
    import msal  # type: ignore
except ImportError:
    msal = None


def log(msg: str, level: str = "INFO") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] [{level}] graph_sync: {msg}\n"
    with (LOG_DIR / f"graph-sync-{datetime.now().strftime('%Y-%m-%d')}.log").open("a") as f:
        f.write(line)
    if sys.stderr.isatty():
        sys.stderr.write(line)


def load_config(vault: Path) -> dict:
    # Prefer a LOCAL (non-CloudStorage) mirror — under launchd the system Python is TCC-blocked
    # from reading OneDrive CloudStorage (per-binary privacy), so the vault path raises
    # PermissionError and the graph silently never syncs. The mirror at ~/.config/ai-brain/ is
    # always readable. Falls back to the vault copy (works when run interactively / with FDA).
    # Keep the mirror current: re-copy 99_Meta/config/graph-config.json there after any change.
    local = Path.home() / ".config" / "ai-brain" / "graph-config.json"
    for cand in (local, vault / "99_Meta" / "config" / "graph-config.json"):
        try:
            if cand.exists():
                return json.loads(cand.read_text())
        except OSError as e:
            log(f"config unreadable at {cand} ({e.__class__.__name__}: {e}); "
                "grant Full Disk Access to the launchd Python, or use the ~/.config/ai-brain mirror", "WARN")
    return {}


def load_token_cache() -> str | None:
    if not TOKEN_CACHE.exists():
        return None
    try:
        return TOKEN_CACHE.read_text()
    except OSError:
        return None


def save_token_cache(serialized: str) -> None:
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(serialized)
    try:
        TOKEN_CACHE.chmod(0o600)  # token = secret
    except OSError:
        pass


def build_msal_app(cfg: dict) -> "msal.PublicClientApplication":
    cache = msal.SerializableTokenCache()
    cached = load_token_cache()
    if cached:
        cache.deserialize(cached)
    app = msal.PublicClientApplication(
        client_id=cfg["client_id"],
        authority=f"https://login.microsoftonline.com/{cfg.get('tenant_id', 'common')}",
        token_cache=cache,
    )
    return app, cache


def acquire_token(app, cache, scopes: list[str], interactive: bool = False) -> str | None:
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
        if result and "access_token" in result:
            if cache.has_state_changed:
                save_token_cache(cache.serialize())
            return result["access_token"]

    if not interactive:
        log("no cached token and not in interactive mode — run with --auth", "WARN")
        return None

    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        log(f"device flow init failed: {flow}", "ERROR")
        return None

    print("\n══════════════════════════════════════════════════════════════")
    print(f"OPEN ON YOUR PHONE: {flow['verification_uri']}")
    print(f"CODE TO TYPE:       {flow['user_code']}")
    print("══════════════════════════════════════════════════════════════\n")
    print(f"(Expires in {flow.get('expires_in', 900)}s)")

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        save_token_cache(cache.serialize())
        return result["access_token"]
    log(f"device flow failed: {result.get('error_description', result)}", "ERROR")
    return None


def graph_get(token: str, url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", s.strip())
    return s.strip("-").lower()[:60] or "item"


def sync_emails(token: str, vault: Path, cfg: dict, dry_run: bool) -> int:
    cap = cfg.get("max_unread_emails_per_sync", 20)
    url = (f"https://graph.microsoft.com/v1.0/me/messages"
           f"?$filter=isRead eq false&$top={cap}"
           f"&$select=id,subject,from,receivedDateTime,bodyPreview,webLink,toRecipients")

    try:
        data = graph_get(token, url)
    except urllib.error.HTTPError as e:
        log(f"email fetch HTTP {e.code}: {e.reason}", "WARN")
        return 0
    except urllib.error.URLError as e:
        log(f"email fetch network error: {e.reason}", "WARN")
        return 0

    out_dir = vault / "00_Inbox" / "from-graph" / "email"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    bl = cfg.get("email_domain_blocklist") or []
    al = cfg.get("email_domain_allowlist") or []
    written = 0

    for msg in data.get("value", []):
        sender = (msg.get("from") or {}).get("emailAddress", {})
        addr = sender.get("address", "")
        domain = addr.split("@")[-1].lower() if "@" in addr else ""

        # Allowlist takes precedence
        if al and domain not in al:
            continue
        if bl and domain in bl:
            continue

        msg_id = msg["id"]
        subj = msg.get("subject", "(no subject)")
        slug = slugify(subj)[:50]
        received = msg.get("receivedDateTime", "")
        fname = f"{received[:10]}-{slug}.md"

        # Pre-compute escaped values OUTSIDE the f-string — Python 3.9's f-string
        # parser refuses backslashes in expression parts.
        subj_escaped = subj.replace('"', '\\"')
        gen_iso = datetime.now(timezone.utc).isoformat()
        weblink = msg.get('webLink', '')

        body = f"""---
sbap_version: "1.0"
source_agent: claude-code
source_run_id: graph-email-{msg_id[:16]}
generated: "{gen_iso}"
input_context_refs:
  - "graph:/me/messages/{msg_id}"
output_type: inbound_email
target_path: ""
confidence: 1.0
needs_review: true
reasoning_summary: |
  Inbound email synced from Microsoft Graph. Original message id {msg_id}.
email_metadata:
  from: "{addr}"
  subject: "{subj_escaped}"
  received: "{received}"
  web_link: "{weblink}"
  thread_id: ""
  in_reply_to: ""
---

# {subj}

**From:** {sender.get('name', '?')} <{addr}>
**Received:** {received}
**Open in Outlook:** {msg.get('webLink', '')}

## Preview

{msg.get('bodyPreview', '')[:1000]}

---

*Sync'd by `build/tools/graph_sync.py`. The `email-responder` agent can read this and draft a reply into `00_Inbox/from-dust/email-responder/`.*
"""
        target = out_dir / fname
        if dry_run:
            print(f"[dry-run] would write {target.relative_to(vault)}")
        else:
            if not target.exists():  # idempotent
                target.write_text(body)
                written += 1
    return written


def sync_calendar(token: str, vault: Path, cfg: dict, dry_run: bool) -> dict:
    today = datetime.now(timezone.utc).date()
    days_ahead = cfg.get("max_calendar_days_lookahead", 7)
    start = today.isoformat() + "T00:00:00Z"
    end = (today + timedelta(days=days_ahead)).isoformat() + "T23:59:59Z"

    url = (f"https://graph.microsoft.com/v1.0/me/calendarview"
           f"?startDateTime={start}&endDateTime={end}"
           f"&$select=subject,start,end,location,attendees,organizer,bodyPreview"
           f"&$orderby=start/dateTime&$top=50")

    try:
        data = graph_get(token, url)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        log(f"calendar fetch error: {e}", "WARN")
        return {"today": [], "tomorrow": [], "week": []}

    by_day = {"today": [], "tomorrow": [], "week": []}
    today_iso = today.isoformat()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    for ev in data.get("value", []):
        start_dt = (ev.get("start") or {}).get("dateTime", "")[:10]
        record = {
            "subject": ev.get("subject", ""),
            "start": (ev.get("start") or {}).get("dateTime"),
            "end": (ev.get("end") or {}).get("dateTime"),
            "location": (ev.get("location") or {}).get("displayName"),
            "attendees": [(a.get("emailAddress") or {}).get("address") for a in ev.get("attendees", [])],
            "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address"),
            "body_preview": (ev.get("bodyPreview") or "")[:300],
        }
        if start_dt == today_iso:
            by_day["today"].append(record)
        if start_dt == tomorrow_iso:
            by_day["tomorrow"].append(record)
        by_day["week"].append(record)

    out_dir = vault / "_brain_api" / "calendar"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        for key in ("today", "tomorrow", "week"):
            (out_dir / f"{key}.json").write_text(json.dumps({
                "generated": datetime.now(timezone.utc).isoformat(),
                "events": by_day[key],
            }, indent=2))
    return {k: len(v) for k, v in by_day.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth", action="store_true", help="interactive device-code login")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vault", default=os.environ.get("CLAUDE_VAULT", VAULT_DEFAULT))
    args = ap.parse_args()

    vault = Path(args.vault)

    if msal is None:
        log("msal not installed — run: pip install --user msal", "WARN")
        print("graph_sync.py: msal library not installed.")
        print("Install with: python3 -m pip install --user msal")
        print("Then re-run with --auth for first-time device-code authentication.")
        return 0

    cfg = load_config(vault)
    if not cfg:
        log("no graph-config.json found", "WARN")
        return 0

    app, cache = build_msal_app(cfg)
    token = acquire_token(app, cache, cfg["scopes"], interactive=args.auth)
    if not token:
        if not args.auth:
            print("No cached Graph token. Run: python3 build/tools/graph_sync.py --auth")
        return 0

    emails = sync_emails(token, vault, cfg, args.dry_run)
    cal = sync_calendar(token, vault, cfg, args.dry_run)
    print(f"Graph sync: {emails} new emails written, calendar today={cal['today']} tomorrow={cal['tomorrow']} week={cal['week']}")
    log(f"sync ok: emails={emails} cal={cal}", "INFO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
