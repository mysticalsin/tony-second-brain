#!/usr/bin/env python3
"""rfp_research.py — native RFP-research engine (NotebookLM replacement).

Reads a bid's rfp-source.md, retrieves grounding from the local recall Qdrant
index (via recall_vector.py — fastembed MiniLM, on-disk, no server), then has
the Claude CLI synthesize a cited research.md into the bid folder.

NO VOICE. Pure script — never touches Ultron / TTS. Run from the vault root:

    python build/tools/rfp_research.py "RFPs/Globex/AI QA/Q2-AI-QA"
    python build/tools/rfp_research.py "RFPs/Globex/AI QA/Q2-AI-QA" --dry-run

Decisions: AskUserQuestion 2026-06-08 — native replication over a NotebookLM
MCP (confidential RFPs stay local; no cookie automation). Sibling of
recall_vector.py / build_brain_index.py in build/tools/ (local-only dir).
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
RECALL = "build/tools/recall_vector.py"

# Research sections → (heading in research.md, retrieval-query intent).
SECTIONS = [
    ("win_themes", "Win themes (grounded in prior wins)",
     "win themes differentiators why we win for {company} {topic}"),
    ("requirement_capability", "Requirement → our capability map",
     "{topic} capabilities case studies delivery approach for the requirements"),
    ("competitive", "Competitive read",
     "competitors {topic} {company} competitive positioning displacement"),
    ("pricing", "Pricing rhythm (from deck corpus)",
     "{topic} pricing model rate card commercial structure margin"),
    ("risks", "Risks & gaps",
     "{topic} delivery risks gaps red flags compliance constraints"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict (shallow), body)."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, m.group(2)


def recall(query: str, top: int) -> list[dict]:
    """Query the local recall index. Returns [] on any failure.

    Primary path = the warm recall daemon (127.0.0.1:7766), which owns the
    on-disk Qdrant single-writer lock — same contract Ultron uses. The uv
    fallback only succeeds when the daemon is DOWN (otherwise: 'locked').
    """
    import urllib.request, urllib.parse
    try:
        url = "http://127.0.0.1:7766/retrieve?q=" + urllib.parse.quote(query) + f"&top={top}"
        with urllib.request.urlopen(url, timeout=6) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                if isinstance(data, list):
                    return data
    except Exception:
        pass  # daemon down → fall through to uv (works only if lock is free)
    uv = shutil.which("uv")
    if not uv:
        return []
    try:
        r = subprocess.run(
            [uv, "run", "--quiet", "--with", "qdrant-client", "--with", "fastembed",
             "python", RECALL, query, "--top", str(top)],
            cwd=str(VAULT), capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        return json.loads(r.stdout)
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
        raise RuntimeError("claude CLI not found (looked in ~/.local/bin, /usr/local/bin, /opt/homebrew/bin, PATH)")
    p = subprocess.run(
        [cb, "-p", prompt, "--model", model,
         "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        cwd=str(VAULT), capture_output=True, text=True, timeout=180,
        env={**os.environ, "ULTRON_VOICE": "1", "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1"},
    )
    if p.returncode != 0:
        raise RuntimeError(f"claude -p failed (rc={p.returncode}): {p.stderr[:300]}")
    return p.stdout.strip()


def build_prompt(company: str, topic: str, rfp_text: str, retrieved: dict, extra: str = "") -> str:
    blocks = []
    for key, heading, _ in SECTIONS:
        hits = retrieved.get(key, [])
        if hits:
            lines = "\n".join(f"  - [{h['path']}] {h.get('snippet','')[:240]}" for h in hits if h.get("path"))
            blocks.append(f"### Sources for: {heading}\n{lines}")
        else:
            blocks.append(f"### Sources for: {heading}\n  (no vault matches retrieved)")
    sources_block = "\n\n".join(blocks)
    headings = "\n".join(f"## {h}" for _, h, _ in SECTIONS)
    return f"""You are a bid-research analyst working for your firm. Produce the body of a research note for an RFP.

COMPANY: {company}
TOPIC: {topic}
{extra}
=== RFP SOURCE (the request) ===
{rfp_text[:8000]}

=== RETRIEVED VAULT SOURCES (grounding — prior wins, case studies, account intel) ===
{sources_block}

