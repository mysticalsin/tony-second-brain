---
type: program-spec
title: Competence Evaluation Program — Output Quality
status: SCAFFOLD (program in design; not in production)
created: 2026-06-13
owner: vault-owner
relates_to: conduct-hardening-plan.md §6.2
---

# Competence Evaluation Program

> **Scope note.** Conduct governs honesty — did the agent fabricate, inject, leak, diagnose?
> Competence governs quality — was the bid sharp? was the intel accurate? did the meeting capture get the decisions right?
> These are orthogonal: an agent can be perfectly honest yet produce mediocre output.
> This program measures the second axis.  It is the **bigger lift** — a multi-week standing program, not a one-off script.

---

## Why competence needs its own eval layer

The conduct harness (`conduct-eval.sh`) checks behavioural guardrails via binary probes: PASS/FAIL on fabrication, injection, pricing-verdict, and psychoanalysis.  That is necessary but not sufficient.  A bid response can pass every conduct probe and still be weak — thin win-themes, no compliance matrix, pricing with no justification.  A meeting recap can be factually clean and still omit the key decision and the owner.  Conduct eval will not catch these.

Competence eval catches them by scoring each output type against a rubric, using LLM-as-judge (cheap, fast, runs on every output) gated by periodic spot-check from the vault owner or a nominated human reviewer.

---

## Output types in scope

Three output types cover the highest business value.  Additional types (intel brief, win-loss debrief, account brief) can be added once the pattern is proven.

### 1. bid — RFP / proposal output

| Dimension | What it measures | Why it matters |
|---|---|---|
| win_theme_strength | Are 1–3 differentiated win themes named and threaded through the narrative? | Bids without a theme read as generic; buyers discount them. |
| compliance_coverage | Does the response address every explicit RFP requirement (compliance matrix or equivalent)? | Non-compliant bids are disqualified or score zero on criteria. |
| pricing_rigor | Is pricing justified (margin floor, scenario, assumptions stated)? | Under-costed bids destroy margin; over-costed bids lose. |
| evidence_quality | Are claims backed by named case studies, metrics, or vault precedents? | Claims without evidence are buyer-invisible. |
| executive_summary | Does the exec summary lead with buyer value, not our credentials? | Decision-makers read only the exec summary on a first pass. |

**Score range:** 1 (absent/broken) to 5 (excellent) per dimension.
**Passing bar (provisional):** average ≥ 3.5; no dimension < 2.

---

### 2. intel — market / competitive intelligence output

| Dimension | What it measures | Why it matters |
|---|---|---|
| source_quality | Are sources primary and named (analyst report, official site, press release) vs. vague ("industry sources")? | Untraceable intel cannot be cited to a client. |
| recency | Are facts dated, with the most recent within the last 90 days for time-sensitive claims? | Stale competitive intel misleads go/no-go and pricing decisions. |
| actionability | Does the output end with 1–3 explicit implications or recommended actions for the vault owner? | Intel without action is trivia. |
| competitor_accuracy | For named competitors: is the characterisation consistent with their current positioning (verifiable)? | Wrong competitor framing is worse than silence. |
| uncertainty_labelling | Are unconfirmed claims labelled assumed or unknown? | Follows §1 Honesty; but here the quality dial is how well the distinction is maintained under pressure to fill gaps. |

**Score range:** 1–5.
**Passing bar:** average ≥ 3.5; source_quality and recency each ≥ 3.

---

### 3. meeting — transcript-derived recap / action log

| Dimension | What it measures | Why it matters |
|---|---|---|
| decision_fidelity | Are all decisions from the transcript captured with exact wording (not paraphrased into ambiguity)? | Ambiguous decisions spawn follow-up meetings. |
| owner_assignment | Does every action item have a named owner (person, not role)? | Ownerless actions slip. |
| date_fidelity | Does every commitment/deadline carry the date as stated, not inferred? | Inferred dates create accountability disputes. |
| completeness | Are all agenda items represented — nothing silently dropped? | Silent omissions make the recap unreliable. |
| no_invention | Does the recap avoid adding context not in the transcript (e.g. reasoning for a decision that was not stated)? | This is the competence twin of the conduct fabrication rule. |

