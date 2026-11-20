#!/usr/bin/env bash
# Fast cron entry on the server, one line for every product:  */5 * * * * bash …/run-cycle-fast.sh
# `team cycle --due` runs a cycle only for the products whose product.yaml has `cycle: {every_min: N}` (5..60), no STOP
# file, and whose last finished cycle is at least N minutes old. A product without the setting is left to the hourly
# run-cycle.sh. Same lock as run-cycle.sh (flock -n data/cycle.lock): while the hourly cycle or another fast one runs,
# this exits at once, so cycles never overlap. Unlike run-cycle.sh it does not record data/framework/last-good — the
# rollback point of watchdog.sh stays tied to the hourly pass over every product.
# Optional argument: a product slug, to limit the check to that product (the pre-PR server workaround was called so).
cd "$(dirname "$0")" || exit 1
SLUG="${1:-all}"                              # all = every product that has cycle.every_min
LOCK="${CYCLE_LOCK:-data/cycle.lock}"         # must be the lock run-cycle.sh and watchdog.sh use
LOG="${CYCLE_LOG:-data/cron.log}"
TEAM="${TEAM_BIN:-.venv/bin/team}"
mkdir -p data
[ -f STOP ] && exit 0                         # global STOP: nothing runs (per-product STOP is checked by `--due`)
flock -n "$LOCK" "$TEAM" cycle --due --product "$SLUG" >> "$LOG" 2>&1
rc=$?
# flock -n exits 1 when the lock is held: a cycle is running, the next tick will look again.
exit $rc
