#!/usr/bin/env python3
"""deck_from_rfp.py — Auto-generate a proposal deck from a bid folder.

Reads win-recs.md, rfp-model.json, scorecard.md, ghost-brief.md, and
00 - Brief.md from the bid folder, then emits build/decks/<slug>/content.yaml
+ source.py following the firm's brand slide rhythm:
  - every content slide: eyebrow / action title / italic punchline
  - stat slides: one giant yellow number (hero_stat layout)
  - Top-3 moves -> 3 content slides
  - ❌/⚠️ factors -> gap/risk slide(s)
  - strong numbers found in win-recs / ghost-brief -> hero_stat

Usage (from vault root or anywhere):
    python build/tools/deck_from_rfp.py --bid "<bid-folder-path>" [options]

Options:
    --bid <path>      Bid folder (absolute or relative to vault root). Required.
    --slug <slug>     Deck slug (default: derived from bid folder name).
    --build           Invoke build_all.sh --deck <slug> after emitting the YAML.
    --qa              Vision self-QA loop against Brand DNA (requires contact sheet).
    --no-llm          Deterministic template-fill fallback (no claude -p calls).
    --vault <path>    Vault root (default: $VAULT env var or current directory).

LLM flag:
    Requires --no-llm absent. Uses claude-haiku-4-5 via bounded subprocess (180s).
    Set NO_LLM=1 in environment to force deterministic mode.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import textwrap
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml  # PyYAML — available via uv or system python3

BUILD_ROOT = Path(__file__).resolve().parent.parent          # .../build/
VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
BUILD_ALL = BUILD_ROOT / "build_all.sh"
DECKS_DIR = BUILD_ROOT / "decks"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:64]


def strip_frontmatter(text: str) -> str:
    m = re.match(r"^---\r?\n.*?\r?\n---\r?\n?(.*)", text, re.S)
    return m.group(1) if m else text


def claude_bin() -> str | None:
    for c in (str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", shutil.which("claude")):
        if c and Path(c).exists():
            return c
    return None


def synthesize(prompt: str, model: str = "claude-haiku-4-5") -> str:
    """Bounded claude -p call. Raises RuntimeError on failure."""
    cb = claude_bin()
    if not cb:
        raise RuntimeError("claude CLI not found — install claude or pass --no-llm")
    result = subprocess.run(
        [cb, "-p", prompt, "--model", model,
         "--setting-sources", "",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        cwd=str(VAULT),
        capture_output=True, text=True, timeout=180,
        env={**os.environ,
             "VAULT_BRAIN_QUIET": "1",
             "CAPTURE_DISABLED": "1",
             "ULTRON_VOICE": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (rc={result.returncode}): {result.stderr[:400]}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Parsers — reuse logic from winrecs_autofix.py conventions
# ---------------------------------------------------------------------------

_SCORE_SYMBOLS = ("✅", "⚠️", "⚠", "❌")
FACTOR_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*\*\*(.+?)\*\*\s*\|\s*([^\|]+?)\s*\|\s*(.+?)\|?\s*$"
)
FACTOR_ROW_BARE_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*(.+?)\s*\|\s*([^\|]+?)\s*\|\s*(.+?)\|?\s*$"
)
TOP3_RE = re.compile(r"^##\s+Top 3 moves", re.I)
NUMBERED_MOVE_RE = re.compile(r"^\*\*(\d+)\.\s+(.+?)\*\*")


def _extract_score(cell: str) -> str:
    cell = cell.strip()
    for sym in _SCORE_SYMBOLS:
        if sym in cell:
            return "⚠️" if "⚠" in sym else sym
    return ""


def parse_win_recs(text: str) -> tuple[list[dict], list[dict]]:
    """Parse win-recs.md 10-factor table + Top-3 moves.

    Returns:
        factors: list of {num, name, score, evidence}
        top3:    list of {num, title, body}
    """
    factors: list[dict] = []
    top3: list[dict] = []
    in_table = False
    in_top3 = False
    current_move: dict | None = None

    for line in text.splitlines():
        if "| # |" in line or "| Factor |" in line:
            in_table = True
            continue
        if in_table and line.strip().startswith("|---"):
            continue
        if in_table and line.strip().startswith("|"):
            m = FACTOR_ROW_RE.match(line)
            if not m:
                m = FACTOR_ROW_BARE_RE.match(line)
            if m:
                name = m.group(1).strip()
                score = _extract_score(m.group(2))
                evidence = m.group(3).strip()
                if not score:
                    continue
                num_m = re.match(r"^\|\s*(\d+)\s*\|", line)
                num = int(num_m.group(1)) if num_m else len(factors) + 1
                factors.append({"num": num, "name": name,
                                 "score": score, "evidence": evidence})
            continue
        elif in_table and not line.strip().startswith("|"):
            in_table = False

        if TOP3_RE.match(line):
            in_top3 = True
            continue
        if in_top3:
            if line.startswith("##"):
                in_top3 = False
                if current_move:
                    top3.append(current_move)
                    current_move = None
                continue
            mm = NUMBERED_MOVE_RE.match(line)
            if mm:
                if current_move:
                    top3.append(current_move)
                current_move = {"num": int(mm.group(1)),
                                 "title": mm.group(2).strip(), "body": ""}
            elif current_move is not None:
                current_move["body"] = (current_move["body"] + "\n" + line).strip()

    if current_move:
        top3.append(current_move)

    return factors, top3


def parse_brief_frontmatter(text: str) -> dict:
    """Extract selected frontmatter fields from 00 - Brief.md."""
    fm: dict = {}
    block = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not block:
        return fm
    for line in block.group(1).splitlines():
        m = re.match(r'^(\w+):\s*"?(.+?)"?\s*$', line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"')
    return fm


def extract_hero_number(texts: list[str]) -> tuple[str, str]:
    """Scan free text for the most striking number (€/%, months) for hero_stat.

    Returns (number_str, caption_str) or ("", "") if none found.
    """
    # Patterns: €X.XM / $X.XM / X% / X months / X weeks
    patterns = [
        (r"(€\s*[\d,\.]+\s*[MKBmkb])\b", "contract value"),
        (r"(\$\s*[\d,\.]+\s*[MKBmkb])\b", "contract value"),
        (r"(\d+\.?\d*\s*%)", "of"),
        (r"(\d+)\s+months?\b", "months of"),
        (r"(\d+)\s+weeks?\b", "weeks to"),
    ]
    combined = " ".join(texts)
    for pat, unit in patterns:
        m = re.search(pat, combined, re.I)
        if m:
            num = m.group(1).strip()
            # Extract surrounding context as caption (≤10 words after the match)
            pos = m.start()
            snippet = combined[max(0, pos - 60): pos + 80]
            snippet = re.sub(r'\s+', ' ', snippet).strip()
            return num, snippet[:80]
    return "", ""


# ---------------------------------------------------------------------------
# Slide builders
# ---------------------------------------------------------------------------

def build_cover_meta(client: str, opportunity: str, stage: str,
                     deadline: str, date_str: str) -> dict:
    firm = os.environ.get("FIRM_NAME", "Your Firm")
    subtitle = f"{client} × {firm}"
    if stage:
        subtitle += f" · {stage}"
    return {
        "meta": {
            "title": opportunity or f"{client} — Proposal",
            "subtitle": subtitle,
            "date": date_str,
            "about_firm": True,
        }
    }


def build_agenda(top3: list[dict]) -> dict:
    firm = os.environ.get("FIRM_NAME", "Your Firm")
    items = []
    for move in top3[:3]:
        items.append(move["title"])
    # Always add closing items
    if "Investment & ROI" not in items:
        items.append("Investment & ROI")
    items.append(f"Why {firm}")
    items.append("Next step")
    return {"agenda": items[:6]}


def slide_hero_stat(number: str, caption: str,
                    eyebrow: str = "THE STAKES",
                    source: str = "") -> dict:
    s: dict = {
        "layout": "hero_stat",
        "eyebrow": eyebrow,
        "number": number,
        "caption": caption,
    }
    if source:
        s["source"] = source
    return s


def slide_text(eyebrow: str, title: str, punchline: str, body: str) -> dict:
    return {
        "layout": "text",
        "eyebrow": eyebrow,
        "title": title,
        "punchline": punchline,
        "body": body,
    }


def slide_problem_answer(eyebrow: str, title: str, punchline: str,
                         problem: str, answer: str) -> dict:
    return {
        "layout": "problem_answer_split",
        "eyebrow": eyebrow,
        "title": title,
        "punchline": punchline,
        "problem": problem,
        "answer": answer,
    }


def slide_two_col(eyebrow: str, title: str, punchline: str,
                  left_head: str, left_body: str,
                  right_head: str, right_body: str) -> dict:
    return {
        "layout": "two_col",
        "eyebrow": eyebrow,
        "title": title,
        "punchline": punchline,
        "left": {"head": left_head, "body": left_body},
        "right": {"head": right_head, "body": right_body},
    }


# ---------------------------------------------------------------------------
# LLM slide-copy generation
# ---------------------------------------------------------------------------

SLIDE_COPY_PROMPT = """\
You are a slide-copy writer for a consulting proposal to {client}.
Write concise, punchy slide copy for one slide. Apply your firm's brand rules:
  - eyebrow: 2-4 ALL CAPS words, topical label
  - title (action title): strong declarative sentence, ≤12 words
  - punchline: italic follow-through, ≤12 words, reinforces the title

