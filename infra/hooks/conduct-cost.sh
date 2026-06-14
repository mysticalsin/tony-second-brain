#!/usr/bin/env bash
# conduct-cost.sh — Measures the per-turn TOKEN TAX the conduct core adds across all surfaces.
#
# Usage:
#   bash infra/hooks/conduct-cost.sh            # Default N=20 turns/agent/day
#   bash infra/hooks/conduct-cost.sh --turns 50 # Custom turns/agent/day
#
# What is measured:
#   (a) Rule 8 / builder-rules-brief block  (if present)
#   (b) Inline core blocks in AGENTS.md, GEMINI.md, HERMES.md, CLAUDE.md
#   (c) Ultron persona conduct              (.obsidian/plugins/... if present)
#   (d) Injectable core                     infra/conduct/agent-behavior-standard.md
#
# Fleet-scale model:
#   Agent count read from _agent_state/_registry.json (_meta.fleet_count * active fraction)
#   Tokenization: ~4 chars per token (standard heuristic)
#
# Flags:
#   Any single surface > 400 tokens: printed as TRIM CANDIDATE
#
# Output: plain-text table to stdout. Non-destructive, idempotent.
# bash -n clean; python3 stdlib only.

set -uo pipefail

VAULT="${VAULT_ROOT:?Set VAULT_ROOT to your vault path}"
REGISTRY="$VAULT/_agent_state/_registry.json"
RULES_BRIEF="$VAULT/99_Meta/builder-rules-brief.md"
AGENTS_MD="$VAULT/AGENTS.md"
GEMINI_MD="$VAULT/GEMINI.md"
HERMES_MD="$VAULT/HERMES.md"
CLAUDE_MD="$VAULT/CLAUDE.md"
BEHAVIOR_STANDARD="$VAULT/infra/conduct/agent-behavior-standard.md"
ULTRON_JS="$VAULT/.obsidian/plugins/claude-command-center/main.js"

# ── Parse --turns flag ────────────────────────────────────────────────────────

TURNS_PER_DAY=20
while [[ $# -gt 0 ]]; do
  case "$1" in
    --turns)
      TURNS_PER_DAY="${2:?--turns requires a number}"
      shift 2
      ;;
    *)
      echo "Unknown flag: $1" >&2
      echo "Usage: bash infra/hooks/conduct-cost.sh [--turns N]" >&2
      exit 1
      ;;
  esac
done

# ── Delegate all measurement and output to python3 (stdlib only) ──────────────

python3 - "$RULES_BRIEF" "$AGENTS_MD" "$GEMINI_MD" "$HERMES_MD" "$CLAUDE_MD" \
           "$BEHAVIOR_STANDARD" "$ULTRON_JS" "$REGISTRY" "$TURNS_PER_DAY" << 'PYEOF'
import sys, re, json

(rules_brief_path, agents_path, gemini_path, hermes_path, claude_path,
 behavior_path, ultron_path, registry_path, turns_str) = sys.argv[1:]

TURNS = int(turns_str)
CHARS_PER_TOKEN = 4  # standard heuristic (Claude ~3.8-4.0 chars/tok)
TRIM_THRESHOLD = 400  # tokens -- flag if exceeded
DAYS_PER_MONTH = 30


def tok(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"[WARN] Cannot read {path}: {e}", file=sys.stderr)
        return ""


# ── (a) Rule 8 block in builder-rules-brief.md ───────────────────────────────
rules_brief = read(rules_brief_path)
m = re.search(r"(## Rule 8.*?)(?=^## |\Z)", rules_brief, re.M | re.S)
rule8_text = m.group(1).strip() if m else ""
if not rule8_text:
    print("[WARN] Rule 8 block not found in builder-rules-brief.md (ok if file absent)", file=sys.stderr)


# ── (b) Inline conduct cores ──────────────────────────────────────────────────

def extract_agents_inline(text: str) -> str:
    m = re.search(
        r"inline so it.s always in context:\s*(.*?)\n\n",
        text, re.S
    )
    if m:
        return m.group(1).replace("\n", " ").strip()
    m2 = re.search(
        r"inline so it.s always in context:\s*(.*?)(?=\n\*\*You \(Codex\))",
        text, re.S
    )
    if m2:
        return m2.group(1).replace("\n", " ").strip()
    return ""


