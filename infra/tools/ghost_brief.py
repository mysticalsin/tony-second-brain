#!/usr/bin/env python3
"""ghost_brief.py — Competitor ghost brief generator for a bid folder.

Standalone tool. Reads the bid's rfp-model.json, queries _wiki/competitive/ for
named rivals, pulls past head-to-heads via recall_vector.py (semantic index), and
synthesizes per-competitor positioning intel via a bounded claude -p call.

Output: ghost-brief.md written to the bid folder.

Usage (run from vault root):
    python build/tools/ghost_brief.py "RFPs/Globex Cloud Migration"
    python build/tools/ghost_brief.py "<bid>" --dry-run   # print prompt, don't write

HOW TO WIRE INTO rfp_pipeline.py AS A FUTURE STAGE
----------------------------------------------------
1. Add stage "GHOST" after stage "WIN-RECS" in rfp_pipeline.py's STAGES list.
2. In the run_stage("ghost") block:
       from ghost_brief import run as ghost_run
       ghost_run(bid_path, vault, dry_run=False)
3. Add --skip-ghost flag alongside the existing --skip-* flags.
4. Reference ghost-brief.md in the RESEARCH stage prompt so the LLM can
   use competitor positioning as research context (currently it doesn't).
No other code change required — the file path contract is
    <bid_folder>/ghost-brief.md
which is already the ghost_brief.py output path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
COMPETITIVE_WIKI = VAULT / "_wiki" / "competitive"
TOOLS = VAULT / "build" / "tools"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def claude_bin() -> str:
    for c in (str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", shutil.which("claude")):
        if c and Path(c).exists():
            return c
    raise RuntimeError("claude CLI not found")


def synthesize(prompt: str, model: str = "claude-haiku-4-5") -> str:
    """Bounded, fast claude -p call.  Uses haiku for speed + cost."""
    cb = claude_bin()
    result = subprocess.run(
        [cb, "-p", prompt, "--model", model,
         "--setting-sources", "",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        cwd=str(VAULT),
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1",
             "ULTRON_VOICE": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (rc={result.returncode}): {result.stderr[:400]}"
        )
    return result.stdout.strip()


def recall_vector(query: str, top: int = 8) -> list[dict]:
    """Query the local Qdrant semantic index.  Returns [] on DB-locked or missing."""
    script = TOOLS / "recall_vector.py"
    if not script.exists():
        print(f"  [ghost] recall_vector.py not found at {script} — skipping vector recall",
              file=sys.stderr)
        return []
    uv = shutil.which("uv")
    if not uv:
        print("  [ghost] uv not found — skipping vector recall", file=sys.stderr)
        return []
    result = subprocess.run(
        [uv, "run", "--with", "qdrant-client", "--with", "fastembed",
         "python", str(script), query, "--top", str(top)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ},
    )
    if result.returncode == 3:
        print("  [ghost] Qdrant DB locked — skipping vector recall", file=sys.stderr)
        return []
    if result.returncode == 4:
        print("  [ghost] Qdrant collection missing — skipping vector recall", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"  [ghost] recall_vector error: {result.stderr[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


# --------------------------------------------------------------------------- #
# Stage 1 — read rfp-model.json
# --------------------------------------------------------------------------- #

def load_rfp_model(bid_path: Path) -> dict:
    f = bid_path / "rfp-model.json"
    if not f.exists():
        raise FileNotFoundError(f"rfp-model.json not found: {f}")
    return json.loads(f.read_text())


# --------------------------------------------------------------------------- #
# Stage 2 — resolve competitors from _wiki/competitive/
# --------------------------------------------------------------------------- #

def load_competitive_wiki() -> list[dict]:
    """
    Read all .md files in _wiki/competitive/ (except _index.md).
    Return list of {slug, content}.
    """
    items = []
    if not COMPETITIVE_WIKI.exists():
        return items
    for f in sorted(COMPETITIVE_WIKI.glob("*.md")):
        if f.stem.startswith("_"):
            continue
        items.append({"slug": f.stem, "content": f.read_text()})
    return items


def infer_competitors_from_scope(rfp_model: dict) -> list[str]:
    """
    Derive the likely competitor set from RFP scope + client using domain knowledge.
    This is pure reasoning — no index required.

    For large ERP-AMS RFPs the typical bidder landscape is:
    Accenture, Capgemini, IBM, Deloitte, Wipro, Infosys, TCS, Cognizant,
    NTT Data, HCL, UST, EY, KPMG.
    We narrow based on RFP size signals and LATAM footprint.
    """
    client = rfp_model.get("client", "")
    scope = rfp_model.get("scope", "")
    title = rfp_model.get("title", "")

    combined = f"{client} {scope} {title}".lower()

    # ERP AMS: narrow to firms with known ERP + regional delivery capability
    if "sap" in combined and ("latam" in combined or "latin" in combined or "brazil" in combined):
        return [
            "Accenture",
            "Capgemini",
            "IBM",
            "Wipro",
            "Infosys",
            "NTT Data",
        ]

    # Generic fallback for consulting RFPs
    if "consulting" in combined or "management" in combined or "services" in combined:
        return ["Accenture", "Capgemini", "Deloitte", "IBM", "TCS"]

    return ["Accenture", "Capgemini", "IBM"]


# --------------------------------------------------------------------------- #
# Stage 3 — recall head-to-heads from semantic index
# --------------------------------------------------------------------------- #

def pull_recall_context(client: str, scope: str, competitors: list[str]) -> str:
    """
    Fire recall_vector.py queries for competitive + win-story context.
    Returns concatenated snippets or empty string.
    """
    queries = [
        f"{client} SAP AMS competitor win",
        f"ERP AMS competitor positioning consulting",
        f"competitor ghosting {' '.join(competitors[:3])}",
        "win theme SAP AMS beat competitor",
    ]
    seen: set[str] = set()
    snippets: list[str] = []
    for q in queries:
        print(f"  [ghost] recall: {q!r}", file=sys.stderr)
        hits = recall_vector(q, top=5)
        for h in hits:
            key = h.get("path", "") + h.get("snippet", "")[:60]
            if key in seen:
                continue
            seen.add(key)
            score = h.get("score", 0)
            path = h.get("path", "")
            snippet = h.get("snippet", "").strip()
            if snippet:
                snippets.append(f"[score={score:.2f} | {path}]\n{snippet[:300]}")
    if not snippets:
        return ""
    return "\n\n---\n\n".join(snippets[:12])


# --------------------------------------------------------------------------- #
# Stage 4 — synthesize ghost brief
# --------------------------------------------------------------------------- #

GHOST_PROMPT_TMPL = """You are an expert bid strategist at your firm writing a competitor ghost brief.