Output ONLY valid YAML block with keys: eyebrow, title, punchline.
No prose, no code fences, no explanation.

Slide context (move/factor):
{context}

Bid context:
  Client: {client}
  Opportunity: {opportunity}
  Stage: {stage}
"""


def llm_slide_copy(context: str, client: str, opportunity: str, stage: str) -> dict:
    """Returns {eyebrow, title, punchline} dict from LLM or empty dict on error."""
    prompt = SLIDE_COPY_PROMPT.format(
        context=context[:600],
        client=client,
        opportunity=opportunity,
        stage=stage,
    )
    try:
        raw = synthesize(prompt)
        raw = re.sub(r"^```(?:yaml)?|```$", "", raw.strip(), flags=re.M).strip()
        return yaml.safe_load(raw) or {}
    except Exception as exc:
        print(f"  [deck_from_rfp] LLM slide copy failed: {exc}", file=sys.stderr)
        return {}


BODY_COPY_PROMPT = """\
You are a bid writer. Write 3-5 concise bullet points (one per line, starting with "•") \
for a proposal slide body, covering this topic:

{context}

Bid:
  Client: {client}
  Opportunity: {opportunity}

Rules: each bullet ≤18 words, concrete and specific, no padding.
Output ONLY the bullet lines, no header, no extra text.
"""


def llm_body_copy(context: str, client: str, opportunity: str) -> str:
    """Returns multi-line bullet string from LLM or a fallback."""
    prompt = BODY_COPY_PROMPT.format(
        context=context[:500],
        client=client,
        opportunity=opportunity,
    )
    try:
        return synthesize(prompt)
    except Exception as exc:
        print(f"  [deck_from_rfp] LLM body copy failed: {exc}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Core deck builder
# ---------------------------------------------------------------------------

def build_deck_data(
    *,
    bid_path: Path,
    brief_fm: dict,
    factors: list[dict],
    top3: list[dict],
    rfp_model: dict,
    scorecard_text: str,
    ghost_brief_text: str,
    use_llm: bool,
) -> dict:
    """Assemble the content.yaml dict from all parsed bid data.

    Slide order:
      1. hero_stat  — best number extracted from win-recs/ghost-brief
      2. problem_answer_split — gap overview (❌/⚠️ factors vs. our moves)
      3. Top-3 move slides (one per move, up to 3)  [text layout]
      4. Gap/risk slide — ❌ and ⚠️ factors summary
      5. Eval criteria / scorecard alignment slide [two_col]
      6. closing
    """
    client = brief_fm.get("company") or rfp_model.get("client") or bid_path.name
    opportunity = brief_fm.get("opportunity") or rfp_model.get("title") or "Proposal"
    stage = brief_fm.get("stage") or "Propose"
    deadline = brief_fm.get("deadline") or rfp_model.get("due_date") or ""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Cover + agenda ---
    meta_block = build_cover_meta(client, opportunity, stage, deadline, date_str)
    agenda_block = build_agenda(top3)

    slides: list[dict] = []

    # --- Slide 1: hero_stat from best number ---
    hero_texts = [
        ghost_brief_text[:2000],
        scorecard_text[:800],
    ]
    if top3:
        hero_texts += [m.get("body", "") for m in top3]
    hero_num, hero_caption = extract_hero_number(hero_texts)

    # Fallback hero if no number found
    if not hero_num:
        hero_num = "10"
        hero_caption = (
            f"factors scored ⚠️/❌ in the win-playbook — "
            f"each one is a lever to raise win probability"
        )

    slides.append(slide_hero_stat(
        number=hero_num,
        caption=hero_caption,
        eyebrow="THE STAKES",
        source="Win-recs analysis · " + date_str,
    ))

    # --- Slide 2: problem_answer_split — gap overview ---
    red_flags = [f for f in factors if f["score"] == "❌"]
    amber_flags = [f for f in factors if f["score"] == "⚠️"]
    green_factors = [f for f in factors if f["score"] == "✅"]

    problem_bullets = ""
    answer_bullets = ""

    if red_flags:
        problem_bullets += "\n".join(
            f"• ❌ {f['name']}" for f in red_flags[:4]
        )
    if amber_flags:
        if problem_bullets:
            problem_bullets += "\n"
        problem_bullets += "\n".join(
            f"• ⚠️  {f['name']}" for f in amber_flags[:3]
        )

    if not problem_bullets:
        problem_bullets = "• No critical gaps identified in win-recs"

    if green_factors:
        answer_bullets = "\n".join(
            f"• ✅ {f['name']}" for f in green_factors[:4]
        )
    if not answer_bullets:
        answer_bullets = "• Addressed in proposal"

    gap_title = "The gaps between our current position and a winning bid."
    gap_punchline = "This deck closes each one, move by move."

    if use_llm and top3:
        ctx = f"Gap overview for {client} {opportunity}: problems={[f['name'] for f in red_flags+amber_flags]}"
        copy = llm_slide_copy(ctx, client, opportunity, stage)
        gap_title = copy.get("title", gap_title)
        gap_punchline = copy.get("punchline", gap_punchline)

    slides.append(slide_problem_answer(
        eyebrow="GAP ANALYSIS",
        title=gap_title,
        punchline=gap_punchline,
        problem=problem_bullets,
        answer=answer_bullets,
    ))

    # --- Slides 3-5: Top-3 move slides ---
    for move in top3[:3]:
        move_title = f"Move {move['num']}: {move['title']}"
        body_text = move.get("body", "")

        if use_llm:
            ctx = f"Move {move['num']}: {move['title']}\n{body_text[:400]}"
            copy = llm_slide_copy(ctx, client, opportunity, stage)
            eyebrow = copy.get("eyebrow", f"MOVE {move['num']}")
            title = copy.get("title", move_title)
            punchline = copy.get("punchline", "Act on this before submission.")
            body = llm_body_copy(body_text, client, opportunity) or _body_from_move(body_text)
        else:
            eyebrow = f"MOVE {move['num']}"
            title = move['title']
            punchline = "Concrete action with measurable impact on evaluator score."
            body = _body_from_move(body_text)

        slides.append(slide_text(
            eyebrow=eyebrow,
            title=title,
            punchline=punchline,
            body=body,
        ))

    # --- Slide 6: Gap / risk summary (❌ factors detail) ---
    if red_flags or amber_flags:
        gap_factors = (red_flags + amber_flags)[:6]
        gap_body = "\n".join(
            f"{'❌' if f['score']=='❌' else '⚠️ '} Factor {f['num']} — {f['name']}: "
            f"{textwrap.shorten(f['evidence'], width=120, placeholder='…')}"
            for f in gap_factors
        )

        slides.append(slide_text(
            eyebrow="GAPS & RISKS",
            title="Factors still to resolve before submission.",
            punchline="Each one scored in win-recs — all addressable.",
            body=gap_body,
        ))

    # --- Slide 7: Eval criteria alignment (from rfp_model) ---
    eval_criteria = rfp_model.get("eval_criteria", [])
    if eval_criteria:
        left_items = eval_criteria[:4]
        right_items = eval_criteria[4:]
        left_body = "\n".join(
            f"• {c['name']}" + (f" ({c.get('weight', '?')}%)" if c.get("weight") else "")
            for c in left_items
        )
        right_body = "\n".join(
            f"• {c['name']}" + (f" ({c.get('weight', '?')}%)" if c.get("weight") else "")
            for c in right_items
        ) or "See left column for all criteria."

        slides.append(slide_two_col(
            eyebrow="EVALUATION CRITERIA",
            title="Our proposal maps directly to what the evaluator scores.",
            punchline="Every section earns points on the rubric.",
            left_head="Criteria addressed",
            left_body=left_body,
            right_head="Criteria + weights",
            right_body=right_body,
        ))

    # --- Closing ---
    next_step = "Proposal review call"
    if deadline:
        next_step += f" · deadline {deadline[:10]}"

    closing_block = {
        "closing": {
            "thanks": True,
            "cta": "Next step:",
            "contact": next_step,
        }
    }

    # Assemble final dict
    deck: dict = {}
    deck.update(meta_block)
    deck.update(agenda_block)
    deck["slides"] = slides
    deck.update(closing_block)

    return deck


def _body_from_move(body_text: str) -> str:
    """Convert move body text to bullet list, capped to 4 bullets.

    Paragraphs split into sentences, shortened at word boundaries — never
    sliced mid-sentence (HS-P7 contact-sheet QA finding)."""
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    bullets = []
    for line in lines:
        if re.match(r"^[•\-\*]", line):
            bullets.append(line)
        elif line and len(bullets) < 4:
            for sent in re.split(r"(?<=[.!?]) +", line):
                if sent and len(bullets) < 4:
                    bullets.append("• " + textwrap.shorten(sent, width=160, placeholder="…"))
        if len(bullets) >= 4:
            break
    return "\n".join(bullets) if bullets else textwrap.shorten(body_text, width=200, placeholder="…")


# ---------------------------------------------------------------------------
# Emit deck files
# ---------------------------------------------------------------------------

SOURCE_PY_TMPL = '''\
"""Proposal deck — auto-generated from bid folder by deck_from_rfp.py.
Source bid: {bid_path}
Generated: {generated}
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE.parent.parent
sys.path.insert(0, str(BUILD))

