---
id: ex-01-expat-ev-cheap-middays-week
persona: expat-ev
kind: explore
title: 'Solar-and-battery owner: how often were midday prices near zero or negative this week, and what would a week view have saved'
entry: https://luz.bymachines.ai/d/
inputs:
  question: "I have PV and a battery; on which days this week did the price drop to about zero or below at midday, and for how many hours?"
success: 'Opens up to six day pages from the archive and reports for each: date, the cheap window with its average, and
  the hours whose price is at or below 0 €/MWh (count and which hours), reading them from the hourly table or the
  day JSON; then says in one or two sentences what a week table (days × hours) would have saved and whether the
  JSON is enough for a script. Partial coverage reported as partial is a success.'
expected_gaps:
- 'No week or month view; one page per day. The per-day JSON (/d/<date>/index.json, linked from the archive) is the
  only way to get exact hourly values without reading the table.'
- 'Negative prices appear in the archive (e.g. 2026-11-01 midday); the table prints «-0.000» for the €/kWh column on
  those hours.'
max_steps: 10
budget_usd: 0.5
version: 1
created: '2026-11-02'
baseline: null
---

You have solar panels and a home battery and you have read that Portuguese wholesale prices sometimes drop to
zero or below at midday. Start at https://luz.bymachines.ai/d/ and, for up to six recent days, find on which days
and in which hours the price was at or below 0 €/MWh, and what the cheap 3-hour window was each day. Then say
what would have made this easier and whether the site's JSON would be enough for a script to do it for you. Stop
after max_steps fetches or when done. Report every URL you fetched, where you stopped, and classify the outcome.
