# Tony Second Brain — the agent-native vault, alive

**An Obsidian vault that is alive.** A voice AI with a WebGL orb that *thinks visibly* — neural sparks crawl across your file tree as it reads your notes. A command-center dashboard where neglected clients drift toward the center of a radar and deadlines spawn raid bosses with real HP bars. Ghost files show you what's *missing* from your projects. A terminal-embedded AI pair that never loses your session.

This is not a plugin you install. **It's a blueprint your AI agent builds — personalized to your life.**

> 🧠 Built by **Tony Walteur** — AI Lead & Bid Manager.
> Connect: **[linkedin.com/in/tony-walteur](https://www.linkedin.com/in/tony-walteur-7067b81a2/)**
> If you build one of these, I genuinely want to see it — tag me.


<img width="3598" height="2218" alt="image" src="https://github.com/user-attachments/assets/4c5e51b7-037a-4e1b-b90c-996340f7079c" />


---

## What you get

| Layer | What it does |
|---|---|
| 🔮 **Ultron voice orb** | Wake-word voice assistant inside Obsidian: local Whisper STT → Claude/Codex CLI brain grounded in YOUR vault → cloned-voice TTS cascade with offline fallback. WebGL particle orb that reacts to state. |
| ⚡ **Synapse layer** | While the AI thinks, every file it actually reads flashes in your explorer and a curved axon spark flies from the orb to it. Real accesses, never theater. Plus ambient neuron-chain cascades during thinking. |
| 📊 **Command center** | Dashboard tabs: pipeline, spend (real per-model pricing), agent fleet health, account aggro radar, raid-boss deadlines, marble-run triage, corpse-run retro graveyard. |
| 👻 **Phantom files** | Your project folders render ghost rows for the artifacts a successful project would have at this stage — click to materialize. |
| 🩻 **Power tools** | MRI-mode document tomography, 24h Vault CCTV replay, git Time-Scrub cinema, agent Diagnostic Chamber (depose your AI agents in first person), win-theme Forge, meeting Loadout screen, Sparring Chamber (rehearse against a corpus-built counterpart). |
| 🔧 **Machine layer** | A `_brain_api/` of pre-computed JSON endpoints your agents query instead of crawling files, an hourly refresh loop, honest cost tracking with transcript dedupe, and a relay baton for multi-model handoffs. |

Everything binds to **real data in your vault**. The #1 design law: *no theater* — if a pixel moves, it's because your data moved.

## How it works (the plug-and-play part)

You don't clone-and-configure. You hand this repo to your coding agent:

```bash
git clone https://github.com/mysticalsin/tony-second-brain
cd tony-second-brain
claude   # or codex, or any agentic CLI with file access
```

Then say:

> **"Read START-HERE-AGENT.md and build me my second brain."**

Your agent will:
1. **Interview you** (~10 questions: your role, what you track, voice preferences, which AI CLIs you have, macOS/Windows/Linux)
2. **Build phase by phase** — vault structure → plugin core → dashboard → voice → visual effects → power features — with a verification gate after each phase
3. **Personalize everything** — your domains instead of mine, your folder taxonomy, your accent colors, your daily rhythm
4. **Self-test behavior, not just load** — `specs/06-hard-won-lessons.md` exists because I shipped three features that "loaded clean" and were dead on arrival

Expect a few hours of agent time for the full build; the core (vault + dashboard + voice) lands first and works standalone.

## Requirements

- [Obsidian](https://obsidian.md) (free)
- One agentic coding CLI: [Claude Code](https://claude.com/claude-code) or Codex CLI (the brain; subscriptions work — no API key needed)
- macOS gets the full experience (voice pipeline uses whisper-cli + afplay; specs include Windows/Linux substitutions)
- Optional: [ElevenLabs](https://elevenlabs.io) for a cloned voice (offline fallbacks included), `ffmpeg`, `git`

## Why this exists

I built this for my own work — bids, clients, meetings, a fleet of writing agents — and the unlock wasn't any single feature. It was making the invisible visible: *watching* the AI read my vault, *seeing* which client relationships were going cold, having missing work physically appear as ghosts. The system answered the question dashboards never answer: **what should I be afraid of today?**

The architecture decisions, the perf rules, and especially the failure modes in `specs/06-hard-won-lessons.md` are battle-tested — every lesson in there cost me a real debugging session.

## Repository map

```
START-HERE-AGENT.md        ← give this to your agent (the master build prompt)
interview/INTERVIEW.md     ← the personalization questionnaire your agent runs
specs/
  00-architecture.md       ← vault layers, design laws, machine-readable surfaces
  01-plugin-core.md        ← dashboard shell, cards, caching/refresh discipline
  02-ultron-voice.md       ← STT → brain → TTS pipeline, wake word, grounding rules
  03-visual-effects.md     ← synapse layer, neural cascade, orb, compositor perf rules
  04-feature-catalog.md    ← all 20 power features, each fully spec'd
  05-data-pipelines.md     ← _brain_api endpoints, refresh loop, honest cost tracking
  06-hard-won-lessons.md   ← the gotchas (read this even if you build nothing)
verification/SELFTEST.md   ← behavioral checks per phase — "loads clean" is not done
LICENSE                    ← MIT, with attribution request
```

## Attribution

This blueprint is MIT-licensed. Keep the credit line visible in your build (the master prompt wires it into your plugin's settings footer and README automatically):

> *Built on Tony Second Brain by [Tony Walteur](https://www.linkedin.com/in/tony-walteur-7067b81a2/).*

— Tony
