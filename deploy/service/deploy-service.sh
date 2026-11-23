#!/usr/bin/env bash
# deploy-service.sh — deploy one long-running service (a game server) from its git repository as a new release,
# blue/green on two local ports, switch the nginx upstream only after the health path answers 200, keep the last
# releases, roll back to the previous one. Runs as root on the droplet; the dispatcher calls it through sudo
# (/etc/sudoers.d/agent-team-deploy, installed by install.sh) after every merge of a game product.
# Root does only what needs root: the lock, the ownership of the service's directories, the instance env file,
# systemctl, the nginx upstream file and reload, the state file and the journal. Everything that reads or executes the
# product repository — git clone / fetch / checkout, rsync into the release, install, build, pruning — runs as the
# service user (SVC_USER) through `runuser` with a clean environment: the scripts of package.json come from a
# repository the developer role writes to without a human, so they must never run as root (scheme audit 21.11).
#
# Usage: deploy-service.sh <slug> [--sha <commit>] [--rollback] [--force] [--no-fetch]
#   slug        service registered by add-service.sh (/etc/agent-team/services/<slug>.conf)
#   --sha       deploy this commit (default: origin/<branch> HEAD)
#   --rollback  deploy the previous release again (state.previous), no fetch / install / build
#   --force     redeploy even when the requested commit is the live one
#   --no-fetch  do not fetch; use the clone as it is (tests, air-gapped runs)
#
# Steps: lock → fetch → stage releases/<sha12> (rsync, no .git) → install (npm ci / npm install when package.json is
# there) → build (BUILD_CMD) — these four as SVC_USER → write the instance env for the idle colour → systemctl start agent-team-svc@<slug>-<colour>
# → poll http://127.0.0.1:<port><HEALTH_PATH> until 200 → write the nginx upstream → nginx -t → reload → wait while the
# old colour still serves matches (DRAIN_BUSY_PATH / DRAIN_BUSY_FIELD, at most DRAIN_MAX_SEC), stop it after DRAIN_SEC
# → prune releases (KEEP_RELEASES, never the live or the previous one) → state + journal.
# A failed health check stops the new instance and leaves the live one untouched (exit 20). A failed nginx -t restores
# the previous upstream file (exit 21). The last stdout line on success: `== deployed slug=… sha=… colour=… port=… url=…`.
# Exit codes: 0 deployed · 10 nothing to do (same commit live) · 20 health failed · 21 nginx failed · 64 usage · 1 other.
# Journal: /var/log/agent-team/deploy.log · state: /var/lib/agent-team/state/svc-<slug>.json
# Private repository: an ssh REPO with a deploy key of SVC_USER, or an https REPO with the credential helper
# (GIT_CREDENTIAL_HELPER, git-credential-dispatcher: the token is read from the dispatcher's .env at call time). README.
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
# ai-battles 22.11 (F42): the old colour used to be stopped DRAIN_SEC after the switch, with its matches still running — a
# deploy froze the battle of everyone playing (five deploys in one hour on 21.11). Existing connections stay on the old
# colour after the nginx reload, new ones go to the new colour, so the old colour only has to be left alone until its
# matches end. It reports them itself: a JSON document with a number that is > 0 while a match is running.
DRAIN_BUSY_PATH="${DRAIN_BUSY_PATH-/_team/metrics.json}"        # path on the old colour; empty = no busy check (plain DRAIN_SEC)
DRAIN_BUSY_FIELD="${DRAIN_BUSY_FIELD:-now.rooms_playing}"       # dotted path to the number of running matches in that JSON
DRAIN_MAX_SEC="${DRAIN_MAX_SEC:-300}"                           # cap on the wait; then the old colour is stopped anyway. 300 since 23.11: a room
                                                                # battle of ai-battles may last 240 s (the creator's choice), the cap of 240 cut it at its last second
