# Personas and scenarios — Release Radar

- One file per scenario: YAML front matter (`id`, `persona`, `kind`, `title`, `entry`, `inputs`, `success`,
  `expected_gaps`, `max_steps`, `budget_usd`, `version`, `created`, `baseline`) and a body. The body is the task
  handed to the persona verbatim and ends with the stop sentence. Scenario files live in `../scenarios/`.
- The dispatcher (`team/cycle.py` → `run_personas`) hands the persona the front matter without `success`,
  `expected_gaps` and `title` — those are the answer key for the PO at triage — plus the body, and the persona card
  from this folder (`<persona>.md`). The persona's only tool is `team-fetch <url>` (GET, body cut at ~30 000 chars).
- The dispatcher runs at most the first 6 files of `../scenarios/` in name order (`ex-` sorts before `fx-`; any other .md there, e.g. a README, would take a slot — that is why this file lives in `personas/`). Keep the set at 6 or
  accept that later `fx-` files are skipped.
- `fx-*` is the regression set: same inputs every run. `ex-*` is exploratory and may be rewritten.
- Rolling data: the site shows a 7-day window. Scenarios that name a specific release (fx-01) state in `success`
  what counts once that release has left the window; re-pin the inputs on the Monday review.
- Hops `/go/<id>/` are meta-refresh pages; team-fetch does not follow them and external hosts may be refused —
  reading the target URL on the hop page is the "external click" for every scenario here.
- A scenario never asks the persona to look for defects; a clean success and a justified refusal are full results.
- New ids: `fx-NN-<slug>` / `ex-NN-<slug>`.
