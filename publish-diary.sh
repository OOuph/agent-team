#!/usr/bin/env bash
# Rebuilds the team diary site from the public export of all products and pushes it (no model, deterministic).
# Runs on the dispatcher server after the morning digest; the diary's developer role handles everything else.
set -euo pipefail
cd "$(dirname "$0")"
DIARY_SLUG="${DIARY_SLUG:-agent-team-diary}"
REPO="data/repos/$DIARY_SLUG"
set -a; . ./.env; set +a
.venv/bin/team export-public --out data/public --days 30
.venv/bin/team friction --out data/public/framework >/dev/null || true
if [ ! -d "$REPO/.git" ]; then
  url=$(.venv/bin/python -c "import yaml;print(yaml.safe_load(open('products/$DIARY_SLUG/product.yaml'))['repo']['url'])")
  git clone -q "${url/https:\/\/github.com\//https://x-access-token:${GITHUB_TOKEN}@github.com/}" "$REPO"
  git -C "$REPO" config user.name agent-team; git -C "$REPO" config user.email agent-team@formachines.ai
fi
git -C "$REPO" checkout -q main && git -C "$REPO" pull -q --ff-only origin main
mkdir -p "$REPO/data/public"
rsync -a --delete --exclude "$DIARY_SLUG" data/public/ "$REPO/data/public/"
( cd "$REPO" && python3 build.py )
if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
  git -C "$REPO" add -A && git -C "$REPO" commit -q -m "diary: $(date -u +%Y-%m-%dT%H:%MZ) rebuild from public export" && git -C "$REPO" push -q origin main
  echo "diary pushed"
else
  echo "diary unchanged"
fi
