---
name: cyborgscore
description: Analyse your Claude Code transcripts to compute and share your AI proficiency score. Score is calculated locally; only metrics are sent to cyborgscore.com (never your conversations or code).
allowed-tools: [Bash, Read, Write, AskUserQuestion]
---

# Cyborg Score

Find out your AI tier. Score is based on how you actually use Claude Code:
tool diversity, agent orchestration, skills & MCPs, custom skill creation,
context leverage (memory, schedules, tasks), and conversation depth.

## First time using this?

1. Run `/cyborgscore` — it will open `cyborgscore.com/pair?session=...` in your browser
2. Sign in with Google or an email code
3. Your Claude Code plugin will automatically receive its API token

No passwords. Your transcripts stay on your machine.

## Usage

### Regular flow (paired already)

Run `/cyborgscore`. The plugin will:

1. Scan your Claude Code transcripts from the last 60 days.
2. Infer a role suggestion (engineer / product / gtm / research / devops / ...) **locally** from the first user message of each session.
3. Ask you to confirm or override the suggested role.
4. Show you exactly the raw counts that will be sent to the server.
5. Ask you to confirm transmission.
6. The **server** computes your tier, score, and dimension breakdown from the raw counts.
7. Return your profile URL and the server-computed tier/score.

No tier, score, or dimensions are computed on your machine — the scoring formula lives on the server so it can evolve without requiring plugin updates.

### Sub-commands

- `/cyborgscore` — full flow (scan + score + confirm + submit)
- `/cyborgscore setup <TOKEN>` — save your API token (if you got it from settings rather than via device pairing)
- `/cyborgscore pair` — force re-pair (if your token was lost or revoked)

## What happens when you run /cyborgscore

1. **Check for token** — look in `~/.config/cyborgscore/config.json`. If missing,
   start the pairing flow (`python3 {{PLUGIN_ROOT}}/scripts/pair_device.py`).

2. **Scan transcripts locally** —
   `python3 {{PLUGIN_ROOT}}/scripts/scan_transcripts.py --days 60 > /tmp/cyborgscore_metrics.json`.
   NOTHING is transmitted yet.

3. **Infer role locally** —
   `python3 {{PLUGIN_ROOT}}/scripts/infer_role.py --from /tmp/cyborgscore_metrics.json > /tmp/cyborgscore_role.json`.
   This only classifies a role from the first user message of each
   session. It does NOT compute any tier or score.

4. **Show the raw data preview** — the counts that will be transmitted
   (sessions, messages, tokens, tool/skill/MCP/agent usage maps, etc.),
   plus the suggested role. Skill and MCP names are metadata — they may
   contain project or company names, so the user should review before
   confirming.

5. **Ask the user to confirm the role.** Show the suggestion ("engineer")
   and let them accept or pick a different one from:
   engineer / product / gtm / research / devops / ops / design / other.

6. **Ask the user: transmit to cyborgscore.com?**
   - If yes: `python3 {{PLUGIN_ROOT}}/scripts/submit_score.py --metrics /tmp/cyborgscore_metrics.json --role <confirmed-role>`
     The client strips `first_messages_sample` before POSTing; the server
     computes the tier/score/dimensions from the raw counts and returns
     them in the response.
   - If no: show the raw counts locally and exit.

7. **On success**, the response contains the server-computed tier,
   score, role, dimension values, activity breakdown, top strength, and
   the profile URL. The skill displays these plus the score delta
   ("+47 since last run") and a pre-composed tweet text.

## What gets transmitted

The client sends raw, aggregated counts from the scanner so the server
can compute the tier/score itself (this prevents clients from tampering
with their rank before transmit). Fields sent:

- Session / message / tool-call counts and per-day totals
- Token totals (input, output, cache read, cache creation)
- Max parallel agents in a single turn, parallel-dispatch turn count
- Max concurrent sessions (main Claude Code windows overlapping)
- Max messages in a single session, avg messages per session
- Distinct tool names + counts (e.g. "Bash: 25598")
- Skill names + counts (e.g. "compound-engineering:ce-plan: 13")
- MCP server names + counts (e.g. "conductor: 393")
- Agent type names + counts (e.g. "Explore: 558")
- Custom-skill-file-write and MCP-config-write counts
- Human-message count and correction-pattern match count
- Inferred role (engineer / product / gtm / research / devops / other)
- `computed_at` timestamp

### What is NEVER transmitted

- Conversation content
- Code
- File paths
- Prompts
- File contents
- Claude's outputs
- The `first_messages_sample` buffer (used only on-device for role inference)
- Project names (other than what appears in skill/MCP names)

## Privacy & trust

All computation happens on your machine. The server only ever receives the
metric summary above. See
[cyborgscore.com/privacy](https://cyborgscore.com/privacy) for details.

## Stale-score reminders (optional)

This plugin installs a `SessionStart` hook that reminds you to re-run
`/cyborgscore` if your score is over 30 days old. It does NOT scan or submit
automatically — it just prints a single line.

To turn it off, add `"HOOK_DISABLED": "true"` to
`~/.config/cyborgscore/config.json`.
