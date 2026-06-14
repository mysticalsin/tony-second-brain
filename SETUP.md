# SETUP — the base standard (deterministic, 10 minutes, zero AI judgment)

> **Read this first, human or agent.** This page produces the SAME result on every machine: same structure, same dark-purple look, same dashboard, same visible Ultron orb. Personalization comes AFTER the base verifies — never instead of it. If your agent is doing this, tell it: *"Execute SETUP.md literally. Do not improvise, substitute, or skip the VERIFY gate."*

## 0. Requirements
[Obsidian](https://obsidian.md) (free, v1.5+) · macOS for the full experience (Windows/Linux: everything works except voice + somatic channels — see specs/02 substitutions).

## 1. Create the vault from the skeleton (copy, don't invent)
```bash
git clone https://github.com/mysticalsin/tony-second-brain.git
cp -R tony-second-brain/starter/vault-skeleton "<path>/My Second Brain"
```
Open `<path>/My Second Brain` in Obsidian as a vault ("Open folder as vault"). When asked about restricted mode: **turn it off** (community plugins must run).

## 2. Install the plugin (verbatim — this IS the product)
```bash
cp -R tony-second-brain/plugin/claude-command-center "<vault>/.obsidian/plugins/"
```
Then in Obsidian → Settings → Community plugins:
1. Enable **Claude Command Center**
2. Browse → install + enable **Dataview**, **Hot Reload** (search "hot reload"), **Local REST API**, **Terminal**

The skeleton's `.obsidian/appearance.json` already sets the look: dark theme, accent `#6600AE`, both CSS snippets enabled. Don't change anything yet.

## 3. Seed the demo brain (so it's alive, not empty)
```bash
cp -R tony-second-brain/starter/demo-brain/_brain_api "<vault>/"
cp -R tony-second-brain/starter/demo-brain/_agent_state "<vault>/"
```
This is labeled synthetic data ([DEMO] Globex/Initech bids, 8 toy agents). It exists so every dashboard surface renders immediately. Wipe it later (`starter/demo-brain/README.md`).

## 3b. Install the infrastructure (the machine layer + conduct/safety layer + Claude Code in Obsidian)
```bash
mkdir -p "<vault>/build" "<vault>/99_Meta"
cp -R tony-second-brain/infra/tools "<vault>/build/tools"
cp tony-second-brain/infra/hooks/*.sh "<vault>/99_Meta/"
export VAULT_ROOT="<vault>"     # add to your shell profile (~/.zshrc)
cd "$VAULT_ROOT" && python3 build/tools/build_brain_index.py --full && python3 build/tools/build_brain_api.py
```
Claude Code inside Obsidian: install the CLI (`npm i -g @anthropic-ai/claude-code`, then `claude login`), then in Obsidian run command "Terminal: Open terminal" — it opens at the vault root; type `claude`. The vault's `CLAUDE.md` (shipped in the skeleton) is its operating contract: it reads your notes, queries `_brain_api/`, and follows the same rules as the original. Details + refresh-loop scheduling: `infra/README.md`.

**Conduct and safety layer (optional but recommended):** the system ships a five-gate enforcement stack — a PreToolUse write-check hook, a triage promotion gate with per-agent reputation thresholds, an injection scanner for external content, an integrity check for canonical files, and an incident path. Every claim agents write to the vault is tagged `verified / assumed / unknown`. Run the eval harness any time to confirm behavioral gates are working:
```bash
bash "$VAULT_ROOT/99_Meta/conduct-eval.sh"      # four behavioral probes — exit 0 = all pass
bash "$VAULT_ROOT/99_Meta/conduct-sync-check.sh" # confirm all inlined conduct copies are in sync
bash "$VAULT_ROOT/99_Meta/conduct-stats.sh" --check  # confirm all five success metrics are within target
```
Full spec: `specs/10-conduct-and-safety.md`.

## 4. VERIFY-BASE (the gate — all eight must pass)
| # | Check | Expected |
|---|---|---|
| 1 | Restart Obsidian (Cmd-R) | No error popups |
| 2 | Look bottom-right | **The Ultron orb is visible** — a purple particle sphere |
| 3 | Command palette → "Claude Command Center: Open dashboard" | Dashboard opens, dark purple, tabs incl. Pipeline/Fleet |
| 4 | Dashboard → Pipeline tab | Two [DEMO] bids render; tide/water visual present |
| 5 | Command palette → type "UX:" | ~20 demo commands listed; run "UX: Pipeline Tide demo" — water animates |
| 6 | Status bar (bottom) | Agent-breath glyphs + a pulsing metabolism dot |
| 7 | Terminal: "Terminal: Open terminal" → `claude` → ask "what bids are open?" | Claude answers from the [DEMO] `_brain_api` data |
| 8 | `bash 99_Meta/verify-brain.sh --session-start` (in the terminal) | Brief prints: freshness, the 2 [DEMO] bids, promises pulse |

**All eight pass → you have the standard.** Anything fails → fix before personalizing (90% of failures: plugin not enabled, restricted mode on, demo-brain folders not copied to the vault ROOT).

## 5. Only now: personalize
Hand your agent `START-HERE-AGENT.md` → it runs the interview and personalizes CONTENT (your folders' notes, your clients, your voice, your colors if you want). The structure and plugin stay the standard — that's what keeps specs, fixes, and updates drop-in compatible.

Voice (optional, macOS): specs/02-ultron-voice.md — local Whisper + TTS daemons; the orb works text-first without any of it.

> Attribution stays visible (LICENSE): *Built on Tony Second Brain by [Tony Walteur](https://www.linkedin.com/in/tony-walteur-7067b81a2/).*
