#!/usr/bin/env bash
# deploy-service.sh — deploy one long-running service (a game server) from its git repository as a new release,
# blue/green on two local ports, switch the nginx upstream only after the health path answers 200, keep the last
# releases, roll back to the previous one. Runs as root on the droplet; the dispatcher calls it through sudo
# (/etc/sudoers.d/agent-team-deploy, installed by install.sh) after every merge of a game product.
#
# Usage: deploy-service.sh <slug> [--sha <commit>] [--rollback] [--force] [--no-fetch]
#   slug        service registered by add-service.sh (/etc/agent-team/services/<slug>.conf)
#   --sha       deploy this commit (default: origin/<branch> HEAD)
#   --rollback  deploy the previous release again (state.previous), no fetch / install / build
#   --force     redeploy even when the requested commit is the live one
#   --no-fetch  do not fetch; use the clone as it is (tests, air-gapped runs)
#
# Steps: lock → fetch → stage releases/<sha12> (rsync, no .git) → install (npm ci / npm install when package.json is
# there) → build (BUILD_CMD) → write the instance env for the idle colour → systemctl start agent-team-svc@<slug>-<colour>
# → poll http://127.0.0.1:<port><HEALTH_PATH> until 200 → write the nginx upstream → nginx -t → reload → stop the old
# colour after DRAIN_SEC → prune releases (KEEP_RELEASES, never the live or the previous one) → state + journal.
# A failed health check stops the new instance and leaves the live one untouched (exit 20). A failed nginx -t restores
# the previous upstream file (exit 21). The last stdout line on success: `== deployed slug=… sha=… colour=… port=… url=…`.
# Exit codes: 0 deployed · 10 nothing to do (same commit live) · 20 health failed · 21 nginx failed · 64 usage · 1 other.
# Journal: /var/log/agent-team/deploy.log · state: /var/lib/agent-team/state/svc-<slug>.json
set -euo pipefail
umask 022

# ---------------------------------------------------------------- settings (every path and timeout is overridable)
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
SERVICES_ETC="${SERVICES_ETC:-$ETC_DIR/services}"             # <slug>.conf (shell vars) + <slug>.secrets.env (owner, never in git)
SERVICES_DIR="${SERVICES_DIR:-/opt/agent-team/services}"       # <slug>/repo — the git clone
VAR_DIR="${VAR_DIR:-/var/lib/agent-team/services}"             # <slug>/releases/<sha12>, <slug>/data, <slug>/instances/<instance>.env
STATE_DIR="${STATE_DIR:-/var/lib/agent-team/state}"
LOG_FILE="${LOG_FILE:-/var/log/agent-team/deploy.log}"
LOCK_DIR="${LOCK_DIR:-/run/lock}"
LOCK_WAIT_SEC="${LOCK_WAIT_SEC:-600}"
NGINX_CONFD="${NGINX_CONFD:-/etc/nginx/conf.d}"                 # upstream file agent-team-svc-<slug>.conf lives here
SYSTEMCTL="${SYSTEMCTL:-systemctl}"                             # tests point this at a fake
NGINX_BIN="${NGINX_BIN:-nginx}"
JOURNALCTL="${JOURNALCTL:-journalctl}"
UNIT_PREFIX="${UNIT_PREFIX:-agent-team-svc@}"                   # instance = <slug>-<colour>
SKIP_NGINX="${SKIP_NGINX:-0}"                                   # 1 = no upstream file, no nginx (tests without nginx)
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"
CLONE_DEPTH="${CLONE_DEPTH:-50}"
START_TIMEOUT_SEC="${START_TIMEOUT_SEC:-90}"
HEALTH_POLL_SEC="${HEALTH_POLL_SEC:-1}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-5}"
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"
DRAIN_SEC="${DRAIN_SEC:-5}"                                     # old colour keeps running this long after the switch (clients reconnect)
KEEP_RELEASES="${KEEP_RELEASES:-3}"
INSTALL_TIMEOUT_SEC="${INSTALL_TIMEOUT_SEC:-600}"
BUILD_TIMEOUT_SEC="${BUILD_TIMEOUT_SEC:-600}"
NODE_ENV_VALUE="${NODE_ENV_VALUE:-production}"
SVC_USER="${SVC_USER:-ubuntu}"                                  # owner of releases/data; the unit runs as this user

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

