"""Triage SBAP writes from Dust agents.

SBAP v1.0 — daily 07:15 job (and on-demand). Walks 00_Inbox/from-dust/,
validates frontmatter against agent_write.schema.json, files appropriately.

Workflow:
1. For each file in 00_Inbox/from-dust/<agent>/:
   a. Parse frontmatter. Reject if missing required SBAP fields (log to dust-errors.log)
   b. Validate against 99_Meta/sbap-schemas/agent_write.schema.json
   c. If target_path exists already: write as <target>.dust-<agent>-<ts>.md (versioned, never overwrite)
   d. Else if confidence >= 0.85: move to target_path
   e. Else: leave in inbox, flag in morning digest
2. Append audit record to 99_Meta/dust-write-log.md
3. Update _agent_state/<agent>/writes.jsonl
4. Update _brain_index incrementally
5. If write was a content block: regenerate canonical/<type>/<key>.json

Usage:
    python3 build/tools/triage_dust_writes.py                # run triage
    python3 build/tools/triage_dust_writes.py --dry-run      # show what would happen
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ── Atomic-write + advisory-lock helpers (CC-007/CC-008) ─────────────────────
# Two agents running triage concurrently over OneDrive can interleave reads and
# writes on the same JSON file, producing a corrupt last-write-wins result.
# Strategy:
#   - atomic_write_json(): write to a sibling temp file then os.replace() — on
#     POSIX this is rename(2), which is atomic even across NFS/APFS.
#   - _lock_path / lock_file(): a .lock sentinel beside the target file + fcntl
#     LOCK_EX so two concurrent flock callers serialise; LOCK_NB lets a third
#     agent detect contention and skip rather than hang.


def _lock_path(p: Path) -> Path:
    """Return the lockfile path for a given JSON target."""
    return p.parent / (p.name + ".lock")


class lock_file:  # noqa: N801  (context manager, lowercase by convention)
    """Advisory exclusive lock on a sentinel file next to the target.

    Usage:
        with lock_file(path_to_json):
            data = json.loads(path_to_json.read_text())
            ...
            atomic_write_json(path_to_json, data)

    On POSIX, fcntl.flock is per-open-file-descriptor — safe across processes.
    Falls back gracefully (no lock) when fcntl is unavailable (non-POSIX).
    """

    def __init__(self, target: Path) -> None:
        self._lp = _lock_path(target)
        self._fh = None

    def __enter__(self) -> "lock_file":
        try:
            self._lp.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._lp, "w")  # noqa: WPS515
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        except (OSError, AttributeError):
            # Non-POSIX or permission error — degrade gracefully (best-effort).
            if self._fh:
                self._fh.close()
            self._fh = None
        return self

    def __exit__(self, *_) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()
            try:
                self._lp.unlink()
            except OSError:
                pass


def atomic_write_json(p: Path, data: object, indent: int = 2) -> None:
    """Serialize *data* to JSON and atomically replace *p*.

    Writes to a sibling temp file, then os.replace() — rename(2) on POSIX,
    which is atomic. The caller is responsible for holding lock_file() around
    the read-modify-write cycle so no two writers race on the same file.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-" + p.name + "-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=indent)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

try:
    import jsonschema  # type: ignore
except ImportError:
    jsonschema = None


VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
INBOX = VAULT / "00_Inbox" / "from-dust"
QUARANTINE = VAULT / "00_Inbox" / "sensitive-quarantine"
LOG = VAULT / "99_Meta" / "dust-write-log.md"
ERRORS = VAULT / "99_Meta" / "dust-errors.log"
SCHEMA = VAULT / "99_Meta" / "sbap-schemas" / "agent_write.schema.json"
REGISTRY = VAULT / "_agent_state" / "_registry.json"
TX_ROOT = VAULT / "_agent_state"  # per-agent /transactions/ dirs live under here

CONFIDENCE_THRESHOLD = 0.85

