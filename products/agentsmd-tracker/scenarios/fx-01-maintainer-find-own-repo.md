---
id: fx-01-maintainer-find-own-repo
persona: maintainer
kind: fixed
title: 'From the home page to the card of pallets/flask (all eight files absent) in at most 5 steps'
entry: https://agentsmd.formachines.ai/
inputs:
  repository: pallets/flask (Python)
  reason: a post said "top Python repos are adding AGENTS.md — is flask listed?"
success: 'Reaches https://agentsmd.formachines.ai/repo/pallets/flask/ within 5 fetches, reports that all eight paths are
  "no" with the scan status and the last-checked timestamp, names the Python row of the home table (AGENTS.md count
  and share as shown), and says in one sentence that only presence, size and a link are published. The route may be
  the address pattern named on the 404 page or in llms.txt, or `--grep flask` on sitemap.xml.'
expected_gaps:
- 'The home page has no search box, no repository list and no links from the language table to repositories; the
  card address must be typed from the pattern /repo/<owner>/<name>/ or taken from sitemap.xml (154 KB, cut).'
- 'Addresses are case-sensitive: /repo/Pallets/Flask/ is a 404. The 404 page names the pattern and the sample size.'
max_steps: 5
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

You maintain pallets/flask. A post claims that top Python repositories are adding AGENTS.md and links
https://agentsmd.formachines.ai/. Starting there, find out whether flask is in the sample and what the tracker
records for it: which of the tracked files exist, their size and link, and when it was checked. Also note what the
home page says about Python repositories as a whole, and whether any file contents are shown. End with one
sentence you would tell your co-maintainers. Stop after max_steps fetches or when done. Report every URL you
fetched, where you stopped, and classify the outcome.
