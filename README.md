# AIQ Rank — Claude Code plugin

Analyse your Claude Code transcripts to compute and share your AI
proficiency score.

Profiles, leaderboards, and groups live at <https://www.aiqrank.com>.

## Install

```
/plugin marketplace add aiqrank/plugin
/plugin install aiqrank
```

Then run `/aiqrank` to get started.

### Cowork users

AIQ Rank can't run from inside Cowork — the chat sandbox (and
scheduled tasks) can't reach your transcript files on the host.
Install Claude Code (the CLI in Terminal, or the **Code** tab in
Claude Desktop) and run `/aiqrank` there. It scans both your
Claude Code _and_ Cowork activity in a single pass.

Full instructions: <https://www.aiqrank.com/setup>

## What the plugin says at session start

The plugin adds a SessionStart hook that can print up to three short notices.
It never prints anything else, and it stays silent when none apply:

- **Stale rank** — your last upload was more than 30 days ago. Run `/aiqrank`
  to refresh.
- **Plugin update** — the server reports a newer plugin version than the one
  you have installed.
- **Terminal install** — at most once every 30 days, and only when the
  `claude` CLI is on your machine without AIQ Rank installed in it. It names
  the two commands that would add it. It is a notice, not an instruction: an
  agent reading it should pass it along rather than run it.

The third notice stops for good once AIQ Rank is installed in the CLI, and
never appears in Codex.

## Supported tools

The plugin scans and scores activity from all of these tools in a single run:

- Claude Code (interactive sessions)
- Claude Cowork (autonomous local-agent-mode sessions)
- OpenAI Codex (CLI rollouts under `~/.codex/`)
- Cursor (local SQLite store at `~/Library/Application Support/Cursor/...`)
- OpenCode (local SQLite store at `~/.local/share/opencode/opencode.db`)
- Pi (local JSONL sessions under `~/.pi/agent/sessions` or Pi's configured session root)
- Hermes (local session database at `~/.hermes/state.db`)
- OpenClaw (per-agent local SQLite stores under `~/.openclaw/agents`)
- NanoClaw (current v2 stores in detected checkouts, or paths from
  `AIQRANK_NANOCLAW_ROOTS`)

For Hermes, OpenClaw, and NanoClaw, interactive conversations count as
sessions, cron/heartbeat/system work counts as scheduled activity, and spawned
children count as orchestration. Automated work therefore cannot inflate the
ordinary session count.

Each tool gets its own per-tool leaderboard chip and profile tab. The All-tools
aggregate combines the seven cross-tool dimensions into a single headline score.

## How it works

1. You run `/aiqrank`. The plugin scans supported local coding-agent history from
   the last 30 days and prints a link to an AIQRank teaser page.
2. Open the printed link and sign in with Google or an 8-digit email code.
3. The plugin buckets activity by calendar day.
4. You see exactly what data will be transmitted, and confirm before
   anything leaves your machine.
5. The server stores per-day records and computes your tier/score from
   the 30-day rollup. Subsequent runs only upload days the server
   doesn't yet have.
6. Your profile appears at `aiqrank.com/@username` — with a 30-day
   activity sparkline.

## Privacy

Scores are computed on the server from per-day metric counts the plugin
sends. The server only ever receives the per-day metric summary (tool /
skill / MCP / agent names with counts) — never conversation content,
code, prompts, or file contents.

## License

[Elastic License 2.0 (ELv2)](LICENSE) — source available, but you may not offer
this software as a hosted or managed service.

This project has been licensed under Elastic License 2.0 (ELv2) since its first
formal license. Prior commits listed "MIT" informally but no MIT LICENSE file was
ever published.
