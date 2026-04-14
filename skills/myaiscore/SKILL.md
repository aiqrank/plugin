---
name: myaiscore
description: Analyse your Claude Code transcripts to compute and share your AI proficiency score. Score is calculated locally; only metrics are sent to myaiscore.com (never your conversations or code).
allowed-tools: [Bash, Read, Write, AskUserQuestion]
---

# MyAIScore

Find out your AI tier. Score is based on how you actually use Claude Code:
tool diversity, agent orchestration, skills & MCPs, custom skill creation,
context leverage (memory, schedules, tasks), and conversation depth.

## First time using this?

1. Run `/myaiscore` — it will open `myaiscore.com/pair?session=...` in your browser
2. Sign in with Google or an email code
3. Your Claude Code plugin will automatically receive its API token

No passwords. Your transcripts stay on your machine.

## Usage

### Regular flow (paired already)

Run `/myaiscore`. The plugin will:

1. Scan your Claude Code transcripts from the last 60 days.
2. Compute your score, tier, role, and dimension breakdown **locally**.
3. Show you exactly what data will be sent to the server.
4. Ask you to confirm before transmitting.
5. Return your profile URL.

### Sub-commands

- `/myaiscore` — full flow (scan + score + confirm + submit)
- `/myaiscore setup <TOKEN>` — save your API token (if you got it from settings rather than via device pairing)
- `/myaiscore pair` — force re-pair (if your token was lost or revoked)

## What happens when you run /myaiscore

1. **Check for token** — look in `~/.config/myaiscore/config.json`. If missing,
   start the pairing flow (`python3 {{PLUGIN_ROOT}}/scripts/pair_device.py`).

2. **Scan transcripts locally** —
   `python3 {{PLUGIN_ROOT}}/scripts/scan_transcripts.py --days 60`.
   Writes metrics to `/tmp/myaiscore_metrics.json`. NOTHING is transmitted.

3. **Compute score locally** —
   `python3 {{PLUGIN_ROOT}}/scripts/scoring.py --from /tmp/myaiscore_metrics.json > /tmp/myaiscore_score.json`.
   Still local.

4. **Show the user a preview** — tier, role, score, top strength, activity
   breakdown. Also show the list of skill and MCP names that will be
   transmitted. These are metadata, not conversations, but they may contain
   project or company names — the user should review before confirming.

5. **Ask the user: transmit to myaiscore.com?**
   - If yes: `python3 {{PLUGIN_ROOT}}/scripts/submit_score.py --from /tmp/myaiscore_score.json`
   - If no: show the score locally and exit.

6. **On success**, show the returned profile URL, the score delta ("+47
   since last run"), and a pre-composed tweet text the user can paste
   anywhere.

## What gets transmitted

When you confirm submission, your device sends these fields:

- Overall score (integer 0-1000)
- Tier letter (S/A/B/C/D/F)
- Inferred role (engineer/gtm/product/research/devops)
- Activity percentages per category
- Dimension values (0-100 per dimension)
- Top strength label
- `computed_at` timestamp
- List of skill & MCP server names with usage counts

### What is NEVER transmitted

- Conversation content
- Code
- File paths
- Prompts
- File contents
- Claude's outputs
- Project names (other than what appears in skill/MCP names)

## Privacy & trust

All computation happens on your machine. The server only ever receives the
metric summary above. See
[myaiscore.com/privacy](https://myaiscore.com/privacy) for details.

## Stale-score reminders (optional)

This plugin installs a `SessionStart` hook that reminds you to re-run
`/myaiscore` if your score is over 30 days old. It does NOT scan or submit
automatically — it just prints a single line.

To turn it off, add `"HOOK_DISABLED": "true"` to
`~/.config/myaiscore/config.json`.
