#!/usr/bin/env bash
# Sync the dispatcher CODE to the server. Product STATE (backlog, questions, observations, reviews, digests, metrics,
# team.db, decisions.md) lives on the server and is never touched here — incident 02.11: an rsync --delete wiped it.
set -euo pipefail
HOST="${TEAM_HOST:-reservememes}"
DEST="${TEAM_DEST:-agent-team/dispatcher/}"
cd "$(dirname "$0")"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude data --exclude .env --exclude STOP --exclude '__pycache__' --exclude '*.egg-info' \
  --exclude 'products/*/backlog' --exclude 'products/*/questions' --exclude 'products/*/observations' \
  --exclude 'products/*/reviews' --exclude 'products/*/digests' --exclude 'products/*/metrics' \
  --exclude 'products/*/team.db*' --exclude 'products/*/decisions.md' --exclude 'products/*/STOP' \
  ./ "$HOST:$DEST"
echo "synced code to $HOST:$DEST (product state untouched)"
