#!/usr/bin/env bash
# Daily snapshot of the state that lives outside git:
#   1. product state (products/, .env): local tarball + push of products/ to the private repo OOuph/agent-team-state
#      (no secrets in git: .env stays in the tarball only);
#   2. service data of game products (owner 22.11, ai-battles: «копия записей боёв и журнала»): <SERVICES_DIR>/<slug>/data —
#      battle records, the journal of players' requests, live balance counters. Everything goes into a local tarball;
#      the small text part (journal, battle summaries, counters) is also pushed to the state repo under services/<slug>/data,
#      so it survives the loss of the droplet. Battle bodies (*.replay.json.gz, ~30 MB a day) are too big for git and stay
#      in the local tarball only — an off-server copy of them needs object storage, which is the owner's decision.
# The service copy in the state repo is never pruned: the game deletes old records itself (retention), git keeps them.
# The script must sit in the dispatcher root (cron: `0 2 * * * bash …/backup-state.sh`); every path below is relative to it.

# ---------- parameters (environment overrides; tests point them at a temp tree)
BACKUP_DIR="${BACKUP_DIR:-data/backups}"                        # local tarballs
BACKUP_KEEP="${BACKUP_KEEP:-14}"                                # how many tarballs of each kind are kept
STATE_REPO_DIR="${STATE_REPO_DIR:-data/state-repo}"             # checkout of the private state repo
STATE_REPO_SLUG="${STATE_REPO_SLUG:-OOuph/agent-team-state}"    # github.com/<this>.git, cloned with GITHUB_TOKEN from .env
SERVICES_DIR="${SERVICES_DIR:-/var/lib/agent-team/services}"    # deploy-service.sh VAR_DIR: <slug>/data per service product
SERVICE_GIT_SKIP="${SERVICE_GIT_SKIP:-*.replay.json.gz *.tmp}"  # rsync excludes for the state repo copy (space-separated)

cd "$(dirname "$0")" || exit 1
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup-state: $*"; }
stamp=$(date -u +%Y%m%d-%H%M)
prune() { ls -1t "$BACKUP_DIR"/"$1"-*.tgz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f; }

mkdir -p "$BACKUP_DIR"
log "start: backups in $BACKUP_DIR (keep $BACKUP_KEEP), services in $SERVICES_DIR, state repo $STATE_REPO_DIR"

# 1. product state: local tarball
tar czf "$BACKUP_DIR/products-$stamp.tgz" products .env 2>/dev/null
prune products
log "products tarball: $(du -h "$BACKUP_DIR/products-$stamp.tgz" 2>/dev/null | cut -f1)"

# 2. service data: local tarball of every <slug>/data (battle bodies included)
svc=()
if [ -d "$SERVICES_DIR" ]; then
  for d in "$SERVICES_DIR"/*/data; do [ -d "$d" ] && svc+=("$(basename "$(dirname "$d")")"); done
fi
if [ "${#svc[@]}" -gt 0 ]; then
  args=(); for s in "${svc[@]}"; do args+=("$s/data"); done
  if tar czf "$BACKUP_DIR/services-$stamp.tgz" -C "$SERVICES_DIR" "${args[@]}" 2>/dev/null; then
    log "services tarball (${svc[*]}): $(du -h "$BACKUP_DIR/services-$stamp.tgz" | cut -f1)"
  else
    log "WARNING services tarball is incomplete (a file changed or could not be read while it was written)"
  fi
  prune services
else
  log "no service data under $SERVICES_DIR: only product state is saved"
fi

# 3. private state repo: products/ as before, plus the small text part of the service data
set -a; . ./.env; set +a
S="$STATE_REPO_DIR"
if [ ! -d "$S/.git" ]; then
  git clone -q "https://x-access-token:${GITHUB_TOKEN}@github.com/${STATE_REPO_SLUG}.git" "$S" 2>/dev/null || { log "state repo not available yet (create $STATE_REPO_SLUG); local tarballs only"; exit 0; }
  git -C "$S" config user.name agent-team; git -C "$S" config user.email agent-team@formachines.ai
fi
rsync -a --delete --exclude "team.db-wal" --exclude "team.db-shm" products/ "$S/products/"
cp team.yaml "$S/team.yaml"
for s in "${svc[@]}"; do
  ex=(); for pat in $SERVICE_GIT_SKIP; do ex+=(--exclude "$pat"); done
  mkdir -p "$S/services/$s/data"
  if rsync -a "${ex[@]}" "$SERVICES_DIR/$s/data/" "$S/services/$s/data/"; then
    log "service $s: $(find "$S/services/$s/data" -type f | wc -l | tr -d ' ') files in the state repo copy (skipped: $SERVICE_GIT_SKIP)"
  else
    log "WARNING service $s: rsync to the state repo failed"
  fi
done
git -C "$S" add -A && git -C "$S" commit -q -m "state $(date -u +%Y-%m-%dT%H:%MZ)" 2>/dev/null
out=$(git -C "$S" push -q origin main 2>&1 | tail -1 | sed -E 's#(x-access-token:)[^@]*@#\1***@#g')   # a push error quotes the remote URL: never print the token
[ -n "$out" ] && echo "$out"
log "done: state repo at $(git -C "$S" rev-parse --short HEAD 2>/dev/null)"
