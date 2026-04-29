---
name: company-list
description: List the AIQ Rank companies you have joined.
allowed-tools: [Bash]
---

# List Companies

Prints the companies you belong to (slug, role, name).

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; python3 "$PLUGIN_ROOT/scripts/company_client.py" list
```
