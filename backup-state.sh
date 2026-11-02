#!/usr/bin/env bash
# daily snapshot of product state: local tarball (keeps 14) + push to the private repo OOuph/agent-team-state when it exists (no secrets in git)
cd "$(dirname "$0")" || exit 1
mkdir -p data/backups
tar czf "data/backups/products-$(date -u +%Y%m%d-%H%M).tgz" products .env 2>/dev/null
ls -1t data/backups/products-*.tgz | tail -n +15 | xargs -r rm -f
set -a; . ./.env; set +a
S=data/state-repo
if [ ! -d "$S/.git" ]; then
  git clone -q "https://x-access-token:${GITHUB_TOKEN}@github.com/OOuph/agent-team-state.git" "$S" 2>/dev/null || { echo "state repo not available yet (create OOuph/agent-team-state); local tarball only"; exit 0; }
  git -C "$S" config user.name agent-team; git -C "$S" config user.email agent-team@formachines.ai
fi
rsync -a --delete --exclude "team.db-wal" --exclude "team.db-shm" products/ "$S/products/"
cp team.yaml "$S/team.yaml"
git -C "$S" add -A && git -C "$S" commit -q -m "state $(date -u +%Y-%m-%dT%H:%MZ)" 2>/dev/null; git -C "$S" push -q origin main 2>&1 | tail -1
