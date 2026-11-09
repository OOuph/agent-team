# Changelog

The living description of the loop is [SCHEME.md](SCHEME.md); this file records when it changed and why. Rule: a pull
request that changes behaviour (roles, phases, states, tools, policies) updates SCHEME.md and adds a line here in the same
PR, citing the friction records (F-xxxx) or the owner decision behind it. The retro role proposes, the developer writes
the PR, a human merges.

## 2026-11-09

- T-0064: PO card: acceptance field names are checked against the task's own constraints (e.g. no-rename); on conflict the task text says which wins (example T-0077) (F-0134, F-0132). Card, SCHEME.md.
- T-0063: marketer card: a thread of replies on one channel counts as one post for the daily limit; no link to a dev.to article whose URL is not yet known (link the product page or omit); the AI label is mentioned in one line only (added by team-publish) (F-0126, F-0130, F-0121). Card, SCHEME.md.

## 2026-11-08

- T-0062: retro report starts with `## Owner digest` (≤ 12 lines): PRs merged in 24 h, PRs waiting for a human (protected files / tests changed without reason), rollbacks to last-good, spend today vs cap; `none` for empty, `unknown` for unreadable sources (voice:2026-11-07:P3; F-0127, rollback records F-kind `rollback`). `team/retro.py`, SCHEME.md, test.

- T-0061: rollback visibility: at the start of `team cycle --product all` the dispatcher compares the checkout HEAD with the previously recorded one (`data/framework/last-head`); when HEAD is an ancestor (watchdog rollback to last-good) it appends one friction record of kind `rollback` listing the dropped commits (hash + subject). Fast-forwards write nothing (voice:2026-11-07:P8). `team/friction.py`, `cli.py`, SCHEME.md, test.

- T-0060: the PR body gets a section `Verified by dispatcher` computed from git (`git diff --stat`, files changed), the test run (passed/failed counts) and the canary result (edited into the PR after the canary); files named in the developer's summary but absent from the diff add a `MISMATCH` line and a friction record. Claim-vs-reality evidence of the F-0127 class (voice 2026-11-07 P2). `team/prfacts.py`, `gitops.py`, `cycle.py`, `friction.py`, test.

- Quality rule (owner 08.11): new task type `quality` (speed, SEO/GEO, mobile, accessibility, clarity, data completeness), outside the hypothesis rule like `defect`. When no developer task is open the dispatcher wakes the PO at most every `cycle.dev_idle_po_every_h` (6 h) to queue 1–2 defect/quality tasks with a named source.
- Voice of the user twice a week: `cycle.voice_every_h` 168 → 84.
- T-0059: developer card: if the PR edits or deletes anything under `tests/`, the result summary carries a line of its own `tests-change-reason: F-xxxx <reason>`, so the T-0058 gate finds it in the PR body (OBS-0108, F-0127). Card, SCHEME.md.
- T-0058: auto-merge gate: a PR that modifies or deletes a file under `tests/` and has no `tests-change-reason: F-xxxx ...` line in its body is not self-merged (`waiting_owner`); pure additions of test files and PRs without `tests/` changes are unchanged (voice P6: the loop must not edit tests to go green). `team/cycle.py`, `team/gitops.py`, test.

## 2026-11-08

- Retro spend section: the cap is now the sum of the products' `budget_usd_per_day` (was a hardcoded $5 compared with the whole team's spend, so every day was flagged OVER 80 %); a credits column shows real money against the $100 daily cap. VISION of `agent-team` states $10 a day (decision 26). The voice-of-user report of 07.11 that quoted "$5 a day" is corrected.

## 2026-11-07

