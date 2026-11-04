#!/usr/bin/env bash
# autonomy-check.sh — daily self-check of the whole contour (cron 08:15 UTC), short result into the ops chat.
# Owner 04.11: "check that everything works without me, formalise the protocol". Protocol: docs/autonomy-check.md.
set -u
cd "$(dirname "$0")" || exit 1
set -a; . ./.env 2>/dev/null; set +a
mkdir -p data
echo "== autonomy-check $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> data/autonomy.log
.venv/bin/team autonomy-check --send >> data/autonomy.log 2>&1
echo "rc=$?" >> data/autonomy.log
