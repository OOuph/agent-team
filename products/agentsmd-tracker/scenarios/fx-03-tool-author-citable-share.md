---
id: fx-03-tool-author-citable-share
persona: tool-author
kind: fixed
title: 'A citable sentence: share of top repos with AGENTS.md, sample, date, method and attribution in at most 4 steps'
entry: https://agentsmd.formachines.ai/
inputs:
  need: one sentence for a blog post about agent instructions in open source
success: 'Writes a sentence of the form "N of 1,922 top GitHub repositories (x %) had an AGENTS.md on <date>" using the
  headline numbers and the updated timestamp shown on the page, names the language with the highest and the lowest
  AGENTS.md share from the by-language table, gives the method in a few words (top-by-stars sample per language,
  HEAD request per path, presence only, no contents), and adds the attribution form from llms.txt or /legal/
  ("AGENTS.md Tracker, agentsmd.formachines.ai, <date>", CC BY 4.0). Does not confuse "any file" with "AGENTS.md".'
expected_gaps:
- 'On the first days the by-day table has one or two rows; the sparklines are static text. A trend is not part of
  this scenario.'
max_steps: 4
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

You are writing a post about agent instructions in open source and need one defensible sentence on how many top
repositories ship an AGENTS.md. Start at https://agentsmd.formachines.ai/. Produce the sentence with the number,
the sample size, the date the site shows, which language is highest and lowest, how the site measured it, and the
attribution the site asks for when its numbers are reused. Stop after max_steps fetches or when done. Report every
URL you fetched, where you stopped, and classify the outcome.