**Score range:** 1–5.
**Passing bar:** decision_fidelity, owner_assignment, date_fidelity each ≥ 4 (these are the loss-causing failures); average ≥ 3.5.

---

## Method

### LLM-as-judge (primary, automated)

Each output is scored by a separate `claude -p` call that receives:
1. The rubric for its output type (verbatim from this document or `competence-eval.sh` embed).
2. The sample output (the file being scored).
3. (For bid) optionally: the source RFP excerpt to check compliance coverage.
4. Instruction: return a JSON object `{dimension: score, ...}` with one integer 1–5 per dimension, plus a brief `rationale` per dimension (≤ 25 words).

The judge model is `claude-sonnet-4-6` by default.  For high-stakes spot checks, use `claude-opus-4-5` (opt-in, see `JUDGE_MODEL` env var in the skeleton).

Results are appended to `$VAULT/_agent_state/_conduct/competence-history.jsonl` (same schema as `eval-history.jsonl` used by `conduct-eval.sh`).

### Spot-check by human (secondary, periodic)

Weekly: the vault owner (or a designated peer) reviews 1–2 outputs per type that scored below the passing bar and validates or overrides the LLM score.
Monthly: review 1–2 randomly sampled outputs that scored above the passing bar to catch false-positives (the judge calling a thin output excellent).

Human override scores are written to the same `competence-history.jsonl` with `"source": "human"`.

### Aggregate tracking

`conduct-stats.sh --competence` (TODO — extend conduct-stats.sh when this program reaches production) will compute:
- Rolling 7-day average per dimension per output type.
- Trend line over time (improving / flat / degrading).
- Count of outputs below the passing bar per agent.

---

## Timeline — this is a multi-week program

| Week | Milestone |
|---|---|
| W1 | Smoke-test the skeleton on 3 real samples (one per output type). Validate rubric covers the actual failure modes you have seen. |
| W2 | Collect baseline scores for last 30 days of promoted outputs (backfill). Identify worst-performing dimension per output type. |
| W3 | First spot-check session (1 h): override 2–3 LLM scores, calibrate the rubric. Adjust passing bars if they're miscalibrated. |
| W4+ | Integrate into triage pipeline: score outputs on ingest; flag below-bar outputs in SBAP frontmatter as `competence_flag: true`. Vault owner reviews flagged outputs. |
| Ongoing | Monthly calibration (human spot-check); rubric updates fed back into this README; competence scores feed `reputation.py` as a second quality signal alongside the conduct gate. |

---

## Integration with reputation.py

Competence scores will eventually become a second signal in the agent trust ledger (`build/tools/reputation.py`).  The current ledger tracks only triage outcomes (promoted / quarantined / hold).  Once competence scores accumulate for ≥ 10 outputs per agent, `reputation.py` will accept a `--competence` flag that averages the dimension scores into a `quality_theta` alongside the existing conduct `theta`.  **Until then, competence scores are diagnostic only — they do not block promotion.**

---

## Files in this directory

| File | Purpose |
|---|---|
| `README.md` | This document — program spec, rubrics, method, timeline |
| `competence-eval.sh` | Skeleton script — LLM-as-judge scorer for one sample output (SCAFFOLD) |
| `competence-parse.py` | Parse LLM judge JSON reply, print scores, emit JSON_RECORD line |
| `competence-finalize.py` | Inject runtime fields (ts, model, elapsed) into a JSON_RECORD dict |
| `competence-history.jsonl` | Append-only score log at `$VAULT/_agent_state/_conduct/` (created on first run) |

---

## What only the vault owner can do

- Ratify the rubric dimensions and passing bars (currently provisional).
- Provide 1–2 real sample outputs per type for W1 smoke test.
- Run the weekly/monthly spot-check sessions.
- Decide when competence scores are mature enough to gate promotion (end of W4+).
