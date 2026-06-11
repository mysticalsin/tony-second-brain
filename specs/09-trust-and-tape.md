# Spec 09 — The trust layer: promise ledger, contradiction detector, deal tape

Three tools that move the system from "remembers what happened" to "holds everyone accountable and reads the deal". All three bind to data the system already collects (meeting notes, agent memories, bid endpoints) — no new capture burden. Build them as standalone Python CLIs in the pipeline tools folder, wired into the refresh loop.

## A · Promise ledger (`promise_ledger.py`)

Every commitment made in every meeting, tracked — both directions: what the owner promised others ("I'll send the revised P&L by Friday") and what others owe the owner ("client will share the data").

- **Extract** from meeting notes' distilled sections (Summary / Decisions / Action items — NOT raw transcripts; distilled text has far fewer false positives). LLM mode (small fast model, strict JSON, conservative: explicit commitment language only, confidence 0–1, never fabricate due dates) + a `--no-llm` regex fallback over action-item bullets so tests run offline.
- **Gate**: only confidence ≥ 0.80 enters `_brain_api/promises/ledger.json`; lower goes to `held.json` for human review. A false "they owe you X" damages trust more than a miss.
- **Reconcile** on every run: scan meetings newer than each pending promise for resolution signals → `kept`; past due +3d → `stale`, +10d → `broken`. Idempotent via a seen-set in the tool's agent-state memory.
- **Surface**: `summary.json` (due-48h, overdue-to-owner, oldest unresolved, 30-day keep-rate) — the SessionStart hook prints 3–4 lines of it in every morning brief; the voice orb can read it aloud.
- **Schedule**: daily throttle in the refresh loop (limit ~20 meetings/run to bound cost).

Why it matters: trust is a consultant's currency; keep-rate is a leading indicator of over-commitment, and "owed to you, 6 days late" is leverage you forget you had.

## B · Cross-agent contradiction detector (`contradiction_detector.py`)

When many agents each hold their own `memory.json`, divergent realities silently coexist — one agent believes the bid is at Qualify, another at Propose; one says the contact is CFO, another VP Finance. Downstream outputs inherit whichever lie they read first.

- **Extraction is deterministic** (no LLM in the core path): a gazetteer of bid ids (from the open-bids endpoint) + client/agent names (from the registry) + a belief-type whitelist {stage, value, budget, deadline, probability, role, owner}; regex for money/stage-words/percentages/titles → (agent, entity, belief_type, value) tuples.
- **Compare pairwise** across agents on (entity, belief_type); conflicting normalized values → ContradictionRecord with severity (high: value/stage/deadline · medium: role/budget · low: rest).
- **Output**: `_brain_api/agent/contradictions.json` (always written, possibly empty — an empty file is a healthy signal) + one escalation note per HIGH record into the priority queue, deduped forever via emitted-id state.
- **Schedule**: every refresh cycle (it's cheap).
- **Honesty rule**: most agent memories hold operational learnings, not entity beliefs — low extraction coverage is correct, not a bug. Tune the gazetteer as clients onboard; never widen regexes until false positives are measured.

## C · Deal microstructure tape (`deal_tape.py`)

Treat each opportunity's meeting series as a price-discovery session. Per meeting, extract CLIENT_ASK vs OUR_OFFER vs UNRESOLVED_OBJECTIONS (+ a price signal: pushback/neutral/warming) and compute a spread score; the time-ordered series is the tape.

- **Trend** = least-squares slope over the series: compressing = legitimately closing; widening on a bid in Propose/Negotiate = re-qualify escalation.
- LLM mode over distilled sections only; `--no-llm` keyword fallback (steep, expensive, not competitive, reduce, discount…). Calibrate the extraction on 3–5 real meeting series the user validates before trusting the tape for decisions — a wrong compressing tape gives false confidence on a stalled deal.
- **Output**: `_brain_api/bid/<bid>/spread_tape.json` + a human `spread_tape.md` (table + sparkline + verdict).
- **Schedule**: per open bid, every refresh, `--no-llm`; full LLM pass weekly or on demand.
- Children worth building once tapes accumulate: objection half-life tracking, pre-meeting spread-velocity voice brief, cross-bid spread-shape mining, competitor anchors from client-mentioned prices.

## Verification gates (behavioral, per SELFTEST discipline)

- Promise ledger: fixture meetings → 2 promises via `--no-llm`, one flips `kept` after reconcile, re-run adds zero duplicates, summary correct.
- Contradiction detector: fixture agents planted with stage + value conflicts → exactly those 2 records, correct severities, `--dry-run` writes nothing, no duplicate escalations on re-run.
- Deal tape: fixture series with resolving objections → compressing; inverted fixture → widening + escalation. Then one REAL meeting series: if the tape shows compressing-to-zero on a deal you KNOW has price pushback, your extraction is broken — fix it before wiring.