# ── Citation-or-silence gate (Holy-Shit #10) ─────────────────────────────────
# Client-facing output types: any agent output that could land in front of a
# real client, prospect, or partner. Derived from the SBAP registry output_types
# for agents with max_sensitivity=confidential or external-audience writes.
#
# Registry mapping (agent → output_type → client-facing?):
#   rfp-drafter      → proposal_draft         YES (live bid document)
#   email-responder  → email_draft             conditional (handled by ext-audience gate)
#   client-coach     → coaching_note           YES (QBR / account note)
#   commercial-deliverables → quote            YES (commercial doc)
#   partner-motion-operator → partner_submission YES (co-sell outreach)
#   amaia-demo-composer → amaia_demo_draft      YES (client demo)
#   silver-surfer    → email_draft             conditional (ext-audience gate covers it)
#
# Spec aliases (the task spec used different names — include both for forward compat):
#   rfp_draft, proposal_section, commercial_deliverable, client_coaching, external_email
#
# NOT client-facing: intelligence_brief, weekly_review, block_curation_recs,
#   qualification, win_loss_retro, escalation_alert, meeting_intel, governance_assessment,
#   orchestration_run, sdlc_run, requirements_doc, video_brief, ai_session_note, other,
#   failure_mode, discovery-log
CLIENT_FACING_OUTPUT_TYPES: frozenset[str] = frozenset({
    # Registry-canonical names
    "proposal_draft",
    "coaching_note",
    "quote",
    "partner_submission",
    "amaia_demo_draft",
    # Spec aliases (forward-compat)
    "rfp_draft",
    "proposal_section",
    "commercial_deliverable",
    "client_coaching",
    "external_email",
})

# Audit log for citation failures (fleet-confession-style).
CITATION_AUDIT_LOG = VAULT / "99_Meta" / "citation-audit.log"


def reputation_theta(agent: str) -> tuple[float, "float | None"]:
    """Floating per-agent auto-promote threshold from reputation.json (build/tools/reputation.py).
    A proven agent clears below 0.85; a sloppy one must clear higher. Falls back to the
    static CONFIDENCE_THRESHOLD when no ledger exists or it's pinned (n<10 labeled outcomes)."""
    rp = VAULT / "_agent_state" / agent / "reputation.json"
    try:
        r = json.loads(rp.read_text())
        return float(r.get("theta", CONFIDENCE_THRESHOLD)), r.get("R")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return CONFIDENCE_THRESHOLD, None

# Targets where confidential content MUST NOT promote without a confidential agent.
# Anything under these prefixes is treated as fleet-visible.
PUBLIC_TARGET_PREFIXES = (
    "_brain_api/canonical/",
    "03_Resources/",
    "02_Areas/Pipeline.md",
)


def _load_registry():
    try:
        return json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return {"agents": []}


def agent_max_sensitivity(agent: str) -> str:
    """Return 'public' | 'internal' | 'confidential'. Default 'internal' if unregistered."""
    reg = _load_registry()
    for a in reg.get("agents", []):
        if a.get("agent_name") == agent:
            return a.get("max_sensitivity", "internal")
    return "internal"


def is_public_target(target: str) -> bool:
    return any(target.startswith(p) for p in PUBLIC_TARGET_PREFIXES)


def sensitivity_blocks_promote(agent: str, target: str, fm_override: str | None) -> str | None:
    """Returns a rejection reason if confidential content would land on a public target,
    else None (OK to promote)."""
    effective = (fm_override or agent_max_sensitivity(agent)).lower()
    if effective == "confidential" and is_public_target(target):
        return f"agent max_sensitivity={effective} cannot promote to public target {target}"
    return None


# External-facing output types: a confidentiality guard MUST pass before auto-promote.
# Confidence alone never clears an external draft (a high-confidence draft can still
# leak a client name / quote). Run a confidentiality check before promote.
EXTERNAL_OUTPUT_TYPES = {
    "email_draft", "outbound_email", "partner_submission", "linkedin_post",
    "linkedin_content", "press_release", "customer_story", "social_post",
    "blog_post", "case_study", "proposal", "client_deliverable",
}
# Recipient domains treated as internal. Anything else = external (fail-safe).
# Set the INTERNAL_EMAIL_DOMAINS env var (comma-separated) to override for your firm.
_env_domains = os.environ.get("INTERNAL_EMAIL_DOMAINS", "")
INTERNAL_EMAIL_DOMAINS: tuple[str, ...] = (
    tuple(d.strip() for d in _env_domains.split(",") if d.strip())
    if _env_domains else ("example.com",)
)


def external_audience_reason(fm: dict) -> str | None:
    """Return a reason if this write targets an external audience (client/partner/
    public), else None. Deterministic — no LLM. Drives the confidentiality gate."""
    ot = (fm.get("output_type") or "").lower()
    if ot in EXTERNAL_OUTPUT_TYPES:
        return f"output_type={ot}"
    meta = fm.get("email_metadata") or {}
    recips = list(meta.get("to") or []) + list(meta.get("cc") or [])
    for r in recips:
        dom = str(r).split("@")[-1].lower()
        if dom and not any(dom == d or dom.endswith("." + d) for d in INTERNAL_EMAIL_DOMAINS):
            return f"external recipient {r}"
    return None


