# Changelog

The living description of the loop is [SCHEME.md](SCHEME.md); this file records when it changed and why. Rule: a pull
request that changes behaviour (roles, phases, states, tools, policies) updates SCHEME.md and adds a line here in the same
PR, citing the friction records (F-xxxx) or the owner decision behind it. The retro role proposes, the developer writes
the PR, a human merges.

## 2026-11-03

- Dispatcher: a ready task whose title names a future DD.MM date and has no `not_before` is not started (observation
  for the PO instead); a task blocked twice on the same question is not restarted by a non-owner answer without
  `not_before` (F-0017, F-0019, F-0023, F-0025, F-0026, F-0027).
- Editor, reviewer, persona, support cards: Shell rule (bare `team-fetch <url> [flags]` only; no cd, pipes, grep, head, tail;
  use `--grep/--around/--count/--tail/--offset` instead) (F-0031, F-0018).
- Marketer card: `Write` added to tools; post files must be created with Write, never shell redirection or heredocs
  (F-0021, F-0022, F-0032, F-0033, F-0035).
- Droplet metrics: `metrics.sh` reports only the team's four hosts (scanner Host headers created 28 junk directories) and passes the union of product `targets:`; before this every site's `targets` block was empty (F-metrics, 03.11).
- PO answers carry `not_before`: a "wait until X" answer reschedules the unblocked task instead of restarting it next
  cycle (ops finding: agentsmd T-0004 blocked every hour after being answered).
- `team-fetch`: `--grep TEXT --around N` (window of N chars around each of up to 40 matches), `--count` (number of
  matches/lines), `--offset K` (body from char K within max_body); reviewer card and usage line list them. Eases
  single-line pages (F-0007, F-0010, F-0014, F-0015, F-0018).

## 2026-11-02

- Editorial loop (owner, late evening: "вычитывание и критика постов, чтобы тексты не деградировали"): `team-publish`
  draft mode (`TEAM_PUBLISH_DRAFT_DIR`), role `editor` with `editor-review` schema, one revision round, dispatcher publishes
  approved drafts (`publish_draft`), scores ledger `data/editorial/scores.jsonl`, rolling mean in the daily digest,
  degradation observation below 3.5, `products/<slug>/editorial-lessons.md` (+ `team lesson`). SCHEME.md §2–3.
- PO card: tasks naming a future date/hour MUST carry `not_before`; metrics the data lacks must name the available field
  or go to the analyst. Marketer card: publish with the available number and say so; block only if no honest post is
  possible. Cause: F-0003, F-0017, F-0019, F-0020.

- Tests are sealed: conftest strips every secret from the environment and forces publish dry-run; the dispatcher's
  test command for PR-mode repositories runs with the same sanitised environment. Cause: the Demo budget test posted
  a "$25.00" digest to the real ops channel during the first unattended PR run.

- Marketer card: a Writing section (lead with the event, one idea, plain words, hook); diary VISION tone rewritten
  from "dry log with numbers" to a living story after the owner's review of the first Telegram post.

- PO prompt carries the owner's answers of the last 72 h and, for the framework product, the friction register, the
  latest retro and the loop status; `Store.ask` returns the existing question instead of repeating the same text
  within 72 h (event `question.repeat`); `product.yaml: models:` picks a model per role (framework PO on sonnet).
  Ops finding: the framework PO asked the owner every cycle whether the loop runs ($0.17 per opus run).

- First unattended framework change: retro → T-0002 → PR #5 → canary → merge (43acb7e). Found on the way: the
  running checkout was not fast-forwarded because untracked state files counted as dirty; `dirty_tracked` fixes it.

- Role cards (T-0002; F-0001, F-0002, F-0008, F-0009, F-0011, F-0012, F-0013, F-0016): persona, marketer, reviewer: one bare
  `team-fetch`/`team-publish` call per URL, no cd/;/&&/loops/pipes; po, analyst: no shell, missing context goes into `friction`.

- Full self-improvement loop on (owner, late evening): retro every cycle, proposals of any risk become tasks,
  canary cycle on the candidate code, auto-merge unless a protected path is touched, external `watchdog.sh` with
  rollback to the last good commit; the server runs from a git clone, `sync.sh` deploys by fast-forward. SCHEME.md §9.
- Role `support`: answers people in GitHub issues/comments and Bluesky replies within VISION (publish channels
  `github`, Bluesky `--reply-to`), escalates the rest to the PO, ≤ 10 replies a day.

- `tool_denied` friction records carry the denied command / url (≤ 80 chars, secrets stripped), so the retro can
  tell a narrow allowlist from a role going off-card. First retro proposal, issue OOuph/agent-team#1 (F-0001…F-0006).

- Self-improvement loop: friction register (`team friction`), `framework-retro` role (`team retro`), PR mode for the
  framework repository (`repo.mode: pr`, `test_cmd`), `friction` field in task-result and review-verdict. Owner
  decision 17. SCHEME.md §9.
- `not_before` on tasks: the PO schedules daily posts, the dispatcher waits (friction: Luz marketer task started a day
  early and blocked).
- Public export for the diary site (`team export-public`, `publish-diary.sh`); fourth product `agent-team-diary`.
- Cron on the server invokes scripts through `bash` (friction: execute bit lost on sync, 21:07 UTC cycle did not run).
- Evening: channel-specific AI disclosure, L0–L4 ladder in VISION, feedback collectors (GitHub issues, Bluesky
  replies), SEO check every 12 h, GEO probe weekly, IndexNow on publish. Owner decisions 13–15.
