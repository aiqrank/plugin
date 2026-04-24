---
name: aiqrank
description: Compute and share your AI proficiency rank from local Codex history.
allowed-tools: [Bash]
---

# AIQ Rank

1. Scan Codex history locally: `python3 ~/.aiqrank/scripts/scan_codex.py --days 30 > /tmp/aiqrank_codex_metrics.json`
2. Upload and open teaser: `python3 ~/.aiqrank/scripts/upload_metrics.py --metrics /tmp/aiqrank_codex_metrics.json --role engineer`
3. Follow the link printed by step 2 to see your rank at aiqrank.com/me.

The teaser page handles preview + sign-in on first run, then redirects to your owner dashboard at `/me`. Print nothing except the one line from `upload_metrics.py`.
