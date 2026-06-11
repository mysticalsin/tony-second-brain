#!/usr/bin/env python3
"""grid_predict.py — predict the buyer's evaluation grid BEFORE drafting a slide.

"RNG manipulation on the buyer": instead of guessing what to emphasize, predict the
scoring grid the client's evaluation committee will actually use (criteria, weights,
rubric, cheap heuristics like page-flip and compliance-matrix checks), then let
rfp-drafter / red-team optimize against the predicted scoring function.

Inputs (per bid folder):
    rfp-model.json        structured RFP scope + any stated criteria
    rfp-source.md         raw RFP text (stated weights are often partial)
    _account-context.md   account history with this buyer
    ghost-brief.md        competitor positioning intel
    _wiki/failure-modes/  past loss patterns

Outputs:
    _brain_api/bid/<bid-id>/predicted_grid.json    machine-readable, consumed by red-team
    <bid_folder>/04 - Predicted Grid.md            human mirror

Every weight ships with a confidence band. LOW-CONFIDENCE CRITERIA MUST FALL BACK TO
BALANCED COVERAGE in drafting — optimizing hard against a hallucinated grid is worse
than a balanced proposal (the load-bearing risk of this whole idea).

Usage (from vault root):
    python3 build/tools/grid_predict.py globex-cloud-migration
    python3 build/tools/grid_predict.py globex-cloud-migration --dry-run
    python3 build/tools/grid_predict.py --backtest   # honesty gate: do we have enough
                                                     # closed-bid debriefs to validate?
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

VAULT_DEFAULT = os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path"))
MODEL = "claude-sonnet-4-6"  # one-shot per bid, quality matters more than the cents
LOW_CONFIDENCE = 0.5          # below this, drafting must NOT over-index on the weight
BACKTEST_MIN_BIDS = 5


def vault_root() -> Path:
    v = Path(os.environ.get("CLAUDE_VAULT", VAULT_DEFAULT))
    return v if v.exists() else Path.cwd()


def claude_bin() -> str | None:
    for c in (str(Path.home() / ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", shutil.which("claude")):
        if c and Path(c).exists():
            return c
    return None


def ask_claude(prompt: str, vault: Path, timeout: int = 300) -> str:
    cb = claude_bin()
    if not cb:
        raise RuntimeError("claude CLI not found")
    result = subprocess.run(
        [cb, "-p", prompt, "--model", MODEL,
         "--setting-sources", "",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'],
        cwd=str(vault), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "VAULT_BRAIN_QUIET": "1", "CAPTURE_DISABLED": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed rc={result.returncode}: {result.stderr[:300]}")
    return result.stdout


def extract_json(text: str):
    m = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"([\[{].*[\]}])", text, re.DOTALL)
        raw = m.group(1) if m else text
    return json.loads(raw)


def read_if(path: Path, limit: int) -> str:
    try:
        return path.read_text()[:limit]
    except OSError:
        return ""


def resolve_bid(vault: Path, bid_id: str) -> tuple[str, Path]:
    open_bids = json.loads((vault / "_brain_api" / "bid" / "_open.json").read_text())
    for b in open_bids.get("bids") or []:
        if b.get("bid_id") == bid_id:
            return bid_id, vault / b["path"]
    raise SystemExit(f"bid '{bid_id}' not in _brain_api/bid/_open.json")


# ──────────────────────────── predict ────────────────────────────


def validate_grid(g: dict) -> dict:
    crits = g.get("criteria") or []
    if not isinstance(crits, list) or len(crits) < 3:
        raise ValueError(f"grid has {len(crits)} criteria — need ≥3")
    total = 0.0
    norm = []
    for c in crits:
        w = float(c.get("weight", 0))
        conf = float(c.get("confidence", 0))
        name = str(c.get("name", "")).strip()
        if not name or not (0 < w <= 1) or not (0 <= conf <= 1):
            raise ValueError(f"invalid criterion: {c}")
        total += w
        norm.append({
            "name": name[:120],
            "weight": round(w, 3),
            "confidence": round(conf, 2),
            "drafting_policy": ("optimize" if conf >= LOW_CONFIDENCE
                                else "balanced-fallback (confidence too low to over-index)"),
            "rubric": str(c.get("rubric", ""))[:400],
            "cheap_heuristics": [str(h)[:160] for h in (c.get("cheap_heuristics") or [])][:4],
            "evidence": str(c.get("evidence", ""))[:300],
        })
    if not (0.85 <= total <= 1.15):
        raise ValueError(f"weights sum to {total:.2f} — not a probability-ish grid")
    # Renormalize so consumers can trust sum == 1.0
    for c in norm:
        c["weight"] = round(c["weight"] / total, 3)
    return {"criteria": sorted(norm, key=lambda c: -c["weight"]),
            "evaluator_personas": (g.get("evaluator_personas") or [])[:4],
            "prediction_basis": str(g.get("prediction_basis", ""))[:600]}


def run_predict(vault: Path, bid_id: str, dry_run: bool) -> int:
    bid_id, folder = resolve_bid(vault, bid_id)
    rfp_model = read_if(folder / "rfp-model.json", 5000)
    rfp_source = read_if(folder / "rfp-source.md", 12000)
    account = read_if(folder / "_account-context.md", 2000)
    ghost = read_if(folder / "ghost-brief.md", 5000)
    failure_modes = "\n\n".join(read_if(p, 1500)
                                for p in sorted((vault / "_wiki" / "failure-modes").glob("*.md"))
                                if p.name != "_index.md")[:4000]
    if not (rfp_model or rfp_source):
        raise SystemExit(f"{folder} has neither rfp-model.json nor rfp-source.md — nothing to predict from")

    prompt = f"""You are predicting the evaluation grid the BUYER's committee will use to score proposals for this RFP. Not what we wish they'd score — what they will actually do, including their cheap heuristics (page-flip test, compliance-matrix check, price-anchor math, incumbency comfort).

