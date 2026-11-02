---
id: ex-01-developer-nextjs-stack-weekly
persona: developer
kind: explore
title: 'Weekly check of a six-package Next.js stack: which released, which are flagged, what would have made it faster'
entry: https://releases.formachines.ai/
inputs:
  packages: 'package.json: next 16.4.3, react 19.3.0, react-dom 19.3.0, vitest 5.0.4, tailwindcss 4.3.2, typescript 7.0.1'
  symptom: none — Monday dependency review
success: 'Produces a table for all six packages: released in the last 7 days yes/no, versions, flagged yes/no with the
  quote when flagged, Errata badge yes/no — using only what fetched pages show, inventing nothing; then names
  concretely what the site could have offered to make this faster (e.g. a page per package list, a "paste your
  package.json" view). Stating "not visible in the cut home page, went to /pkg/npm/<name>/" is correct behaviour.'
expected_gaps:
- 'The home page is cut at 30 000 characters; six per-package pages cost six fetches. No multi-package view exists.'
- 'tailwindcss and typescript show many pre-releases (insiders / nightly) that crowd the lists; the page marks them
  "pre-release".'
- 'typescript carries an Errata badge (npm/typescript) regardless of this week''s release; the badge is per package,
  not per release.'
max_steps: 10
budget_usd: 0.5
version: 1
created: '2026-11-02'
baseline: null
---

It is Monday and you review the dependencies of a Next.js project before bumping them. Your package.json pins:

    next 16.4.3 · react 19.3.0 · react-dom 19.3.0 · vitest 5.0.4 · tailwindcss 4.3.2 · typescript 7.0.1

Starting at https://releases.formachines.ai/, find out for each package whether it released in the last 7 days,
which versions, whether any release is flagged for breaking or deprecated wording (quote the line), and whether the
package carries an Errata badge. Produce a table with one row per package. Then say what the site could have
offered to make this faster. Stop after max_steps fetches or when done. Report every URL you fetched, where you
stopped, and classify the outcome.
