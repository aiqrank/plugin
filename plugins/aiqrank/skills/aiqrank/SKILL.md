---
name: aiqrank
description: Compute and share your AI proficiency rank from supported local coding-agent history, including Pi when present.
allowed-tools: [Bash]
---

# AIQ Rank

AIQ Rank takes privacy seriously. Supported coding-agent sessions, including Pi when present, are scanned locally — only aggregate metrics (session counts, token totals, and tool names used) are sent to aiqrank as part of your profile. Conversation text never leaves your device.

Before scanning, if `~/.aiqrank/scripts/check_update.py` exists, run it with Python. If it prints an update notice, show that notice to the user and stop; do not update automatically.

Each command below resolves the plugin root robustly: it prefers `$CLAUDE_PLUGIN_ROOT`, then `$CODEX_PLUGIN_ROOT` when available, then the highest installed Codex cache version, then the Claude cache version. It aborts with a clear message if none are available.

1. Extract usage metrics locally:
   ```bash
   PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.codex/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — install the AIQ Rank plugin"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/scan_transcripts.py" --days 30 > /tmp/aiqrank_metrics.json
   ```
2. Infer role locally:
   ```bash
   PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.codex/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — install the AIQ Rank plugin"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/infer_role.py" --from /tmp/aiqrank_metrics.json > /tmp/aiqrank_role.json
   ```
3. Submit and open teaser:
   ```bash
   PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.codex/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — install the AIQ Rank plugin"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/upload_metrics.py" --metrics /tmp/aiqrank_metrics.json --role <role-from-step-2>
   ```

The teaser page handles preview + sign-in on first run, then redirects to the owner dashboard at `/me`. Print nothing inside Claude Code except the one line from `upload_metrics.py`.

## Updating in Codex

If the user asks to update AIQ Rank in Codex or reports an update notice, have them run this in a terminal:

```bash
curl -sSL https://aiqrank.com/setup/codex | bash
```

That refreshes both the Codex marketplace plugin and the managed local scanner artifacts. They should start a new Codex session afterward and run `$aiqrank:aiqrank`. Do not run this update automatically.

To disable background uploads: `touch ~/.config/aiqrank/disabled`
