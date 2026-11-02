# Personas and scenarios — AGENTS.md Tracker

- One file per scenario in `../scenarios/`: YAML front matter (`id`, `persona`, `kind`, `title`, `entry`, `inputs`,
  `success`, `expected_gaps`, `max_steps`, `budget_usd`, `version`, `created`, `baseline`) and a body. The body is
  the task handed to the persona verbatim and ends with the stop sentence.
- The dispatcher (`team/cycle.py` → `run_personas`) hands the persona the front matter without `success`,
  `expected_gaps` and `title` — those are the answer key for the PO at triage — plus the body, and the persona card
  from this folder (`<persona>.md`). The persona's only tool is `team-fetch <url>` (GET, body cut at ~30 000 chars).
- The dispatcher runs at most the first 6 files of `../scenarios/` in name order (`ex-` sorts before `fx-`; any
  other .md there, e.g. a README, would take a slot — that is why this file lives in `personas/`). Keep the set at
  6 or accept that later `fx-` files are skipped.
- `fx-*` is the regression set: same inputs every run. `ex-*` is exploratory and may be rewritten.
- Rolling data: counts change daily and the sample only grows. `success` conditions compare structure and
  consistency (presence, sums, dates), not fixed numbers, except where a number is quoted as "as of 2026-11-02".
- Hops `/go/<id>/` are meta-refresh pages to github.com; team-fetch does not follow them and github.com may be a
  refused host — reading the target URL on the hop page is the "external click" for every scenario here.
- A scenario never asks the persona to look for defects; a clean success and a justified refusal are full results.
- New ids: `fx-NN-<slug>` / `ex-NN-<slug>`.
