---
name: aiqrank
description: Compute and share your AI proficiency rank from supported local coding-agent history, including Pi when present.
allowed-tools: [Bash]
---

# AIQ Rank

AIQ Rank takes privacy seriously. Supported coding-agent sessions, including Pi when present, are scanned locally — only aggregate metrics (session counts, token totals, and tool names used) are sent to aiqrank as part of your profile. Conversation text never leaves your device.

1. Scan locally and upload aggregate counts (reads supported coding-agent sessions on this machine, including Pi when present, and sends integer counts plus normalized tool/skill/MCP names — never message content): `python3 ~/.aiqrank/scripts/upload_metrics.py --scan --role engineer`
2. Follow the link printed by that command to see your rank at aiqrank.com/me.

The teaser page handles preview + sign-in on first run, then redirects to your owner dashboard at `/me`. Print nothing except the one line from `upload_metrics.py`.
