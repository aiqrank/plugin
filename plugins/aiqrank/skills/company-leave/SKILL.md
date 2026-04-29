---
name: company-leave
description: Leave an AIQ Rank company you have joined.
allowed-tools: [Bash]
---

# Leave Company

Self-leave a company. The script confirms before deleting your membership.

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; python3 "$PLUGIN_ROOT/scripts/company_client.py" leave "$ARGUMENTS"
```

Pass the company slug: `/aiqrank:company-leave <slug>`.
