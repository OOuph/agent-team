#!/usr/bin/env bash
# hourly cron entry on the server: one cycle for every product.
# The locks are taken by `team cycle` itself (team/locks.py, SCHEME.md §3): data/cycle.lock shared for the whole pass
# (the dispatcher code is not replaced under it), data/locks/hourly-pass.lock so that two passes never overlap, and
# data/locks/product-<slug>.lock per product — a product whose cycle is already running (run-cycle-fast.sh) is skipped.
# Exit 75: nothing was started (another pass is running, or the code is being updated).
# After a pass that exits 0 the current commit is recorded as "last good" for watchdog.sh.
cd "$(dirname "$0")" || exit 1
mkdir -p data data/framework
.venv/bin/team cycle --product all >> data/cron.log 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ -d .git ]; then
  git rev-parse HEAD > data/framework/last-good 2>/dev/null
fi
exit $rc
