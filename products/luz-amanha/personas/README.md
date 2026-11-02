# Personas and scenarios — Luz Amanhã

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
- Rolling data: the home page shows tomorrow after OMIE publishes (≈ 13:15–13:45 UTC build) and today before that.
  `success` conditions are about reading the date and the windows as printed, not about fixed prices. The archive
  `/d/YYYY-MM-DD/` starts at 2026-10-26.
- Known on 2026-11-02: `/go/telegram/` points to the placeholder `https://t.me/TODO_luz_amanha` until the owner
  creates the channel; the 23:00 hour of tomorrow is missing by design (OMIE publishes in Spanish time); the €/kWh
  estimate with regulated components is off (`estimate.enabled: false`); there is no separate `/en/` page — English
  is the lower half of every day page (`#en`).
- The resident persona reads Portuguese; its report is still in English (the role requires it), quoting the
  Portuguese text as seen.
- A scenario never asks the persona to look for defects; a clean success and a justified refusal are full results.
- New ids: `fx-NN-<slug>` / `ex-NN-<slug>`.
