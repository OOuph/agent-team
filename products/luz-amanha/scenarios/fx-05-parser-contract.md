---
id: fx-05-parser-contract
persona: parser
kind: fixed
title: 'Public data contract: llms.txt, tomorrow.json fields and window consistency, day archive JSON, today.json, feed.xml, build.json, 404 for an unknown date'
entry: https://luz.bymachines.ai/llms.txt
inputs:
  client: a home-automation script pulling tomorrow's hours after 14:30 UTC
success: 'All checks pass: llms.txt is 200 text/plain and names tomorrow.json, today.json, /d/YYYY-MM-DD/index.json,
  feed.xml and build.json; tomorrow.json is 200 JSON with date, timezone == Europe/Lisbon, unit == EUR/MWh, hours[]
  each with hour, start_utc and price_pt_eur_mwh, stats.available, stats.cheap_window{start,end,avg} and
  stats.dear_window, and the top-level cheap_window.hours string ("HH:00–HH:00") matches stats.cheap_window start/end
  (or the file says available: false and the check is reported as the documented no-data case); /d/<that
  date>/index.json is 200 with the same date; today.json is 200 and its date is the day of built_utc; feed.xml is
  200 XML with <rss and a first <item> whose <link> is a /d/<date>/ page; build.json is 200 with target_date equal
  to tomorrow.json date; /d/2099-01-01/ returns 404.'
expected_gaps:
- 'tomorrow.json has 23 priced hours, not 24: the 23:00 Lisbon hour needs the next OMIE file (llms.txt documents it;
  stats.available says 23). Known, not a failure.'
- 'estimate.enabled is false until the owner sets tariff components; the field is present and boolean.'
- 'Before the 13:15/13:45 UTC build the file may carry available: false per VISION; not observed on 2026-11-02.'
max_steps: 8
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

Run these checks in order and report each as pass or fail with URL, status and the field in question:

1. https://luz.bymachines.ai/llms.txt — 200, text/plain, names tomorrow.json, today.json, the per-day
   /d/YYYY-MM-DD/index.json pattern, feed.xml and build.json.
2. https://luz.bymachines.ai/tomorrow.json — 200, JSON; date, timezone == "Europe/Lisbon", unit == "EUR/MWh";
   every entry of hours[] has hour, start_utc, price_pt_eur_mwh; stats has available, cheap_window and
   dear_window; the top-level cheap_window.hours string agrees with stats.cheap_window start and end. Note how
   many hours carry a price and what the file says about it.
3. https://luz.bymachines.ai/d/<date from step 2>/index.json — 200, JSON, same date.
4. https://luz.bymachines.ai/today.json — 200, JSON; its date is the calendar day of its built_utc.
5. https://luz.bymachines.ai/feed.xml — 200, XML, contains <rss; the first <item> <link> is a /d/<date>/ page.
6. https://luz.bymachines.ai/build.json — 200, JSON; target_date equals the date from step 2; target_available
   is a boolean.
7. https://luz.bymachines.ai/d/2099-01-01/ — 404.

Stop after max_steps fetches or when done. Report every URL you fetched, where you stopped, and classify the
outcome.
