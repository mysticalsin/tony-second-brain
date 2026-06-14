#!/usr/bin/env bash
# =============================================================================
# competence-eval.sh -- LLM-as-judge competence scorer (SCAFFOLD / PROOF OF CONCEPT)
#
# STATUS: SCAFFOLD -- proof-of-concept that proves the LLM-judge loop works.
# Scores ONE sample output against ONE output type's rubric and prints a
# 1-5 score per dimension.  NOT production-ready:
#   - No integration with triage pipeline or reputation.py yet.
#   - No multi-sample batch mode yet.
#   - Passing-bar enforcement is advisory (prints WARN, no gate).
#   - See infra/conduct/competence-eval/README.md for the full program plan.
#
# USAGE:
#   VAULT_ROOT=/path/to/vault bash infra/hooks/competence-eval.sh \
#       --type bid|intel|meeting \
#       --file <path-to-sample-output.md> \
#       [--rfp <path-to-rfp-excerpt.md>]   # bid only, optional compliance anchor
#
# ENVIRONMENT:
#   VAULT_ROOT         required — your vault path (e.g. export VAULT_ROOT=/path/to/vault)
#   JUDGE_MODEL        override judge model (default: claude-sonnet-4-6)
#   VAULT_BRAIN_QUIET  set to 1 automatically during judge call
#
# OUTPUT:
#   Prints one line per dimension: DIMENSION  score/5  rationale
#   Prints PASS/WARN aggregate line.
#   Appends JSON record to $VAULT/_agent_state/_conduct/competence-history.jsonl.
#
# Python helpers: infra/conduct/competence-eval/competence-parse.py
#                 infra/conduct/competence-eval/competence-finalize.py (stdlib only)
#
# bash -n check:  bash -n infra/hooks/competence-eval.sh
# =============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$SCRIPT_DIR/../conduct/competence-eval"
HISTORY="$VAULT/_agent_state/_conduct/competence-history.jsonl"
CLAUDE="${CLAUDE_CLI:-$HOME/.local/bin/claude}"
JUDGE_MODEL="${JUDGE_MODEL:-claude-sonnet-4-6}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
OUTPUT_TYPE=""
SAMPLE_FILE=""
RFP_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --type)   OUTPUT_TYPE="$2"; shift 2 ;;
    --file)   SAMPLE_FILE="$2"; shift 2 ;;
    --rfp)    RFP_FILE="$2"; shift 2 ;;
    *)        echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [ -z "$OUTPUT_TYPE" ] || [ -z "$SAMPLE_FILE" ]; then
  echo "Usage: VAULT_ROOT=/path/to/vault $0 --type bid|intel|meeting --file <sample.md> [--rfp <rfp-excerpt.md>]"
  exit 1
fi

if [ ! -f "$SAMPLE_FILE" ]; then
  echo "ERROR: sample file not found: $SAMPLE_FILE"
  exit 1
fi

case "$OUTPUT_TYPE" in
  bid|intel|meeting) ;;
  *)
    echo "ERROR: --type must be one of: bid, intel, meeting"
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# Rubric definitions (inline; one per output_type)
# To add a new output type: add a RUBRIC_<type> variable and a case branch
# in the selector below, then add matching logic in competence-parse.py.
# ---------------------------------------------------------------------------

RUBRIC_bid="Score the following bid/proposal output on each dimension from 1 (absent or broken) to 5 (excellent).

Dimensions:
  win_theme_strength   -- Are 1-3 differentiated win themes named and threaded through the narrative?
  compliance_coverage  -- Does the response address every explicit RFP requirement?
  pricing_rigor        -- Is pricing justified (margin floor, scenario, assumptions stated)?
  evidence_quality     -- Are claims backed by named case studies, metrics, or vault precedents?
  executive_summary    -- Does the exec summary lead with buyer value, not our credentials?

Return ONLY valid JSON in this exact shape, no prose before or after:
{
  \"output_type\": \"bid\",
  \"win_theme_strength\":   {\"score\": 0, \"rationale\": \"25 words max\"},
  \"compliance_coverage\":  {\"score\": 0, \"rationale\": \"25 words max\"},
  \"pricing_rigor\":        {\"score\": 0, \"rationale\": \"25 words max\"},
  \"evidence_quality\":     {\"score\": 0, \"rationale\": \"25 words max\"},
  \"executive_summary\":    {\"score\": 0, \"rationale\": \"25 words max\"}
}"

RUBRIC_intel="Score the following intelligence / market research output on each dimension from 1 (absent or broken) to 5 (excellent).

Dimensions:
  source_quality       -- Are sources primary and named (analyst report, official site) vs. vague?
  recency              -- Are facts dated? Time-sensitive claims within last 90 days?
  actionability        -- Does the output end with 1-3 explicit implications or recommended actions?
  competitor_accuracy  -- For named competitors: characterisation consistent with current positioning?
  uncertainty_labelling -- Are unconfirmed claims labelled assumed or unknown?

Return ONLY valid JSON in this exact shape, no prose before or after:
{
  \"output_type\": \"intel\",
  \"source_quality\":        {\"score\": 0, \"rationale\": \"25 words max\"},
  \"recency\":               {\"score\": 0, \"rationale\": \"25 words max\"},
  \"actionability\":         {\"score\": 0, \"rationale\": \"25 words max\"},
  \"competitor_accuracy\":   {\"score\": 0, \"rationale\": \"25 words max\"},
  \"uncertainty_labelling\": {\"score\": 0, \"rationale\": \"25 words max\"}
}"

RUBRIC_meeting="Score the following meeting recap / action log on each dimension from 1 (absent or broken) to 5 (excellent).

