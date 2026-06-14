---
type: governance-policy
title: "Send-Gating Policy — External Egress is a Human-Only Step"
status: active
created: 2026-06-13
owner: vault-owner
phase: "5.2 conduct-hardening"
applies_to: [all-agents, all-scripts, claude, dust, ultron, gemini, codex, hermes]
---

# Send-Gating Policy

## The one rule

**No agent, script, or automation in this vault has an external-send capability.**
Send is a separate, explicit, human-run step — it never happens automatically.

---

## What this means in practice

### Agents

Every agent in the fleet (Dust, Claude Code, Gemini, Codex, Hermes, Ultron) operates
under the same constraint:

- **Output goes to the vault inbox** (`00_Inbox/from-dust/<agent>/` or `Outbound/`).
- **Auto-promotion is not sending.** Triage (`triage_dust_writes.py`) may auto-promote
  a high-confidence write to its `target_path` inside the vault. This is a file copy
  inside the sync service. It is not an email, not a webhook, not a POST to an external host.
- **Outbound/ is a review queue.** Files in `Outbound/` are drafts awaiting the owner's
  manual review and action. Nothing in the pipeline reads Outbound/ and transmits it.
- `email_draft` and `social_post` output types are always **Tier 0** (review-only).
  Auto-promotion to Tier 1+ for these types requires explicit owner sign-off.

### Scripts and tools

- `brain-refresh.sh` — no send capability. Any outbound HTTP call should be limited to
  local services or the allowlisted Claude API. Localhost only for daemon communication.
- `graph_sync.py` / email sync — read-only calls (`Mail.Read`, `Calendars.Read`).
  No `Mail.Send`, no `/sendMail` endpoint, no write-back.
- Any capture / ingest scripts — POST to `https://api.anthropic.com/v1/messages`
  (Claude API, allowlisted internal tooling). Not an email send; not an external
  notification channel.

---

## Allowlisted outbound hosts (scripts, not agents)

These are the only external hosts any script in this vault may reach. All are read/query,
never send:

| Host / endpoint                                   | Script              | Purpose                        | Method    |
|---------------------------------------------------|---------------------|--------------------------------|-----------|
| `https://api.anthropic.com/v1/messages`           | `capture_session.py` | Claude API summarisation call  | POST (LLM inference, not email) |
| `https://graph.microsoft.com/v1.0/me/messages`    | `graph_sync.py`     | Read unread emails into vault  | GET       |
| `https://graph.microsoft.com/v1.0/me/calendarview`| `graph_sync.py`     | Read calendar into vault       | GET       |
| `https://graph.microsoft.com/v1.0/me/onenote/...` | `onenote_to_obsidian.py` | Read notes                | GET       |
| `http://127.0.0.1:7766/reindex`                   | `brain-refresh.sh`  | Local recall-daemon trigger    | POST (localhost) |
| `http://127.0.0.1:7766/health`                    | `brain-refresh.sh`  | Local recall-daemon health     | GET (localhost) |

Any deviation from this table is a finding for `infra/hooks/send-gating-audit.sh`.

---

## What "send" requires

Any external transmission (email, webhook, API write, Teams DM to a human, Slack message,
social post) requires:

1. The owner opens the draft manually in the relevant application (Outlook, Teams, LinkedIn, etc.).
2. The owner reviews the content.
3. The owner clicks Send / Post / Publish.

No script, agent, scheduled job, or automation may perform step 3.

---

## Enforcement mechanism

`infra/hooks/send-gating-audit.sh` — run on demand or scheduled — scans agent playbooks,
scripts, and configuration for egress patterns and flags any that could bypass this policy.
Findings are logged to `_agent_state/_conduct/conduct-violations.jsonl` (source `"send-gating"`).

The audit script's allowlist mirrors the table above. New allowlist entries require a
policy update here first.

---

## Change control

This policy may only be changed by the vault owner explicitly. Any agent proposing to graduate an
`email_draft` or external-notify output type to Tier 2 (auto-act) must go through the
gate defined in `autonomy-policies.md §"The gate to graduate ANY output type"`:
calibration proven, reversibility confirmed, owner signs off, kill switch tested.
