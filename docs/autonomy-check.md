# Autonomy check protocol

Question it answers: **can the whole contour run for a week with nobody watching?** Written 04.11.2026 before the
owner's week away (04–10.11), from the 03.11 plan and drills. The check is code (`team autonomy-check`), runs daily at
08:15 UTC by cron (`autonomy-check.sh`) and puts a short summary into the ops chat: one line on a clean day, plus every
WARN and FAIL line otherwise. The full table is kept in `data/autonomy/<stamp>.md`. Exit code 1 means at least one FAIL.

```
.venv/bin/team autonomy-check            # full table, runs the test suite too
.venv/bin/team autonomy-check --no-tests # quick
.venv/bin/team autonomy-check --send     # also to the ops chat
```

## Statuses

- **OK**: the component works and there is fresh evidence.
- **WARN**: degraded or stale but not stopping the contour. Nobody needs to act tonight; the loop or the next cycle
  usually clears it.
- **FAIL**: the contour, or a site, is stopped or about to stop. The remedy column says who acts.

## Checks

| ID | What | Evidence read | OK when | On FAIL |
|---|---|---|---|---|
| C1 | Cron entries | `crontab -l` | cycle, watchdog, backup, diary, role report, three site builds and this check present | operator restores the line from this file; nothing else can |
| C2 | No STOP file | `STOP` in the checkout | absent | STOP is a human decision; remove it only if the reason is known |
| C3 | Last finished cycle per product | product stores, `cycles` | every product ≤ 75 min (WARN ≤ 150 min) | the watchdog restarts the cycle after 2 h of silence and rolls back to `last-good` if code changed |
| C4 | Role runs in 24 h | `run` events | failures < 10 % (WARN < 25 %) | read the failed runs' `run.json`; plan or credit limits show up in C14 |
| C5 | Code and watchdog | `HEAD`, `data/framework/last-good`, `data/watchdog.log` | HEAD = last-good, 0 watchdog interventions (WARN: they differ or 1–2 interventions; FAIL: ≥ 3) | ≥ 3 interventions a day = the loop is breaking itself: `touch STOP` and wait for a human |
| C6 | Tests on HEAD | `pytest tests` | green | the loop never merges red; a red HEAD means a manual change; revert it |
| C7 | State backup | `data/backups/products-*.tgz` | newest ≤ 26 h | run `backup-state.sh` |
| C8 | Site builds pushed | `data/builds.log`, `data/diary.log` | each site and the diary pushed ≤ 26 h ago | the site keeps yesterday's data; rerun `build-site.sh <slug>` |
| C9 | Sites answer, certificates | HTTPS to every product site | 200 and certificate > 14 days (WARN ≤ 14) | droplet: `systemctl status agent-team-publish.timer nginx`; certbot renews itself |
| C10 | A public post in 26 h | `posts` table per product with channels | every such product posted (WARN otherwise: quality, not outage) | the PO sees it in the digest; the marketer's next slot |
| C11 | 12-hour role report | `data/reports/*.md` | newest ≤ 13 h | rerun `report-roles.sh`; Telegram token in C16 |
| C12 | Owner questions answered | open questions `to: owner` | none older than 2 h (WARN) | the deputy answers every cycle; if it does not, read its last run |
| C13 | External critic in and out | critic `reports/`, `context/agent-team-responses.json` | report ≤ 3 h, responses ≤ 2 h (WARN otherwise) | the critic is a separate system; its silence does not stop the team |
| C14 | Claude plan and credits | `data/billing.json` (written by every run) | limit info ≤ 2 h old and credits allowed, or plan < 90 % | FAIL = plan and credits exhausted or the $100/day credit cap reached: runs resume by themselves at the window reset or 00:00 UTC; to resume sooner add credits |
| C15 | Disk | `statvfs` | > 10 GB free (WARN > 3) | clear `data/work/` and old `data/runs/` |
| C16 | Required secrets | names in `.env` (values never printed) | Claude token, GitHub token, Telegram bot and ops chat present | only the owner can add a secret |
| C17 | Daily budget per product | product stores | below 90 % of the product cap (WARN otherwise) | the product stops for the rest of the UTC day by design |

## Drills (live contour, run with the owner's consent)

| Drill | Status 04.11 | How |
|---|---|---|
| STOP and resume | passed 03.11 (no cycle 09:07 under STOP, cycle 10:07 after removal, watchdog silent) | `touch STOP`, wait a cycle, `rm STOP`, wait a cycle |
| A breaking pull request does not reach the server | passed by code and tests: the dispatcher merges only its own PRs after green tests and a canary | — |
| Watchdog rollback after silence | not run: it rolls code back, refused without the owner | `MAX_SILENCE_S=0 bash watchdog.sh` |
| Droplet reboot | not run: timers are `enabled`, systemd restarts them | `reboot` on the droplet, check C9 after 3 min |
| Plan limit reached → credits | not run (cannot be provoked); credits confirmed allowed by a probe run 04.11 06:5x UTC | C14 shows the switch; the owner gets one Telegram message |

## Residual risks (no check closes them)

1. One server runs the team. If reservememes is down, sites stay up on the droplet with their last data.
2. Claude token expiry is unknown; when it expires every run fails, C4 and C14 go red, the watchdog keeps restarting.
3. Quality without a human: the editor's bar and the personas' scores guard posts and pages, but a confident wrong
   post can still go out; the owner reads the channels on return.
4. Threads token expires 02.01.2027 (`bin/threads-refresh-token`).
5. Usage credits are real money: capped at $100 per UTC day for the whole team; the owner's own Claude sessions are
   not counted by this cap.