def extract_inline_conduct(text: str) -> str:
    m = re.search(
        r"The load-bearing rules, inline so they.re always in context:\s*(.*?)(?=\n---|\Z)",
        text, re.S
    )
    if m:
        return m.group(1).replace("\n", " ").strip()
    m2 = re.search(
        r"## Conduct \(Fable-5 core.*?\n\n.*?Full standard:.*?\n\n(.*?)(?=\n---|\Z)",
        text, re.S
    )
    if m2:
        return m2.group(1).strip()
    return ""


agents_inline = extract_agents_inline(read(agents_path))
gemini_inline = extract_inline_conduct(read(gemini_path))
hermes_inline = extract_inline_conduct(read(hermes_path))
claude_inline = extract_inline_conduct(read(claude_path))


# ── (c) Ultron persona conduct line ───────────────────────────────────────────

def extract_ultron_conduct(text: str) -> str:
    m = re.search(r"Conduct: a note.*?not his lawyer\.", text, re.S)
    if m:
        return ("- Conduct: " + m.group(0).replace("Conduct: ", "", 1)).strip()
    m2 = re.search(r'"- Conduct: (.*?)(?=\\n"|\n")', text, re.S)
    if m2:
        raw = m2.group(1).replace("\\n", " ").replace('\\"', '"').strip()
        return "- Conduct: " + raw
    return ""


ultron_conduct = extract_ultron_conduct(read(ultron_path))


# ── (d) Injectable core in agent-behavior-standard.md ────────────────────────

def extract_injectable_core(text: str) -> str:
    m = re.search(
        r"## Injectable core \(SessionStart brief\)(.*?)(?=^## |\Z)",
        text, re.M | re.S
    )
    if m:
        return m.group(1).strip()
    return ""


injectable_core = extract_injectable_core(read(behavior_path))


# ── Fleet count from registry ─────────────────────────────────────────────────

fleet_count = 29
active_count = 22
try:
    reg = json.loads(read(registry_path))
    fleet_count = reg["_meta"].get("fleet_count", 29)
    active_count = reg["_meta"].get("active", 22)
except Exception as e:
    print(f"[WARN] Registry parse error: {e}. Using defaults.", file=sys.stderr)


# ── Build surface table ───────────────────────────────────────────────────────

surfaces = [
    ("(a) Rule 8 / builder-rules-brief.md",          rule8_text,
     "per-Claude-session, injected once at SessionStart"),
    ("(b) Inline core / AGENTS.md (Codex)",           agents_inline,
     "per-Codex-turn (baked into Codex system prompt)"),
    ("(b) Inline core / GEMINI.md",                   gemini_inline,
     "per-Gemini-turn"),
    ("(b) Inline core / HERMES.md",                   hermes_inline,
     "per-Hermes-turn"),
    ("(b) Inline core / CLAUDE.md",                   claude_inline,
     "note: CLAUDE.md links only; Rule 8 is the actual per-session inject"),
    ("(c) Ultron conduct / main.js persona",          ultron_conduct,
     "per-Ultron-turn (conduct segment of full persona)"),
    ("(d) Injectable core / agent-behavior-standard", injectable_core,
     "per-inject when agent loads it explicitly"),
]

TRIM = TRIM_THRESHOLD
SEP_WIDTH = 120
sep = "-" * SEP_WIDTH

print()
print("=" * SEP_WIDTH)
print("  CONDUCT CORE -- TOKEN TAX AUDIT")
print("=" * SEP_WIDTH)
print()
print(f"  Char -> token conversion : 1 token ~= {CHARS_PER_TOKEN} chars (standard heuristic)")
print(f"  Trim threshold           : {TRIM} tokens  (flag surfaces that are trim candidates)")
print(f"  Fleet                    : {fleet_count} registered, {active_count} active agents")
print(f"  N (turns/agent/day)      : {TURNS}")
print()

COL_NAME = 55
COL_CHARS = 7
COL_TOK = 8

header = (f"  {'Surface':<{COL_NAME}} {'chars':>{COL_CHARS}} {'~tokens':>{COL_TOK}}"
          f"  {'When loaded / context'}")
print(header)
print(f"  {sep}")

trim_candidates = []
for name, text, when in surfaces:
    c = len(text)
    t = tok(c)
    flag = "  *** TRIM CANDIDATE > 400 tok ***" if t > TRIM else ""
    if t > TRIM:
        trim_candidates.append((name, t))
    print(f"  {name:<{COL_NAME}} {c:>{COL_CHARS}} {t:>{COL_TOK}}  {when}{flag}")

print(f"  {sep}")
print()


