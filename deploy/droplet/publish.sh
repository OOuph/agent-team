#!/usr/bin/env bash
# publish.sh — pull every pilot site repo listed in sites.tsv and publish its
# prebuilt public_dir atomically (symlink swap). Runs from agent-team-publish.timer
# every 5 minutes and from add-site.sh. Idempotent: an unchanged HEAD is skipped.
#
# Usage: publish.sh [--site <slug>] [--force]
#   --site <slug>  process only this slug
#   --force        republish even if HEAD did not change
#
# Exit codes: 0 all sites ok or skipped · 1 at least one site failed (clone/rsync/smoke).
# Journal: /var/log/agent-team/publish.log · state: /var/lib/agent-team/state/<slug>.json
set -euo pipefail
umask 022

# ---------------------------------------------------------------- settings
SITES_FILE="${SITES_FILE:-/etc/agent-team/sites.tsv}"
SITES_DIR="${SITES_DIR:-/opt/agent-team/sites}"          # git clones, one per slug
WWW_DIR="${WWW_DIR:-/var/www}"                           # /var/www/<slug> -> /var/www/<slug>-releases/<sha>
STATE_DIR="${STATE_DIR:-/var/lib/agent-team/state}"
LOG_FILE="${LOG_FILE:-/var/log/agent-team/publish.log}"
LOCK_FILE="${LOCK_FILE:-/run/lock/agent-team-publish.lock}"
LOCK_WAIT_SEC="${LOCK_WAIT_SEC:-300}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
CLONE_DEPTH="${CLONE_DEPTH:-50}"
SMOKE_TIMEOUT_SEC="${SMOKE_TIMEOUT_SEC:-15}"
SMOKE_RESOLVE_IP="${SMOKE_RESOLVE_IP:-127.0.0.1}"

FORCE=0
ONLY_SLUG=""

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --site)
      [ $# -ge 2 ] || { echo "ERROR: --site needs a slug" >&2; exit 64; }
      ONLY_SLUG="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# log <message>: stdout (journal) + append to LOG_FILE
log() {
  local line
  line="$(now_iso) $*"
  echo "$line"
  echo "$line" >> "$LOG_FILE"
}

# write_state <slug> <host> <sha> <release> <smoke_status> <smoke_http_code> <published_at>
write_state() {
  python3 - "$STATE_DIR/$1.json" "$@" <<'PY'
import json, os, sys
path, slug, host, sha, release, smoke, code, published_at = sys.argv[1:9]
data = {
    "slug": slug,
    "host": host,
    "sha": sha,
    "release": release,
    "published_at": published_at,
    "smoke": smoke,            # ok | fail
    "smoke_http_code": code,   # "200", "000" when nothing answered
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
PY
}

# swap_symlink <target_dir> <link_path>: atomic replace of a symlink. rename(2) does
# not follow symlinks, so a reader sees either the old or the new release, never none.
swap_symlink() {
  local target="$1" link="$2" tmp
  tmp="$link.tmp.$$"
  ln -sfn "$target" "$tmp"
  python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp" "$link"
}

# smoke <host> -> prints the HTTP code ("000" if nothing answered); never fails the script
smoke() {
  local host="$1" code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$SMOKE_TIMEOUT_SEC" \
            --resolve "$host:443:$SMOKE_RESOLVE_IP" "https://$host/" 2>/dev/null || true)"
  [ -n "$code" ] || code="000"
  echo "$code"
}

# publish_site <slug> <host> <repo> <branch> <public_dir>
# Runs inside a subshell with set -e. Exit: 0 published · 10 skipped (same HEAD) ·
# 20 published but smoke failed · anything else = error.
publish_site() {
  local slug="$1" host="$2" repo="$3" branch="$4" public_dir="$5"
  local repo_dir="$SITES_DIR/$slug"
  local releases="$WWW_DIR/$slug-releases"
  local live="$WWW_DIR/$slug"
  local sha short release current src tmp code status ts old

  echo "[$slug] repo=$repo branch=$branch public_dir=$public_dir host=$host"

  if [ -e "$live" ] && [ ! -L "$live" ]; then
    echo "[$slug] ERROR: $live exists and is not a symlink; refusing to touch it" >&2
    exit 1
  fi

  # --- 1. clone or fetch --------------------------------------------------
  if [ ! -d "$repo_dir/.git" ]; then
    if [ -e "$repo_dir" ]; then
      echo "[$slug] removing incomplete checkout $repo_dir"
      rm -rf "$repo_dir"
    fi
    echo "[$slug] cloning (depth $CLONE_DEPTH) into $repo_dir"
    git clone --quiet --depth "$CLONE_DEPTH" --branch "$branch" "$repo" "$repo_dir"
  else
    echo "[$slug] fetching origin/$branch"
    git -C "$repo_dir" remote set-url origin "$repo"
    git -C "$repo_dir" fetch --quiet --depth "$CLONE_DEPTH" origin \
      "+refs/heads/$branch:refs/remotes/origin/$branch"
  fi
  git -C "$repo_dir" reset --quiet --hard "origin/$branch"

  sha="$(git -C "$repo_dir" rev-parse HEAD)"
  short="${sha:0:12}"
  release="$releases/$short"
  current=""
  [ -L "$live" ] && current="$(readlink "$live")"
  echo "[$slug] HEAD=$sha current=${current:-<none>}"

  # --- 2. skip when nothing changed ----------------------------------------
  if [ "$FORCE" = 0 ] && [ "$current" = "$release" ] && [ -d "$release" ]; then
    echo "[$slug] HEAD unchanged, release in place — skipping"
    log "slug=$slug sha=$short action=skipped"
    exit 10
  fi

  # --- 3. stage the release -------------------------------------------------
  src="$repo_dir/$public_dir"
  if [ ! -d "$src" ]; then
    echo "[$slug] ERROR: $src does not exist — is $public_dir committed in the repo?" >&2
    exit 1
  fi
  if [ ! -f "$src/index.html" ]; then
    echo "[$slug] WARNING: $src/index.html missing; publishing anyway" >&2
  fi
  mkdir -p "$releases"
  tmp="$releases/.$short.tmp"
  rm -rf "$tmp"
  echo "[$slug] rsync $src/ -> $tmp/"
  rsync -a --delete "$src/" "$tmp/"
  rm -rf "$release"
  mv "$tmp" "$release"
  chmod 755 "$release"

  # --- 4. atomic switch -----------------------------------------------------
  echo "[$slug] switching $live -> $release"
  swap_symlink "$release" "$live"
  ts="$(now_iso)"

  # --- 5. prune old releases (keep KEEP_RELEASES newest, never the live one) --
  while IFS= read -r old; do
    old="${old%/}"
    [ -n "$old" ] || continue
    [ "$old" = "$release" ] && continue
    echo "[$slug] pruning old release $old"
    rm -rf "$old"
  done < <(ls -1dt "$releases"/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) || true)

  # --- 6. smoke -------------------------------------------------------------
  code="$(smoke "$host")"
  if [ "$code" = "200" ]; then status="ok"; else status="fail"; fi
  echo "[$slug] smoke https://$host/ via $SMOKE_RESOLVE_IP -> HTTP $code ($status)"

  write_state "$slug" "$host" "$sha" "$release" "$status" "$code" "$ts"
  log "slug=$slug sha=$short action=published release=$release smoke=$status http=$code"
  [ "$status" = "ok" ] || exit 20
}

# ---------------------------------------------------------------- main
echo "== agent-team publish $(now_iso) sites=$SITES_FILE force=$FORCE only=${ONLY_SLUG:-all}"
mkdir -p "$SITES_DIR" "$WWW_DIR" "$STATE_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")"

