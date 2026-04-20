---
name: aiqrank
description: Analyse your Claude Code transcripts to compute and share your AI proficiency score. Score is calculated locally; only metrics are sent to aiqrank.com (never your conversations or code).
allowed-tools: [Bash, Read, Write, AskUserQuestion]
---

# AIQ Rank

Find out your AI tier. Score is based on how you actually use Claude Code:
tool diversity, agent orchestration, skills & MCPs, custom skill creation,
context leverage (memory, schedules, tasks), and conversation depth.

## First time using this?

1. Run `/aiqrank` — it will open `aiqrank.com/pair?session=...` in your browser
2. Sign in with Google or an email code
3. Your Claude Code plugin will automatically receive its API token

No passwords. Your transcripts stay on your machine.

## Usage

### Regular flow (paired already)

Run `/aiqrank`. The plugin will:

1. Scan your Claude Code transcripts from the last 30 days, bucketed by day.
2. Infer a role suggestion (engineer / product / marketing / sales / research / devops / founder / ...) **locally** from the first user message of each session.
3. Ask you to confirm or override the suggested role.
4. Show you exactly the raw counts that will be sent to the server.
5. Ask you to confirm transmission.
6. The **server** computes your tier, score, and dimension breakdown from the raw counts.
7. Return your profile URL and the server-computed tier/score.

No tier, score, or dimensions are computed on your machine — the scoring formula lives on the server so it can evolve without requiring plugin updates.

### Sub-commands

- `/aiqrank` — full flow (scan + score + confirm + submit)
- `/aiqrank setup <TOKEN>` — save your API token (if you got it from settings rather than via device pairing)
- `/aiqrank pair` — force re-pair (if your token was lost or revoked)

## What happens when you run /aiqrank

> **Running the helper scripts**: every `python3` command below uses the
> shell env var `$CLAUDE_PLUGIN_ROOT`, which Claude Code sets to this
> plugin's install directory for commands spawned from this skill. Run
> the commands *exactly as written* — do NOT substitute `$CLAUDE_PLUGIN_ROOT`
> yourself or derive a path from this SKILL.md's location; the shell will
> expand the variable at execution time. If the env var is unset, the
> plugin is at `~/.claude/plugins/cache/aiqrank/aiqrank/<version>/`
> — but this is only a fallback; the env var is the canonical path.

1. **Scan transcripts locally** —
   `python3 "$CLAUDE_PLUGIN_ROOT/scripts/scan_transcripts.py" --days 30 > /tmp/aiqrank_metrics.json`.
   NOTHING is transmitted yet. The scanner emits a per-day `daily` array
   plus a 30-day `rollup` summary; we do this first so an unauthenticated
   teaser page can show raw counts during the pairing flow.

2. **Check for token** — look in `~/.config/aiqrank/config.json`. If missing,
   start the pairing flow and pass the freshly-scanned metrics so the teaser
   page can display them:
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pair_device.py --metrics /tmp/aiqrank_metrics.json`.
   The plugin POSTs the raw counts to the server, which stashes them on the
   pair session and returns a teaser URL. The teaser shows the counts with a
   `?` tier — the user signs in to unlock the real tier, score, and profile.

3. **Infer role locally** —
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/infer_role.py --from /tmp/aiqrank_metrics.json > /tmp/aiqrank_role.json`.
   This only classifies a role from the first user message of each
   session. It does NOT compute any tier or score.

4. **Show the raw data preview** — the counts that will be transmitted
   (sessions, messages, tokens, tool/skill/MCP/agent usage maps, etc.),
   plus the suggested role. Skill and MCP names are metadata — they may
   contain project or company names, so the user should review before
   confirming.

5. **Ask the user to confirm the role.** Show the suggestion ("engineer")
   and let them accept or pick a different one from:
   engineer / product / marketing / sales / revops / research / devops / ops / design / founder / executive / other.

6. **Ask the user: transmit to aiqrank.com?**
   - If yes: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/submit_score.py --metrics /tmp/aiqrank_metrics.json --role <confirmed-role>`
     The client first fetches `GET /api/metrics/latest_date` so it only
     uploads days the server doesn't already have (subsequent runs are
     near-instant — just today's new data). `first_messages_sample` is
     stripped before POSTing; the server stores the daily records, rolls
     them up over 30 days, and returns the computed tier/score/dimensions.
   - If no: show the raw counts locally and exit.

7. **On success**, the response contains the profile URL. Open it in
   the browser: `open "<profile_url>"` (macOS) or `xdg-open "<profile_url>"` (Linux).
   Tell the user their profile is opening and show the URL. Do NOT display
   tier, score, or dimension tables — the profile page shows all of that.

## What gets transmitted

The client sends per-day records (one entry per calendar day that had
activity, only for days the server doesn't already have) so the server
can compute the tier/score itself and retain usage history for trending
graphs. Each day's entry contains:

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
- Inferred role (engineer / product / marketing / sales / research / devops / founder / other)
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
[aiqrank.com/privacy](https://aiqrank.com/privacy) for details.

## Stale-score reminders (optional)

This plugin installs a `SessionStart` hook that reminds you to re-run
`/aiqrank` if your score is over 30 days old. It does NOT scan or submit
automatically — it just prints a single line.

To turn it off, add `"HOOK_DISABLED": "true"` to
`~/.config/aiqrank/config.json`.
