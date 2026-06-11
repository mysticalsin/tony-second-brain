#!/usr/bin/env python3
"""rfp_pipeline.py — the stable, universal, account-aware RFP pipeline (P7).

ONE pipeline. Any RFP. Any account. Zero code change per RFP. Drop a file in a
bid folder and this produces the full intelligence pack. Stages:

  1. INGEST     — newest source doc (pdf/docx/pptx/xlsx) → rfp-source.md (markitdown).
  2. MODEL      — LLM extracts a STRUCTURED model (requirements, weighted eval
                  criteria, deadlines, mandatory clauses, scope) → rfp-model.json.
                  Format-agnostic: understanding lives in the prompt, never in code.
  3. ACCOUNT    — auto-join this client's dossier (Clients/<slug>, Accounts/<slug>,
                  _brain_api/account/<slug>, sibling bids) → _account-context.md.
  4. RESEARCH   — rfp_research.py (now reads the model + account dossier) → research.md.
  5. WIN-RECS   — win_patterns.py --bid → win-recs.md (playbook-scored).
  6. COMPLIANCE — mandatory clauses + criteria → compliance-gaps.md (submission gate).
  6b. SUBMISSION-GATE — checks disqualifying clauses are resolved; BLOCKED → kanban banner
                  + Important/escalations/<date>-<bid>-BLOCKED.md (SBAP held note);
                  PASS → clears the banner. Run after COMPLIANCE + checkbox review.
  7. SCORE      — grade 02 - Proposal Draft.md 1-5 per WEIGHTED eval criterion (fast model)
                  → scorecard.md: predicted evaluator score (0-100) + per-criterion cited gaps.
                  Weights come from MODEL (RFP-stated, else honest even-split, flagged inferred).

NO VOICE — never touches Ultron. Run from the vault root:
  python build/tools/rfp_pipeline.py "RFPs/Globex Cloud Migration"
  python build/tools/rfp_pipeline.py "<bid>" --skip-ingest        # rfp-source already current
  python build/tools/rfp_pipeline.py "<bid>" --stage model --stage score   # just rubric + grade
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("VAULT", Path.cwd())).resolve()
TOOLS = "build/tools"
SRC_EXT = (".pdf", ".docx", ".pptx", ".xlsx")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()  # é→e, ô→o …
    return re.sub(r"[^a-z0-9]", "", s.lower())


def claude_bin() -> str | None:
    for c in (str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", shutil.which("claude")):
        if c and Path(c).exists():
            return c
    return None


def synthesize(prompt: str, model: str = "claude-sonnet-4-6") -> str:
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


def extract_text(path: Path) -> str:
    """Any format → text via the vault's markitdown (uv-managed deps)."""
    uv = shutil.which("uv")
    if not uv:
        return ""
    code = ("from markitdown import MarkItDown;"
            f"print(MarkItDown().convert({str(path)!r}).text_content)")
    r = subprocess.run([uv, "run", "--quiet", "--with", "markitdown[pdf]", "python", "-c", code],
                       cwd=str(VAULT), capture_output=True, text=True, timeout=180)
    return r.stdout.strip() if r.returncode == 0 else ""


def strip_fm(text: str) -> str:
    m = re.match(r"^---\r?\n.*?\r?\n---\r?\n?(.*)$", text, re.S)
    return m.group(1) if m else text