if [ ! -f "$SITES_FILE" ]; then
  echo "no $SITES_FILE — nothing to publish (add sites with add-site.sh)"
  exit 0
fi

for bin in git rsync curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: $bin not found" >&2; exit 1; }
done

# one publish at a time (timer vs. manual run / add-site.sh)
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -w "$LOCK_WAIT_SEC" 9; then
    echo "ERROR: could not take $LOCK_FILE within ${LOCK_WAIT_SEC}s — another publish is running" >&2
    exit 1
  fi
fi

n_ok=0; n_skip=0; n_fail=0; n_smoke=0; failed_slugs=""
# sites.tsv is read on fd 3 so git/curl inside the loop cannot swallow lines from stdin
exec 3< "$SITES_FILE"
while IFS= read -r -u 3 line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|'#'*) continue ;; esac
  IFS=$'\t' read -r slug host repo branch public_dir _rest <<< "$line"
  branch="${branch:-main}"
  public_dir="${public_dir:-public}"
  if [ -n "$ONLY_SLUG" ] && [ "$slug" != "$ONLY_SLUG" ]; then
    continue
  fi
  if ! [[ "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]] || [ -z "$host" ] || [ -z "$repo" ]; then
    echo "ERROR: bad line in $SITES_FILE (need slug<TAB>host<TAB>repo[<TAB>branch[<TAB>public_dir]]): $line" >&2
    n_fail=$((n_fail + 1)); failed_slugs="$failed_slugs ${slug:-?}"
    continue
  fi

  set +e
  ( set -e; publish_site "$slug" "$host" "$repo" "$branch" "$public_dir" )
  rc=$?
  set -e
  case "$rc" in
    0)  n_ok=$((n_ok + 1)) ;;
    10) n_skip=$((n_skip + 1)) ;;
    20) n_smoke=$((n_smoke + 1)); failed_slugs="$failed_slugs $slug"
        echo "[$slug] published but smoke FAILED (see above)" >&2 ;;
    *)  n_fail=$((n_fail + 1)); failed_slugs="$failed_slugs $slug"
        log "slug=$slug action=error rc=$rc"
        echo "[$slug] ERROR: publish failed with rc=$rc" >&2 ;;
  esac
done
exec 3<&-

if [ -n "$ONLY_SLUG" ] && [ $((n_ok + n_skip + n_fail + n_smoke)) -eq 0 ]; then
  echo "ERROR: slug '$ONLY_SLUG' not found in $SITES_FILE" >&2
  exit 1
fi

echo "== done: published=$((n_ok + n_smoke)) skipped=$n_skip smoke_failed=$n_smoke errors=$n_fail${failed_slugs:+ (${failed_slugs# })}"
[ $((n_fail + n_smoke)) -eq 0 ]