SLUG=""; WANT_SHA=""; ROLLBACK=0; FORCE=0; FETCH=1
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) [ $# -ge 2 ] || { echo "ERROR: --sha needs a commit" >&2; exit 64; }; WANT_SHA="$2"; shift ;;
    --rollback) ROLLBACK=1 ;;
    --force) FORCE=1 ;;
    --no-fetch) FETCH=0 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "ERROR: unknown option $1" >&2; usage >&2; exit 64 ;;
    *) [ -z "$SLUG" ] || { echo "ERROR: one slug only" >&2; exit 64; }; SLUG="$1" ;;
  esac
  shift
done
[ -n "$SLUG" ] || { usage >&2; exit 64; }
if ! [[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then echo "ERROR: slug '$SLUG' must match [a-z0-9][a-z0-9-]*" >&2; exit 64; fi
if [ -n "$WANT_SHA" ] && ! [[ "$WANT_SHA" =~ ^[0-9a-f]{7,40}$ ]]; then echo "ERROR: --sha '$WANT_SHA' is not a commit id" >&2; exit 64; fi
if [ "$REQUIRE_ROOT" = 1 ] && [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root (the dispatcher uses sudo -n)" >&2; exit 1; fi

CONF="$SERVICES_ETC/$SLUG.conf"
[ -f "$CONF" ] || { echo "ERROR: $CONF missing — register the service with add-service.sh first" >&2; exit 1; }
# the conf is plain shell assignments written by add-service.sh: HOST, REPO, BRANCH, RUN_CMD, BUILD_CMD, HEALTH_PATH, PORT_A, PORT_B
HOST=""; REPO=""; BRANCH="main"; RUN_CMD=""; BUILD_CMD=""; INSTALL_CMD=""; HEALTH_PATH="/healthz"; PORT_A=""; PORT_B=""
# shellcheck disable=SC1090
. "$CONF"
[ -n "$REPO" ] && [ -n "$RUN_CMD" ] && [ -n "$PORT_A" ] && [ -n "$PORT_B" ] || { echo "ERROR: $CONF must set REPO, RUN_CMD, PORT_A, PORT_B" >&2; exit 1; }
for p in "$PORT_A" "$PORT_B"; do [[ "$p" =~ ^[0-9]{2,5}$ ]] || { echo "ERROR: bad port '$p' in $CONF" >&2; exit 1; }; done
case "$HEALTH_PATH" in /*) ;; *) HEALTH_PATH="/$HEALTH_PATH" ;; esac
SECRETS_FILE="$SERVICES_ETC/$SLUG.secrets.env"

REPO_DIR="$SERVICES_DIR/$SLUG/repo"
RELEASES="$VAR_DIR/$SLUG/releases"
DATA_DIR="$VAR_DIR/$SLUG/data"
INSTANCES="$VAR_DIR/$SLUG/instances"
STATE="$STATE_DIR/svc-$SLUG.json"
UPSTREAM="$NGINX_CONFD/agent-team-svc-$SLUG.conf"
LOCK_FILE="$LOCK_DIR/agent-team-deploy-$SLUG.lock"

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { local line; line="$(now_iso) slug=$SLUG $*"; echo "$line"; echo "$line" >> "$LOG_FILE"; }
# with_timeout <sec> <cmd...>: coreutils timeout when present (the droplet), plain run otherwise (macOS test runs)
with_timeout() { local s="$1"; shift; if command -v timeout >/dev/null 2>&1; then timeout "$s" "$@"; else "$@"; fi; }

# state_get <key> → value or "" (python keeps the JSON honest; no jq on the droplet)
state_get() {
  [ -f "$STATE" ] || { echo ""; return 0; }
  python3 - "$STATE" "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(""); sys.exit(0)
v = d
for k in sys.argv[2].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print("" if v is None else v)
PY
}

# write_state <sha> <release> <colour> <port> <health_code> <prev_sha> <prev_release> <prev_colour> <prev_port>
write_state() {
  mkdir -p "$STATE_DIR"
  python3 - "$STATE" "$SLUG" "$HOST" "$@" "$(now_iso)" <<'PY'
import json, os, sys
path, slug, host, sha, release, colour, port, code, psha, prel, pcol, pport, ts = sys.argv[1:14]
data = {"slug": slug, "host": host, "sha": sha, "release": release, "colour": colour, "port": int(port),
        "deployed_at": ts, "health_http_code": code,
        "previous": ({"sha": psha, "release": prel, "colour": pcol, "port": int(pport)} if prel else None)}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2); f.write("\n")
os.replace(tmp, path)
PY
}

# health <port> → HTTP code ("000" when nothing answered)
health() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$HEALTH_TIMEOUT_SEC" "http://$HEALTH_HOST:$1$HEALTH_PATH" 2>/dev/null || true)"
  [ -n "$code" ] || code="000"
  echo "$code"
}

unit_of() { echo "$UNIT_PREFIX$SLUG-$1"; }

# instance_env <colour> <port> <release_dir> <sha>: what svc-run.sh sources before exec'ing RUN_CMD
instance_env() {
  local colour="$1" port="$2" release="$3" sha="$4" f
  mkdir -p "$INSTANCES"
  f="$INSTANCES/$SLUG-$colour.env"
  {
    echo "# written by deploy-service.sh $(now_iso); sourced by svc-run.sh for $(unit_of "$colour")"
    printf 'RELEASE_DIR=%q\n' "$release"
    printf 'RUN_CMD=%q\n' "$RUN_CMD"
    printf 'PORT=%q\n' "$port"
    printf 'DATA_DIR=%q\n' "$DATA_DIR"
    printf 'RELEASE_SHA=%q\n' "$sha"
    printf 'NODE_ENV=%q\n' "$NODE_ENV_VALUE"
    printf 'SERVICE_SLUG=%q\n' "$SLUG"
    printf 'SERVICE_HOST=%q\n' "$HOST"
    printf 'SECRETS_FILE=%q\n' "$SECRETS_FILE"
  } > "$f.tmp"
  mv "$f.tmp" "$f"
  echo "$f"
}

stop_instance() {
  local colour="$1"
  "$SYSTEMCTL" stop "$(unit_of "$colour")" >/dev/null 2>&1 || true
}

journal_tail() {
  "$JOURNALCTL" -u "$(unit_of "$1")" -n 40 --no-pager 2>/dev/null || true
}

# write_upstream <port>: the vhost (service.conf.template) proxies to upstream svc_<slug>; only this file changes per deploy
write_upstream() {
  local port="$1" tmp
  tmp="$UPSTREAM.tmp"
  {
    echo "# agent-team service $SLUG — written by deploy-service.sh $(now_iso); the live colour's port"
    echo "upstream svc_${SLUG//-/_} {"
    echo "    server $HEALTH_HOST:$port;"
    echo "    keepalive 16;"
    echo "}"
  } > "$tmp"
  mv "$tmp" "$UPSTREAM"
}

# ---------------------------------------------------------------- main
mkdir -p "$SERVICES_DIR/$SLUG" "$RELEASES" "$DATA_DIR" "$INSTANCES" "$STATE_DIR" "$LOCK_DIR" "$(dirname "$LOG_FILE")"
for bin in git rsync curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: $bin not found" >&2; exit 1; }
done
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -w "$LOCK_WAIT_SEC" 9; then
    echo "ERROR: could not take $LOCK_FILE within ${LOCK_WAIT_SEC}s — another deploy of $SLUG is running" >&2; exit 1
  fi
fi

cur_sha="$(state_get sha)"; cur_release="$(state_get release)"; cur_colour="$(state_get colour)"; cur_port="$(state_get port)"
echo "== deploy-service $SLUG host=${HOST:-?} live=${cur_sha:0:12}${cur_colour:+ ($cur_colour:$cur_port)} rollback=$ROLLBACK"

# --- 1. which release -------------------------------------------------------------------------------------------
if [ "$ROLLBACK" = 1 ]; then
  sha="$(state_get previous.sha)"; release="$(state_get previous.release)"
  [ -n "$release" ] && [ -d "$release" ] || { echo "ERROR: no previous release to roll back to (state: $STATE)" >&2; exit 1; }
  echo "[$SLUG] rollback to ${sha:0:12} ($release)"
else
  if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[$SLUG] cloning $REPO ($BRANCH) into $REPO_DIR"
    rm -rf "$REPO_DIR"
    git clone --quiet --branch "$BRANCH" "$REPO" "$REPO_DIR"
  elif [ "$FETCH" = 1 ]; then
    echo "[$SLUG] fetching origin/$BRANCH"
    git -C "$REPO_DIR" remote set-url origin "$REPO"
    git -C "$REPO_DIR" fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
  fi
  if [ -n "$WANT_SHA" ]; then
    sha="$(git -C "$REPO_DIR" rev-parse --verify "$WANT_SHA^{commit}" 2>/dev/null || true)"
    [ -n "$sha" ] || { echo "ERROR: commit $WANT_SHA is not in $REPO_DIR (fetched origin/$BRANCH)" >&2; exit 1; }
  else
    sha="$(git -C "$REPO_DIR" rev-parse "origin/$BRANCH" 2>/dev/null || git -C "$REPO_DIR" rev-parse HEAD)"
  fi
  release="$RELEASES/${sha:0:12}"
  if [ "$FORCE" = 0 ] && [ "$sha" = "$cur_sha" ] && [ -d "$release" ] && [ "$(health "$cur_port")" = "200" ]; then
    echo "[$SLUG] ${sha:0:12} is live and healthy — nothing to do"
    log "sha=${sha:0:12} action=skipped"
    exit 10
  fi
  # --- 2. stage ---------------------------------------------------------------------------------------------------
  if [ -f "$release/.built" ] && [ "$FORCE" = 0 ]; then
    echo "[$SLUG] release $release already built — reusing"
  else
    echo "[$SLUG] staging $release from $sha"
    git -C "$REPO_DIR" checkout --quiet --detach "$sha"
    rm -rf "$release.tmp"
    rsync -a --delete --exclude .git --exclude node_modules "$REPO_DIR/" "$release.tmp/"
    rm -rf "$release"
    mv "$release.tmp" "$release"
    echo "$sha" > "$release/.release-sha"
    # --- 3. install -------------------------------------------------------------------------------------------------
    install_cmd="$INSTALL_CMD"
    if [ -z "$install_cmd" ] && [ -f "$release/package.json" ]; then
      if [ -f "$release/package-lock.json" ]; then install_cmd="npm ci --no-audit --no-fund"; else install_cmd="npm install --no-audit --no-fund"; fi
    fi
    if [ -n "$install_cmd" ]; then
      echo "[$SLUG] install: $install_cmd"
      if ! (cd "$release" && with_timeout "$INSTALL_TIMEOUT_SEC" env NODE_ENV=development bash -c "$install_cmd" > "$release/.install.log" 2>&1); then
        echo "[$SLUG] ERROR: install failed; tail of $release/.install.log:" >&2; tail -n 30 "$release/.install.log" >&2
        log "sha=${sha:0:12} action=error step=install"; exit 1
      fi
    fi
    # --- 4. build ---------------------------------------------------------------------------------------------------
    if [ -n "$BUILD_CMD" ]; then
      echo "[$SLUG] build: $BUILD_CMD"
      if ! (cd "$release" && with_timeout "$BUILD_TIMEOUT_SEC" env NODE_ENV="$NODE_ENV_VALUE" bash -c "$BUILD_CMD" > "$release/.build.log" 2>&1); then
        echo "[$SLUG] ERROR: build failed; tail of $release/.build.log:" >&2; tail -n 30 "$release/.build.log" >&2
        log "sha=${sha:0:12} action=error step=build"; exit 1
      fi
    fi
    date -u +%Y-%m-%dT%H:%M:%SZ > "$release/.built"
    chown -R "$SVC_USER":"$SVC_USER" "$release" "$DATA_DIR" 2>/dev/null || true
  fi
fi

# --- 5. start the idle colour ---------------------------------------------------------------------------------------
if [ "$cur_colour" = "a" ]; then colour="b"; port="$PORT_B"; else colour="a"; port="$PORT_A"; fi
envf="$(instance_env "$colour" "$port" "$release" "$sha")"
unit="$(unit_of "$colour")"
echo "[$SLUG] starting $unit on :$port (env $envf)"
stop_instance "$colour"
"$SYSTEMCTL" start "$unit"
code="000"; waited=0
while [ "$waited" -lt "$START_TIMEOUT_SEC" ]; do
  code="$(health "$port")"
  [ "$code" = "200" ] && break
  if ! "$SYSTEMCTL" is-active --quiet "$unit"; then
    echo "[$SLUG] ERROR: $unit exited before the health path answered" >&2; break
  fi
  sleep "$HEALTH_POLL_SEC"; waited=$((waited + 1))
done
if [ "$code" != "200" ]; then
  echo "[$SLUG] ERROR: health http://$HEALTH_HOST:$port$HEALTH_PATH → $code after ${waited}s; stopping $unit, live colour untouched" >&2
  journal_tail "$colour" >&2
  stop_instance "$colour"
  log "sha=${sha:0:12} action=health_failed colour=$colour port=$port http=$code"
  exit 20
fi
echo "[$SLUG] health ok on :$port after ${waited}s"

# --- 6. switch nginx -------------------------------------------------------------------------------------------------
if [ "$SKIP_NGINX" != 1 ]; then
  mkdir -p "$NGINX_CONFD"
  backup=""
  if [ -f "$UPSTREAM" ]; then backup="$(mktemp)"; cp "$UPSTREAM" "$backup"; fi
  write_upstream "$port"
  if ! "$NGINX_BIN" -t >/dev/null 2>&1; then
    echo "[$SLUG] ERROR: nginx -t failed with the new upstream; restoring the previous one and stopping $unit" >&2
    "$NGINX_BIN" -t 2>&1 | tail -n 5 >&2 || true
    if [ -n "$backup" ]; then mv "$backup" "$UPSTREAM"; else rm -f "$UPSTREAM"; fi
    stop_instance "$colour"
    log "sha=${sha:0:12} action=nginx_failed colour=$colour port=$port"
    exit 21
  fi
  [ -n "$backup" ] && rm -f "$backup"
  "$SYSTEMCTL" reload nginx
  echo "[$SLUG] nginx upstream → :$port ($UPSTREAM)"
fi

# --- 7. retire the old colour, prune, state ----------------------------------------------------------------------------
if [ -n "$cur_colour" ] && [ "$cur_colour" != "$colour" ]; then
  echo "[$SLUG] draining $(unit_of "$cur_colour") for ${DRAIN_SEC}s, then stopping"
  sleep "$DRAIN_SEC"
  stop_instance "$cur_colour"
fi
while IFS= read -r old; do
  old="${old%/}"
  [ -n "$old" ] || continue
  if [ "$old" = "$release" ] || [ "$old" = "$cur_release" ]; then continue; fi
  echo "[$SLUG] pruning old release $old"
  rm -rf "$old"
done < <(ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) || true)
if [ "$release" = "$cur_release" ]; then
  write_state "$sha" "$release" "$colour" "$port" "$code" "$(state_get previous.sha)" "$(state_get previous.release)" "$(state_get previous.colour)" "${cur_port:-0}"
else
  write_state "$sha" "$release" "$colour" "$port" "$code" "$cur_sha" "$cur_release" "$cur_colour" "${cur_port:-0}"
fi
url="https://$HOST/"; [ -n "$HOST" ] || url="http://$HEALTH_HOST:$port/"
log "sha=${sha:0:12} action=$([ "$ROLLBACK" = 1 ] && echo rolled_back || echo deployed) colour=$colour port=$port release=$release url=$url"
echo "== deployed slug=$SLUG sha=$sha colour=$colour port=$port url=$url"
