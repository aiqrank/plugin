# AIQ Rank — Claude Code plugin

Analyse your Claude Code transcripts to compute and share your AI
proficiency score.

## Install

```
/plugin marketplace add https://github.com/aiqrank/plugin
/plugin install aiqrank
```

Then run `/aiqrank` to get started.

## How it works

1. You run `/aiqrank`. The plugin opens a browser tab to `aiqrank.com/pair?session=...`.
2. You sign in with Google or an 8-digit email code.
3. The plugin scans your Claude Code transcripts from the last 30 days,
   bucketing activity by calendar day.
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

MIT
