# infra — the building (your data is the people; this is the structure)

The full machine layer from the original build, sanitized and vault-relative.
Everything reads `VAULT_ROOT` from the environment — no hardcoded paths.

## Install (after SETUP.md steps 1-3)

```bash
mkdir -p "<vault>/build" "<vault>/99_Meta"
cp -R infra/tools "<vault>/build/tools"
cp infra/hooks/brain-refresh.sh infra/hooks/verify-brain.sh "<vault>/99_Meta/"
export VAULT_ROOT="<vault>"   # add to your shell profile
```

## What you get

- **`build/tools/`** (50 tools): `build_brain_index.py` + `build_brain_api.py` (the machine layer), `triage_dust_writes.py` + `triage_watcher.py` (agent-write gate), `meeting_ingest.py`, `promise_ledger.py`, `contradiction_detector.py`, `deal_tape.py`, `prerfp_radar.py`, the full bid engine (`rfp_pipeline.py`, `win_patterns.py`, `bid_risk.py`, `close_bid.py`, `ghost_brief.py`, `grill_bid.py`, `deck_from_rfp.py`…), cost tracking, semantic recall set, agent reputation, self-promote loop.
- **`99_Meta/` hooks**: `verify-brain.sh` (SessionStart brief: freshness, open bids, promises pulse, stale agents — wire into your agent CLI's hooks) and `brain-refresh.sh` (the 16-step refresh loop — run hourly via cron/launchd/Task Scheduler).

## First run (on the demo brain)

```bash
cd "$VAULT_ROOT"
python3 build/tools/build_brain_index.py --full
python3 build/tools/build_brain_api.py
python3 build/tools/contradiction_detector.py
bash 99_Meta/verify-brain.sh --session-start
```
The session brief should print with the [DEMO] bids and promises. From then on the loop maintains itself; replace demo data with your own as meetings/notes flow in (specs/05).

LLM-powered tools (`promise_ledger.py`, `deal_tape.py` extraction, `rfp_pipeline.py` synthesis) call the `claude` CLI when present; every one has a `--no-llm` deterministic mode.
