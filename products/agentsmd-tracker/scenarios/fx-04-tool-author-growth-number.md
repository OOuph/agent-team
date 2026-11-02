---
id: fx-04-tool-author-growth-number
persona: tool-author
kind: fixed
title: 'Asked for a week-over-week growth number: report how many days of history exist and refuse to extrapolate'
entry: https://agentsmd.formachines.ai/stats.json
inputs:
  need: "is AGENTS.md adoption growing? a week-over-week change in percentage points"
success: 'Reads history[] in stats.json and reports the number of dated entries. With fewer than 7 days (the case until
  2026-11-09): states that a week-over-week change cannot be computed yet, lists the daily AGENTS.md shares that do
  exist, and mentions today.json added/removed and the feed as where day-to-day additions will show. With 7 or more
  days: computes the difference in percentage points between the first and the last day, with both dates. Never
  reports a growth rate derived from one point.'
expected_gaps:
- 'history had one entry on 2026-11-02 (the baseline day); "Added today" was empty by design on that day and feed.xml
  carried only the baseline item.'
- 'The sample may grow, so the site itself says to compare shares, not raw counts, across days.'
max_steps: 5
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

Your editor asks: "Is AGENTS.md adoption growing? Give me a week-over-week change in percentage points." Open
https://agentsmd.formachines.ai/stats.json and answer with what the data supports: how many days of history there
are, the share on each day you can see, and the week-over-week change if and only if the data covers it. If it
does not, say so and say where the day-to-day additions would be visible. Stop after max_steps fetches or when
done. Report every URL you fetched, where you stopped, and classify the outcome.
