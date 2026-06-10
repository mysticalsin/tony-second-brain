# Spec 02 — The voice: a grounded assistant with a face

The orb is a floating WebGL particle sphere on `document.body` (fixed, bottom-right, draggable) with states: idle / listening / thinking / speaking. Voice is optional — the same brain serves a text ask-bar. Build text first, voice on top.

## Pipeline overview

```
wake word ──┐
PTT click ──┼─> record (WebAudio) ─> STT (whisper-cli) ─> intent gate ─> BRAIN (agent CLI) ─> TTS cascade ─> playback
hotkey ─────┘                                              │ deterministic commands (no LLM)
```

## STT (speech-to-text)

- **macOS:** `whisper-cli` (whisper.cpp via Homebrew) with a small model (base/small) — serverless, per-call spawn, no localhost daemon (privacy + simplicity). Record mic to wav via WebAudio + ffmpeg conversion.
- **Windows/Linux:** whisper.cpp builds everywhere; or faster-whisper via a python venv.
- Mic discipline: ONE AudioContext + stream, **single-flight init guard** (two racing callers must never build two graphs — leaks a live mic). Track `ended` events on tracks (OS can revoke mic) → full teardown + user notice. Adaptive noise floor for wake-word VAD — but freeze adaptation while TTS is playing (it latches onto the assistant's own voice).
- AEC on (`echoCancellation: true`) so it doesn't hear itself.

## Wake word

Continuous lightweight loop: short rolling captures → STT → fuzzy-match the wake word (their assistant's name). Keep a ring buffer of last ~12 heard strings for a diagnostics modal ("why didn't it wake?"). Wake keeps the mic hot; explicit mic-off releases it. Every state change shows a Notice — silent state changes are the #1 UX complaint.

## The brain (the important part)

Spawn the user's agent CLI per turn — `claude -p "<prompt>"` or `codex exec` — **keyless** via their subscription login. Build the prompt from:

1. **Persona block** — from interview. Hard rules that survived production:
   - *Never claim capabilities the tool list doesn't grant.* If the brain can only read, the persona must say so — otherwise it hallucinates actions ("I've opened that for you") and trust dies.
   - Spoken style: ≤3 sentences default, numbers stated directly, no markdown (strip before TTS — but don't mangle `snake_case` or delete code-span contents).
2. **Grounding context** — recall results (below) + 2–3 key JSON endpoints (`_brain_api/...`) inlined. Strict rule in prompt: *answer ONLY from provided context + question; if context lacks it, say so.*
3. **History** — last ~6 turns, persisted across reloads; FILTER on load (drop `[BLANK_AUDIO]`-style garbage and the weird replies it provoked — poisoned history replays its style into every future answer).

**Semantic recall (the grounding engine):** index the vault into a local vector DB (qdrant via a tiny python daemon, fastembed/all-MiniLM embeddings — fully offline). Daemon exposes `GET 127.0.0.1:<port>/retrieve?q=...&top=6` + `/health`; plugin probes with a 1.2s-timeout curl and degrades gracefully when cold. Re-index on a schedule. Hygiene on hits: dedupe by basename, drop template/export junk, rank core dirs above bulk-import dirs.

**Deterministic command layer BEFORE the LLM:** pattern-match "remember that …" / "add … to today" / "open …" → direct vault writes with an audit log (`actions.jsonl`) and a voice yes/no confirm gate for writes. Cheap, instant, and can't hallucinate.

## TTS cascade

Priority chain with **sticky-per-reply engine choice** (never switch engines mid-reply — the timbre shift is jarring):

1. **Cloned/premium voice** (ElevenLabs API) when online — their own voice w/ consent, or stock.
2. **Local neural fallback** (F5-TTS / NeuTTS / Kokoro python daemon) — offline capable. ⚠️ Heavy (torch model load): background-warm AFTER boot, don't gate spawn on `navigator.onLine` (lies), and any "is the primary up" guard must include every engine in the chain.
3. **OS voice** (`say` on macOS / SAPI / espeak) — last resort, always works.

Daemon protocol gotcha (cost a full day): if a daemon answers over a FIFO/stdin-stdout queue, **never splice a timed-out waiter out of a positional queue** — every later reply pairs with the wrong sentence. Mark dead, keep the slot, drop late chunks, arm timers only at queue head.

Playback: sentence-chunked for low latency; barge-in (mic click while speaking = cancel turn, then listen); a visible "processing" stepper HUD under the orb labeling the stage (record→transcribe→think→speak) with elapsed time — silence must read as *working*, not *dead*.

## Proactive layer (optional, after core works)

- Morning digest at their chosen time: brain summarizes today's pipeline/meetings → spoken once when the app is first visible. **Stamp 'delivered' only AFTER successful speech** (separate written-flag vs spoken-flag — stamping first burns the daily slot silently).
- Monitors: deadline alerts, agent-write counts — debounced, quiet-hours gated, same delivered-after-speak rule.

## Resource rules

- Keep-warm pings (CLI prewarm) only while orb visible AND user interacted <10 min ago AND `!document.hidden`.
- All daemons: spawn detached-safe, kill in teardown, idle-reap after N minutes unused.
- Everything spawned per-turn must be async (`execFile`, not `spawnSync`) — sync spawns freeze the renderer.
