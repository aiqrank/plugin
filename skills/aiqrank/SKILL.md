---
name: aiqrank
description: Compute and share your AI proficiency rank from local Claude Code transcripts.
allowed-tools: [Bash]
---

# AIQ Rank

1. Scan transcripts locally: `python3 $CLAUDE_PLUGIN_ROOT/scripts/scan_transcripts.py --days 30 > /tmp/aiqrank_metrics.json`
2. Infer role locally: `python3 $CLAUDE_PLUGIN_ROOT/scripts/infer_role.py --from /tmp/aiqrank_metrics.json > /tmp/aiqrank_role.json`
3. Upload + open teaser: `python3 $CLAUDE_PLUGIN_ROOT/scripts/upload_metrics.py --metrics /tmp/aiqrank_metrics.json --role <role-from-step-2>`

The teaser page handles preview + sign-in on first run, then redirects to the owner dashboard at `/me`. Print nothing inside Claude Code except the one line from `upload_metrics.py`.
