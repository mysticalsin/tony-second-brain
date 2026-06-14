#!/usr/bin/env bash
# send-gating-audit.sh  —  conduct-hardening: external-egress scanner
#
# Scans agent playbooks, scripts, and build tools for any external-egress capability
# that could transmit data without a human step. Flags and logs findings.
#
# Usage:
#   bash infra/hooks/send-gating-audit.sh             # full scan, print table, log findings
#   bash infra/hooks/send-gating-audit.sh --summary   # one-line summary only (for cron/CI)
#   bash infra/hooks/send-gating-audit.sh --dry-run   # scan + print, do NOT write violations.jsonl
#
# Output:
#   stdout — audit table (agent/script | finding | severity | verdict)
#   _agent_state/_conduct/conduct-violations.jsonl  — one record per finding (source="send-gating")
#   exit 0 — clean (no egress capability found outside policy)
#   exit 1 — findings requiring review
#
# Policy reference: infra/conduct/send-gating-policy.md
#
# Dependencies: python3 stdlib only. No pip, no jq required.
# bash -n clean; python3 -m py_compile clean.

set -uo pipefail

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
VIOLATIONS_LOG="$VAULT/_agent_state/_conduct/conduct-violations.jsonl"
AGENT_STATE_DIR="$VAULT/_agent_state"
BUILD_TOOLS_DIR="$VAULT/build/tools"
META_DIR="$VAULT/99_Meta"

SUMMARY_MODE=0
DRY_RUN=0
[[ "${1:-}" == "--summary" ]] && SUMMARY_MODE=1
[[ "${1:-}" == "--dry-run" ]]  && DRY_RUN=1

python3 - \
  "$VIOLATIONS_LOG" \
  "$DRY_RUN" \
  "$SUMMARY_MODE" \
  "$AGENT_STATE_DIR" \
  "$BUILD_TOOLS_DIR" \
  "$META_DIR" \
  "$VAULT" \
<<'PYEOF'
import sys
import os
import re
import json
import datetime
from pathlib import Path
from collections import defaultdict


# ── Args ──────────────────────────────────────────────────────────────────────
violations_log   = Path(sys.argv[1])
dry_run          = sys.argv[2] == "1"
summary_mode     = sys.argv[3] == "1"
agent_state_dir  = Path(sys.argv[4])
build_tools_dir  = Path(sys.argv[5])
meta_dir         = Path(sys.argv[6])
vault_dir        = Path(sys.argv[7])


# ── Helpers ───────────────────────────────────────────────────────────────────

def iso_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_from_path(path_str):
    p = Path(path_str)
    parts = p.parts
    for i, part in enumerate(parts):
        if part == "_agent_state" and i + 1 < len(parts):
            return parts[i + 1]
    return p.name


def append_violation(record):
    violations_log.parent.mkdir(parents=True, exist_ok=True)
    with violations_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Egress patterns ───────────────────────────────────────────────────────────

EGRESS_PATTERNS = [
    (r"\bsmtplib\b",
     "high", "§send-gating smtp",
     "Python smtplib import — email send capability"),
    (r"\bsmtp\b",
     "high", "§send-gating smtp",
     "SMTP reference — potential email send capability"),
    (r"\bsendmail\b",
     "high", "§send-gating sendmail",
     "sendmail binary reference — potential email send capability"),
    (r"\bmail\s+-[sSabc]",
     "high", "§send-gating mail-cmd",
     "mail command with flags — potential email send capability"),
    (r"[/\"]sendMail\b",
     "high", "§send-gating graph-sendmail",
     "Microsoft Graph /sendMail endpoint — direct email send capability"),
    (r"\bMail\.Send\b",
     "high", "§send-gating mail-send-scope",
     "Mail.Send OAuth scope — grants email send permission"),
    (r"messages/send\b",
     "high", "§send-gating graph-messages-send",
     "Microsoft Graph messages/send endpoint"),
    (r"/me/sendMail",
     "high", "§send-gating graph-me-sendmail",
     "Microsoft Graph /me/sendMail — user email send"),
    (r"gmail.*['\"]send['\"]",
     "high", "§send-gating gmail-send",
     "Gmail API send method reference"),
    (r"users\.messages\.send",
     "high", "§send-gating gmail-messages-send",
     "Gmail API users.messages.send — direct email send"),
    (r"curl\s+[^'\"\n]*-X\s+POST\s+[^'\"\n]*https?://(?!api\.anthropic\.com)(?!127\.0\.0\.1)",
     "high", "§send-gating curl-post-external",
     "curl POST to non-allowlisted external host"),
    (r"wget\s+[^'\"\n]*--post[- ]",
     "high", "§send-gating wget-post",
     "wget --post-data/--post-file — outbound HTTP POST"),
    (r"requests\.post\(",
     "med", "§send-gating requests-post",
     "requests.post() call — verify target host is in policy allowlist"),
    (r"send_message_to_chat\b",
     "high", "§send-gating teams-dm-send",
     "send_message_to_chat — direct DM bypasses review queue"),
    (r"send_chat_message\b",
     "high", "§send-gating teams-dm-send",
     "send_chat_message — direct DM bypasses review queue"),
    (r"send_direct_message\b",
     "high", "§send-gating direct-message-send",
     "send_direct_message method — bypasses review queue"),
    (r"\bsend_dm\b",
     "high", "§send-gating send-dm",
     "send_dm method — bypasses review queue"),
    (r"\bsend\s+email\b",
     "med", "§send-gating send-email-tool-ref",
     "'send email' capability reference — verify this is draft-only"),
    (r"\bauto.?send\b",
     "low", "§send-gating auto-send-reference",
     "auto-send reference — verify this is negated / draft-only context"),
]