Dimensions:
  decision_fidelity  -- Are all decisions captured with exact wording, not paraphrased into ambiguity?
  owner_assignment   -- Does every action item have a named owner (person, not role)?
  date_fidelity      -- Does every commitment/deadline carry the date as stated, not inferred?
  completeness       -- Are all agenda items represented, nothing silently dropped?
  no_invention       -- Does the recap avoid adding context not in the transcript?

Return ONLY valid JSON in this exact shape, no prose before or after:
{
  \"output_type\": \"meeting\",
  \"decision_fidelity\": {\"score\": 0, \"rationale\": \"25 words max\"},
  \"owner_assignment\":  {\"score\": 0, \"rationale\": \"25 words max\"},
  \"date_fidelity\":     {\"score\": 0, \"rationale\": \"25 words max\"},
  \"completeness\":      {\"score\": 0, \"rationale\": \"25 words max\"},
  \"no_invention\":      {\"score\": 0, \"rationale\": \"25 words max\"}
}"

# ---------------------------------------------------------------------------
# Passing-bar definitions (advisory -- SCAFFOLD does not gate on these)
# CRITICAL format: "dimension:min_score ..." space-separated pairs
# ---------------------------------------------------------------------------
PASSING_AVG_bid="3.5";     CRITICAL_bid="compliance_coverage:2 pricing_rigor:2"
PASSING_AVG_intel="3.5";   CRITICAL_intel="source_quality:3 recency:3"
PASSING_AVG_meeting="3.5"; CRITICAL_meeting="decision_fidelity:4 owner_assignment:4 date_fidelity:4"

# ---------------------------------------------------------------------------
# Select rubric + passing bar for the requested output type
# ---------------------------------------------------------------------------
case "$OUTPUT_TYPE" in
  bid)
    RUBRIC="$RUBRIC_bid"
    PASSING_AVG="$PASSING_AVG_bid"
    CRITICAL="$CRITICAL_bid"
    ;;
  intel)
    RUBRIC="$RUBRIC_intel"
    PASSING_AVG="$PASSING_AVG_intel"
    CRITICAL="$CRITICAL_intel"
    ;;
  meeting)
    RUBRIC="$RUBRIC_meeting"
    PASSING_AVG="$PASSING_AVG_meeting"
    CRITICAL="$CRITICAL_meeting"
    ;;
esac

# ---------------------------------------------------------------------------
# Build judge prompt; write to temp file so the shell never interpolates it
# ---------------------------------------------------------------------------
TMPDIR_SAFE="${TMPDIR:-/tmp}"
PROMPT_FILE=$(mktemp "${TMPDIR_SAFE}/competence-prompt-XXXXXX.txt")
REPLY_FILE=$(mktemp "${TMPDIR_SAFE}/competence-reply-XXXXXX.txt")
trap 'rm -f "$PROMPT_FILE" "$REPLY_FILE"' EXIT

SAMPLE_CONTENT=$(cat "$SAMPLE_FILE")

if [ -n "$RFP_FILE" ] && [ -f "$RFP_FILE" ]; then
  RFP_SECTION="

=== RFP EXCERPT (compliance anchor) ===
$(cat "$RFP_FILE")
=== END RFP EXCERPT ==="
else
  RFP_SECTION=""
fi

printf '%s\n\n=== OUTPUT TO SCORE ===\n%s%s\n=== END OUTPUT ===' \
  "$RUBRIC" "$SAMPLE_CONTENT" "$RFP_SECTION" > "$PROMPT_FILE"

# ---------------------------------------------------------------------------
# Call LLM judge
# ---------------------------------------------------------------------------
echo "============================================================"
echo "competence-eval.sh (SCAFFOLD) -- $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "output_type : $OUTPUT_TYPE"
echo "sample_file : $SAMPLE_FILE"
echo "judge_model : $JUDGE_MODEL"
echo "============================================================"

t0=$(date +%s)
VAULT_BRAIN_QUIET=1 "$CLAUDE" \
  -p "$(cat "$PROMPT_FILE")" \
  --model "$JUDGE_MODEL" \
  --setting-sources "" \
  --strict-mcp-config \
  --mcp-config '{"mcpServers":{}}' \
  2>/dev/null > "$REPLY_FILE"
t1=$(date +%s)
ELAPSED=$(( t1 - t0 ))

if [ ! -s "$REPLY_FILE" ]; then
  echo "ERROR: claude returned empty reply (model unavailable or rate-limited)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Parse JSON with python3 (stdlib only -- see competence-parse.py)
# ---------------------------------------------------------------------------
PARSE_RESULT=$(python3 "$EVAL_DIR/competence-parse.py" \
  "$OUTPUT_TYPE" "$PASSING_AVG" "$CRITICAL" \
  < "$REPLY_FILE")

# Print display lines (filter JSON_RECORD marker from display)
echo "$PARSE_RESULT" | grep -v "^JSON_RECORD:"

# ---------------------------------------------------------------------------
# Finalize and append the history record
# ---------------------------------------------------------------------------
JSON_LINE=$(echo "$PARSE_RESULT" | grep "^JSON_RECORD:" | sed 's/^JSON_RECORD://')

if [ -n "$JSON_LINE" ]; then
  TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  FINAL_JSON=$(echo "$JSON_LINE" | python3 "$EVAL_DIR/competence-finalize.py" \
    "$TS" "$JUDGE_MODEL" "$SAMPLE_FILE" "$ELAPSED")
  mkdir -p "$(dirname "$HISTORY")"
  touch "$HISTORY"
  echo "$FINAL_JSON" >> "$HISTORY"
  echo ""
  echo "Record appended to: $HISTORY"
fi

echo "Elapsed: ${ELAPSED}s"
echo "============================================================"
