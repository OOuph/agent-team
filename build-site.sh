#!/usr/bin/env bash
# build-site.sh — the daily build of one product site, run by cron on the server at the hour its VISION promises.
#
#   bash build-site.sh <slug> [build.py args…]     e.g.  bash build-site.sh agentsmd-tracker --max-seconds 2400
#
# Why a separate clone: the dispatcher's working clone (data/repos/<slug>) is where the developer makes task branches;
# a build running there at the same time would mix the two. Each site is built in builds/<slug>/ (outside the repo),
# always from a fresh origin/main. `build.py` writes data/ and public/; both are committed and pushed to main. A push that
# loses the race with a dispatcher merge is retried on top of the new main; if the rebase does not apply cleanly the
# build is run again from the new main (builds are deterministic from data/). A failed build writes a friction record
# (kind env_error, product <slug>) and tells the ops Telegram chat, so the retro sees it and the owner is informed.
# Protected path: the loop never edits this file.
set -u
cd "$(dirname "$0")" || exit 1
ROOT=$(pwd)
SLUG="${1:?slug}"; shift
BUILD_ARGS=("$@")
BUILDS_DIR="${BUILDS_DIR:-$ROOT/builds}"
TIMEOUT_S="${TIMEOUT_S:-3000}"            # hard cap on one build.py run
ATTEMPTS="${ATTEMPTS:-2}"                 # build → push; on a lost race: rebuild on the new main once more
PY="${PY:-python3}"
LOG="$ROOT/data/builds.log"
mkdir -p "$BUILDS_DIR" "$ROOT/data"
set -a; . ./.env 2>/dev/null; set +a

now() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }
log() { echo "$(now) [$SLUG] $*" | tee -a "$LOG"; }

PRODUCT_YAML="$ROOT/products/$SLUG/product.yaml"
[ -f "$PRODUCT_YAML" ] || { log "no product.yaml"; exit 2; }
URL=$("$ROOT/.venv/bin/python" -c "import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))['repo']['url'])" "$PRODUCT_YAML")
BRANCH=$("$ROOT/.venv/bin/python" -c "import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))['repo'].get('branch') or 'main')" "$PRODUCT_YAML")
[ -n "$URL" ] || { log "repo.url missing in product.yaml"; exit 2; }

# token from the environment, never in .git/config
export GIT_TERMINAL_PROMPT=0
GIT=(git -c user.name="agent-team build" -c user.email="agent-team@formachines.ai"
     -c credential.helper= -c "credential.helper=!f() { echo username=x-access-token; echo password=\${GITHUB_TOKEN:-}; }; f")

fail() {
  local msg="$1"; local ts; ts=$(now)
  log "FAILED: $msg"
  local fp; fp=$(date -u +%Y-%m-%d)
  printf '{"ts":"%s","product":"%s","role":"build-cron","kind":"env_error","summary":"daily build failed: %s","task":null,"run_id":null,"fingerprint":"%s:env_error:build:%s"}\n' \
    "$ts" "$SLUG" "$(echo "$msg" | tr -d '"' | cut -c1-200)" "$SLUG" "$fp" >> "$ROOT/data/framework/friction.jsonl"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_OPS_CHAT_ID:-}" ]; then
    curl -s -o /dev/null --data-urlencode "chat_id=${TELEGRAM_OPS_CHAT_ID}" --data-urlencode "text=🏗 ${SLUG}: daily build failed — ${msg}" \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
  fi
  exit 1
}

exec 9>"$BUILDS_DIR/$SLUG.lock"
flock -n 9 || { log "another build of $SLUG is running; skip"; exit 0; }

DIR="$BUILDS_DIR/$SLUG"
if [ ! -d "$DIR/.git" ]; then
  log "cloning $URL"
  "${GIT[@]}" clone -q --branch "$BRANCH" "$URL" "$DIR" || fail "clone failed"
fi
cd "$DIR" || fail "no dir"

for attempt in $(seq 1 "$ATTEMPTS"); do
  "${GIT[@]}" fetch -q origin "$BRANCH" || fail "fetch failed"
  git reset -q --hard "origin/$BRANCH" && git clean -qfd || fail "reset failed"
  base=$(git rev-parse --short HEAD)
  log "attempt $attempt: build on $base: $PY build.py ${BUILD_ARGS[*]:-}"
  t0=$(date +%s)
  timeout "$TIMEOUT_S" "$PY" build.py "${BUILD_ARGS[@]}" >> "$BUILDS_DIR/$SLUG.out" 2>&1
  rc=$?
  log "build.py rc=$rc in $(( $(date +%s) - t0 ))s (log: builds/$SLUG.out)"
  [ "$rc" -eq 0 ] || fail "build.py rc=$rc (timeout ${TIMEOUT_S}s = rc 124)"
  git add -A data public 2>/dev/null
  if git diff --cached --quiet; then log "no changes to commit"; exit 0; fi
  n=$(git diff --cached --name-only | wc -l)
  "${GIT[@]}" commit -q -m "build: $(date -u +%Y-%m-%d\ %H:%M) UTC (cron, $n files)" || fail "commit failed"
  if "${GIT[@]}" push -q origin "HEAD:$BRANCH"; then
    log "pushed $(git rev-parse --short HEAD) ($n files)"; exit 0
  fi
  log "push rejected (main moved); rebasing"
  "${GIT[@]}" fetch -q origin "$BRANCH"
  if "${GIT[@]}" rebase -q "origin/$BRANCH" && "${GIT[@]}" push -q origin "HEAD:$BRANCH"; then
    log "pushed after rebase $(git rev-parse --short HEAD)"; exit 0
  fi
  git rebase --abort 2>/dev/null
  log "rebase did not apply; rebuilding on the new main"
done
fail "could not push after $ATTEMPTS attempts"
