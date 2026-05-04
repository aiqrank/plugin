---
name: daily-update
description: Run the AIQ Rank scan and upload once without opening a browser. Intended for Claude Cowork scheduled tasks.
allowed-tools: [Bash]
---

# AIQ Rank Daily Update

Run the non-interactive updater:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/daily_update.py"
```

This command scans local AI tool activity, infers role locally, uploads the metric summary, and does not open a browser.