# Off-limits client names — a named NDA/regulated client in an external draft must clear
# a confidentiality pass even when the audience heuristic alone wouldn't trip.
_OFF_LIMITS = None  # cache: (names: list[(canonical, lowered)], stale: bool)


def load_off_limits():
    """Lazy-load off_limits.json → (names, stale). names = [(canonical, lowered)] over
    client_name + aliases. stale = last_reviewed older than freshness_threshold_days.
    Missing/unreadable list → ([], False) (fail-open on absence; the audience gate still runs)."""
    global _OFF_LIMITS
    if _OFF_LIMITS is None:
        names, stale = [], False
        try:
            cfg = json.loads((VAULT / "99_Meta" / "config" / "confidentiality"
                              / "off_limits.json").read_text())
            for e in cfg.get("entries", []):
                for nm in [e.get("client_name", "")] + list(e.get("aliases") or []):
                    nm = (nm or "").strip()
                    if nm:
                        names.append((nm, nm.lower()))
            lr = cfg.get("last_reviewed")
            thr = int(cfg.get("freshness_threshold_days", 30))
            if lr:
                age = (datetime.now(timezone.utc).date()
                       - datetime.fromisoformat(lr).date()).days
                stale = age > thr
        except (OSError, json.JSONDecodeError, ValueError):
            names, stale = [], False
        _OFF_LIMITS = (names, stale)
    return _OFF_LIMITS


def off_limits_hit(text: str) -> str | None:
    """Return the canonical name of the first off-limits client mentioned in text, else None."""
    names, _ = load_off_limits()
    low = (text or "").lower()
    for canonical, lowered in names:
        if lowered and lowered in low:
            return canonical
    return None


# ────────────────────── Transaction log (Phase 1.3) ──────────────────────


def tx_dir(agent: str) -> Path:
    d = TX_ROOT / agent / "transactions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tx_begin(agent: str, payload: dict) -> Path:
    """Write a pending marker BEFORE any side effect. Return the marker path."""
    import hashlib
    sha = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    p = tx_dir(agent) / f"{sha}.pending.json"
    p.write_text(json.dumps({"started": utcnow(), **payload}, indent=2))
    return p


def tx_commit(marker: Path) -> None:
    """Side effect succeeded — remove the marker."""
    try:
        marker.unlink()
    except OSError:
        pass


def tx_rollback_orphans() -> list[str]:
    """Find any leftover .pending.json markers (crashes mid-triage) and report them.
    Doesn't auto-undo file moves — flags for Tony to inspect via /dust-resolve."""
    out = []
    for p in TX_ROOT.rglob("transactions/*.pending.json"):
        try:
            data = json.loads(p.read_text())
            age_sec = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(data["started"].replace("Z", "+00:00"))).total_seconds()
            if age_sec > 60:  # older than 60s = abandoned
                out.append(f"orphan tx: {p.relative_to(VAULT)} (age {int(age_sec)}s)")
                log_error(f"ORPHAN_TX {p.relative_to(VAULT)} age={int(age_sec)}s data={data}")
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ────────────────────── Idempotency seen-set (Phase 1.4) ──────────────────────
# Each agent gets a persistent seen.json at _agent_state/<agent>/seen.json.
# The dedup key is (content_hash, source_run_id):
#   - content_hash alone would suppress legitimate re-runs of the SAME run that
#     produced genuinely different content (extremely unlikely but possible).
#   - source_run_id alone would suppress a new write from the same agent run that
#     has different content (possible if a run produces multiple files).
# Together they are the minimal-safe key: the exact same bytes from the exact
# same run is by definition a replay and should never produce a second event.


def _seen_path(agent: str) -> Path:
    return VAULT / "_agent_state" / agent / "seen.json"