## RFP Context
Client: {client}
Scope: {scope}
Title: {title}
Evaluation criteria (weights ~equal, inferred):
{eval_criteria}

## Competitors to ghost
{competitors}

## Competitive wiki intel (from vault — may be thin for this specific account)
{wiki_intel}

## Recall index snippets (semantic search over 16000+ vault chunks)
{recall_intel}

## Our known strengths for this bid
- Active delivery relationship inside the client (not a cold pitch)
- The firm's AI Lead has insider system knowledge of the client environment
- Live LATAM SAP AMS reference available (T&M, managed services)
- RISE+Max Success operator positioning (SAP Joule on ECC6 hybrid)
- Brazil S/4HANA migration locked mid-2028 — bridge scope is defined
- ServiceNow integration track record (competitor differentiator)

---

Write a ghost brief with this structure. Be concrete, specific, and honest — if intel is thin, say so and reason from domain knowledge.

# Ghost Brief — {title}

## Purpose
1-paragraph: why this brief exists, how to use it in proposal drafting.

## Competitor Profiles

For each of the {n_competitors} competitors below, produce a structured block:

### [Competitor Name]
**How they will bid this:**
3-5 bullets covering their likely positioning, team structure, price posture, LATAM credentials.

**Their structural weaknesses on THIS bid:**
2-4 specific weaknesses — things that are genuinely harder for them than for your firm, given the RFP criteria and our known advantages.

**Ghosting move:**
1-2 sentences that can be woven into the proposal (without naming the competitor) to seed doubt about this weakness in the evaluator's mind. Must be truthful — exploits a real gap, not manufactured.

**Win themes that neutralize them:**
2-3 specific win-theme ideas that flip their strength into our advantage.

---

## Cross-competitor patterns

After all profiles: 2-3 observations that apply to the whole competitive field — common weaknesses, shared blind spots, or RFP criteria where your firm stands alone.

## Recommended ghosting phrases for proposal

