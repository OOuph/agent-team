#!/usr/bin/env bash
# remote-install.sh — run from the Mac: upload deploy/service/ to the droplet and run install.sh there as root.
# Idempotent (install.sh overwrites scripts, unit, template and sudoers; keeps service definitions, secrets, releases).
#
# Usage: /Users/Shared/files/info/agent-team/deploy/service/remote-install.sh
# Env:   DROPLET_HOST  (default root@68.183.2.193 — the droplet that also runs the dispatcher as `ubuntu`)
#        DROPLET_KEY   (default ~/.ssh/bymachines_lab_ed25519)
#        SVC_USER      (default ubuntu — the dispatcher's user)
#        REMOTE_TMP    (default /tmp/agent-team-deploy)
set -euo pipefail

DROPLET_HOST="${DROPLET_HOST:-root@68.183.2.193}"
DROPLET_KEY="${DROPLET_KEY:-$HOME/.ssh/bymachines_lab_ed25519}"
SVC_USER="${SVC_USER:-ubuntu}"
REMOTE_TMP="${REMOTE_TMP:-/tmp/agent-team-deploy}"
SSH_OPTS=(-i "$DROPLET_KEY" -o BatchMode=yes -o ConnectTimeout=15)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DEPLOY_REL="deploy/service"

echo "== remote-install (service stack) -> $DROPLET_HOST (key $DROPLET_KEY, user $SVC_USER)"
[ -f "$DROPLET_KEY" ] || { echo "ERROR: ssh key $DROPLET_KEY not found" >&2; exit 1; }
echo "-- local syntax check"
for f in "$HERE"/*.sh; do bash -n "$f"; echo "   ok $f"; done

echo "-- uploading $DEPLOY_REL to $DROPLET_HOST:$REMOTE_TMP"
ssh "${SSH_OPTS[@]}" "$DROPLET_HOST" "rm -rf '$REMOTE_TMP/$DEPLOY_REL' && mkdir -p '$REMOTE_TMP'"
COPYFILE_DISABLE=1 tar --no-xattrs -C "$REPO_ROOT" -cf - "$DEPLOY_REL" | ssh "${SSH_OPTS[@]}" "$DROPLET_HOST" "tar -xf - -C '$REMOTE_TMP'"

echo "-- running install.sh on the droplet"
ssh "${SSH_OPTS[@]}" "$DROPLET_HOST" "SRC_DIR='$REMOTE_TMP/$DEPLOY_REL' SVC_USER='$SVC_USER' bash '$REMOTE_TMP/$DEPLOY_REL/install.sh'"

echo "== done. Register a game: ssh ${SSH_OPTS[*]} $DROPLET_HOST '/opt/agent-team/bin/add-service.sh <slug> <host> <repo> [--run …] [--build …]'"
echo "   then in products/<slug>/product.yaml: deploy: {mode: service, host: local}"
