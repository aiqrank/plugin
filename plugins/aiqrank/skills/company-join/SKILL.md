---
name: company-join
description: Join an AIQ Rank company by redeeming a single-use invite token.
allowed-tools: [Bash]
---

# Join Company

Redeem a join token to add this device's user to a company. The plugin script prints the consent text and asks for confirmation before posting to the server.

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; python3 "$PLUGIN_ROOT/scripts/company_client.py" redeem "$ARGUMENTS"
```

Pass the token as the only argument: `/aiqrank:company-join <token>`.