RFP STRUCTURED MODEL:
{rfp_model}

RFP SOURCE TEXT (stated criteria are often partial — infer the unstated ones):
{rfp_source}

ACCOUNT CONTEXT (how this buyer behaves):
{account}

COMPETITOR LANDSCAPE (who else they're scoring us against):
{ghost}

PAST LOSS PATTERNS (criteria we historically underweight):
{failure_modes}

Output ONLY a JSON object:
{{"criteria": [{{"name": "...", "weight": <0-1, all weights sum to 1.0>,
   "confidence": <0-1, how sure you are of this WEIGHT given the evidence>,
   "rubric": "<how the committee scores 0-5 on this criterion>",
   "cheap_heuristics": ["<shortcut evaluators actually use>"],
   "evidence": "<which input justifies this weight — quote/point to it>"}}],
  "evaluator_personas": [{{"role": "...", "bias": "...", "what_they_kill_on": "..."}}],
  "prediction_basis": "<2-3 sentences: what drove the weights; what is guessed vs stated>"}}

Rules:
- 5-9 criteria. Weights must sum to 1.0.
- confidence reflects EVIDENCE: stated-in-RFP ≈ 0.9, strongly implied ≈ 0.6-0.7, inferred from buyer type ≈ 0.3-0.5. Be honest — a low-confidence weight triggers balanced-coverage fallback downstream, which is the safe behavior.
- Include price/commercials, compliance, and delivery-risk style criteria if plausible — committees always have them even when unstated."""

    raw = ask_claude(prompt, vault)
    grid = validate_grid(extract_json(raw))
    now = datetime.now(timezone.utc).isoformat()
    doc = {"bid_id": bid_id, "generated": now, "model": MODEL,
           "low_confidence_threshold": LOW_CONFIDENCE, **grid}

    md = ["---", "type: predicted-grid", f"bid: {bid_id}", f"generated: {now}",
          f"source_tool: build/tools/grid_predict.py ({MODEL})", "---", "",
          f"# Predicted Evaluation Grid — {bid_id}", "",
          "> The committee's likely scoring function, predicted from RFP + account + competitor intel.",
          "> **Low-confidence weights (< 0.5) must NOT be over-indexed** — drafting falls back to",
          "> balanced coverage there. Red-team scores the draft against this grid cell-by-cell.", "",
          f"_{grid['prediction_basis']}_", "",
          "| # | Criterion | Weight | Confidence | Drafting policy |",
          "|---:|---|---:|---:|---|"]
    for i, c in enumerate(grid["criteria"], 1):
        md.append(f"| {i} | {c['name']} | {c['weight']:.0%} | {c['confidence']:.2f} | {c['drafting_policy']} |")
    md.append("")
    for c in grid["criteria"]:
        md += [f"## {c['name']} ({c['weight']:.0%})", "",
               f"**Rubric:** {c['rubric']}", ""]
        if c["cheap_heuristics"]:
            md.append("**Cheap heuristics the committee will use:**")
            md += [f"- {h}" for h in c["cheap_heuristics"]]
            md.append("")
        if c["evidence"]:
            md += [f"**Evidence:** {c['evidence']}", ""]
    if grid["evaluator_personas"]:
        md += ["## Evaluator personas", ""]
        for p in grid["evaluator_personas"]:
            md.append(f"- **{p.get('role', '?')}** — bias: {p.get('bias', '?')} · "
                      f"kills on: {p.get('what_they_kill_on', '?')}")
        md.append("")

    if dry_run:
        print(json.dumps(doc, indent=2))
        return 0
    api_path = vault / "_brain_api" / "bid" / bid_id / "predicted_grid.json"
    api_path.parent.mkdir(parents=True, exist_ok=True)
    api_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    mirror = folder / "04 - Predicted Grid.md"
    mirror.write_text("\n".join(md))
    print(f"grid: {len(grid['criteria'])} criteria → {api_path}")
    print(f"grid: human mirror → {mirror}")
    low = [c["name"] for c in grid["criteria"] if c["confidence"] < LOW_CONFIDENCE]
    if low:
        print(f"grid: ⚠ low-confidence weights (balanced-fallback applies): {', '.join(low)}")
    return 0


# ──────────────────────────── backtest gate ────────────────────────────


def run_backtest(vault: Path) -> int:
    """Honesty gate: the predictor is only trustworthy once we can backtest predicted
    grids against real debrief scores from ≥5 closed bids. Report corpus status."""
    candidates = []
    for root in (vault / "04_Archives", vault / "RFPs"):
        if not root.exists():
            continue
        for brief in root.rglob("00 - Brief.md"):
            txt = read_if(brief, 2000)
            stage = ""
            m = re.search(r"^stage:\s*(\w+)", txt, re.MULTILINE)
            if m:
                stage = m.group(1)
            if stage not in ("Won", "Lost"):
                continue
            folder = brief.parent
            has_debrief = any((folder / n).exists() and len(read_if(folder / n, 99999)) > 500
                              for n in ("03 - Decision Log.md", "debrief.md", "retro.md"))
            candidates.append({"bid": folder.name, "stage": stage, "debrief": has_debrief})
    usable = [c for c in candidates if c["debrief"]]
    print(f"backtest corpus: {len(candidates)} closed bids found, {len(usable)} with a filled debrief")
    for c in candidates:
        print(f"  - {c['bid']} ({c['stage']}) {'✅ debrief' if c['debrief'] else '— no debrief'}")
    if len(usable) < BACKTEST_MIN_BIDS:
        print(f"\nVERDICT: corpus too thin ({len(usable)}/{BACKTEST_MIN_BIDS}). The predictor runs "
              f"forward-only with confidence bands + balanced-fallback as the safety net. "
              f"Re-run --backtest after each bid closes with a filled Decision Log.")
    else:
        print(f"\nVERDICT: corpus sufficient — backtest by predicting each closed bid's grid "
              f"with the outcome hidden, then rank-correlate predicted weights vs debrief emphasis.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bid_id", nargs="?", help="bid id from _brain_api/bid/_open.json")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args()
    vault = Path(args.vault) if args.vault else vault_root()
    if args.backtest:
        return run_backtest(vault)
    if not args.bid_id:
        ap.error("bid_id required (or --backtest)")
    return run_predict(vault, args.bid_id, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
