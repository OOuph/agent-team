#!/usr/bin/env bash
# hourly cron entry on the server: one cycle for every product; flock prevents overlapping cycles.
# After a cycle that exits 0 the current commit is recorded as "last good" for watchdog.sh.
cd "$(dirname "$0")" || exit 1
mkdir -p data data/framework
flock -n data/cycle.lock .venv/bin/team cycle --product all >> data/cron.log 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ -d .git ]; then
  git rev-parse HEAD > data/framework/last-good 2>/dev/null
fi
exit $rc
