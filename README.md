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

## Planning credit and local explanations

Claude Code earns Planning credit for a recognized planning signal followed
by a successful main-session Write/Edit on the same local date, or a successful
main-session Write/Edit to a recognized plan artifact. Codex also supports
apply-patch. Shell writes (including heredocs and sed) are not mutation evidence;
Claude subagent-only writes do not credit the parent. Each qualifying session
earns at most one credit per local date, regardless of file or edit count.

A plan artifact is a Markdown file under any `plans/` directory **or** a file
named `PLAN.md` **or** ending in `-plan.md`. Planning measurement version 3
matches the directory, basename, and `.md` extension without case sensitivity.
For example, `Plans/DESIGN.MD` and `plan.md` qualify; `DESIGN.md` outside such a
directory does not qualify by naming alone. Earlier measurement versions use
case-sensitive matching. We do not read plan content or judge its quality.

Run the scanner locally with explanations (this does not upload anything):

```sh
python3 plugin/plugins/aiqrank/scripts/scan_transcripts.py --days 30 --explain-planning
```

The command above is relative to this repository; in an installed plugin, use
its `scripts/scan_transcripts.py`. The standalone `scripts/scan_codex.py` accepts
the same flag. Normal metrics JSON stays on stdout; an additional JSON report
goes to stderr. Explanations cover Claude Code, Cowork's Claude-format parser,
and Codex, and include only source, date, and counts for dates retained by the
scanner. They never enter upload metrics. No artifact path, command, transcript
excerpt, or session identifier appears in the explanation.

Counts distinguish credited/uncredited main-session days and child-session
days. Reasons count session-days with observed shell calls, excluded child
mutations, missing/failed completion, completion on another date, missing
mutation targets, or unrecognized paths without a prior planning signal.
Each reason counts once per session-date; reasons can overlap and can coexist
with eventual credit. These are observed detection limits, **not counts of
missed valid plans**. Dates omitted for incomplete Codex evidence are omitted
from explanations too; the normal metrics output carries completeness status.

After upgrading, a full retained-window rescan can recover eligible historical
activity still on disk. Current-day hook uploads alone do not repair older days.

## Instruction-edit credit

Plugin 0.3.29 corrects instruction-edit counters for Claude Code, Cowork, and
Codex. `CLAUDE.md` and `AGENTS.md` match as exact basenames, including relative
paths such as `CLAUDE.md` and `./AGENTS.md`, absolute paths, and Windows paths.
Each Write/Edit needs a paired successful result on the invocation's local
date. Codex also supports apply-patch, including supported nested calls with
successful enclosing completion. Failed, unpaired, and cross-date completions
earn no instruction-edit credit. Successful repeated edits and Claude subagent
edits still count; there is no Planning-style main-session restriction here.

Shell writes still do not increment these counters. Claude/Cowork can separately
count recognized instruction files on disk as configuration surfaces regardless
of the tool used to create them. File discovery, MCP-edit counters, and the
Customization formula are unchanged. Codex's `agents_md_writes` remains the
AGENTS.md subset of `claude_md_writes`.

Daily rows carry `instruction_writes_measurement_version: 1`, independently of
the configuration-surface marker. For the same source and date, the server uses
instruction counts only from rows with the highest valid instruction marker;
a corrected zero beats older attempt counts. Other dates, sources, and metrics
are unaffected. NanoClaw's shared Claude provider parser inherits this correction
and retains the marker when provider rows are merged.

The server must support the new marker before upgraded uploads arrive. Publish
the plugin only after that support is deployed, coordinating the advertised
version with the public release. A full retained-window rescan can recover
eligible historical relative-path edits and remove false attempt credit while
transcripts remain available. Older clients cannot reproduce these semantics.

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
