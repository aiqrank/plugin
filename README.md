# MyAIScore — Claude Code plugin

Analyse your Claude Code transcripts to compute and share your AI
proficiency score.

## Install

```
/plugin marketplace add https://github.com/myaiscore/plugin
/plugin install myaiscore
```

Then run `/myaiscore` to get started.

## How it works

1. You run `/myaiscore`. The plugin opens a browser tab to `myaiscore.com/pair?session=...`.
2. You sign in with Google or an 8-digit email code.
3. The plugin scans your Claude Code transcripts from the last 60 days and
   computes your tier, role, and activity breakdown — all locally.
4. You see exactly what data will be transmitted, and confirm before
   anything leaves your machine.
5. Your profile appears at `myaiscore.com/@username`.

## Privacy

Scores are computed on your device. The server only ever receives the
metric summary (scores, percentages, skill/MCP names with usage counts) —
never conversation content, code, prompts, or file contents.

## License

MIT
