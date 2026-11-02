---
id: fx-05-parser-contract
persona: parser
kind: fixed
title: 'Public data contract: llms.txt, stats.json sums, today.json date, feed.xml, build.json, one repo JSON, 404 for an unknown repo'
entry: https://agentsmd.formachines.ai/llms.txt
inputs:
  client: a dashboard refreshing its cache once a day
success: 'All checks pass: llms.txt is 200 text/plain and names stats.json, today.json, feed.xml, build.json and the
  per-repository JSON pattern; stats.json is 200 JSON with generated, sample, scanned, counts, shares, by_language,
  history and data_delayed == false, the sum of by_language[*]["AGENTS.md"] equals counts["AGENTS.md"] and the sum
  of by_language[*].repos equals scanned; today.json is 200 JSON whose date equals the day of its generated
  timestamp and has added/removed arrays; feed.xml is 200 XML with <rss and at least one <item>; build.json is 200
  JSON; /repo/tokio-rs/tokio/index.json is 200 JSON with repo, language, checked and files containing all eight
  paths each with present/size/url; /repo/nonexistent/repo/ returns 404.'
expected_gaps:
- 'sitemap.xml is about 154 KB and is cut by the tool; it is not needed for these checks.'
- 'On the baseline day history has one entry and added/removed are empty; that is consistent, not a failure.'
max_steps: 8
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

Run these checks in order and report each as pass or fail with URL, status and the field in question:

1. https://agentsmd.formachines.ai/llms.txt — 200, text/plain, names stats.json, today.json, feed.xml, build.json
   and the pattern /repo/<owner>/<name>/index.json.
2. https://agentsmd.formachines.ai/stats.json — 200, JSON; data_delayed is false; the AGENTS.md counts of all
   languages in by_language add up to counts["AGENTS.md"]; the repos of all languages add up to scanned; history
   has at least one entry with a date.
3. https://agentsmd.formachines.ai/today.json — 200, JSON; date equals the day of generated; added and removed are
   arrays.
4. https://agentsmd.formachines.ai/feed.xml — 200, XML, contains <rss and at least one <item>.
5. https://agentsmd.formachines.ai/build.json — 200, JSON.
6. https://agentsmd.formachines.ai/repo/tokio-rs/tokio/index.json — 200, JSON with repo, language, checked and
   files; files has all eight tracked paths, each with present, size and url.
7. https://agentsmd.formachines.ai/repo/nonexistent/repo/ — 404.

Stop after max_steps fetches or when done. Report every URL you fetched, where you stopped, and classify the
outcome.
