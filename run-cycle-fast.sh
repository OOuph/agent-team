#!/usr/bin/env bash
# Fast cron entry on the server, one line for every product:  */5 * * * * bash …/run-cycle-fast.sh
# `team cycle --due` runs a cycle only for the products whose product.yaml has `cycle: {every_min: N}` (5..60), no STOP
# file, and whose last finished cycle is at least N minutes old. A product without the setting is left to the hourly
# run-cycle.sh. It does not wait for the hourly pass (friction F17): `team cycle` takes a lock per product
# (data/locks/product-<slug>.lock, team/locks.py), so a due product starts unless its own cycle is already running.
# data/cycle.lock is held shared by every running cycle and exclusively by whatever replaces the dispatcher code
# (update-code.sh, watchdog.sh, the self-merge); while that is waiting or running, this exits 75 and the next tick looks
# again. Unlike run-cycle.sh it does not record data/framework/last-good — the rollback point of watchdog.sh stays tied
# to the hourly pass over every product.
# Optional argument: a product slug, to limit the check to that product (the pre-PR server workaround was called so).
cd "$(dirname "$0")" || exit 1
SLUG="${1:-all}"                              # all = every product that has cycle.every_min
LOG="${CYCLE_LOG:-data/cron.log}"
TEAM="${TEAM_BIN:-.venv/bin/team}"
mkdir -p data
[ -f STOP ] && exit 0                         # global STOP: nothing runs (per-product STOP is checked by `--due`)
"$TEAM" cycle --due --product "$SLUG" >> "$LOG" 2>&1
