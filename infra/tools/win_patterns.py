#!/usr/bin/env python3
"""win_patterns.py — learn what wins, raise the win-rate (P6).

TWO MODES, both grounded in your real corpus via the local recall index:

  DESCRIPTIVE (default) — mine the won-deal corpus (proposition decks, win-stories,
  _wiki/competitive, prior proposals) → extract recurring WIN factors + LOSS factors,
  each cited to a vault path → write _wiki/methodology/win-playbook.md.

  PRESCRIPTIVE (--bid <path>) — score one open bid's research.md against the playbook
  → name present/missing win-factors → top concrete additions → write win-recs.md
  into the bid folder.

NOTE (2026-06-08): no closed Won/Lost bids exist in the vault yet, so this learns
from the proposition/win-story CORPUS, not bid outcomes. When bids close
(stage: Won|Lost), re-run — outcome evidence will sharpen the playbook automatically.

NO VOICE. Pure script. Run from the vault root:
  python build/tools/win_patterns.py                                      # build playbook
  python build/tools/win_patterns.py --bid "RFPs/Globex/AI QA/Q2-AI-QA"  # score a bid
  python build/tools/win_patterns.py --dry-run                            # retrieve + prompt only
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
RECALL = "build/tools/recall_vector.py"
PLAYBOOK = VAULT / "_wiki" / "methodology" / "win-playbook.md"

# Phase 2: outcome-ledger integration — count DISTINCT Won bids for the playbook label.
MIN_CLOSED = 5   # PLAN §4: below this, label as LOW CONFIDENCE corpus-derived


def _count_distinct_won_bids() -> int:
    """Read _agent_state/outcome-ledger.jsonl and return the number of DISTINCT bid_ids
    with at least one 'won' outcome. Compacted by (bid_id, block_key) — last updated_at
    wins — so we count unique bids, not outcome records."""
    ledger = VAULT / "_agent_state" / "outcome-ledger.jsonl"
    if not ledger.exists():
        return 0
    # compaction: keep last updated_at per (bid_id, block_key)
    compacted: dict[tuple, dict] = {}
    with ledger.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = (str(rec.get("bid_id", "")), str(rec.get("block_key", "")))
            prev = compacted.get(k)
            if prev is None or str(rec.get("updated_at", "")) >= str(prev.get("updated_at", "")):
                compacted[k] = rec
    # count distinct bid_ids with any 'won' outcome
    won_bids: set[str] = set()
    for rec in compacted.values():
        if str(rec.get("outcome", "")).lower() == "won":
            won_bids.add(str(rec.get("bid_id", "")))
    return len(won_bids)

# Retrieval angles for mining the win/loss corpus.
WIN_QUERIES = [
    "winning proposal win themes that closed the deal differentiators",
    "why we won client engagement success case study outcome",
    "pricing model commercial structure that won a competitive deal margin",
    "competitive displacement beat incumbent competitor positioning",
    "win story proof point measurable result delivered for client",
]
LOSS_QUERIES = [
    "lost deal reasons why we lost objection gap",
    "competitive loss weakness buyer concern pricing too high",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_frontmatter(text: str) -> str:
    m = re.match(r"^---\r?\n.*?\r?\n---\r?\n?(.*)$", text, re.S)
    return m.group(1) if m else text


def recall(query: str, top: int) -> list[dict]:
    """Daemon-first (owns the on-disk Qdrant lock); uv fallback. Returns [] on failure."""
    import urllib.request, urllib.parse
    try:
        url = "http://127.0.0.1:7766/retrieve?q=" + urllib.parse.quote(query) + f"&top={top}"
        with urllib.request.urlopen(url, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    uv = shutil.which("uv")
    if not uv:
        return []
    try:
        r = subprocess.run(
            [uv, "run", "--quiet", "--with", "qdrant-client", "--with", "fastembed",
             "python", RECALL, query, "--top", str(top)],
            cwd=str(VAULT), capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []


def claude_bin() -> str | None:
    for c in (str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", shutil.which("claude")):
        if c and Path(c).exists():
            return c
    return None


def synthesize(prompt: str, model: str) -> str:
    cb = claude_bin()
    if not cb:
        raise RuntimeError("claude CLI not found")
    p = subprocess.run(
        [cb, "-p", prompt, "--model", model, "--setting-sources", "",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        cwd=str(VAULT), capture_output=True, text=True, timeout=240,
        env={**os.environ, "ULTRON_VOICE": "1", "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1"})
    if p.returncode != 0:
        raise RuntimeError(f"claude -p failed (rc={p.returncode}): {p.stderr[:300]}")
    return p.stdout.strip()


def gather(queries: list[str], top: int) -> tuple[str, set]:
    blocks, cited = [], set()
    for q in queries:
        hits = recall(q, top)
        for h in hits:
            if h.get("path"):
                cited.add(h["path"])
        if hits:
            blocks.append("\n".join(f"  - [{h['path']}] {h.get('snippet','')[:240]}"
                                    for h in hits if h.get("path")))
    return "\n".join(blocks), cited


def build_playbook(model: str, top: int, dry: bool) -> int:
    win_ctx, win_cited = gather(WIN_QUERIES, top)
    loss_ctx, loss_cited = gather(LOSS_QUERIES, top)
    cited = win_cited | loss_cited
    print(f"[win_patterns] corpus: {len(win_cited)} win-sources, {len(loss_cited)} loss-sources, {len(cited)} unique.")
    if not cited:
        print("ERROR: recall returned nothing — is the recall daemon (127.0.0.1:7766) up?", file=sys.stderr)
        return 1
    prompt = f"""You are a bid strategist working for your firm. From the retrieved corpus below (proposition
decks, win-stories, competitive notes — NOT bid outcomes), extract the recurring patterns that
distinguish WINS, and the patterns behind LOSSES.