# ── Stage 1: ingest ───────────────────────────────────────────────────────
def ingest(bid: Path) -> str:
    src_md = bid / "rfp-source.md"
    docs = sorted((p for p in bid.iterdir() if p.suffix.lower() in SRC_EXT),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not docs:
        if src_md.exists() and len(strip_fm(src_md.read_text(encoding="utf-8")).strip()) >= 80:
            return "ingest: rfp-source.md present, no source doc — kept"
        return "ingest: NO source doc and no rfp-source.md — nothing to ingest"
    doc = docs[0]
    if src_md.exists() and src_md.stat().st_mtime >= doc.stat().st_mtime and \
       len(strip_fm(src_md.read_text(encoding="utf-8")).strip()) >= 80:
        return f"ingest: rfp-source.md newer than {doc.name} — skipped"
    text = extract_text(doc)
    if len(text) < 80:
        return f"ingest: extraction of {doc.name} returned too little — kept existing"
    fm = (f'---\ntype: rfp-source\nsource_file: "{doc.name}"\nsource_format: "{doc.suffix.lstrip(".")}"\n'
          f"ingested: true\ngenerated: {utcnow()}\nconfidential: true\ntags: [rfp-source]\n---\n\n"
          f"# RFP — {bid.name} (source)\n\n> Extracted verbatim from `{doc.name}` via markitdown.\n\n## Raw RFP text\n\n")
    src_md.write_text(fm + text + "\n", encoding="utf-8")
    return f"ingest: {doc.name} → rfp-source.md ({len(text)} chars)"


# ── Stage 2: structured model ─────────────────────────────────────────────
MODEL_PROMPT = """Extract a STRUCTURED model of this RFP as STRICT JSON (no prose, no markdown fences).
Schema:
{{"title": "", "client": "", "due_date": "", "submission_method": "",
  "scope": "", "requirements": ["..."],
  "eval_criteria": [{{"name": "", "weight": <number or null>}}],
  "deadlines": [{{"what": "", "date": ""}}],
  "mandatory_clauses": [{{"clause": "", "why_it_matters": "", "disqualifying": true}}],
  "contacts": ["..."]}}
For eval_criteria.weight: ONLY put a number if the RFP STATES an explicit weight, %, or
points for that criterion (e.g. "Cost — 30%", "Technical: 40 points"). Express it as the
raw number the RFP gives (30 for 30%, 40 for 40 points). If the RFP lists criteria but
states NO numeric weight/points for them, put null — do NOT guess or distribute.
For mandatory_clauses.disqualifying: set true ONLY when non-compliance results in the RFP
text EXPLICITLY stating the bid will not be considered / is ineligible / is disqualified
(e.g. "will not be considered", "ineligible", "disqualified", "must not exceed … or bid
will be rejected"). Hard deadlines and NDA execution are canonical examples. Set false for
clauses that are important but where non-compliance is a risk rather than an explicit
bid-void (e.g. IP assignment, post-award ESG obligations).
Fill ONLY from the RFP text; use "" / [] when absent. Output ONLY the JSON object.

RFP TEXT:
{rfp}"""


def normalize_weights(crit: list[dict]) -> tuple[list[dict], bool]:
    """Resolve eval_criteria weights to a percentage that sums to ~100.
    Returns (criteria, any_inferred). Rules:
      - If the RFP stated numeric weights for ALL criteria → normalize to % (sum 100),
        weight_inferred=false.
      - If it stated weights for SOME but not all → keep the stated ones as %, spread the
        remaining percentage evenly across the unstated ones, flag only the filled ones.
      - If NONE stated → even-split across all, every one weight_inferred=true.
    Each criterion keeps its existing fields and gains: weight (number, % points to one
    decimal) + weight_inferred (bool)."""
    n = len(crit)
    if n == 0:
        return crit, False
    stated = [c for c in crit if isinstance(c.get("weight"), (int, float)) and c["weight"] > 0]
    any_inferred = False
    if len(stated) == n:                       # all stated → just normalize to %
        total = sum(float(c["weight"]) for c in stated)
        for c in crit:
            c["weight"] = round(float(c["weight"]) / total * 100, 1)
            c["weight_inferred"] = False
    elif stated:                               # partial → keep stated %, even-split the rest
        stated_pct = sum(float(c["weight"]) for c in stated)
        # interpret stated numbers as already-percentages; clamp the remainder to >=0
        remainder = max(0.0, 100.0 - stated_pct)
        n_unstated = n - len(stated)
        per = round(remainder / n_unstated, 1) if n_unstated else 0.0
        for c in crit:
            if isinstance(c.get("weight"), (int, float)) and c["weight"] > 0:
                c["weight"] = round(float(c["weight"]), 1)
                c["weight_inferred"] = False
            else:
                c["weight"] = per
                c["weight_inferred"] = True
                any_inferred = True
    else:                                      # none stated → honest even-split
        per = round(100.0 / n, 1)
        for c in crit:
            c["weight"] = per
            c["weight_inferred"] = True
        any_inferred = True
    return crit, any_inferred


def build_model(bid: Path, model: str) -> str:
    src = bid / "rfp-source.md"
    if not src.exists():
        return "model: no rfp-source.md — skipped"
    rfp = strip_fm(src.read_text(encoding="utf-8")).strip()
    if len(rfp) < 80:
        return "model: rfp-source.md empty — skipped"
    out = synthesize(MODEL_PROMPT.format(rfp=rfp[:9000]), model)
    out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return "model: LLM did not return valid JSON — skipped (research still runs on raw RFP)"
    crit, any_inferred = normalize_weights(obj.get("eval_criteria", []) or [])
    obj["eval_criteria"] = crit
    obj["_generated"] = utcnow()
    (bid / "rfp-model.json").write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    wnote = "inferred even-split" if any_inferred else "RFP-stated"
    return (f"model: rfp-model.json ({len(obj.get('requirements', []))} reqs, "
            f"{len(crit)} criteria [{wnote} weights], {len(obj.get('mandatory_clauses', []))} clauses)")


# ── Stage 3: account join ─────────────────────────────────────────────────
def account_join(bid: Path, company: str) -> str:
    slug = slugify(company)
    parts, found = [], []
    for base in ("Clients", "02_Areas/Accounts"):
        d = VAULT / base / slug
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                parts.append(f"### {base}/{slug}/{f.name}\n{strip_fm(f.read_text(encoding='utf-8'))[:1200]}")
                found.append(str(f.relative_to(VAULT)))
    bj = VAULT / "_brain_api" / "account" / slug / "brief.json"
    if bj.exists():
        try:
            parts.append(f"### account brief.json\n{json.dumps(json.loads(bj.read_text()), indent=1)[:1200]}")
            found.append(str(bj.relative_to(VAULT)))
        except json.JSONDecodeError:
            pass
    # sibling bids under RFPs/<Company>/
    company_root = bid.parent.parent
    sibs = []
    for b in company_root.rglob("00 - Brief.md"):
        if b.parent != bid:
            fm = strip_fm(b.read_text(encoding="utf-8"))
            sibs.append(f"- {b.parent.relative_to(VAULT)} :: {fm[:120].splitlines()[0] if fm.strip() else ''}")
    if sibs:
        parts.append("### Sibling bids for this company\n" + "\n".join(sibs))
    if not parts:
        (bid / "_account-context.md").write_text(
            f"# Account context — {company}\n\n_No account folder found "
            f"(looked: Clients/{slug}, 02_Areas/Accounts/{slug}, _brain_api/account/{slug}). "
            f"Recall corpus still grounds research._\n", encoding="utf-8")
        return f"account: no dossier for '{slug}' — wrote placeholder"
    (bid / "_account-context.md").write_text(
        f"# Account context — {company}\n\n> Sources: {', '.join(found) or '(sibling bids)'}\n\n"
        + "\n\n".join(parts) + "\n", encoding="utf-8")
    return f"account: joined {len(found)} dossier file(s) + {len(sibs)} sibling bid(s)"


# ── Disqualifying-clause detection helpers (used by compliance + submission-gate) ──
_DISQ_KEYWORDS = ("will not be considered", "ineligible", "disqualified",
                  "disqualify", "automatic disqualification", "bid will be rejected",
                  "not considered")


def _is_disqualifying_heuristic(clause: dict) -> bool:
    """Fallback: scan why_it_matters text for explicit disqualifier language."""
    text = (clause.get("why_it_matters", "") + " " + clause.get("clause", "")).lower()
    return any(kw in text for kw in _DISQ_KEYWORDS)


# ── Stage 6: compliance gaps (deterministic, from the model) ──────────────
def compliance(bid: Path) -> str:
    mj = bid / "rfp-model.json"
    if not mj.exists():
        return "compliance: no rfp-model.json — skipped"
    try:
        m = json.loads(mj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "compliance: model unreadable — skipped"
    clauses = m.get("mandatory_clauses", [])
    crit = m.get("eval_criteria", [])
    dl = m.get("deadlines", [])
    lines = [f"---\ntype: compliance-gaps\ngenerated: {utcnow()}\ntags: [compliance, gate]\n---\n",
             f"# Compliance & submission gate — {bid.name}\n",
             "> Auto-derived from `rfp-model.json`. Each item is a GATE — confirm before submission.\n",
             "## Mandatory clauses"]
    lines += [
        f"- [ ] **{c.get('clause','?')}**"
        + (" 🔴 DISQUALIFYING" if c.get("disqualifying") or _is_disqualifying_heuristic(c) else "")
        + f" — {c.get('why_it_matters','')}"
        for c in clauses
    ] or ["- (none extracted)"]
    lines += ["\n## Evaluation criteria — coverage check"]
    lines += [f"- [ ] {c.get('name','?')}" + (f"  _(weight: {c['weight']})_" if c.get("weight") else "") for c in crit] or ["- (none extracted)"]
    lines += ["\n## Deadlines"]
    lines += [f"- [ ] {d.get('what','?')} — **{d.get('date','?')}**" for d in dl] or ["- (none extracted)"]
    (bid / "compliance-gaps.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"compliance: compliance-gaps.md ({len(clauses)} clauses, {len(crit)} criteria, {len(dl)} deadlines)"


# ── Stage: SUBMISSION-GATE ────────────────────────────────────────────────
# Checks every disqualifying clause is resolved (compliance-gaps.md checked off).
# BLOCKED → writes kanban.md banner + Important/escalations/<date>-<bid>-BLOCKED.md
# PASS    → writes a PASS notice to kanban.md
def submission_gate(bid: Path) -> str:
    mj = bid / "rfp-model.json"
    if not mj.exists():
        return "submission-gate: no rfp-model.json — skipped (run MODEL first)"
    try:
        m = json.loads(mj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "submission-gate: rfp-model.json unreadable — skipped"

    clauses = m.get("mandatory_clauses", []) or []

    # Identify disqualifying clauses (model-tagged OR heuristic-detected)
    disq_clauses = [
        c for c in clauses
        if c.get("disqualifying") is True or _is_disqualifying_heuristic(c)
    ]

    # Read compliance-gaps.md to find which clauses are checked off
    gaps_f = bid / "compliance-gaps.md"
    resolved_texts: list[str] = []
    if gaps_f.exists():
        for line in gaps_f.read_text(encoding="utf-8").splitlines():
            # A checked-off item: "- [x] **clause text**" (case-insensitive x)
            if re.match(r"^\s*-\s*\[x\]", line, re.I):
                resolved_texts.append(line.lower())

    def _is_resolved(clause_text: str) -> bool:
        """Check if a clause is reflected in any checked-off line in compliance-gaps.md.

        Strategy: extract 2+ meaningful words (>4 chars) from the clause and check
        that a resolved line contains ALL of those keywords. This handles slight
        wording differences between rfp-model.json and compliance-gaps.md.
        """
        words = re.findall(r"[a-z]{5,}", clause_text.lower())
        # Take the 3 most distinctive words (skip ultra-common stopwords)
        _STOP = {"shall", "must", "will", "their", "which", "where", "there",
                 "supplier", "response", "submit", "submission", "every", "before",
                 "after", "without"}
        keywords = [w for w in words if w not in _STOP][:3]
        if not keywords:
            return False
        for r in resolved_texts:
            if all(k in r for k in keywords):
                return True
        return False

    unresolved = [c for c in disq_clauses if not _is_resolved(c.get("clause", ""))]

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bid_slug = bid.name

    if unresolved:
        # ── BLOCKED ──────────────────────────────────────────────────────────
        banner_lines = [
            "\n\n---",
            "",
            "> [!danger] SUBMISSION GATE — BLOCKED",
            f"> Generated: {utcnow()}",
            "> The following disqualifying clauses are UNRESOLVED. This bid CANNOT be submitted.",
            ">",
        ]
        for c in unresolved:
            banner_lines.append(f"> - **{c.get('clause', '?')}**")
            banner_lines.append(f">   _{c.get('why_it_matters', '')}_")
        banner_lines += [">", "> Resolve each item in `compliance-gaps.md` (tick the checkbox) then re-run `--stage submission-gate`.", ""]

        # Append banner to kanban.md
        kanban_f = bid / "kanban.md"
        if kanban_f.exists():
            existing = kanban_f.read_text(encoding="utf-8")
            # Remove any previous gate banner before appending fresh one
            existing = re.sub(r"\n---\n\n> \[!danger\] SUBMISSION GATE.*?(?=\n---|\Z)", "",
                              existing, flags=re.S).rstrip()
            kanban_f.write_text(existing + "\n" + "\n".join(banner_lines) + "\n",
                                encoding="utf-8")

        # Write escalation note (SBAP frontmatter — confidence low → triage HOLDS)
        esc_dir = VAULT / "Important" / "escalations"
        esc_dir.mkdir(parents=True, exist_ok=True)
        esc_file = esc_dir / f"{date_str}-{bid_slug}-BLOCKED.md"
        run_id = f"{utcnow()}-submission-gate-{bid_slug}"
        rel_bid = str(bid.relative_to(VAULT))
        clause_bullets = "\n".join(
            f"- **{c.get('clause','?')}** — {c.get('why_it_matters','')}"
            for c in unresolved
        )
        esc_content = (
            f"---\n"
            f'sbap_version: "1.0"\n'
            f"source_agent: rfp-pipeline\n"
            f'source_run_id: "{run_id}"\n'
            f'generated: "{utcnow()}"\n'
            f"input_context_refs:\n"
            f'  - "{rel_bid}/rfp-model.json"\n'
            f'  - "{rel_bid}/compliance-gaps.md"\n'
            f"output_type: escalation_alert\n"
            f'target_path: "Important/escalations/{date_str}-{bid_slug}-BLOCKED.md"\n'
            f"confidence: 0.95\n"
            f"needs_review: true\n"
            f'reasoning_summary: "Submission gate BLOCKED: {len(unresolved)} disqualifying clause(s) unresolved for {bid_slug}. Bid cannot be submitted until resolved."\n'
            f"---\n\n"
            f"# SUBMISSION GATE BLOCKED — {bid_slug}\n\n"
            f"> **Generated:** {utcnow()}\n"
            f"> **Bid:** `{rel_bid}`\n"
            f"> **Status:** BLOCKED — {len(unresolved)} disqualifying clause(s) unresolved\n\n"
            f"## Action required\n\n"
            f"The following disqualifying clauses must be resolved **before submission**. "
            f"Each one is an explicit bid-void trigger from the RFP.\n\n"
            f"{clause_bullets}\n\n"
            f"## How to clear this gate\n\n"
            f"1. Open `{rel_bid}/compliance-gaps.md`\n"
            f"2. Confirm each clause is addressed in your submission\n"
            f"3. Tick the checkbox (`- [x]`) for each resolved clause\n"
            f"4. Re-run: `python build/tools/rfp_pipeline.py \"{rel_bid}\" --stage submission-gate`\n"
        )
        esc_file.write_text(esc_content, encoding="utf-8")

        return (f"submission-gate: BLOCKED — {len(unresolved)} disqualifying clause(s) unresolved "
                f"(of {len(disq_clauses)} disqualifying). "
                f"Banner → kanban.md; escalation → Important/escalations/{date_str}-{bid_slug}-BLOCKED.md")
    else:
        # ── PASS ─────────────────────────────────────────────────────────────
        pass_lines = [
            "\n\n---",
            "",
            "> [!success] SUBMISSION GATE — PASS",
            f"> Generated: {utcnow()}",
            f"> All {len(disq_clauses)} disqualifying clause(s) are resolved. Safe to submit.",
            "",
        ]
        kanban_f = bid / "kanban.md"
        if kanban_f.exists():
            existing = kanban_f.read_text(encoding="utf-8")
            existing = re.sub(r"\n---\n\n> \[!\w+\] SUBMISSION GATE.*?(?=\n---|\Z)", "",
                              existing, flags=re.S).rstrip()
            kanban_f.write_text(existing + "\n" + "\n".join(pass_lines) + "\n",
                                encoding="utf-8")
        return (f"submission-gate: PASS — all {len(disq_clauses)} disqualifying clause(s) resolved "
                f"({len(clauses)} total mandatory clauses checked)")


# ── Stage 7: SCORE — grade the proposal draft against the weighted rubric ──
SCORE_PROMPT = """You are a hard-nosed procurement evaluator scoring a supplier's
proposal. You will grade the DRAFT below against each weighted EVALUATION CRITERION,
1-5, exactly as a buyer's panel would (1 = absent/empty, 2 = named but unevidenced,
3 = addressed generically, 4 = strong + specific, 5 = best-in-class + quantified proof).

Be ruthless and honest: an empty template, a placeholder, or a section that is just a
rate card with no value narrative scores LOW. Reward only what is actually written in the
draft — never credit intent or what "could" be added.

=== WEIGHTED EVALUATION CRITERIA ===
{criteria}

=== PROPOSAL DRAFT ({bid}) ===
{draft}

Output ONE line per criterion, in the SAME order, in EXACTLY this pipe format (no header,
no prose, no markdown, no extra lines):
<criterion name> | <score 1-5 integer> | <one specific cited gap: what's missing/weak and why it loses points, naming the criterion's stake — e.g. "Cost is 30% of the score and this section is a rate card with no value-per-dollar narrative">
Output ONLY those lines."""


def _draft_present(draft: str) -> bool:
    """True only if the proposal draft has REAL content, not just the scaffold template.
    The template ships with headings + empty table rows + 'Per CLAUDE.md story rhythm'."""
    body = draft.lower()
    # strip the known scaffold markers; if little prose survives, treat as empty
    signal = re.sub(r"[#>|`\-\[\]\s]", "", draft)
    has_template_marker = "per claude.md story rhythm" in body or "deck source:" in body
    return len(signal) > 400 and not (has_template_marker and len(signal) < 900)


def score_proposal(bid: Path, model: str) -> str:
    mj = bid / "rfp-model.json"
    draft_f = bid / "02 - Proposal Draft.md"
    if not mj.exists():
        return "score: no rfp-model.json — skipped (run MODEL first)"
    if not draft_f.exists():
        return "score: no '02 - Proposal Draft.md' — skipped"
    try:
        m = json.loads(mj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "score: rfp-model.json unreadable — skipped"
    crit = m.get("eval_criteria", []) or []
    if not crit:
        return "score: no eval_criteria in model — skipped"
    draft = strip_fm(draft_f.read_text(encoding="utf-8")).strip()
    has_inferred = any(c.get("weight_inferred") for c in crit)
    draft_real = _draft_present(draft)

    criteria_block = "\n".join(
        f"- {c.get('name','?')} — weight {c.get('weight','?')}%"
        + ("  (weight inferred, not RFP-stated)" if c.get("weight_inferred") else "")
        for c in crit)

    print(f"   …SCORE: grading {len(crit)} criteria against the draft via {model} (this calls claude -p, ~10-60s)…")
    raw = synthesize(SCORE_PROMPT.format(criteria=criteria_block, bid=bid.name, draft=draft[:9000]), model)
    print(f"   …SCORE: claude -p returned {len(raw)} chars; computing weighted prediction…")

    # Deterministic parse: <name> | <score> | <gap>  — math done in Python, not the LLM.
    parsed: list[dict] = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        bits = [b.strip() for b in line.split("|")]
        if len(bits) < 3:
            continue
        mscore = re.search(r"[1-5]", bits[1])
        if not mscore:
            continue
        parsed.append({"name": bits[0].lstrip("-* ").strip(),
                       "score": int(mscore.group()),
                       "gap": "|".join(bits[2:]).strip()})
    if not parsed:
        return f"score: could not parse any criterion line from LLM output — skipped (got: {raw[:120]!r})"

    # Map parsed lines back to the model's criteria by order (LLM was told to keep order);
    # fall back to fuzzy name match if counts differ.
    def _match(c, idx):
        if idx < len(parsed) and parsed[idx]["name"][:12].lower() in c.get("name", "").lower():
            return parsed[idx]
        cn = c.get("name", "").lower()
        for p in parsed:
            if p["name"][:12].lower() in cn or cn[:12] in p["name"].lower():
                return p
        return parsed[idx] if idx < len(parsed) else {"score": 1, "gap": "(no evaluator line returned)"}

    rows, weighted_sum, weight_total = [], 0.0, 0.0
    for i, c in enumerate(crit):
        p = _match(c, i)
        w = float(c.get("weight", 0) or 0)
        s = int(p.get("score", 1))
        weighted_sum += w * s
        weight_total += w
        rows.append({"name": c.get("name", "?"), "weight": w,
                     "inferred": bool(c.get("weight_inferred")),
                     "score": s, "gap": p.get("gap", "")})
    # 1-5 → 0-100: ((Σ w·s)/(Σ w·5)) · 100
    predicted = round(weighted_sum / (weight_total * 5) * 100, 1) if weight_total else 0.0

    # ── render scorecard.md (match win-recs.md / compliance-gaps.md house style) ──
    band = ("🔴 well below threshold" if predicted < 50 else
            "🟠 below a competitive bid" if predicted < 65 else
            "🟡 competitive but beatable" if predicted < 80 else
            "🟢 strong" )
    lines = [
        f"---\ntype: scorecard\ngenerated: {utcnow()}\nmodel: {model}\n"
        f"predicted_evaluator_score: {predicted}\nweights_inferred: {str(has_inferred).lower()}\n"
        f"draft_has_real_content: {str(draft_real).lower()}\ntags: [scorecard, gate]\n---\n",
        f"# Evaluator scorecard — {bid.name}\n",
        f"> Predicts how the evaluator panel would score **`02 - Proposal Draft.md`** against the "
        f"weighted rubric in `rfp-model.json`. Each criterion graded 1-5; the prediction is the "
        f"weight-rolled %, computed deterministically. Re-run as the draft matures.\n",
        f"## Predicted evaluator score: **{predicted} / 100** — {band}\n",
    ]
    if not draft_real:
        lines.append("> The proposal draft is still the empty scaffold template — these "
                     "scores reflect an unwritten proposal, not a weak one. Write the draft, then "
                     "re-run SCORE to get a real read.\n")
    if has_inferred:
        lines.append("> Weights are inferred (even-split) — the RFP states criteria but no "
                     "numeric weights. Treat the per-criterion %s as a working assumption.\n")
    lines.append("## Per-criterion grade\n")
    lines.append("| Criterion | Weight | Score (1-5) | Cited gap |")
    lines.append("|---|---|---|---|")
    for r in rows:
        wlbl = f"{r['weight']:g}%" + ("*" if r["inferred"] else "")
        lines.append(f"| {r['name']} | {wlbl} | {r['score']}/5 | {r['gap']} |")
    if has_inferred:
        lines.append("\n_\\* weight inferred (even-split), not RFP-stated._")
    lines.append("\n## How to read this")
    lines.append("- **Lowest weighted-loss criteria first.** The biggest point losses are the "
                 "high-weight criteria with the lowest scores — attack those before polishing "
                 "anything already at 4-5.")
    # surface the 2 biggest point-losers
    losers = sorted(rows, key=lambda r: r["weight"] * (5 - r["score"]), reverse=True)[:2]
    if losers:
        lines.append("- **Biggest point recovery available:**")
        for r in losers:
            lost = round(r["weight"] * (5 - r["score"]) / 5, 1)
            lines.append(f"  - **{r['name']}** — up to **+{lost} pts** recoverable: {r['gap']}")
    (bid / "scorecard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (f"score: scorecard.md — predicted {predicted}/100 across {len(rows)} criteria"
            + (" [weights inferred]" if has_inferred else "")
            + ("" if draft_real else " [draft is empty scaffold]"))


def run_tool(script: str, args: list[str]) -> str:
    r = subprocess.run([sys.executable, f"{TOOLS}/{script}", *args],
                       cwd=str(VAULT), capture_output=True, text=True, timeout=300)
    tail = (r.stdout.strip().splitlines() or [""])[-1]
    return f"{script}: {tail}" if r.returncode == 0 else f"{script}: FAILED rc={r.returncode} {r.stderr[:160]}"


STAGES = ("ingest", "model", "account", "research", "win-recs", "compliance", "submission-gate", "score")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bid_path")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--score-model", default="haiku",
                    help="fast model for the SCORE stage (default: haiku — keeps the grade cheap)")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--stage", choices=STAGES, action="append",
                    help="run only these stage(s); repeatable. e.g. --stage model --stage score")
    args = ap.parse_args()
    bid = (VAULT / args.bid_path).resolve()
    if not bid.is_dir():
        print(f"ERROR: bid not found: {bid}", file=sys.stderr); return 2
    brief = bid / "00 - Brief.md"
    company = bid.parent.parent.name
    if brief.exists():
        m = re.search(r'^company:\s*"?([^"\n]+)"?', brief.read_text(encoding="utf-8"), re.M)
        if m:
            company = m.group(1).strip()

    only = set(args.stage) if args.stage else None
    def want(stage: str) -> bool:
        return only is None or stage in only

    print(f"=== rfp_pipeline · {company} · {bid.relative_to(VAULT)}"
          + (f" · stages={','.join(args.stage)}" if only else "") + " ===")
    if want("ingest") and not args.skip_ingest:
        print(" 1 " + ingest(bid))
    if want("model"):
        print(" 2 " + build_model(bid, args.model))
    if want("account"):
        print(" 3 " + account_join(bid, company))
    if want("research"):
        print(" 4 " + run_tool("rfp_research.py", [str(bid.relative_to(VAULT)), "--model", args.model]))
    if want("win-recs"):
        print(" 5 " + run_tool("win_patterns.py", ["--bid", str(bid.relative_to(VAULT)), "--model", args.model]))
    if want("compliance"):
        print(" 6 " + compliance(bid))
    if want("submission-gate"):
        print(" 6b " + submission_gate(bid))
    if want("score"):
        print(" 7 " + score_proposal(bid, args.score_model))
    print("=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