# ── Per-session / per-turn cost summary ──────────────────────────────────────

rule8_tok = tok(len(rule8_text))
ultron_tok = tok(len(ultron_conduct))
agents_tok = tok(len(agents_inline))
gemini_tok = tok(len(gemini_inline))
hermes_tok = tok(len(hermes_inline))
injectable_tok = tok(len(injectable_core))

print("  PER-SESSION / PER-TURN COST SUMMARY")
print(f"  {sep}")
rows_summary = [
    ("Claude session tax (Rule 8, injected once at SessionStart)",  rule8_tok),
    ("Ultron per-turn tax (conduct segment of Ultron persona)",      ultron_tok),
    ("Codex per-turn tax (AGENTS.md inline core)",                   agents_tok),
    ("Gemini per-turn tax (GEMINI.md inline core)",                  gemini_tok),
    ("Hermes per-turn tax (HERMES.md inline core)",                  hermes_tok),
    ("Injectable core (agent-behavior-standard, when loaded)",       injectable_tok),
]
for label, t in rows_summary:
    print(f"  {label:<65} {t:>6} tokens")
print(f"  {sep}")
print()


# ── Fleet-scale monthly estimate ─────────────────────────────────────────────

CLAUDE_SESSIONS_PER_DAY = 3
ULTRON_TURNS_PER_DAY = max(1, TURNS // 2)

claude_daily = CLAUDE_SESSIONS_PER_DAY * rule8_tok
dust_daily = active_count * TURNS * agents_tok
ultron_daily = ULTRON_TURNS_PER_DAY * ultron_tok
gemini_daily = 1 * gemini_tok
hermes_daily = 1 * hermes_tok

total_daily = claude_daily + dust_daily + ultron_daily + gemini_daily + hermes_daily
total_monthly = total_daily * DAYS_PER_MONTH

print(f"  FLEET-SCALE MONTHLY TOKEN ESTIMATE")
print(f"  Assumptions: N={TURNS} turns/agent/day, {active_count} active agents, "
      f"{CLAUDE_SESSIONS_PER_DAY} Claude sessions/day, ~{ULTRON_TURNS_PER_DAY} Ultron turns/day")
print(f"  {sep}")

COL_LABEL = 58
header2 = f"  {'Source':<{COL_LABEL}} {'daily (tok)':>12} {'monthly (tok)':>14}"
print(header2)
print(f"  {sep}")

fleet_rows = [
    (f"Claude sessions ({CLAUDE_SESSIONS_PER_DAY}/day x Rule 8 = {rule8_tok} tok)",
     claude_daily),
    (f"Dust fleet ({active_count} active x {TURNS} turns x {agents_tok} tok/turn)",
     dust_daily),
    (f"Ultron (~{ULTRON_TURNS_PER_DAY} turns/day x {ultron_tok} tok/turn)",
     ultron_daily),
    (f"Gemini (1 session/day x {gemini_tok} tok)",
     gemini_daily),
    (f"Hermes (1 session/day x {hermes_tok} tok)",
     hermes_daily),
]

for label, daily in fleet_rows:
    monthly = daily * DAYS_PER_MONTH
    print(f"  {label:<{COL_LABEL}} {daily:>12,} {monthly:>14,}")

print(f"  {sep}")
total_label = "TOTAL (conduct overhead only)"
print(f"  {total_label:<{COL_LABEL}} {total_daily:>12,} {total_monthly:>14,}")
print(f"  {sep}")
print()


# ── Trim candidates ───────────────────────────────────────────────────────────

if trim_candidates:
    print("  TRIM CANDIDATES  (surfaces exceeding 400-token threshold)")
    print(f"  {sep}")
    for name, t in trim_candidates:
        print(f"    *** {name} ({t} tokens) -- consider trimming to <400 tokens ***")
    print()
else:
    print("  No single surface exceeds the 400-token trim threshold. Fleet looks lean.")
    print()


# ── Notes ─────────────────────────────────────────────────────────────────────

print("  NOTES")
print(f"  {sep}")
print("  1. Rule 8 is the Claude session tax -- injected once at SessionStart via")
print("     builder-rules-brief.md, not on every turn.")
print("  2. Token count is approximate (chars/4). Claude tokenization varies +-10%.")
print("  3. Fleet monthly = daily x 30 days.")
print("  4. Re-run with a custom turn rate: bash infra/hooks/conduct-cost.sh --turns N")
print()

PYEOF