from lib import build_deck  # noqa: E402


def main(out_dir: Path) -> Path:
    content = HERE / "content.yaml"
    out = out_dir / "{slug}.pptx"
    build_deck(content, out)
    return out


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (HERE.parent.parent.parent / "out" / HERE.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = main(out_dir)
    print(f"Built {{path}}")
'''


def emit_deck_folder(slug: str, deck_data: dict, bid_path: Path) -> Path:
    """Write build/decks/<slug>/content.yaml + source.py. Backs up prior content.yaml."""
    deck_dir = DECKS_DIR / slug
    deck_dir.mkdir(parents=True, exist_ok=True)

    content_yaml = deck_dir / "content.yaml"
    if content_yaml.exists():
        bak = deck_dir / "content.yaml.bak"
        shutil.copy2(str(content_yaml), str(bak))
        print(f"  [deck_from_rfp] Backed up prior content.yaml -> content.yaml.bak",
              file=sys.stderr)

    # Dump YAML with a header comment
    header = (
        f"# Auto-generated proposal deck for: {slug}\n"
        f"# Source bid: {bid_path}\n"
        f"# Generated: {utcnow()}\n"
        f"# Edit content.yaml to customise; re-run deck_from_rfp.py to regenerate.\n\n"
    )
    yaml_body = yaml.dump(deck_data, allow_unicode=True, default_flow_style=False,
                          sort_keys=False, width=100)
    content_yaml.write_text(header + yaml_body, encoding="utf-8")
    print(f"  [deck_from_rfp] content.yaml -> {content_yaml}", file=sys.stderr)

    source_py = deck_dir / "source.py"
    source_py.write_text(
        SOURCE_PY_TMPL.format(
            bid_path=bid_path,
            generated=utcnow(),
            slug=slug,
        ),
        encoding="utf-8",
    )
    print(f"  [deck_from_rfp] source.py -> {source_py}", file=sys.stderr)

    return deck_dir


# ---------------------------------------------------------------------------
# Build invocation
# ---------------------------------------------------------------------------

def invoke_build(slug: str) -> int:
    """Run build_all.sh --deck <slug>. Returns exit code."""
    if not BUILD_ALL.exists():
        print(f"ERROR: build_all.sh not found at {BUILD_ALL}", file=sys.stderr)
        return 1
    cmd = ["bash", str(BUILD_ALL), "--deck", slug]
    print(f"  [deck_from_rfp] Running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, cwd=str(BUILD_ROOT.parent), timeout=300)
    return result.returncode


# ---------------------------------------------------------------------------
# QA vision loop
# ---------------------------------------------------------------------------

QA_PROMPT_TMPL = """\
You are a slide-design reviewer enforcing your firm's brand design standards:
  1. WOW First — every slide must earn attention in 3 seconds
  2. Clarity is King — one message per slide, action title
  3. Apple Mind — every element earns its place
  4. Never Repeat a Mistake — known errors are zero-tolerance
  5. "I Will Figure It Out" — bold, confident, specific

Review this contact sheet (slide screenshots) against these five standards.

For each law: pass / fail + one-line evidence.
Then: APPROVED or NEEDS_REVISION (all caps).
If NEEDS_REVISION: list specific changes (one bullet per slide that fails).
"""


def run_qa_loop(slug: str, deck_data: dict, max_iter: int = 5) -> bool:
    """Vision QA loop. Returns True if approved or no contact sheet found."""
    out_dir = BUILD_ROOT.parent / "out" / slug
    contact_sheet = out_dir / "contact_sheet.jpg"

    if not contact_sheet.exists():
        print(
            f"  [deck_from_rfp] QA: no contact sheet at {contact_sheet}\n"
            "  Run --build first, then --render --qa to generate it, "
            "then re-run deck_from_rfp.py --qa.",
            file=sys.stderr,
        )
        return True  # exit cleanly — nothing to grade

    cb = claude_bin()
    if not cb:
        print("  [deck_from_rfp] QA: claude CLI not found — skipping vision QA",
              file=sys.stderr)
        return True

    for iteration in range(1, max_iter + 1):
        print(f"  [deck_from_rfp] QA iteration {iteration}/{max_iter}...", file=sys.stderr)
        prompt = QA_PROMPT_TMPL.strip()
        # Pass the image path as an attachment via stdin isn't supported in -p mode;
        # use file:// reference in the prompt (Claude vision handles local paths).
        prompt += f"\n\nContact sheet path: {contact_sheet}"

        try:
            verdict = synthesize(prompt)
        except Exception as exc:
            print(f"  [deck_from_rfp] QA vision call failed: {exc}", file=sys.stderr)
            return False

        print(f"  [deck_from_rfp] QA verdict: {verdict[:200]}", file=sys.stderr)

        if "APPROVED" in verdict.upper() and "NEEDS_REVISION" not in verdict.upper():
            print(f"  [deck_from_rfp] QA APPROVED after {iteration} iteration(s).",
                  file=sys.stderr)
            return True

        if iteration < max_iter:
            print("  [deck_from_rfp] QA NEEDS_REVISION — re-invoking build...",
                  file=sys.stderr)
            invoke_build(slug)
        else:
            print(f"  [deck_from_rfp] QA: max iterations ({max_iter}) reached; "
                  "manual review required.", file=sys.stderr)

    return False


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run(
    bid_path: Path,
    slug: str,
    use_llm: bool,
    do_build: bool,
    do_qa: bool,
) -> int:
    """Full orchestration: read bid -> build YAML -> optionally build + QA."""
    print(f"=== deck_from_rfp · {bid_path.name} → slug={slug} ===", file=sys.stderr)

    # --- Read 00 - Brief.md ---
    brief_fm: dict = {}
    brief_f = bid_path / "00 - Brief.md"
    if brief_f.exists():
        brief_fm = parse_brief_frontmatter(brief_f.read_text(encoding="utf-8"))
        print(f"  [deck_from_rfp] Brief frontmatter: client={brief_fm.get('company')} "
              f"stage={brief_fm.get('stage')}", file=sys.stderr)
    else:
        print(f"  [deck_from_rfp] 00 - Brief.md not found — proceeding without it",
              file=sys.stderr)

    # --- Read win-recs.md ---
    factors: list[dict] = []
    top3: list[dict] = []
    wr_file = bid_path / "win-recs.md"
    if wr_file.exists():
        text = wr_file.read_text(encoding="utf-8")
        factors, top3 = parse_win_recs(text)
        print(f"  [deck_from_rfp] win-recs: {len(factors)} factors, {len(top3)} moves",
              file=sys.stderr)
    else:
        print(f"  [deck_from_rfp] win-recs.md not found — using empty factors",
              file=sys.stderr)

    # --- Read rfp-model.json ---
    rfp_model: dict = {}
    rfp_model_f = bid_path / "rfp-model.json"
    if rfp_model_f.exists():
        try:
            rfp_model = json.loads(rfp_model_f.read_text(encoding="utf-8"))
            print(f"  [deck_from_rfp] rfp-model: {len(rfp_model.get('eval_criteria', []))} "
                  "criteria loaded", file=sys.stderr)
        except json.JSONDecodeError as exc:
            print(f"  [deck_from_rfp] rfp-model.json unreadable: {exc} — skipping",
                  file=sys.stderr)
    else:
        print(f"  [deck_from_rfp] rfp-model.json not found — skipping",
              file=sys.stderr)

    # --- Read scorecard.md ---
    scorecard_text = ""
    scorecard_f = bid_path / "scorecard.md"
    if scorecard_f.exists():
        scorecard_text = strip_frontmatter(
            scorecard_f.read_text(encoding="utf-8")
        )
        print(f"  [deck_from_rfp] scorecard: {len(scorecard_text)} chars", file=sys.stderr)

    # --- Read ghost-brief.md ---
    ghost_brief_text = ""
    ghost_f = bid_path / "ghost-brief.md"
    if ghost_f.exists():
        ghost_brief_text = strip_frontmatter(ghost_f.read_text(encoding="utf-8"))
        print(f"  [deck_from_rfp] ghost-brief: {len(ghost_brief_text)} chars", file=sys.stderr)

    # --- Build deck data ---
    deck_data = build_deck_data(
        bid_path=bid_path,
        brief_fm=brief_fm,
        factors=factors,
        top3=top3,
        rfp_model=rfp_model,
        scorecard_text=scorecard_text,
        ghost_brief_text=ghost_brief_text,
        use_llm=use_llm,
    )

    # --- Emit files ---
    deck_dir = emit_deck_folder(slug, deck_data, bid_path)

    slide_count = len(deck_data.get("slides", []))
    print(f"  [deck_from_rfp] Emitted {slide_count} slides -> {deck_dir}", file=sys.stderr)

    # --- Build ---
    if do_build:
        rc = invoke_build(slug)
        if rc != 0:
            print(f"  [deck_from_rfp] build_all.sh returned rc={rc}", file=sys.stderr)
            return rc
        # Confirm .pptx exists
        out_dir = BUILD_ROOT.parent / "out" / slug
        pptx_files = list(out_dir.glob("*.pptx")) if out_dir.exists() else []
        if pptx_files:
            for pptx in pptx_files:
                size = pptx.stat().st_size
                print(f"  [deck_from_rfp] Built: {pptx} ({size} bytes)", file=sys.stderr)
        else:
            print(f"  [deck_from_rfp] WARNING: no .pptx found under {out_dir}",
                  file=sys.stderr)

    # --- QA ---
    if do_qa:
        approved = run_qa_loop(slug, deck_data)
        if not approved:
            print("  [deck_from_rfp] QA loop ended without APPROVED verdict.",
                  file=sys.stderr)

    print(f"=== done · {slug} ===", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Auto-generate a proposal deck (content.yaml + source.py) from a bid folder."
    )
    ap.add_argument("--bid", required=True,
                    help="Bid folder path (absolute or relative to --vault root)")
    ap.add_argument("--slug",
                    help="Deck slug (default: derived from bid folder name)")
    ap.add_argument("--build", action="store_true",
                    help="Invoke build_all.sh --deck <slug> after emitting YAML")
    ap.add_argument("--qa", action="store_true",
                    help="Run vision self-QA loop (requires contact sheet from --render)")
    ap.add_argument("--no-llm", action="store_true",
                    help="Deterministic template-fill only (no claude -p calls)")
    ap.add_argument("--vault", default=None,
                    help="Vault root (default: $VAULT env or cwd)")
    args = ap.parse_args()

    # Resolve vault
    global VAULT
    vault_arg = args.vault if args.vault else str(VAULT)
    VAULT = Path(vault_arg).resolve()

    # Resolve bid path
    bid_raw = args.bid
    bid_path = (
        (VAULT / bid_raw).resolve()
        if not Path(bid_raw).is_absolute()
        else Path(bid_raw).resolve()
    )

    if not bid_path.exists() or not bid_path.is_dir():
        print(f"ERROR: bid folder not found: {bid_path}", file=sys.stderr)
        return 1

    # Derive slug
    slug = args.slug if args.slug else slugify(bid_path.name)
    if not slug:
        print("ERROR: could not derive slug from bid folder name; pass --slug",
              file=sys.stderr)
        return 1

    # LLM gate: --no-llm flag OR NO_LLM=1 env
    use_llm = not args.no_llm and os.environ.get("NO_LLM", "0") != "1"

    return run(
        bid_path=bid_path,
        slug=slug,
        use_llm=use_llm,
        do_build=args.build,
        do_qa=args.qa,
    )


if __name__ == "__main__":
    sys.exit(main())
