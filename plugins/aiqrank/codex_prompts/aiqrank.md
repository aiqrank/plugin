---
name: aiqrank
description: Compute and share your AI proficiency rank from supported local coding-agent history, including Pi when present.
allowed-tools: [Bash]
---

# AIQ Rank

AIQ Rank takes privacy seriously. Supported coding-agent sessions, including Pi when present, are scanned locally — only aggregate metrics (session counts, token totals, and tool names used) are sent to aiqrank as part of your profile. Conversation text never leaves your device.

Before scanning, if `~/.aiqrank/scripts/self_update.py` exists, run it with Python. It applies any pending update in place, so the scan below uses the refreshed scripts. If it prints an update line, show that one line to the user. If it reports that the update did not complete, show that line and continue.

1. Scan locally and upload aggregate counts (reads supported coding-agent sessions on this machine, including Pi when present, and sends integer counts plus normalized tool/skill/MCP names — never message content): `python3 ~/.aiqrank/scripts/upload_metrics.py --scan --role engineer`
2. Follow the link printed by that command to see your rank at aiqrank.com/me.

The teaser page handles preview + sign-in on first run, then redirects to your owner dashboard at `/me`. Print nothing except the one line from `upload_metrics.py`.

## Updating

Updates apply automatically before each scan, refreshing the managed scanner and prompt artifacts from the published release.

That covers the managed artifacts only. If the user asks to update the Codex marketplace plugin as well, or the automatic update reports that it did not complete, tell them to run this in a terminal:

```bash
codex plugin marketplace upgrade aiqrank && codex plugin add aiqrank@aiqrank
```

They should then start a new Codex session and run `$aiqrank:aiqrank`.
