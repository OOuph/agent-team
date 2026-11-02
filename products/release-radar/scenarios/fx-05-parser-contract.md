---
id: fx-05-parser-contract
persona: parser
kind: fixed
title: 'Public data contract: llms.txt, build.json, today.json counts, one per-package JSON, feed.xml, 404 for an unknown package'
entry: https://releases.formachines.ai/llms.txt
inputs:
  client: an aggregator refreshing its cache once a day
success: 'All checks pass: llms.txt is 200 text/plain and names today.json, feed.xml, build.json and the per-package JSON
  pattern; build.json is 200 JSON with packages, releases, flagged and errors == []; today.json is 200 JSON whose
  counts.releases equals build.json releases and equals the number of lines matching "id": (via --grep); the first
  per-package json URL listed in today.json resolves 200 with keys package, ecosystem, latest, releases, errata;
  feed.xml is 200 XML containing <rss and at least one <item>; /pkg/pypi/nonexistent/ returns 404.'
expected_gaps:
- 'today.json is about 94 KB, above the tool''s 30 000-character cut: the item count is only reachable with
  --grep ''"id":'' (one field per line). counts.* sits at the top of the file and is visible in the cut body.'
- 'build.json has github_remaining: null when the build used a cached GitHub state; not a failure.'
max_steps: 8
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

Run these checks in order and report each as pass or fail with URL, status and the field in question:

1. https://releases.formachines.ai/llms.txt — 200, text/plain, names today.json, feed.xml, build.json and the
   per-package JSON pattern /pkg/<ecosystem>/<name>/index.json.
2. https://releases.formachines.ai/build.json — 200, JSON, has packages, releases, flagged; errors is an empty list.
3. https://releases.formachines.ai/today.json — 200, JSON; read counts.releases from the top of the body; then
   count the items with `--grep '"id":'` and compare both numbers with build.json releases.
4. The first `json` URL you see in today.json — 200, JSON with package, ecosystem, latest, releases, errata.
5. https://releases.formachines.ai/feed.xml — 200, XML, contains <rss and at least one <item>.
6. https://releases.formachines.ai/pkg/pypi/nonexistent/ — 404.

Stop after max_steps fetches or when done. Report every URL you fetched, where you stopped, and classify the
outcome.
