---
name: company
description: Join, leave, or list aiqrank companies. Use when the user types /aiqrank:company list, /aiqrank:company join <token>, or /aiqrank:company leave <slug>.
allowed-tools: [Bash]
---

# AIQ Rank Companies

Run the company-membership CLI directly. Pass through whatever subcommand and arguments the user typed (`list`, `join <token>`, or `leave <slug>`). The script reads the device pairing from `~/.config/aiqrank/device.json`, fetches consent text from the server before redeeming a join token, and writes one short status line on success or stderr on failure.

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; python3 "$PLUGIN_ROOT/scripts/company.py" "$@"
```
