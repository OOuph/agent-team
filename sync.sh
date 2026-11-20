#!/usr/bin/env bash
# Deploy the dispatcher code to the server. The server runs from a git clone of OOuph/agent-team (so the self-improvement
# loop can fast-forward it after a merge): this script pushes the local main and fast-forwards the server checkout.
# Product state (products/<slug>/backlog… , team.db) and data/ are not in git and are never touched.
set -euo pipefail
HOST="${TEAM_HOST:-agentteam}"
DEST="${TEAM_DEST:-agent-team/dispatcher}"
cd "$(dirname "$0")"
if [ -n "$(git status --porcelain)" ]; then
  echo "local changes not committed; commit first" >&2; exit 1
fi
git push -q origin main
# update-code.sh on the server waits until no cycle is running (data/cycle.lock, SCHEME.md §3) and fast-forwards the
# checkout. A server whose checkout is older than that script is updated the old way, under the same lock.
WAIT="${UPDATE_WAIT_S:-3600}"                 # how long the server waits for the running cycles, seconds
ssh -o BatchMode=yes "$HOST" "cd $DEST && if [ -f update-code.sh ]; then UPDATE_WAIT_S=$WAIT bash update-code.sh; else flock -w $WAIT data/cycle.lock sh -c 'git fetch -q origin main && git merge -q --ff-only origin/main && git rev-parse --short HEAD && (.venv/bin/pip install -q -e . >/dev/null 2>&1 || true)'; fi"
echo "deployed to $HOST:$DEST (git fast-forward; product state untouched)"
