# Claude Command Center — the shipped plugin

This is the REAL plugin from the original build (sanitized: no personal data, no
hardcoded paths — voice-daemon paths are empty defaults you set in Settings).
Installing it verbatim is what guarantees your dashboard + Ultron orb look and
behave exactly like the blueprint describes.

## Install (2 minutes)

1. Copy this whole folder to `<your-vault>/.obsidian/plugins/claude-command-center/`
   (the starter skeleton already has `.obsidian/community-plugins.json` expecting it).
2. Obsidian → Settings → Community plugins → enable **Claude Command Center**
   (plus **hot-reload**, **Local REST API**, **Dataview** from the community store).
3. Open the command palette → "Claude Command Center: Open dashboard".
4. Voice (optional): the orb works text-first out of the box; for speech, follow
   `specs/02-ultron-voice.md` and point Settings → voice paths at your daemons.

## What's inside

- `main.js` — dashboard tabs, Ultron orb host, synapse layers (explorer + graph +
  active note), phantom files, all power features. Build-stamped.
- `jarvis-orb.bundle.js` — the WebGL particle orb.
- `styles.css`, `manifest.json`, `data.json` (clean defaults).

Your agent personalizes BEHAVIOR through vault data and Settings — not by
forking the code. Specs 01–04 explain every subsystem when you do want to modify it.

> Built on Tony Second Brain by Tony Walteur — keep the attribution (see LICENSE).
