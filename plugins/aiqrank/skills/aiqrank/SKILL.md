---
name: aiqrank
description: Compute and share your AI proficiency rank from local Claude Code transcripts.
allowed-tools: [Bash]
---

# AIQ Rank

Each command below resolves the plugin root robustly: it prefers `$CLAUDE_PLUGIN_ROOT`, then falls back to the highest installed semver under `~/.claude/plugins/cache/aiqrank/aiqrank/*/`, and aborts with a clear message if neither is found.

1. Scan transcripts locally:
   ```bash
   PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; python3 "$PLUGIN_ROOT/scripts/scan_transcripts.py" --days 30 > /tmp/aiqrank_metrics.json
   ```
2. Infer role locally:
   ```bash
   PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; python3 "$PLUGIN_ROOT/scripts/infer_role.py" --from /tmp/aiqrank_metrics.json > /tmp/aiqrank_role.json
   ```
3. Upload + open teaser:
   ```bash
   PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')}"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — set \$CLAUDE_PLUGIN_ROOT or install the plugin via /plugin install aiqrank"; exit 1; }; python3 "$PLUGIN_ROOT/scripts/upload_metrics.py" --metrics /tmp/aiqrank_metrics.json --role <role-from-step-2>
   ```

The teaser page handles preview + sign-in on first run, then redirects to the owner dashboard at `/me`. Print nothing inside Claude Code except the one line from `upload_metrics.py`.
