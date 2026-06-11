# Starter — copy this, don't reinvent it

`vault-skeleton/` is the exact folder structure of the original second brain
(PARA + daily-driver navigation + machine layer + relay). Your agent copies it
VERBATIM as Phase 1 so every build of this blueprint shares the same skeleton —
that's what keeps dashboards, agents, and specs portable between builds.

- Folders only (with `.gitkeep`) — your content is yours.
- `_relay/` comes pre-seeded with the baton protocol files (see `specs/08-relay-baton.md`).
- `.obsidian/community-plugins.json` declares the required plugins; the
  Claude Command Center plugin itself ships in `../plugin/`.

Rename nothing in Phase 1. Move things around later if you must — but a shared
skeleton is what makes someone else's specs, agents, and fixes drop into YOUR
vault without translation.
