#!/usr/bin/env bash
# remote-bootstrap.sh — run from the Mac: prepare a FRESH server for a game in one go (owner 24.11: the game gets its own
# machine). Uploads deploy/service/, deploy/droplet/ and team/sitemetrics.py, then runs on the server as root, in order:
#   1. deploy/service/bootstrap-host.sh   packages, Node, the service user with the dispatcher's key, nginx basics, firewall
#   2. deploy/droplet/install.sh          render-vhost.sh, the headers snippet, /etc/agent-team/env — the vhost tooling
#   3. deploy/service/install.sh          deploy-service.sh, add-service.sh, the systemd unit template, the sudoers rule
# Idempotent: every step may be repeated. No service is registered and no secret is written by this script.
#
# Usage:
#   GAME_HOST=root@203.0.113.7 DEPLOY_PUBKEY="ssh-ed25519 AAAA… dispatcher" CERTBOT_EMAIL=owner@example.org \
#     /Users/Shared/files/info/agent-team/deploy/service/remote-bootstrap.sh
# Env (parameters):
#   GAME_HOST      root@<address of the new server>, required
#   GAME_KEY       ssh key of the Mac for that server, default ~/.ssh/bymachines_lab_ed25519
#   DEPLOY_PUBKEY  one public key line of the dispatcher's user (on the dispatcher's host: cat ~/.ssh/id_ed25519.pub), required
#   CERTBOT_EMAIL  address for Let's Encrypt, required on the first run
#   SVC_USER       service user on the new server, default ubuntu
#   REMOTE_TMP     upload folder on the server, default /tmp/agent-team-deploy
#   DRY_RUN        1 = show what would be uploaded and run, connect to nothing
set -euo pipefail

# ---------------------------------------------------------------- parameters
GAME_HOST="${GAME_HOST:-}"
GAME_KEY="${GAME_KEY:-$HOME/.ssh/bymachines_lab_ed25519}"
DEPLOY_PUBKEY="${DEPLOY_PUBKEY:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
SVC_USER="${SVC_USER:-ubuntu}"
REMOTE_TMP="${REMOTE_TMP:-/tmp/agent-team-deploy}"
DRY_RUN="${DRY_RUN:-0}"
SSH_OPTS=(-i "$GAME_KEY" -o BatchMode=yes -o ConnectTimeout=15)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
UPLOAD=(deploy/service deploy/droplet team/sitemetrics.py)

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1 remote-bootstrap: $2"; }
q() { printf '%q' "$1"; }

[[ "$GAME_HOST" =~ ^root@[A-Za-z0-9._:-]+$ ]] || { log ERROR "GAME_HOST must be root@<address of the new server> (got '$GAME_HOST')"; exit 64; }
[ -n "$DEPLOY_PUBKEY" ] || { log ERROR "DEPLOY_PUBKEY is empty: paste the dispatcher's public key line (cat ~/.ssh/id_ed25519.pub on its host)"; exit 64; }
[[ "$DEPLOY_PUBKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256)\ [A-Za-z0-9+/=]+(\ [^[:cntrl:]]*)?$ ]] || { log ERROR "DEPLOY_PUBKEY is not one public key line"; exit 64; }
[[ "$SVC_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { log ERROR "SVC_USER '$SVC_USER' is not a valid user name"; exit 64; }
for p in "${UPLOAD[@]}"; do [ -e "$REPO_ROOT/$p" ] || { log ERROR "$REPO_ROOT/$p not found"; exit 1; }; done
log INFO "target $GAME_HOST (key $GAME_KEY), service user $SVC_USER"
log INFO "local syntax check"
for f in "$REPO_ROOT"/deploy/service/*.sh "$REPO_ROOT"/deploy/droplet/*.sh; do bash -n "$f"; done

STEPS=(
  "SVC_USER=$(q "$SVC_USER") DEPLOY_PUBKEY=$(q "$DEPLOY_PUBKEY") CERTBOT_EMAIL=$(q "$CERTBOT_EMAIL") bash $(q "$REMOTE_TMP/deploy/service/bootstrap-host.sh")"
  "SRC_DIR=$(q "$REMOTE_TMP/deploy/droplet") bash $(q "$REMOTE_TMP/deploy/droplet/install.sh")"
  "SRC_DIR=$(q "$REMOTE_TMP/deploy/service") SVC_USER=$(q "$SVC_USER") bash $(q "$REMOTE_TMP/deploy/service/install.sh")"
)
STEP_NAMES=("$REMOTE_TMP/deploy/service/bootstrap-host.sh" "$REMOTE_TMP/deploy/droplet/install.sh" "$REMOTE_TMP/deploy/service/install.sh")
if [ "$DRY_RUN" = 1 ]; then
  log INFO "dry-run: upload ${UPLOAD[*]} to $GAME_HOST:$REMOTE_TMP"
  for s in "${STEP_NAMES[@]}"; do log INFO "dry-run: ssh $GAME_HOST bash $s"; done   # the key is not printed
  echo "DRY-RUN remote-bootstrap $GAME_HOST"
  exit 0
fi
[ -f "$GAME_KEY" ] || { log ERROR "ssh key $GAME_KEY not found"; exit 1; }

log INFO "uploading ${UPLOAD[*]} to $GAME_HOST:$REMOTE_TMP"
ssh "${SSH_OPTS[@]}" "$GAME_HOST" "mkdir -p $(q "$REMOTE_TMP")"
# COPYFILE_DISABLE / --no-xattrs: no macOS ._* resource forks or xattr headers in the archive
COPYFILE_DISABLE=1 tar --no-xattrs -C "$REPO_ROOT" -cf - "${UPLOAD[@]}" | ssh "${SSH_OPTS[@]}" "$GAME_HOST" "tar -xf - -C $(q "$REMOTE_TMP")"
n=0
for s in "${STEPS[@]}"; do
  n=$((n + 1))
  log INFO "step $n of ${#STEPS[@]} on the server: ${STEP_NAMES[$((n - 1))]}"
  ssh "${SSH_OPTS[@]}" "$GAME_HOST" "$s"
done
log INFO "done. Next on the server: /opt/agent-team/bin/add-service.sh <slug> <host> local --run '…' --build '…' (see deploy/service/README.md, section «Отдельный сервер игры»)"
echo "BOOTSTRAPPED $GAME_HOST"
