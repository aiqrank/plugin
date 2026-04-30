---
name: aiqrank
description: Compute and share your AI proficiency rank from local Codex history.
allowed-tools: [Bash]
---

# AIQ Rank

AIQ Rank takes privacy seriously. Sessions are scanned locally — only aggregate metrics (session counts, token totals, and tool names used) are sent to aiqrank as part of your profile. Conversation text never leaves your device.

1. Extract usage metrics locally (reads Codex sessions on this machine, produces integer counts only — no message content): `python3 ~/.aiqrank/scripts/scan_codex.py --days 30 > /tmp/aiqrank_codex_metrics.json`
2. Upload aggregate counts + open teaser (sends session/token counts and tool names used — no conversation text): `python3 ~/.aiqrank/scripts/upload_metrics.py --metrics /tmp/aiqrank_codex_metrics.json --role engineer`
3. Follow the link printed by step 2 to see your rank at aiqrank.com/me.

The teaser page handles preview + sign-in on first run, then redirects to your owner dashboard at `/me`. Print nothing except the one line from `upload_metrics.py`.