3-5 specific sentences (ready to paste into proposal sections) that subtly reinforce your firm's differentiators against the field. No competitor names. All truthful.

## What remains uncertain

Honest 3-5 bullet list of gaps in competitive intel — what to confirm through network/champion before submission.

---

Keep the whole document under 900 words. No padding. Strategic register, not template language."""


def build_ghost_brief(rfp_model: dict, dry_run: bool = False) -> str:
    """Full orchestration: wiki + recall + synthesize → markdown."""

    client = rfp_model.get("client", "Unknown client")
    scope = rfp_model.get("scope", "")
    title = rfp_model.get("title", "")
    eval_criteria = "\n".join(
        f"- {c['name']} ({c.get('weight', '?')}%)"
        for c in rfp_model.get("eval_criteria", [])
    )

    print(f"[ghost] Client: {client}", file=sys.stderr)
    print(f"[ghost] Scope: {scope}", file=sys.stderr)

    # Stage 2: resolve competitors
    competitors = infer_competitors_from_scope(rfp_model)
    print(f"[ghost] Competitors: {competitors}", file=sys.stderr)

    # Stage 2b: wiki intel
    wiki_items = load_competitive_wiki()
    if wiki_items:
        wiki_intel = "\n\n".join(
            f"### {item['slug']}\n{item['content'][:800]}"
            for item in wiki_items
        )
    else:
        wiki_intel = "No competitive wiki entries found for this client/scope."

    print(f"[ghost] Wiki entries loaded: {len(wiki_items)}", file=sys.stderr)

    # Stage 3: recall
    print("[ghost] Querying recall index...", file=sys.stderr)
    recall_intel = pull_recall_context(client, scope, competitors)
    if not recall_intel:
        recall_intel = "Recall index unavailable or returned no relevant snippets."
    print(f"[ghost] Recall snippets: {len(recall_intel.split(chr(10))) // 4} chunks",
          file=sys.stderr)

    # Stage 4: synthesize
    prompt = GHOST_PROMPT_TMPL.format(
        client=client,
        scope=scope,
        title=title,
        eval_criteria=eval_criteria,
        competitors="\n".join(f"- {c}" for c in competitors),
        wiki_intel=wiki_intel,
        recall_intel=recall_intel[:3000],  # cap to avoid token bloat
        n_competitors=len(competitors),
    )

    if dry_run:
        print("--- PROMPT (dry-run) ---")
        print(prompt)
        print("--- END PROMPT ---")
        return ""

    print("[ghost] Synthesizing... (claude-haiku-4-5, bounded 180s)", file=sys.stderr)
    raw = synthesize(prompt)

    # Wrap in frontmatter
    fm = (
        "---\n"
        f"type: ghost-brief\n"
        f"generated: {utcnow()}\n"
        f"client: \"{client}\"\n"
        f"competitors: {json.dumps(competitors)}\n"
        f"wiki_entries_loaded: {len(wiki_items)}\n"
        f"recall_available: {recall_intel != 'Recall index unavailable or returned no relevant snippets.'}\n"
        "tags: [ghost-brief, competitive]\n"
        "---\n\n"
    )
    return fm + raw


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def run(bid_path: Path, vault: Path, dry_run: bool = False) -> Path | None:
    """Importable entry point for future pipeline wiring."""
    global VAULT
    VAULT = vault
    rfp_model = load_rfp_model(bid_path)
    content = build_ghost_brief(rfp_model, dry_run=dry_run)
    if dry_run or not content:
        return None
    out = bid_path / "ghost-brief.md"
    out.write_text(content)
    print(f"[ghost] Written: {out}", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate a competitor ghost brief for a bid folder."
    )
    ap.add_argument("bid", help="Bid folder path (relative to vault root or absolute)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the synthesize prompt but don't call claude or write files")
    ap.add_argument("--vault", default=str(VAULT),
                    help="Vault root (default: $VAULT or cwd)")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    bid_raw = args.bid
    bid_path = (vault / bid_raw).resolve() if not Path(bid_raw).is_absolute() else Path(bid_raw).resolve()

    if not bid_path.exists():
        print(f"ERROR: bid path not found: {bid_path}", file=sys.stderr)
        return 1

    out = run(bid_path, vault, dry_run=args.dry_run)
    if out:
        print(f"ghost-brief written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
