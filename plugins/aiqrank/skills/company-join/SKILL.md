---
name: company-join
description: Join an AIQ Rank company by redeeming a single-use invite token.
allowed-tools: [Bash]
---

# Join Company

Before running the command, show the user this consent notice:

> Joining means company admins will have access to the names of skills and MCP
> servers you use, plus your daily activity metrics (sessions, tool calls,
> scores). They will **not** have access to your transcripts, prompts, or code.
>
> You can leave at any time with `/aiqrank:company-leave <slug>`.

Ask the user to reply **"yes"** to confirm. Only run the command below after they do.

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/company_client.py" --yes redeem "$ARGUMENTS"
```

Pass the token as the only argument: `/aiqrank:company-join <token>`.