_COMPILED = [
    (re.compile(pat, re.IGNORECASE), sev, label, desc)
    for pat, sev, label, desc in EGRESS_PATTERNS
]


# ── Allowlist suppressions ────────────────────────────────────────────────────

ALLOWLIST_SUPPRESSIONS = [
    ("graph_sync.py",         "Mail.Read"),
    ("graph_sync.py",         "Calendars.Read"),
    ("graph_sync.py",         "graph.microsoft.com/v1.0/me/messages"),
    ("graph_sync.py",         "graph.microsoft.com/v1.0/me/calendarview"),
    ("onenote_to_obsidian.py","Notes.Read"),
    ("capture_session.py",    "api.anthropic.com"),
    ("brain-refresh.sh",      "127.0.0.1"),
    ("brain-refresh.sh",      "DAEMON_URL"),
    # This audit script and policy doc mention patterns as documentation text
    ("send-gating-audit.sh",  ""),
    ("send-gating-policy.md", ""),
    ("conduct-violations.jsonl", ""),
    ("sbap-quarantine",       ""),
]


def is_suppressed(path_str, snippet):
    for path_sub, snip_sub in ALLOWLIST_SUPPRESSIONS:
        path_match = (not path_sub) or (path_sub in path_str)
        if not path_match:
            continue
        snip_match = (not snip_sub) or (snip_sub in snippet)
        if not snip_match:
            continue
        if path_sub:
            return True
    return False


def is_negated_context(rule, snippet, full_line=""):
    haystack = (full_line or snippet).lower()
    if rule == "§send-gating auto-send-reference":
        return any(neg in haystack for neg in (
            "never auto-send", "never auto send",
            "no auto-send", "not auto-send",
            "never send", "draft only", "drafts only",
        ))
    if rule == "§send-gating send-email-tool-ref":
        return any(neg in haystack for neg in (
            "never send", "don't send", "do not send",
            "never auto-send", "drafts only", "draft only",
            "never auto",
        ))
    return False


# ── File collection ───────────────────────────────────────────────────────────

SKIP_DIRS = {
    "_brain_index", "_brain_api", "graphify-out", "out",
    ".git", ".obsidian", "sbap-quarantine", "skill-versions",
    "__pycache__", ".mypy_cache", "audits",
}
SKIP_SUFFIXES = {
    ".pptx", ".docx", ".xlsx", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".zip", ".tar", ".gz", ".pyc",
    ".injection-flagged", ".bak", ".bak2",
}
SCAN_SUFFIXES = {".md", ".py", ".sh", ".json"}
SCAN_ROOTS    = [agent_state_dir, build_tools_dir, meta_dir]
MAX_FILE_BYTES = 2 * 1024 * 1024


def collect_files(roots):
    results = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                p = Path(dirpath) / fname
                if p.suffix.lower() in SKIP_SUFFIXES:
                    continue
                if p.suffix.lower() not in SCAN_SUFFIXES:
                    continue
                results.append(p)
    return results


def scan_file(path):
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except (PermissionError, OSError):
        return []

    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pattern, sev, label, desc in _COMPILED:
            m = pattern.search(line)
            if m:
                hits.append({
                    "path":    str(path),
                    "lineno":  lineno,
                    "matched": m.group(0),
                    "snippet": line.strip()[:200],
                    "full_line": line,
                    "rule":    label,
                    "sev":     sev,
                    "desc":    desc,
                })
    return hits


# ── Main scan ─────────────────────────────────────────────────────────────────

files     = collect_files(SCAN_ROOTS)
all_hits  = []

