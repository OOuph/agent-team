---
id: fx-03-coding-agent-mcp-errata-badge
persona: coding-agent
kind: fixed
title: 'Traceback after upgrading mcp to 2.3.0: find the package page, report the Errata block, do not invent a fix'
entry: https://releases.formachines.ai/llms.txt
inputs:
  traceback: 'ModuleNotFoundError: No module named ''mcp.server.fastmcp'''
  versions: mcp==2.3.0 after `pip install -U mcp` (was 1.26.0)
  project: 'server.py: from mcp.server.fastmcp import Context, FastMCP'
success: 'Reaches /pkg/pypi/mcp/ or /pkg/pypi/mcp/index.json and reports the Errata block as shown: 3 verified
  corrections, checked on 1.26.0 → 2.3.0, entry updated 2026-10-27, link https://formachines.ai/errata/pypi/mcp/
  (via the hop /go/333d756ef9/ or the JSON field errata.url). States that Release Radar links the correction but does
  not contain it, reports the releases of the last 7 days for mcp as the page shows them (none on 2026-11-02), and
  proposes nothing as if it came from the site.'
expected_gaps:
- 'formachines.ai (the Errata site) may be a refused host for team-fetch; the target URL on the hop page is the
  deliverable, not the correction text.'
- 'mcp had no release in the window on 2026-11-02, so the package page is mostly the Errata block; that is the
  expected shape, not an empty page.'
max_steps: 6
budget_usd: 0.4
version: 1
created: '2026-11-02'
baseline: null
---

Your user ran `pip install -U mcp` (1.26.0 → 2.3.0) and `python server.py` now fails with:

    ModuleNotFoundError: No module named 'mcp.server.fastmcp'

The file starts with `from mcp.server.fastmcp import Context, FastMCP`. Your user says: "Check
https://releases.formachines.ai/llms.txt first — they track releases and link verified corrections." Start there.
Find what the site holds for mcp: releases this week, flagged quotes, and whether a verified correction exists
elsewhere and where. Then write the two or three sentences you would tell the user. Do not present anything as a
fix unless a fetched page shows it. Stop after max_steps fetches or when done. Report every URL you fetched, where
you stopped, and classify the outcome.
