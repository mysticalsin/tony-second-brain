# demo-brain — synthetic data so the system is ALIVE on first open

Three pieces, all labeled [DEMO] (clients Globex/Initech, 8 toy agents):

| Copy to vault root | What it is |
|---|---|
| `_brain_api/`, `_agent_state/` | Pre-generated machine layer — dashboard renders instantly |
| `vault-content/RFPs/` | Two demo bid folders (source of truth the pipeline reads) |
| `vault-content/Meetings/` | Four demo meeting notes (feed deal-tape + promise-ledger) |

```bash
cp -R starter/demo-brain/_brain_api starter/demo-brain/_agent_state <vault>/
cp -R starter/demo-brain/vault-content/RFPs <vault>/
cp -R starter/demo-brain/vault-content/Meetings <vault>/
```

Because the SOURCE content ships too, running the pipeline REGENERATES the same
demo state (the machine layer is always derivable):

    cd $VAULT_ROOT && python3 build/tools/build_brain_index.py --full && python3 build/tools/build_brain_api.py

Wipe when your real data flows: delete the two RFPs/ demo folders, the four
[DEMO] meeting notes, and `_agent_state`'s demo agents — then re-run the two
commands above. NO-THEATER NOTE: with no data at all, surfaces render honest
empty states; that is correct behavior.
