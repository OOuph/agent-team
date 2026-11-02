---
id: fx-03-expat-ev-overnight-charge
persona: expat-ev
kind: fixed
title: 'Set the wallbox timer: find the English section, the cheap window, and whether the night is cheaper than it'
entry: https://luz.bymachines.ai/
inputs:
  question: "I usually charge 00:00–06:00 — is that still the cheap time tomorrow, or should I move it?"
success: 'Finds the English block («Electricity price tomorrow — Portugal»), reports tomorrow''s date, the cheapest and the
  most expensive 3-hour windows as printed, reads the 00:00–06:00 prices from the hourly table and says in one
  sentence whether the night is cheaper or dearer than the cheap window and where the charge should go; repeats the
  page''s caveat that the price is wholesale before grid fees and taxes. At most 3 fetches.'
expected_gaps:
- 'There is no separate English page (/en/ is 404); English is the lower half of the same page, anchor #en. The whole
  page (≈ 15 KB) fits in one fetch.'
- 'The 23:00 row of tomorrow is «no data» by design; the €/kWh estimate with regulated components is off.'
max_steps: 3
budget_usd: 0.2
version: 1
created: '2026-11-02'
baseline: null
---

You searched "electricity price tomorrow Portugal" and landed on https://luz.bymachines.ai/. You normally charge
the car 00:00–06:00. For tomorrow: what date does the page show, what are the cheapest and the most expensive
3-hour windows, what do the hours 00:00–06:00 cost compared with the cheap window, and where would you put the
charge? Say in one sentence what the price shown is and is not. Stop after max_steps fetches or when done. Report
every URL you fetched, where you stopped, and classify the outcome.