- T-0057: PO card: the runtime-file rule extends to anything under gitignored paths (`data/`, e.g. `data/framework/retro/`): no acceptance needs a committed file there, deliverables go to tracked paths; also for retro-originated tasks in `agent-team` (F-0127, F-0122). Card only.
- T-0056: the retro report gets a `Spend` section: spend per day for the last 7 days (billing ledger) next to the $5 cap, days over 80 % flagged; no ledger data → `spend: n/a` plus a `missing_data` friction record (voice P1: unattended loops burn money unnoticed). `team/retro.py`, test.
- T-0055: diary `product.yaml` `extra_hosts` gets the apex `formachines.ai` (errata and other pages; F-0125). Host-matching rule of `team-fetch` (already in code, now documented and tested): a listed host is allowed together with all its subdomains (`www.threads.com` under `threads.com`), never by bare suffix (`notthreads.com` is refused). Config, docs, test.
- Voice of the user (owner 07.11: «голос пользователя — часть фреймворка, нужен для любого продукта»): new role `voice` (Opus, WebSearch/WebFetch), phase `voice` weekly (`cycle.voice_every_h: 168`), topics from `product.yaml: voice_topics` or per-product defaults; report `products/<slug>/voice/<day>.json|md`, one `voice` observation per sourced pain (`voice:<day>:P#`), a VOICE OF THE USER block in the PO prompt (`team/voice.py`, `team/roles/voice.md`, `voice-report` schema).
- Hypothesis rule (owner 07.11): a PO `feature` carries `hypothesis` (who, pain, evidence, change, expected effect, metric, min_sample, read_on, score); without evidence from real people it is created in `proposed`, and a Ready feature without one goes back to `proposed` before it starts; `promote_tasks` lifts it; a due `read_on` gives the PO one `hypothesis_readout` card. Defects, content, marketing, analyst, owner and retro tasks are outside (`team/hypothesis.py`, PO card, po-decision schema). Generic dispatcher tests now use type `defect` for their PO tasks, since the rule would hold a bare `feature`.
- `team-publish --reply-to` accepts bsky.app and threads.com links (resolved to the at:// uri / our media id); real send errors reach the PO's question; a saved draft is no longer reported as dry-run (T-0057: the correction under Pivots 2/7 failed twice and was closed as «dry-run»).

## 2026-11-06

- T-0054: `add_lessons` drops lessons about the AI label / disclosure at write time (`team-publish` appends it); marketer card: the system prompt overrides any lesson on disclosure; PO card: runtime files (`editorial-lessons.md`, `ux-scores.jsonl`, `marketing-plan.md`) are not in the product checkout and never go into developer task scope (F-0121, F-0122).
- T-0053: editor card: never write a lesson about the disclosure / AI-agent label (`team-publish` appends it); stale lessons that told the marketer to put the label in the draft text are to be removed from `editorial-lessons.md` (runtime state, not in git; absent in the checkout) (F-0121). Card only.
- T-0052: luz-amanha `product.yaml` gets `extra_hosts: [threads.com]`, so the marketer can check by `team-fetch` whether a Threads post already exists before publishing (F-0117); the diary's `bymachines.ai` (F-0115) is confirmed in its marketer list (added by T-0050, the refusal predates it). Tests assert both. Config only.
- T-0051: reviewer and persona cards: double quotes, `=` and attribute text are denied in `--grep` (bad `class="card`); count with `--tag NAME --count` or one plain word (F-0116, F-0101, F-0099, F-0084). Cards only.
- T-0050: diary `product.yaml` gets `extra_hosts` (bymachines.ai, releases.formachines.ai, agentsmd.formachines.ai, luz.bymachines.ai), so `team-fetch` of every diary role (marketer included) opens the portfolio sites and claims about them are checked on the live pages (F-0115). Config only.

## 2026-11-05

- T-0049: analyst card names `curl` and `/_team/metrics.json` as denied (metrics are already in the prompt); editor card says Read (incl. `/dev/null`), `cat` and shell file creation are unavailable, use only `team-fetch` (F-0097, F-0105, F-0039). Cards only.
- T-0048: editor card: the publish tool appends the channel disclosure after review, so a draft without it is complete (do not flag or add one); marketer card: do not write own disclosure line in the text. Fixes double disclosure on t.me/formachines_team/9 and /7. Cards only.
- T-0047: reviewer card: first Rules bullet bans `<` `>` and other shell metacharacters in `--grep` and points to `--tag h1` / `--tag h1 --count`; persona card repeats the rule at the top (F-0101, F-0098, F-0099, F-0084). Card only.
- T-0046: guard `Cycle.guard_no_commit_loop` (≥2 «committed nothing» records, developer role) now runs for tasks in any state (ready, in_progress, claimed) and again inside `execute_task`, so no path can start the developer a third time (T-0057 replay test) (F-0107, F-0108, F-0109, F-0110, F-0112, F-0113).
- T-0045: developer card: first rule checks `git rev-parse --show-toplevel` and `git branch --show-current` before any edit; wrong checkout or branch other than `task/<id>` means `blocked` with a question (F-0111, F-0093). Card only, no dispatcher behaviour change.
- T-0044: F-0095/F-0096 cause recorded: `repo_only` WAS set on luz-amanha T-0042 (the reviewer ran in `review-T-0042/checkout`); the allowlist then lacked the trailing-flag form `tests.* -v`, which T-0033 (1ed6328, 11-04 08:07) added after the 02:09 denial. Not a matcher bug; test `test_f0095_exact_command_allowed` feeds the exact command through the matcher.
- T-0043: `previous_no_commit` counts «committed nothing» records over the task's whole history, not only since the last return to `ready`; a developer task with two such records that the PO re-readies is not started: a title starting with `Check:` becomes type `check` for the analyst, anything else goes back to `blocked` with a question (F-0103, F-0104, F-0107–F-0110, F-0112, F-0113).
- T-0042: PO card: a verification-only task (no repo change needed) goes to the analyst or names the file to change; no zero-commit developer tasks (F-0104, F-0107, F-0109, F-0110, F-0113).
- T-0041: optional task-result field `no_change_reason`; `done` with no commit, a non-empty reason and self_check evidence goes to `review` (`accepted` for type `check`) with no `no_commit` friction record; without it the T-0038 rule stays (F-0103, F-0104, F-0107–F-0110, F-0112, F-0113).
- T-0040: transient GitHub 5xx on push/PR returns the task to `ready` at most 3 times in a row (`transient_failures`), then `blocked` with a clear reason; push 5xx detection matches «returned error: 5xx», «HTTP 502», «HTTP/2 504», «status 5xx», «remote: … 503», 4xx never retried. Tests: https://github.com/OOuph/agent-team/blob/main/tests/test_gitops.py and https://github.com/OOuph/agent-team/blob/main/tests/test_store_cycle.py (F-0100, OBS-0070, OBS-0071).

## 2026-11-04

- T-0039: push of the task branch and `POST /pulls` are retried 3 times with backoff (5/15/45 s) on HTTP 5xx; if GitHub still answers 5xx the task goes back to `ready` with a `pr.transient_failure` event instead of `blocked` (F-0100).
- T-0038: «done but committed nothing» puts the developer's summary and self_check head into the transition note (so the `no_commit` friction record shows them); the second no-commit in a row blocks the task and asks the PO to re-scope instead of returning it to `ready` (F-0103, F-0104, F-0107, F-0108, F-0109).
- T-0037: `team-fetch --tag NAME [--count]` matches the literal `<NAME` internally; reviewer, persona and editor cards point to it instead of `--grep '<h1'` (F-0084, F-0099, F-0101).
- T-0036: reviewer, support and persona cards: `--grep` patterns are plain text (`'h1'`, not `'<h1'`), use `--around N`; in the persona card the rule moved from the end into the first block (F-0101, F-0099, F-0084, F-0100).
- T-0035: persona, reviewer, editor and marketer cards: shell rule says `< > | ; & $` are denied even inside quoted arguments, so `--grep` patterns are plain text (`'item'`, not `'<item'`; `'Rust'`, not `'>Rust<'`), use `--around N` (F-0084, F-0099).
- T-0034: reviewer card carries the working test form for repo_only tasks: `python3 -m unittest discover -s tests -t .` when `tests/` has no `__init__.py` (bare `unittest` finds 0 tests), and `unittest tests.test_x -v` is allowed; SCHEME reviewer row aligned (F-0095, F-0096, OBS-0063).
- T-0033: repo_only reviewer allowlist accepts flags after the module (`unittest tests.<module> -v`), `unittest -v discover…` and `discover -s <dir> -t <dir>`; prompt tells to use `discover -s tests -t .` when tests/ has no `__init__.py`; test checks every command quoted in the prompt against the allowlist (F-0095, F-0096).
- T-0032: repo_only reviewer may run module-targeted unittest: `python3 -m unittest [-v] tests.<module>`, `discover -s <dir>`, `discover -v -s <dir>`; card, SCHEME, task schema and prompt updated; test checks list and prompt agree (F-0095, F-0096).
- Personas judge the reader's experience on Opus (owner 04.11): an `experience` block in every run (first impression, steps to understand, interest and usefulness 1–5, would return/share, why, wish) → `products/<slug>/ux-scores.jsonl`, rolling scores in the digest and the PO prompt; observations only for what should change (`ux_friction`, `value_gap`, defects, hypotheses), successes no longer filed; low scores become `value_gap`; twice a day per product, 2 scenarios in rotation; PO puts a task on interest/usefulness when scores stay ≤ 3.
- sitemetrics: browser traffic is counted as people only if it is not (a) from a published cloud/crawler network (Googlebot, Bingbot, AWS, GCP, Oracle, DigitalOcean, Linode, Vultr — refreshed weekly by metrics.sh — plus a built-in list for Tencent, Alibaba, Azure, Hetzner, OVH, Meta), (b) a stale browser string (Chrome/Firefox < 100, Presto, IE, Android ≤ 6, iOS ≤ 13), (c) one address presenting ≥ 3 operating systems in a day, (d) a proxy pool: the same UA from ≥ 5 addresses of one /16 in a day, none loading an asset. Owner 04.11: the 'people' counter was inflated by bots.
- T-0031: denial hint (transcript «Denied calls») names the allowed form for `team-fetch ... --grep '<item' --around 0` (a `<`/`>` in an argument reads as a redirect; grep without it, `--around N≥1`); persona card already carries the shell rule; tests (F-0084, F-0038, F-0040, F-0033, F-0031).
- T-0030: reviewer card states one verdict rule: criteria visible on the live site decide pass/fail; non-public ones (build marker, scan time, code, zero-case) go to `limits` as `not observable`; `inconclusive` only if no criterion is observable (F-0064, F-0060, F-0050, F-0055, F-0046).
- T-0029: PO card: before Ready list the files each criterion needs, including the generator (e.g. `build.py` when `llms.txt` links a report), and put all in `scope`; no `depends_on` on a task that is not done with a non-stub report (F-0088, F-0089, F-0092, F-0094).
- T-0028: a retry whose branch `task/<id>` conflicts with base no longer starts a fresh branch at base (the earlier attempt's commit was lost); base is merged with the earlier attempt's side winning, and as a last resort the earlier commit is kept in history (F-0090, F-0093).
- T-0027: a developer `partial` result is checked for repetition before the «committed nothing» check (a no-commit partial used to bypass it and run a third time, T-0033); new optional task field `depends_on` — the task is not started until every listed task is accepted/measured/done with a non-stub result (F-0085, F-0088, F-0091, F-0094).
- T-0025: the repeated-partial rule matches on overlapping friction kinds (any shared kind) and ignores `environment` when other kinds are present; before, a new `environment` kind made the sets differ and a third round ran (F-0085, F-0086, F-0088–F-0094).

## 2026-11-03

- T-0024: a developer task whose partial result repeats the previous attempt's friction category (same set of `kind`s) is blocked at once with a PO question; no third hourly run (F-0085, F-0088, F-0091, F-0094).
- Guards: a task whose partial result repeats (same summary or same frictions) is blocked with a question to the PO instead of running again (release-radar T-0033 burned three cycles); an analyst stub ('duplicate call; see the previous report', or < 120 chars) does not close the task — blocked + question (T-0029).
- T-0022: a retry of a developer task continues `task/<id>` (local, else origin) with base merged in; on a merge conflict it starts fresh from base (F-0087, F-0090).
- sitemetrics: scanner probes at any depth (`/app/.env`, `/api/.env`, `wp-login.php`, `/.git/`, phpmyadmin…) are class scanner, not human (they showed up in release-radar human top pages).
- T-0021: PO card `create_task` rules: `scope` must list every file or build step the criteria need; a task that consumes another task's report names it, is created after acceptance or carries `not_before` plus a fallback (F-0082, F-0083).
- T-0020: a denied Bash call gets the allowed form — `denial_hint()` in `runner.py` (Write tool for files, bare `team-fetch`, `--json-path`/`--count`/`--grep` instead of pipes) is written to the run transcript under «Denied calls»; marketer and editor cards open with the same three-line shell rule (F-0022, F-0031, F-0032, F-0033, F-0038, F-0039, F-0040, F-0041).
- Task dedup: two posts for different channels at the same `not_before` are two slots (a Threads trial was dropped as a duplicate of an X trial, 16:13). Framework VISION: merge wording aligned with decision 24a (critic FW-02, Q-0005).
- SCHEME §10 «Внешний критик»: contract, place in the loop, role as the independent verifier of 'done', the measure of its value and its limits; the 12-hour role report counts critic cards per product (PO decisions, tasks created) and critic reports by finding status.
- Critic: JSON twin (`<report>.json`, REPORT_FORMAT.md §2) is the machine source when present; `fixed`/`unverified` findings make no card; `agent-team-responses.json` is written to the critic's context dir after every `cycle --product all` with the PO decision and task per finding id.
- Droplet metrics: the external critic's user agent `FormachinesExternalCritic/` counts as own traffic (with `agent-team/`), not as an agent.
- External critic (`team/critic.py`, `external_critic` in team.yaml): Markdown reports of an outside Codex critic are read every cycle; each `**ID · Pn · title**` finding becomes a `feedback` observation of the product it names, the PO answers with a task or a reasoned reject (owner 03.11).
- T-0018: `team-fetch` gets `--json-path a.b[0].c` (prints the value, or its length with `--count`) and `--json-keys`, so roles read counts from JSON without pipes; flags listed in the editor and marketer cards (F-0038, F-0039, F-0040, F-0041, F-0072).
- PO rule: a new tool (channel, flag, data file) is tried the day it becomes available — first real use in the next cycle, not in the weekly plan days later (owner 03.11: Mastodon/X/Threads were live since the morning but the first posts were planned for 04–06.11).
- T-0017: dispatcher fallback for `repo_only` — if the developer's `how_to_verify` lists only `raw.githubusercontent.com` URLs, `review_task` sets `repo_only` on the task before the review, so the reviewer gets the read-only checkout (F-0046, F-0048, F-0053, F-0055, F-0062, F-0058). The PO flag itself came with T-0015.
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
