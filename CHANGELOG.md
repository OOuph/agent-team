# Changelog

The living description of the loop is [SCHEME.md](SCHEME.md); this file records when it changed and why. Rule: a pull
request that changes behaviour (roles, phases, states, tools, policies) updates SCHEME.md and adds a line here in the same
PR, citing the friction records (F-xxxx) or the owner decision behind it. The retro role proposes, the developer writes
the PR, a human merges.

## 2026-11-03

- T-0016: marketer card — `dry_run: true` in draft mode and missing post URLs are the expected flow and are never reported as friction (F-0071, F-0073, F-0074). Card text only; SCHEME.md unchanged.
- T-0015: PO card and `po-decision.schema.json` get `repo_only` — the PO sets `repo_only: true` on tasks whose criteria name repo files (mcp/, tests, README, code), `apply_po` stores it, so the reviewer gets the read-only checkout instead of inconclusive (F-0046, F-0048, F-0053, F-0055, F-0062).
- Canary false positive fixed: the deputy is skipped in dry-run (its 'run failed: no run' warning matched the old ' failed: ' substring check and failed PR #32); the canary now fails only on `phase.error` or a 'phase X failed' line.
- T-0014: marketer card — `--text-file` is an absolute path (the Write tool's directory; the shell cwd differs), and a `team-publish` result with `dry_run: true` counts as the deliverable: draft ids in `changes`, status done, not friction (F-0071, F-0073, F-0074, F-0076). Card text only; SCHEME.md unchanged.
- X: OAuth 2.0 user context in `team-publish` (the 2026 developer console issues a client id/secret + access/refresh token, not OAuth 1.0a keys): with `X_CLIENT_ID`, `X_CLIENT_SECRET`, `X_REFRESH_TOKEN` in .env the 2-hour access token is refreshed on demand and the rotated refresh token is kept in `data/x_oauth2.json` (lock + atomic write; .env holds only the seed). OAuth 1.0a stays as the fallback. Account `@formachines_ai`.
- Marketer runs channels, not single posts (channels session's review 03.11: reactive role, one task = one post, no plan, no memory of its posts, no feedback by visits, a "plan" task impossible because it blocked without a post). Plan task: PO `ready_tasks[].plan: true` (role marketer, type marketing) → the marketer writes `marketing-plan.md` (Channels, Rubrics, Slots for 7 days with UTC dates and VISION goals, Drop, Why) in its work dir, the dispatcher copies it to `products/<slug>/marketing-plan.md` and accepts the task by the file (≥ 200 chars) with `posted 0`; `content_without_post` and the PO question are skipped for it. PO prompt shows the plan (or "NONE → create a plan task", or "older than 7 days → new plan"), the channel table and the week's posts, and cuts slots into post tasks with `not_before`. Memory in the marketer prompt: `PUBLISHED IN THE LAST 7 DAYS` from the `posts` table (new column `text`, migrated on open; `record_post(..., text=)` stores the editor's final text), `CHANNELS: VISITS BY ?src=` from the site's metrics `by_src` (per day over 14 days, totals as fallback) with the rule "no human visits after two weeks of posting → less often, visits → more often, no posts → try", `WHAT PEOPLE SAID` (feedback observations, 7 days), `CURRENT MARKETING PLAN`. Role card: channel strategy table (telegram, bluesky, mastodon, devto, threads, x: audience, format, frequency, VISION goal); tone rules untouched. `metrics/latest.md` gains a `Visits by ?src=` line. Tests: `tests/test_marketing_plan.py` (8).
- Publishing channels `threads` (Threads API: container → publish → permalink, `THREADS_USER_ID` + `THREADS_ACCESS_TOKEN`, 500 chars) and `x` (API v2 `POST /2/tweets`, OAuth 1.0a HMAC-SHA1 on stdlib, `X_API_KEY`/`X_API_SECRET`/`X_ACCESS_TOKEN`/`X_ACCESS_SECRET`, optional `X_HANDLE` for links; 280 weighted chars, links count 23) in `team-publish`; `--reply-to` works for both. Mastodon connected (`@formachines@mastodon.social`, bot flag on) and added to the four site products (owner decision 22 "write everywhere"). The Threads long-lived token expires after 60 days: run `bash bin/threads-refresh-token` (or `.venv/bin/python -m team.threads_token .env`) on the server (after 24 h, before expiry); not automated yet, no cron.
- Canary runs a `--dry-run` cycle on a COPY of the product state with publishing forced to dry-run and no channel secrets — the first version ran a real cycle on live state and executed real tasks on the candidate code (release-radar T-0009 published and T-0019 merged by the 08:07 canary).
- T-0013: reviewer — a negative case that cannot be produced on the live site goes into `limits` as `not observable`, not into an inconclusive verdict, when every other criterion passed; inconclusive is only for unchecked main criteria (F-0060, F-0066, F-0064, F-0056).
- Deputy owner (`team/deputy.py`, role `deputy`, mandate `DEPUTY.md`, protected): open questions to the owner older than `deputy.after_h` are answered by mandate — decided (conservative), unavailable (secrets, money, accounts, channels, VISION) or info — so no task waits on an unreachable owner (owner 03.11: a week away, no access).
- Execute seats per role (`cycle.execute_slots`: developer 1, marketer 1, analyst 1) — the daily post no longer starves behind developer tasks; a marketing task without a recorded publication is never `accepted` by the reviewer (blocked + question to the PO); the developer prompt names the product's `test_cmd` (framework checkout has no venv, F-0067).
- T-0011: tasks with `repo_only: true` get a reviewer run in a read-only checkout of the released commit (`Read`/`Glob`/`Grep`, `python3 -m unittest`, `python3 -m pytest -q`, `git log -1`, `team-fetch`), so tests give pass/fail instead of inconclusive (F-0046, F-0047, F-0049, F-0054, F-0042, F-0053, F-0055, F-0050, F-0051). New `team/schemas/task.schema.json` documents the task fields.
- T-0010: reviewer — a build-marker mismatch alone never makes a verdict inconclusive; note it in limits and check every criterion on the live site (F-0044, F-0042, F-0037, F-0048).
- team-fetch hosts per role (`fetch.extra_hosts_by_role` in team.yaml): the reviewer and support may open github.com / raw.githubusercontent.com / api.github.com — repo-only reviews were denied_permission and inconclusive (F-0059…F-0065). sitemetrics `--own-ip`: manual checks from reservememes count as own traffic, not agents.
- build-site.sh: a lost push race no longer repeats the scan — data/ is kept, the clone is reset to the new main and the site is re-rendered offline (`--no-scan` / `--offline`), then pushed (ops session caught agentsmd losing 40 min of scan to the 05:07 merges). agentsmd cron 04:20 with `--max-seconds 5400` for the full daily scan of 1922 repos.
- T-0009: PO criteria must be observable via a public URL (repo-only deliverables get a published artefact cited in the criterion); reviewer marks checkout/process-only criteria `not observable: repo-only` in limits and does not repeat an inconclusive verdict on re-review (F-0042, F-0043, F-0046, F-0047, F-0048, F-0049).
- sitemetrics: glob targets (`/go/vote/*/up/`, `**`); droplet metrics.sh default targets extended with /go/vote/ up/down, /go/github/, /llms.txt (analyst requests on luz and release-radar).
- `team report` + `report-roles.sh` (cron 07:30 / 19:30 UTC): 12-hour role report to the owner's Telegram — a deterministic
  ledger per product and role from the stores (tasks, results, reviews, posts, editorial, support, PO decisions, merges,
  builds, spend) → one `reporter` run writes first-person paragraphs per role and the PO's "chosen / implemented";
  the dry ledger is sent when the model fails (owner 03.11).
- 2026-11-03 ops (duty session, decision 24): reviewer gets the repository URL and reads repo-only deliverables via raw.githubusercontent.com (F-0042, F-0046, F-0048, F-0053, F-0055); a newer build marker is normal with daily cron builds; developer lists raw URLs of changed files; PO never creates developer tasks for metrics counting (luz T-0013 loop, Q-0005/Q-0006) — targets are ops config in product.yaml; targets added: luz /go/vote/, release-radar /go/vote/ /llms.txt, diary /go/github/ /llms.txt.
- Protected paths reduced to the rollback pair (`run-cycle.sh`, `watchdog.sh`) — owner 03.11: maximum automation during
  the pilot; `store.py`, `team.yaml`, `build-site.sh`, `deploy/`, `bin/` and the rest now merge by themselves after tests + canary.
- Daily site builds: `build-site.sh` + cron on the server at the hours each VISION promises (agentsmd 04:00, release-radar
  06:00, luz 13:15/13:45 UTC) — before this no scan or rebuild ran on schedule, sites refreshed only with developer
  merges (found by the ops session 03.11 04:30). Separate clone per site, rebase on a lost push, friction + Telegram on failure.
- PO triage: the backlog brief now carries `not_before` (the PO re-created every scheduled post as "a task with a start
  time": luz-amanha T-0011/T-0012/T-0014, release-radar T-0012/T-0013/T-0015/T-0016); the dispatcher drops a proposed task
  whose title or role+`not_before` slot is already open (`task.duplicate_skipped`); PO can close tasks (`close_tasks`).
- Dispatcher: a ready task whose title names a future DD.MM date and has no `not_before` is not started (observation
  for the PO instead); a task blocked twice on the same question is not restarted by a non-owner answer without
  `not_before` (F-0017, F-0019, F-0023, F-0025, F-0026, F-0027).
- Editor, reviewer, persona, support cards: Shell rule (bare `team-fetch <url> [flags]` only; no cd, pipes, grep, head, tail;
  use `--grep/--around/--count/--tail/--offset` instead) (F-0031, F-0018).
- Marketer card: `Write` added to tools; post files must be created with Write, never shell redirection or heredocs
  (F-0021, F-0022, F-0032, F-0033, F-0035).
- Marketer and editor cards: Shell rule extended (heredoc, `cat >`, `echo >`, `python3`, `grep`, `Edit` denied; post file only via Write; editor has no Write, no saving pages to /tmp, narrows output with `team-fetch` flags) (F-0022, F-0032, F-0033, F-0035, F-0038, F-0039, F-0040, F-0041, F-0031).
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
