#!/usr/bin/env bash
# Deploy the dispatcher code to the server. The server runs from a git clone of OOuph/agent-team (so the self-improvement
# loop can fast-forward it after a merge): this script pushes the local main and fast-forwards the server checkout.
# Product state (products/<slug>/backlog… , team.db) and data/ are not in git and are never touched.
set -euo pipefail
HOST="${TEAM_HOST:-reservememes}"
DEST="${TEAM_DEST:-agent-team/dispatcher}"
cd "$(dirname "$0")"
if [ -n "$(git status --porcelain)" ]; then
  echo "local changes not committed; commit first" >&2; exit 1
fi
git push -q origin main
ssh -o BatchMode=yes "$HOST" "cd $DEST && git fetch -q origin main && git merge -q --ff-only origin/main && git rev-parse --short HEAD && .venv/bin/pip install -q -e . >/dev/null 2>&1 || true"
echo "deployed to $HOST:$DEST (git fast-forward; product state untouched)"