=== WIN-CORPUS SOURCES ===
{win_ctx}

=== LOSS-CORPUS SOURCES ===
{loss_ctx}

Write a Markdown win-playbook with EXACTLY these sections:
## Win factors (ranked)
  A numbered list. Each: **bold factor name** — one-line description — `how to apply to a new bid` — cite the vault path(s) `[path]` it's grounded in.
## Loss factors to avoid
  Same shape, from the loss corpus.
## The 5-move win checklist
  5 concrete, do-this-on-every-bid actions distilled from the above.

Ground every factor in a cited source. State ONLY what the corpus supports — if loss data is thin, say so rather than inventing. No preamble."""
    if dry:
        print("--- DRY RUN ---\n" + prompt[:1800]); return 0
    print(f"[win_patterns] synthesizing playbook with {model} …")
    body = synthesize(prompt, model)
    if not body.strip():
        print("ERROR: empty synthesis", file=sys.stderr); return 1
    fm = ("---\ntype: methodology\ntitle: \"Win Playbook\"\n"
          f"generated: {utcnow()}\nengine: native-qdrant\n"
          "sources_cited:\n" + "".join(f'  - "{p}"\n' for p in sorted(cited)) +
          "tags: [methodology, win-loss, playbook]\n---\n\n")
    # Phase 2: dynamic outcome-ledger label (PLAN §4 — replaces the hard-coded static string).
    n_won_bids = _count_distinct_won_bids()
    if n_won_bids >= MIN_CLOSED:
        outcome_label = f"closed outcomes: {n_won_bids} won bids"
    else:
        outcome_label = f"closed outcomes: {n_won_bids} (LOW CONFIDENCE — corpus-derived)"
    PLAYBOOK.parent.mkdir(parents=True, exist_ok=True)
    PLAYBOOK.write_text(fm + f"# Win Playbook\n\n> Learned from the proposition/win-story corpus "
                        f"({outcome_label}). Re-run as bids close to sharpen.\n\n" + body + "\n",
                        encoding="utf-8")
    print(f"[win_patterns] wrote {PLAYBOOK.relative_to(VAULT)} ({len(cited)} sources, {outcome_label}).")
    return 0


def score_bid(bid_path: str, model: str, top: int, dry: bool) -> int:
    bid_dir = (VAULT / bid_path).resolve()
    if not bid_dir.is_dir():
        print(f"ERROR: bid not found: {bid_dir}", file=sys.stderr); return 2
    if not PLAYBOOK.exists():
        print("ERROR: no win-playbook yet — run without --bid first.", file=sys.stderr); return 2
    research = bid_dir / "research.md"
    rfp = bid_dir / "rfp-source.md"
    src = research if research.exists() else rfp
    if not src.exists():
        print(f"ERROR: {bid_path} has neither research.md nor rfp-source.md.", file=sys.stderr); return 2
    bid_text = strip_frontmatter(src.read_text(encoding="utf-8"))
    playbook = strip_frontmatter(PLAYBOOK.read_text(encoding="utf-8"))
    prompt = f"""You are a bid strategist working for your firm. Score this bid against our win-playbook.

=== WIN-PLAYBOOK ===
{playbook[:6000]}

=== THIS BID ({bid_dir.name}) ===
{bid_text[:6000]}

Write Markdown:
## Win-factor coverage
  For each playbook win-factor: ✅ present / ⚠️ partial / ❌ missing — one line of evidence from the bid.
## Top 3 moves to raise win probability
  Concrete, specific to THIS bid, each tied to a playbook factor.
## Estimated win-rate lever
  One sentence: the single highest-impact change.
Ground in the bid text + playbook only. No invention."""
    if dry:
        print("--- DRY RUN ---\n" + prompt[:1800]); return 0
    print(f"[win_patterns] scoring {bid_dir.name} against playbook with {model} …")
    body = synthesize(prompt, model)
    out = bid_dir / "win-recs.md"
    out.write_text(f"---\ntype: win-recs\ngenerated: {utcnow()}\ntags: [win-recs]\n---\n\n"
                   f"# Win recommendations — {bid_dir.name}\n\n" + body + "\n", encoding="utf-8")
    print(f"[win_patterns] wrote {out.relative_to(VAULT)}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bid", help="prescriptive mode: score this bid folder against the playbook")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.bid:
        return score_bid(args.bid, args.model, args.top, args.dry_run)
    return build_playbook(args.model, args.top, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