DRAIN_POLL_SEC="${DRAIN_POLL_SEC:-5}"                           # how often the old colour is asked
KEEP_RELEASES="${KEEP_RELEASES:-3}"
INSTALL_TIMEOUT_SEC="${INSTALL_TIMEOUT_SEC:-600}"
BUILD_TIMEOUT_SEC="${BUILD_TIMEOUT_SEC:-600}"
NODE_ENV_VALUE="${NODE_ENV_VALUE:-production}"
SVC_USER="${SVC_USER:-ubuntu}"                                  # owner of the clone, releases and data; runs git, install, build; the unit runs as this user
RUNUSER="${RUNUSER:-runuser}"                                   # util-linux; how root becomes SVC_USER (tests point this at a fake)
SVC_VIA_RUNUSER="${SVC_VIA_RUNUSER:-0}"                         # root always goes through RUNUSER; 1 forces it for a non-root run (tests)
SVC_PATH="${SVC_PATH:-$PATH}"                                   # PATH of the clean environment (under sudo this is sudo's secure_path)
SVC_LANG="${SVC_LANG:-C.UTF-8}"
SVC_HOME="${SVC_HOME:-}"                                        # empty = the home directory of SVC_USER from the passwd database
# https REPO of a private repository: git asks this helper for the credential (it reads the token from the dispatcher's
# .env when called and stores nothing). Empty = no helper: a public repository, an ssh REPO with a deploy key, or the
# git configuration of SVC_USER itself. Both can also be set per service in <slug>.conf.
GIT_CREDENTIAL_HELPER="${GIT_CREDENTIAL_HELPER-/opt/agent-team/bin/git-credential-dispatcher}"
DISPATCHER_ENV_FILE="${DISPATCHER_ENV_FILE:-}"                  # the dispatcher's .env for the helper; empty = the helper's default (~SVC_USER/agent-team/dispatcher/.env)

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; }

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