INSTRUCTIONS:
- Write Markdown using EXACTLY these section headings, in order:
{headings}
- Ground every claim. When a point comes from a retrieved source, cite its vault path inline like `[path]`.
- State ONLY what the RFP text or retrieved sources support. If a section has nothing, write "Nothing recorded yet — needs human input." Do NOT invent clients, numbers, dates, or competitors.
- For "Requirement → our capability map", use a Markdown table: | RFP requirement | Our answer | Evidence (vault path) |.
- If a STRUCTURED RFP MODEL is present, drive the requirement map from ITS evaluation criteria, in weight order (heaviest first).
- If an ACCOUNT DOSSIER is present, ground "why us" in THIS client's named stakeholders, prior bids, and relationship — cite those paths.
- Be concise and concrete. No preamble, no closing remarks — output the sections only."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bid_path", help="bid folder relative to vault, e.g. 'RFPs/Globex/AI QA/Q2-AI-QA'")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--dry-run", action="store_true", help="retrieve + build prompt, skip the LLM + write")
    args = ap.parse_args()

    bid_dir = (VAULT / args.bid_path).resolve()
    src = bid_dir / "rfp-source.md"
    if not bid_dir.is_dir():
        print(f"ERROR: bid folder not found: {bid_dir}", file=sys.stderr)
        return 2
    if not src.exists():
        print(f"ERROR: no rfp-source.md in {args.bid_path} — drop the RFP there first (copy from RFPs/_template/).", file=sys.stderr)
        return 2

    _, rfp_body = strip_frontmatter(src.read_text(encoding="utf-8"))
    if len(rfp_body.strip()) < 80:
        print(f"ERROR: rfp-source.md looks empty ({len(rfp_body.strip())} chars). Paste the RFP text first.", file=sys.stderr)
        return 2

    brief = bid_dir / "00 - Brief.md"
    bfm, _ = strip_frontmatter(brief.read_text(encoding="utf-8")) if brief.exists() else ({}, "")
    company = bfm.get("company") or bid_dir.parent.parent.name
    topic = bfm.get("topic") or bid_dir.parent.name

    # Retrieve grounding per section.
    retrieved: dict[str, list] = {}
    cited: set[str] = set()
    for key, _, q in SECTIONS:
        query = q.format(company=company, topic=topic)
        hits = recall(query, args.top)
        retrieved[key] = hits
        for h in hits:
            if h.get("path"):
                cited.add(h["path"])
    print(f"[rfp_research] {company} / {topic}: retrieved {sum(len(v) for v in retrieved.values())} hits across {len(SECTIONS)} sections, {len(cited)} unique sources.")

    extra = ""
    mj = bid_dir / "rfp-model.json"
    if mj.exists():
        extra += "\n=== STRUCTURED RFP MODEL (criteria + clauses) ===\n" + mj.read_text(encoding="utf-8")[:3000] + "\n"
    ac = bid_dir / "_account-context.md"
    if ac.exists():
        extra += "\n=== ACCOUNT DOSSIER (this client) ===\n" + ac.read_text(encoding="utf-8")[:3500] + "\n"
    prompt = build_prompt(company, topic, rfp_body, retrieved, extra)
    if args.dry_run:
        print("--- DRY RUN: prompt built, skipping LLM + write ---")
        print(prompt[:1500])
        return 0

    print(f"[rfp_research] synthesizing with {args.model} …")
    body = synthesize(prompt, args.model)
    if not body.strip():
        print("ERROR: empty synthesis — not overwriting research.md", file=sys.stderr)
        return 1

    fm = (
        "---\n"
        "type: rfp-research\n"
        f'company: "{company}"\n'
        f'topic: "{topic}"\n'
        f'opportunity: "{bid_dir.name}"\n'
        f"generated: {utcnow()}\n"
        "engine: native-qdrant\n"
        "sources_cited:\n" + "".join(f'  - "{p}"\n' for p in sorted(cited)) +
        "confidence: 0.6\n"
        "tags: [rfp-research]\n"
        "---\n\n"
    )
    out = bid_dir / "research.md"
    out.write_text(fm + f"# Research — {company} {topic}\n\n" + body + "\n", encoding="utf-8")
    print(f"[rfp_research] wrote {out.relative_to(VAULT)} ({len(cited)} sources cited).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
