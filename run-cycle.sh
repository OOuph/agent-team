#!/usr/bin/env bash
# hourly cron entry on the server: one cycle for every product; flock prevents overlapping cycles
cd "$(dirname "$0")" || exit 1
mkdir -p data
exec flock -n data/cycle.lock .venv/bin/team cycle --product all >> data/cron.log 2>&1