for fp in sorted(files):
    raw_hits = scan_file(fp)
    for h in raw_hits:
        path_str = h["path"]
        snippet  = h["snippet"]
        rule     = h["rule"]
        if is_suppressed(path_str, snippet):
            continue
        if is_negated_context(rule, snippet, h.get("full_line", "")):
            continue
        h["agent"] = agent_from_path(path_str)
        all_hits.append(h)

# ── Log violations ─────────────────────────────────────────────────────────────

ts_run = iso_now()
SEV_VERDICT = {"high": "FAIL", "med": "REVIEW", "low": "NOTE"}
SEV_ORDER   = {"low": 0, "med": 1, "high": 2}
SEV_TAG     = {"low": "LOW ", "med": "MED ", "high": "HIGH"}

if not dry_run:
    for h in all_hits:
        record = {
            "ts":       ts_run,
            "source":   "send-gating",
            "agent":    h["agent"],
            "rule":     h["rule"],
            "severity": h["sev"],
            "detail":   (
                f"line {h['lineno']}: {h['matched']!r} — {h['snippet'][:120]}"
            ),
            "file":     h["path"],
        }
        append_violation(record)


# ── Output ────────────────────────────────────────────────────────────────────

SEP = "═" * 80

if summary_mode:
    if not all_hits:
        print(
            f"send-gating-audit: CLEAN — 0 findings across {len(files)} files. "
            f"No egress capability outside policy allowlist."
        )
    else:
        n_high = sum(1 for h in all_hits if h["sev"] == "high")
        n_med  = sum(1 for h in all_hits if h["sev"] == "med")
        n_low  = sum(1 for h in all_hits if h["sev"] == "low")
        label  = "DRY-RUN (not written)" if dry_run else f"logged to {violations_log}"
        print(
            f"send-gating-audit: {len(all_hits)} finding(s) "
            f"[HIGH={n_high} MED={n_med} LOW={n_low}] "
            f"across {len(files)} files — {label}"
        )
    sys.exit(1 if all_hits else 0)

print(f"\n{SEP}")
print("  SEND-GATING AUDIT")
print(f"  Scanned:   {len(files)} files")
print(f"  Generated: {ts_run}")
if dry_run:
    print("  Mode:      DRY-RUN (violations NOT written to log)")
else:
    print(f"  Log:       {violations_log}")
print(f"  Policy:    {vault_dir}/infra/conduct/send-gating-policy.md")
print(SEP)

if not all_hits:
    print()
    print("  RESULT: CLEAN")
    print("  No external-egress capability found outside the policy allowlist.")
    print()
    print("  Interpretation:")
    print("  - No agent has smtp/sendmail/mail-send/Graph-send capability.")
    print("  - Outbound/ is a review queue; auto-promotion is a vault-internal file copy.")
    print("  - Send is a separate, explicit, human-run step.")
    print(f"\n{SEP}\n")
    sys.exit(0)

by_file = defaultdict(list)
for h in all_hits:
    by_file[h["path"]].append(h)

n_high = sum(1 for h in all_hits if h["sev"] == "high")
n_med  = sum(1 for h in all_hits if h["sev"] == "med")
n_low  = sum(1 for h in all_hits if h["sev"] == "low")

print(f"\n  RESULT: {len(all_hits)} FINDING(S) in {len(by_file)} file(s)")
print(f"  HIGH={n_high}  MED={n_med}  LOW={n_low}")
print()

col_sev     = 6
col_verdict = 7
col_agent   = 28
col_rule    = 36

header = (
    f"  {'SEV':{col_sev}}  {'VERDICT':{col_verdict}}  "
    f"{'AGENT/SCRIPT':{col_agent}}  {'RULE':{col_rule}}  LINE"
)
divider = (
    f"  {'───':{col_sev}}  {'───────':{col_verdict}}  "
    f"{'────────────':{col_agent}}  {'────':{col_rule}}  ────"
)
print(header)
print(divider)

for fpath in sorted(by_file.keys()):
    hits = sorted(
        by_file[fpath],
        key=lambda h: (SEV_ORDER.get(h["sev"], 0) * -1, h["lineno"]),
    )
    agent_name = hits[0]["agent"]
    for h in hits:
        verdict = SEV_VERDICT.get(h["sev"], "?")
        sev_tag = SEV_TAG.get(h["sev"], h["sev"])
        print(
            f"  {sev_tag:{col_sev}}  {verdict:{col_verdict}}  "
            f"{agent_name:{col_agent}}  {h['rule']:{col_rule}}  {h['lineno']}"
        )
        print(f"         snippet: {h['snippet'][:96]}")
    print()

logged_str = "(DRY-RUN — not written)" if dry_run else f"logged to {violations_log}"
print(f"  Violations {logged_str}")
print(f"\n{SEP}\n")

sys.exit(1 if all_hits else 0)

PYEOF
