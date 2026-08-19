---
name: aiqrank
description: Compute and share your AI proficiency rank from supported local coding-agent and agent-runtime history.
allowed-tools: [Bash]
---

# AIQ Rank

AIQ Rank takes privacy seriously. Supported coding-agent sessions, including Pi, Hermes, OpenClaw, and NanoClaw when present, are scanned locally — only aggregate metrics (session counts, token totals, and tool names used) are sent to aiqrank as part of your profile. Conversation text never leaves your device.

Run step 0 first. It applies any pending update and records the resolved plugin root at `~/.config/aiqrank/plugin_root`, which the later steps read — so the run uses the freshly updated scripts. If step 0 prints an update line, show that one line to the user.

0. Apply pending updates and resolve the plugin root:
   ```bash
   PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.codex/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$(ls -d ~/.claude/plugins/cache/aiqrank/aiqrank/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin scripts not found — install the AIQ Rank plugin"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/self_update.py"
   ```
1. Extract usage metrics locally:
   ```bash
   PLUGIN_ROOT="$(cat "$HOME/.config/aiqrank/plugin_root" 2>/dev/null)"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin root not resolved — run step 0 first"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/scan_transcripts.py" --days 30 > /tmp/aiqrank_metrics.json
   ```
2. Infer role locally:
   ```bash
   PLUGIN_ROOT="$(cat "$HOME/.config/aiqrank/plugin_root" 2>/dev/null)"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin root not resolved — run step 0 first"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/infer_role.py" --from /tmp/aiqrank_metrics.json > /tmp/aiqrank_role.json
   ```
3. Submit and open teaser:
   ```bash
   PLUGIN_ROOT="$(cat "$HOME/.config/aiqrank/plugin_root" 2>/dev/null)"; [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/scripts" ] || { echo "aiqrank plugin root not resolved — run step 0 first"; exit 1; }; PY="$(command -v python3 || command -v python)"; [ -n "$PY" ] || { echo "python not found — install Python 3"; exit 1; }; "$PY" "$PLUGIN_ROOT/scripts/upload_metrics.py" --metrics /tmp/aiqrank_metrics.json --role <role-from-step-2>
   ```

The teaser page handles preview + sign-in on first run, then redirects to the owner dashboard at `/me`. Print nothing inside Claude Code except the one line from `upload_metrics.py`, plus the update line from step 0 when there is one.

## Updating

Step 0 updates the plugin automatically, so there is normally nothing to do. The refreshed scanner scripts are used in the same session; the skill text and hooks switch over at the start of the next session.

If step 0 reports that the automatic update did not complete, pass along the manual command it printed:

- Claude Code — `claude plugin marketplace update aiqrank && claude plugin update aiqrank@aiqrank`
- Codex — `codex plugin marketplace upgrade aiqrank && codex plugin add aiqrank@aiqrank`, then start a new Codex session and run `$aiqrank:aiqrank`

To disable background uploads: `touch ~/.config/aiqrank/disabled`