# busy_of <port> → the number of matches the instance on that port still serves ("" when it does not say: no such path,
# not JSON, no such field — the caller then falls back to the plain DRAIN_SEC)
busy_of() {
  [ -n "$DRAIN_BUSY_PATH" ] || { echo ""; return 0; }
  curl -sS --max-time "$HEALTH_TIMEOUT_SEC" "http://$HEALTH_HOST:$1$DRAIN_BUSY_PATH" 2>/dev/null | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        v = v[k]
    print(max(0, int(v)))
except Exception:
    print("")
' "$DRAIN_BUSY_FIELD" 2>/dev/null || echo ""
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

# ---------------------------------------------------------------- the service user: who touches the repository
# Rule of this script: root never opens, writes or executes a path inside a tree the service user owns (the clone, the
# releases, the data) — a repository can ship a symlink where a log or a marker is expected. Root only changes the owner
# of such a tree (adopt_tree) and asks whether a path exists; everything else there goes through svc / svc_git / svc_step.
AS_SVC=()                                                       # command prefix: [runuser -u SVC_USER --] env -i HOME=… PATH=… …
GIT_AUTH=()                                                     # git -c options that name the credential helper (https REPO only)
SVC_MODE=""; SVC_GROUP=""; MY_UID="$(id -u)"

# setup_svc: resolve the user, build the prefix. Exits when root cannot drop its privileges — there is no fallback that
# would run the repository's commands as root.
setup_svc() {
  local svc_uid home
  svc_uid="$(id -u "$SVC_USER" 2>/dev/null || true)"
  [ -n "$svc_uid" ] || { echo "ERROR: service user '$SVC_USER' does not exist" >&2; exit 1; }
  if [ "$REQUIRE_ROOT" = 1 ] && [ "$svc_uid" -eq 0 ]; then
    echo "ERROR: SVC_USER '$SVC_USER' is root — install and build run the repository's scripts and must not run as root" >&2; exit 1
  fi
  SVC_GROUP="$(id -gn "$SVC_USER" 2>/dev/null || true)"
  home="$SVC_HOME"
  [ -n "$home" ] || home="$(python3 -c 'import pwd, sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' "$SVC_USER" 2>/dev/null || true)"
  [ -n "$home" ] || home="/"
  local clean=(env -i "HOME=$home" "USER=$SVC_USER" "LOGNAME=$SVC_USER" "SHELL=/bin/bash" "PATH=$SVC_PATH" "LANG=$SVC_LANG" "GIT_TERMINAL_PROMPT=0")
  [ -z "$DISPATCHER_ENV_FILE" ] || clean+=("AGENT_TEAM_DISPATCHER_ENV=$DISPATCHER_ENV_FILE")
  if [ "$MY_UID" -eq 0 ] || [ "$SVC_VIA_RUNUSER" = 1 ]; then
    command -v "$RUNUSER" >/dev/null 2>&1 || { echo "ERROR: $RUNUSER not found — cannot run git, install and build as $SVC_USER; nothing was run as root" >&2; exit 1; }
    AS_SVC=("$RUNUSER" -u "$SVC_USER" -- "${clean[@]}"); SVC_MODE="runuser"
  else
    AS_SVC=("${clean[@]}"); SVC_MODE="direct"
    [ "$MY_UID" = "$svc_uid" ] || echo "[$SLUG] WARNING: not root and not $SVC_USER — git, install and build run as $(id -un) (a test or a manual run)" >&2
  fi
  if ! "${AS_SVC[@]}" true 9>&-; then
    echo "ERROR: cannot run a command as $SVC_USER (${AS_SVC[0]}); nothing was run as root" >&2; exit 1
  fi
  case "$REPO" in
    https://*)
      if [ -n "$GIT_CREDENTIAL_HELPER" ] && [ -x "$GIT_CREDENTIAL_HELPER" ]; then
        # the empty value drops every helper configured elsewhere: only this one answers, and it writes nothing
        GIT_AUTH=(-c credential.helper= -c "credential.helper=$(printf '%q' "$GIT_CREDENTIAL_HELPER")")
      elif [ -n "$GIT_CREDENTIAL_HELPER" ]; then
        echo "[$SLUG] note: credential helper $GIT_CREDENTIAL_HELPER is not installed — a private https repository will not be readable"
      fi ;;
  esac
}

# svc <cmd…> / svc_git <git args…>: as the service user, clean environment, the deploy lock (fd 9) not inherited
svc() { "${AS_SVC[@]}" "$@" 9>&-; }
svc_git() { "${AS_SVC[@]}" git ${GIT_AUTH[@]+"${GIT_AUTH[@]}"} "$@" 9>&-; }

owned_by_svc() { [ -z "$(find "$1" -maxdepth 0 ! -user "$SVC_USER" -print 2>/dev/null)" ]; }
chown_svc() {
  if chown "$@"; then return 0; fi
  if [ "$MY_UID" -eq 0 ]; then echo "ERROR: chown $* failed" >&2; exit 1; fi
  echo "[$SLUG] WARNING: chown $* failed (not root): left as it is" >&2
}
# give_svc <dir>: a directory root has just made becomes the service user's — the directory itself, nothing below it
give_svc() { owned_by_svc "$1" || chown_svc -h "$SVC_USER:$SVC_GROUP" "$1"; }
# adopt_tree <path>: a clone or a release made before this script dropped root belongs to root. Its top directory
# tells: root-owned top = nobody else could write inside, so the whole tree is handed over (-h: links, not their
# targets). A tree whose top the service user already owns is never chowned — its content is that user's business.
adopt_tree() {
  [ -e "$1" ] || [ -L "$1" ] || return 0
  owned_by_svc "$1" && return 0
  echo "[$SLUG] $1 is not owned by $SVC_USER (made by an earlier root deploy) — handing the tree over"
  chown_svc -R -h "$SVC_USER:$SVC_GROUP" "$1"
  log "action=adopted path=$1 owner=$SVC_USER"
}
# svc_rm <path>: remove a tree the service user owns (or is about to: adopt first), as that user
svc_rm() {
  [ -e "$1" ] || [ -L "$1" ] || return 0
  adopt_tree "$1"
  svc rm -rf -- "$1"
}
# svc_step <name> <timeout_sec> <NODE_ENV> <command>: one command of the service definition inside the release, as the
# service user; its output goes to <release>/.<name>.log, opened by that user. Returns the command's status.
svc_step() {
  local name="$1" t="$2" node_env="$3" cmd="$4"
  # shellcheck disable=SC2016
  with_timeout "$t" "${AS_SVC[@]}" "NODE_ENV=$node_env" bash -c 'cd "$1" && exec > "$2" 2>&1 && exec bash -c "$3"' _ "$release" ".$name.log" "$cmd" 9>&-
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
setup_svc
# the clone's parent, the releases and the data belong to the service user before anything is put there; the instance
# env files, the state and the journal stay root's
give_svc "$SERVICES_DIR/$SLUG"; give_svc "$RELEASES"; give_svc "$DATA_DIR"

cur_sha="$(state_get sha)"; cur_release="$(state_get release)"; cur_colour="$(state_get colour)"; cur_port="$(state_get port)"
echo "== deploy-service $SLUG host=${HOST:-?} live=${cur_sha:0:12}${cur_colour:+ ($cur_colour:$cur_port)} rollback=$ROLLBACK repo-steps-as=$SVC_USER ($SVC_MODE)"

# --- 1. which release -------------------------------------------------------------------------------------------
if [ "$ROLLBACK" = 1 ]; then
  sha="$(state_get previous.sha)"; release="$(state_get previous.release)"
  [ -n "$release" ] && [ -d "$release" ] || { echo "ERROR: no previous release to roll back to (state: $STATE)" >&2; exit 1; }
  adopt_tree "$release"                                        # a release left root-owned by an earlier deploy: the server runs as SVC_USER
  echo "[$SLUG] rollback to ${sha:0:12} ($release)"
else
  if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[$SLUG] cloning $REPO ($BRANCH) into $REPO_DIR as $SVC_USER"
    svc_rm "$REPO_DIR"
    if ! svc_git clone --quiet --branch "$BRANCH" "$REPO" "$REPO_DIR"; then
      echo "[$SLUG] ERROR: git clone of $REPO failed (a private repository needs a deploy key of $SVC_USER or the credential helper — see README)" >&2
      log "action=error step=clone"; exit 1
    fi
  else
    adopt_tree "$REPO_DIR"                                     # a clone made by root: git refuses a repository of another owner
    if [ "$FETCH" = 1 ]; then
      echo "[$SLUG] fetching origin/$BRANCH as $SVC_USER"
      svc_git -C "$REPO_DIR" remote set-url origin "$REPO"
      if ! svc_git -C "$REPO_DIR" fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"; then
        echo "[$SLUG] ERROR: git fetch of $REPO failed (a private repository needs a deploy key of $SVC_USER or the credential helper — see README)" >&2
        log "action=error step=fetch"; exit 1
      fi
    fi
  fi
  if [ -n "$WANT_SHA" ]; then
    sha="$(svc_git -C "$REPO_DIR" rev-parse --verify "$WANT_SHA^{commit}" 2>/dev/null || true)"
    [ -n "$sha" ] || { echo "ERROR: commit $WANT_SHA is not in $REPO_DIR (fetched origin/$BRANCH)" >&2; exit 1; }
  else
    sha="$(svc_git -C "$REPO_DIR" rev-parse "origin/$BRANCH" 2>/dev/null || svc_git -C "$REPO_DIR" rev-parse HEAD)"
  fi
  # the clone is the service user's: what git printed there is checked before it becomes a path
  [[ "$sha" =~ ^[0-9a-f]{40,64}$ ]] || { echo "ERROR: git returned '$sha' instead of a commit id for $REPO_DIR" >&2; exit 1; }
  release="$RELEASES/${sha:0:12}"
  if [ "$FORCE" = 0 ] && [ "$sha" = "$cur_sha" ] && [ -d "$release" ] && [ "$(health "$cur_port")" = "200" ]; then
    echo "[$SLUG] ${sha:0:12} is live and healthy — nothing to do"
    log "sha=${sha:0:12} action=skipped"
    exit 10
  fi
  # --- 2. stage ---------------------------------------------------------------------------------------------------
  if [ -f "$release/.built" ] && [ "$FORCE" = 0 ]; then
    adopt_tree "$release"
    echo "[$SLUG] release $release already built — reusing"
  else
    echo "[$SLUG] staging $release from $sha as $SVC_USER"
    svc_git -C "$REPO_DIR" checkout --quiet --detach "$sha"
    svc_rm "$release.tmp"
    svc rsync -a --delete --exclude .git --exclude node_modules "$REPO_DIR/" "$release.tmp/"
    svc_rm "$release"
    svc mv "$release.tmp" "$release"
    # shellcheck disable=SC2016
    svc bash -c 'echo "$1" > "$2/.release-sha"' _ "$sha" "$release"
    # --- 3. install -------------------------------------------------------------------------------------------------
    install_cmd="$INSTALL_CMD"
    if [ -z "$install_cmd" ] && [ -f "$release/package.json" ]; then
      if [ -f "$release/package-lock.json" ]; then install_cmd="npm ci --no-audit --no-fund"; else install_cmd="npm install --no-audit --no-fund"; fi
    fi
    if [ -n "$install_cmd" ]; then
      echo "[$SLUG] install as $SVC_USER: $install_cmd"
      if ! svc_step install "$INSTALL_TIMEOUT_SEC" development "$install_cmd"; then
        echo "[$SLUG] ERROR: install failed; tail of $release/.install.log:" >&2; svc tail -n 30 "$release/.install.log" >&2 || true
        log "sha=${sha:0:12} action=error step=install"; exit 1
      fi
    fi
    # --- 4. build ---------------------------------------------------------------------------------------------------
    if [ -n "$BUILD_CMD" ]; then
      echo "[$SLUG] build as $SVC_USER: $BUILD_CMD"
      if ! svc_step build "$BUILD_TIMEOUT_SEC" "$NODE_ENV_VALUE" "$BUILD_CMD"; then
        echo "[$SLUG] ERROR: build failed; tail of $release/.build.log:" >&2; svc tail -n 30 "$release/.build.log" >&2 || true
        log "sha=${sha:0:12} action=error step=build"; exit 1
      fi
    fi
    # shellcheck disable=SC2016
    svc bash -c 'date -u +%Y-%m-%dT%H:%M:%SZ > "$1/.built"' _ "$release"
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
  old_unit="$(unit_of "$cur_colour")"
  busy="$(busy_of "${cur_port:-0}")"; drained=0
  if [ -n "$busy" ] && [ "$busy" -gt 0 ]; then
    echo "[$SLUG] $old_unit still serves $busy match(es): waiting up to ${DRAIN_MAX_SEC}s before stopping it (new players already go to :$port)"
    drain_t0=$SECONDS
    while [ $((SECONDS - drain_t0)) -lt "$DRAIN_MAX_SEC" ]; do
      sleep "$DRAIN_POLL_SEC"
      busy="$(busy_of "${cur_port:-0}")"
      if [ -z "$busy" ] || [ "$busy" -eq 0 ]; then break; fi
    done
    drained=$((SECONDS - drain_t0))
    if [ -n "$busy" ] && [ "$busy" -gt 0 ]; then
      echo "[$SLUG] WARNING: $old_unit still serves $busy match(es) after ${drained}s (cap DRAIN_MAX_SEC=${DRAIN_MAX_SEC}); stopping it anyway" >&2
      log "sha=${sha:0:12} action=drain_cap colour=$cur_colour busy=$busy waited=${drained}s"
    else
      echo "[$SLUG] $old_unit finished its matches after ${drained}s"
      log "sha=${sha:0:12} action=drained colour=$cur_colour waited=${drained}s"
    fi
  elif [ -z "$busy" ] && [ -n "$DRAIN_BUSY_PATH" ]; then
    echo "[$SLUG] $old_unit does not report running matches ($DRAIN_BUSY_PATH: $DRAIN_BUSY_FIELD): plain drain"
  fi
  echo "[$SLUG] draining $old_unit for ${DRAIN_SEC}s, then stopping"
  sleep "$DRAIN_SEC"
  stop_instance "$cur_colour"
fi
while IFS= read -r old; do
  old="${old%/}"
  [ -n "$old" ] || continue
  if [ "$old" = "$release" ] || [ "$old" = "$cur_release" ]; then continue; fi
  echo "[$SLUG] pruning old release $old"
  svc_rm "$old"
done < <(ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) || true)
if [ "$release" = "$cur_release" ]; then
  write_state "$sha" "$release" "$colour" "$port" "$code" "$(state_get previous.sha)" "$(state_get previous.release)" "$(state_get previous.colour)" "${cur_port:-0}"
else
  write_state "$sha" "$release" "$colour" "$port" "$code" "$cur_sha" "$cur_release" "$cur_colour" "${cur_port:-0}"
fi
url="https://$HOST/"; [ -n "$HOST" ] || url="http://$HEALTH_HOST:$port/"
log "sha=${sha:0:12} action=$([ "$ROLLBACK" = 1 ] && echo rolled_back || echo deployed) colour=$colour port=$port release=$release url=$url"
echo "== deployed slug=$SLUG sha=$sha colour=$colour port=$port url=$url"