def load_seen(agent: str) -> dict:
    """Return the seen-set dict: {content_hash: [source_run_id, ...]}.
    Each hash maps to a list of run_ids that produced it (usually just one)."""
    try:
        return json.loads(_seen_path(agent).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def is_replay(agent: str, content_hash: str, source_run_id: str, seen: dict) -> bool:
    """True if (content_hash, source_run_id) was already processed."""
    return source_run_id in seen.get(content_hash, [])


def mark_seen(agent: str, content_hash: str, source_run_id: str,
              seen: dict, dry_run: bool = False) -> None:
    """Record this (content_hash, source_run_id) pair in the seen-set."""
    if dry_run:
        return
    p = _seen_path(agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    with lock_file(p):
        # Re-read under lock to pick up any concurrent writes.
        try:
            current = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            current = seen
        runs = current.setdefault(content_hash, [])
        if source_run_id not in runs:
            runs.append(source_run_id)
        atomic_write_json(p, current)


def record_replay_suppressed(agent: str, dry_run: bool = False) -> None:
    """Bump replays_suppressed in stats.json — keeps history honest."""
    if dry_run:
        return
    stats_path = VAULT / "_agent_state" / agent / "stats.json"
    with lock_file(stats_path):
        try:
            stats = json.loads(stats_path.read_text())
        except (OSError, json.JSONDecodeError):
            stats = {"agent": agent, "all_time": {}, "by_day": {}}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        all_time = stats.setdefault("all_time", {})
        all_time["replays_suppressed"] = all_time.get("replays_suppressed", 0) + 1
        by_day = stats.setdefault("by_day", {}).setdefault(today, {})
        by_day["replays_suppressed"] = by_day.get("replays_suppressed", 0) + 1
        atomic_write_json(stats_path, stats)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _coerce_for_schema(value):
    """YAML auto-parses ISO datetimes/dates into Python objects; schema wants strings.
    Walk the parsed structure and convert date/datetime → ISO 8601 string."""
    from datetime import date, datetime as _dt
    if isinstance(value, _dt):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _coerce_for_schema(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_for_schema(v) for v in value]
    return value


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end == -1:
        return None, text
    try:
        fm = yaml.safe_load(text[4:end]) or {}
        fm = _coerce_for_schema(fm)
        body = text[end + 4:].lstrip()
        return fm, body
    except yaml.YAMLError:
        return None, text


def validate_frontmatter(fm: dict) -> tuple[bool, str]:
    if jsonschema is None:
        return True, "jsonschema not installed — skipping validation"
    try:
        schema = json.loads(SCHEMA.read_text())
        jsonschema.Draft7Validator(schema).validate(fm)
        return True, "ok"
    except jsonschema.ValidationError as e:
        return False, f"schema: {e.message} at {list(e.path)}"
    except OSError as e:
        return True, f"schema unreadable: {e} — skipping validation"


def log_audit(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"- {utcnow()}: {msg}\n")


def log_error(msg: str) -> None:
    ERRORS.parent.mkdir(parents=True, exist_ok=True)
    with ERRORS.open("a") as f:
        f.write(f"{utcnow()} {msg}\n")


def record_agent_event(agent: str, event: dict, dry_run: bool = False) -> None:
    """Append a write event to the per-agent writes.jsonl and bump stats.json.
    This is what makes "vault as agent memory" work: every Dust action is durable,
    queryable, and surfaces in the dashboard."""
    if dry_run:
        return
    agent_dir = VAULT / "_agent_state" / agent
    if not agent_dir.exists():
        return  # unknown agent; skip silently (not registered)

    # writes.jsonl: one line per event
    writes_path = agent_dir / "writes.jsonl"
    with writes_path.open("a") as f:
        f.write(json.dumps(event) + "\n")

    # stats.json: rolling counters per action + last_active.
    # Wrap in lock_file + atomic_write_json so concurrent agents don't interleave
    # read-modify-write cycles and lose increments (CC-007/CC-008).
    stats_path = agent_dir / "stats.json"
    with lock_file(stats_path):
        try:
            stats = json.loads(stats_path.read_text())
        except (OSError, json.JSONDecodeError):
            stats = {"agent": agent, "all_time": {}, "by_day": {}}
        action = event.get("action", "unknown")
        today = event["ts"][:10]

        all_time = stats.setdefault("all_time", {})
        all_time[action] = all_time.get(action, 0) + 1

        by_day = stats.setdefault("by_day", {}).setdefault(today, {})
        by_day[action] = by_day.get(action, 0) + 1

        stats["last_active"] = event["ts"]
        atomic_write_json(stats_path, stats)


def merge_learnings_into_agent_memory(agent: str, fm: dict, dry_run: bool = False) -> None:
    """If a Dust write declares optional learnings/patterns/mistakes in its frontmatter,
    merge them into _agent_state/<agent>/memory.json (same shape as capture_session.py).
    This lets Dust agents accumulate persistent learnings without needing the Anthropic
    capture pipeline. Vault IS the memory."""
    if dry_run:
        return
    learnings = fm.get("learnings") or []
    patterns = fm.get("patterns") or []
    mistakes = fm.get("mistakes_to_avoid") or []
    if not (learnings or patterns or mistakes):
        return

    memory_path = VAULT / "_agent_state" / agent / "memory.json"
    if not memory_path.parent.exists():
        return  # unknown agent

    with lock_file(memory_path):
        try:
            mem = json.loads(memory_path.read_text())
        except (OSError, json.JSONDecodeError):
            mem = {"agent": agent, "memory_version": 1, "last_updated": None,
                   "global_patterns": [], "per_account_knowledge": {},
                   "self_observations": [], "recent_learnings": []}

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # recent_learnings as a ring buffer (50 entries). Dedupe by text content so
        # re-running triage on the same write doesn't accumulate duplicates.
        existing_texts = {(e.get("text") or "").strip().lower()
                          for e in mem.get("recent_learnings") or []}
        new_entries = []
        for l in learnings:
            if not l:
                continue
            if l.strip().lower() in existing_texts:
                continue
            new_entries.append({"date": today, "text": l, "source": "dust-write"})
        mem["recent_learnings"] = (new_entries + mem.get("recent_learnings", []))[:50]

        # global_patterns: merge with n_observations bump
        existing = {p["pattern"].lower(): p for p in mem.get("global_patterns", [])}
        for p in patterns + mistakes:
            if not p:
                continue
            k = p.lower().strip()
            if k in existing:
                existing[k]["n_observations"] = existing[k].get("n_observations", 1) + 1
                existing[k]["confidence"] = round(min(1.0, existing[k]["n_observations"] / 5.0), 2)
            else:
                existing[k] = {"pattern": p, "confidence": 0.2, "n_observations": 1, "first_seen": today}
        mem["global_patterns"] = sorted(existing.values(), key=lambda x: -x.get("n_observations", 0))[:200]

        mem["last_updated"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(memory_path, mem)


# ── Citation-or-silence gate helpers (Holy-Shit #10) ─────────────────────────

import re as _re

# Patterns that identify "factual claim" sentences — deterministic, no LLM.
#
# A sentence is flagged when it contains ANY of:
#   1. A number (int or decimal), including percentages and currency amounts
#   2. A named organisation/proper noun followed by a possessive or action verb
#      (catches "Globex requires X", "Initech's Y is Z" style hard assertions)
#
# One or more of these citation markers in the SAME sentence clear the flag:
#   - [n]  inline footnote: [1], [2], etc.
#   - (canonical: <type>/<key>)  SBAP canonical reference
#   - (source: ...)  explicit source marker
#   - [[...]]        Obsidian wikilink (treated as citation — links to vault page)
#   - > blockquote lines (quotations are their own citation)
#   - URL (http/https)

_CLAIM_PATTERN = _re.compile(
    r"""
    (?:
        \d+(?:[.,]\d+)*\s*%       # percentage: 40%, 3.5%, 100%
      | [€$£¥]\s*\d+(?:[.,]\d+)* # currency prefix: €50M, $1.2B
      | \d+(?:[.,]\d+)*\s*(?:EUR|USD|GBP|M|K|mn|bn|k)\b  # currency suffix
      | \b\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?\b             # plain numbers with optional separators
    )
    """,
    _re.VERBOSE | _re.IGNORECASE,
)

_CITATION_PATTERN = _re.compile(
    r"""
      \[\d+\]                          # [1], [23]
    | \(canonical:\s*[^\)]+\)          # (canonical: case_study/vertical__x__y)
    | \(source:\s*[^\)]+\)             # (source: ...)
    | \[\[[^\]]+\]\]                   # [[obsidian wikilink]]
    | https?://\S+                     # URL
    """,
    _re.VERBOSE | _re.IGNORECASE,
)


def _split_sentences(text: str) -> list[str]:
    """Split body text into sentences. Strips YAML frontmatter first.
    Simple period/newline-boundary split — good enough for claim detection."""
    # Remove frontmatter
    body = text
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        if end != -1:
            body = body[end + 4:].lstrip()
    # Remove code blocks (JSON/YAML payloads — not prose claims)
    body = _re.sub(r"```.*?```", "", body, flags=_re.DOTALL)
    # Remove blockquote lines (they ARE citations — whole line exempt)
    body = _re.sub(r"^>.*$", "", body, flags=_re.MULTILINE)
    # Split on period + space/newline OR just newlines
    parts = _re.split(r"(?<=[.!?])\s+|\n+", body)
    return [s.strip() for s in parts if s.strip()]


def find_unsourced_claims(text: str) -> list[str]:
    """Return a list of sentences that contain factual claims (numbers / currency /
    percentages) but carry no citation marker. Deterministic — no LLM.

    An empty list means zero unsourced claims (the citation gate is satisfied)."""
    sentences = _split_sentences(text)
    unsourced = []
    for sentence in sentences:
        if _CLAIM_PATTERN.search(sentence):
            if not _CITATION_PATTERN.search(sentence):
                unsourced.append(sentence)
    return unsourced


def log_citation_audit(agent: str, source_file: str, n_claims: int,
                       unsourced: list[str], dry_run: bool = False) -> None:
    """Append a fleet-confession-style line to the citation audit log.
    Format:
        <ts> CITATION_HOLD agent=<a> file=<f> unsourced=<n> sentences=[...]
    """
    if dry_run:
        return
    CITATION_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    snippets = "; ".join(s[:80] + "…" if len(s) > 80 else s for s in unsourced[:5])
    line = (f"{utcnow()} CITATION_HOLD agent={agent} file={source_file} "
            f"unsourced={n_claims} sentences=[{snippets}]\n")
    with CITATION_AUDIT_LOG.open("a") as f:
        f.write(line)


def annotate_held_file(p: Path, unsourced: list[str], dry_run: bool = False) -> None:
    """Append a machine-readable annotation block to the held file so /dust-resolve
    can display the exact offending sentences to Tony without reopening triage."""
    if dry_run:
        return
    try:
        existing = p.read_text()
    except OSError:
        return
    annotation_lines = ["", "<!-- citation-gate-hold: unsourced-claims",
                        f"    held: {utcnow()}",
                        "    offending sentences (add citation markers to clear):"]
    for i, s in enumerate(unsourced, 1):
        annotation_lines.append(f"    [{i}] {s[:200]}")
    annotation_lines.append("-->")
    annotation = "\n".join(annotation_lines) + "\n"
    # Only append if not already annotated (idempotent on re-runs)
    if "citation-gate-hold: unsourced-claims" not in existing:
        p.write_text(existing + annotation)


def triage_file(p: Path, dry_run: bool = False) -> str:
    try:
        text = p.read_text()
    except OSError as e:
        log_error(f"read failed: {p}: {e}")
        return f"SKIP {p.name}: read failed"

    # ── Idempotency gate: compute content hash early so we can check the seen-set
    # BEFORE parsing frontmatter or doing any side-effectful work.
    c_hash = _content_hash(text)

    fm, _ = parse_frontmatter(text)
    if fm is None:
        log_error(f"no frontmatter: {p}")
        return f"REJECT {p.name}: no frontmatter"

    ok, why = validate_frontmatter(fm)
    if not ok:
        log_error(f"validation failed: {p}: {why}")
        return f"REJECT {p.name}: {why}"

    agent = fm.get("source_agent", "unknown")
    source_run_id = fm.get("source_run_id", "")

    # ── Idempotency gate: suppress exact replays (same bytes, same run_id).
    # Fires after frontmatter parse so we have the agent name for seen.json routing.
    # A new write from the same agent with different content still passes through
    # (different c_hash → not in seen-set). Only byte-for-byte identical re-submissions
    # from the same run_id are suppressed — these are guaranteed duplicates.
    seen = load_seen(agent)
    if is_replay(agent, c_hash, source_run_id, seen):
        log_audit(f"REPLAY_SUPPRESSED {agent}/{p.name} run={source_run_id} hash={c_hash[:12]}")
        record_replay_suppressed(agent, dry_run=dry_run)
        return f"REPLAY_SUPPRESSED  {p.name}: identical content+run_id already processed — skipped"

    target = fm.get("target_path", "")
    confidence = fm.get("confidence", 0.0)
    sensitivity_override = fm.get("sensitivity_override")  # optional per-write tier
    now_iso = utcnow()
    output_type = fm.get("output_type", "")

    # Build the event skeleton — every action records the same shape so stats roll up cleanly.
    event = {
        "ts": now_iso,
        "source_run_id": source_run_id,
        "output_type": output_type,
        "confidence": confidence,
        "source_file": p.name,
    }

    # Always merge optional learnings/patterns/mistakes into memory, even on HOLD
    # (the work happened; learnings are valid regardless of confidence).
    merge_learnings_into_agent_memory(agent, fm, dry_run=dry_run)

    # ── Sensitivity gate (Phase 1.2) ─────────────────────────────────────
    # Confidential agents writing to public-fleet targets get quarantined for review.
    if target:
        block_reason = sensitivity_blocks_promote(agent, target, sensitivity_override)
        if block_reason:
            event["action"] = "quarantined"
            event["target"] = target
            event["reason"] = block_reason
            if not dry_run:
                QUARANTINE.mkdir(parents=True, exist_ok=True)
                q_dir = QUARANTINE / agent
                q_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                q_path = q_dir / f"{ts}-{p.name}"
                q_path.write_text(text)
                p.unlink()
                log_audit(f"{agent} QUARANTINED → {q_path.relative_to(VAULT)} ({block_reason})")
            record_agent_event(agent, event, dry_run=dry_run)
            mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
            return f"QUARANTINE {p.name}: {block_reason}"

    if not target:
        event["action"] = "hold-review-only"
        event["target"] = ""
        record_agent_event(agent, event, dry_run=dry_run)
        mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
        return f"HOLD  {p.name}: review-only output (no target_path), confidence={confidence}"

    target_path = VAULT / target
    if target_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        versioned = target_path.with_suffix(f".dust-{agent}-{ts}{target_path.suffix}")
        action = f"VERSION  {p.name} → {versioned.relative_to(VAULT)}"
        event["action"] = "conflict"
        event["target"] = str(versioned.relative_to(VAULT))
        if not dry_run:
            marker = tx_begin(agent, {"op": "version", "src": p.name, "dst": str(versioned.relative_to(VAULT))})
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                versioned.write_text(text)
                p.unlink()
                log_audit(f"{agent} → {versioned.relative_to(VAULT)} (conflict; tony resolves)")
                tx_commit(marker)
            except OSError as e:
                log_error(f"version failed: {p}: {e} (marker stays for inspection)")
                raise
        record_agent_event(agent, event, dry_run=dry_run)
        mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
        return action

    # ── Confidentiality gate ─────────────────────────────────────────────
    # An external-facing draft, OR any draft naming an off-limits client headed to an
    # external/public destination, must clear a confidentiality check before auto-promote;
    # confidence is not enough. Hold until frontmatter carries `confidentiality_guard: pass`
    # (stamped by a human review). Fail-safe: over-hold beats a client-data leak.
    if str(fm.get("confidentiality_guard", "")).lower() != "pass":
        ext_reason = external_audience_reason(fm)
        ol_name = off_limits_hit(text)
        ol_external = ol_name and (ext_reason or is_public_target(target))
        if ext_reason or ol_external:
            _, ol_stale = load_off_limits()
            bits = []
            if ol_external:
                bits.append(f"off-limits client {ol_name}" + (" (off_limits list STALE)" if ol_stale else ""))
            if ext_reason:
                bits.append(ext_reason)
            reason = "; ".join(bits)
            event["action"] = "hold-needs-confidentiality-guard"
            event["target"] = target
            event["reason"] = reason
            record_agent_event(agent, event, dry_run=dry_run)
            mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
            return (f"HOLD  {p.name}: {reason} — needs confidentiality-guard pass "
                    f"before promote (confidence={confidence})")

    # ── Citation-or-silence gate (Holy-Shit #10) ─────────────────────────────
    # Client-facing output types must have every factual claim backed by a citation
    # marker. An unsourced number / percentage / currency in an auto-promoted bid
    # document ships a potential hallucination into a real proposal.
    #
    # The gate fires BEFORE the confidence check: even a conf=1.0 draft from a
    # trusted agent must cite its facts. This is a composition with the existing
    # gates (not a replacement): confidence + zero unsourced claims = promote.
    # Clearing is done by adding inline citation markers to the draft; re-running
    # triage will then find zero unsourced claims and allow promotion.
    #
    # Non-client-facing output types (intelligence_brief, weekly_review, etc.) are
    # UNAFFECTED — this gate is a no-op for them.
    if output_type.lower() in CLIENT_FACING_OUTPUT_TYPES:
        unsourced = find_unsourced_claims(text)
        if unsourced:
            n = len(unsourced)
            event["action"] = "hold-unsourced-claim"
            event["target"] = target
            event["reason"] = f"citation gate: {n} unsourced claim(s)"
            event["unsourced_count"] = n
            # Annotate the held file in-place so /dust-resolve shows the offending sentences
            annotate_held_file(p, unsourced, dry_run=dry_run)
            # Fleet-confession-style audit log line
            log_citation_audit(agent, p.name, n, unsourced, dry_run=dry_run)
            log_audit(f"{agent} HOLD citation-gate: {n} unsourced claim(s) in {p.name}")
            record_agent_event(agent, event, dry_run=dry_run)
            mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
            return (f"HOLD  {p.name}: citation gate — {n} unsourced claim(s) "
                    f"(output_type={output_type}); add citation markers to clear")

    # ── Conduct-provenance gate (Phase 2.2) ──────────────────────────────────
    # Hold the self-promote failure class: an unresolved target_path placeholder
    # ([TYPE_REQUIRED]/<slug>) — which would create a literal placeholder dir on promote —
    # or an empty source_run_id. Flags to conduct-violations.jsonl for telemetry/dreaming.
    _prov = None
    if _re.search(r"(\[[A-Z_]+\]|<[a-zA-Z_]+>|TYPE_REQUIRED)", target):
        _prov = "target_path has an unresolved placeholder"
    elif not str(source_run_id).strip():
        _prov = "empty source_run_id"
    if _prov:
        event["action"] = "hold-conduct-provenance"
        event["target"] = target
        event["reason"] = _prov
        try:
            _clog = VAULT / "99_Meta" / "conduct-violations.jsonl"
            _clog.parent.mkdir(parents=True, exist_ok=True)
            with _clog.open("a", encoding="utf-8") as _cf:
                _cf.write(json.dumps({"ts": utcnow(), "source": "triage", "agent": agent,
                    "rule": "11-SBAP-provenance", "severity": "high", "detail": _prov, "file": str(p)}) + "\n")
        except Exception:
            pass
        record_agent_event(agent, event, dry_run=dry_run)
        mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
        return f"HOLD  {p.name}: conduct-provenance — {_prov}"

    # Reputation-aware promotion: the bar floats per-agent (build/tools/reputation.py).
    theta, agent_R = reputation_theta(agent)
    event["triage_theta"] = theta
    if confidence >= theta:
        rtag = f" R={agent_R:.2f}" if isinstance(agent_R, (int, float)) else ""
        action = f"PROMOTE  {p.name} → {target} (conf={confidence} ≥ θ={theta}{rtag})"
        event["action"] = "promoted"
        event["target"] = target
        if not dry_run:
            marker = tx_begin(agent, {"op": "promote", "src": p.name, "dst": target, "confidence": confidence, "theta": theta})
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(text)
                p.unlink()
                log_audit(f"{agent} → {target} (confidence={confidence} ≥ θ={theta})")
                tx_commit(marker)
            except OSError as e:
                log_error(f"promote failed: {p}: {e} (marker stays for inspection)")
                raise
        record_agent_event(agent, event, dry_run=dry_run)
        mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
        return action

    event["action"] = "hold-low-confidence"
    event["target"] = target
    record_agent_event(agent, event, dry_run=dry_run)
    mark_seen(agent, c_hash, source_run_id, seen, dry_run=dry_run)
    return f"HOLD     {p.name}: confidence={confidence} below θ={theta} (reputation-aware; static floor {CONFIDENCE_THRESHOLD})"


def main() -> int:
    # Global kill switch — fleet-pause.sh. Don't promote/act while paused.
    import os as _os
    _flag = _os.path.join(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")), "_agent_state", "AUTOMATION_PAUSED")
    if _os.path.exists(_flag):
        print("FLEET PAUSED (AUTOMATION_PAUSED) — triage skipped"); return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Phase 1.3: check for orphan transactions from prior crashes
    orphans = tx_rollback_orphans()
    if orphans:
        print(f"⚠ {len(orphans)} orphan transaction(s) detected — see 99_Meta/dust-errors.log")
        for o in orphans[:5]:
            print(f"  {o}")

    if not INBOX.exists():
        print(f"No inbox at {INBOX}")
        return 0

    results = []
    for agent_dir in INBOX.iterdir():
        if not agent_dir.is_dir() or agent_dir.name.startswith("."):
            continue
        for p in agent_dir.glob("*.md"):
            if p.name == "README.md":
                continue
            results.append(triage_file(p, dry_run=args.dry_run))

    if not results:
        print("Inbox empty.")
        return 0

    for r in results:
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
