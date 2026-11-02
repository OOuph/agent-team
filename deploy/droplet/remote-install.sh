#!/usr/bin/env bash
# remote-install.sh — run from the Mac: upload deploy/droplet/ (+ team/sitemetrics.py) to the
# droplet and run install.sh there. Idempotent (install.sh overwrites scripts, keeps config).
#
# Usage: /Users/Shared/files/info/agent-team/deploy/droplet/remote-install.sh
# Env:   DROPLET_HOST  (default root@68.183.2.193)
#        DROPLET_KEY   (default ~/.ssh/bymachines_lab_ed25519)
#        REMOTE_TMP    (default /tmp/agent-team-deploy)
set -euo pipefail

DROPLET_HOST="${DROPLET_HOST:-root@68.183.2.193}"
DROPLET_KEY="${DROPLET_KEY:-$HOME/.ssh/bymachines_lab_ed25519}"
REMOTE_TMP="${REMOTE_TMP:-/tmp/agent-team-deploy}"
SSH_OPTS=(-i "$DROPLET_KEY" -o BatchMode=yes -o ConnectTimeout=15)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DEPLOY_REL="deploy/droplet"
SITEMETRICS_REL="team/sitemetrics.py"

echo "== remote-install -> $DROPLET_HOST (key $DROPLET_KEY)"
[ -f "$DROPLET_KEY" ] || { echo "ERROR: ssh key $DROPLET_KEY not found" >&2; exit 1; }
[ -d "$REPO_ROOT/$DEPLOY_REL" ] || { echo "ERROR: $REPO_ROOT/$DEPLOY_REL not found" >&2; exit 1; }

echo "-- local syntax check"
for f in "$HERE"/*.sh; do bash -n "$f"; echo "   ok $f"; done

tar_paths=("$DEPLOY_REL")
if [ -f "$REPO_ROOT/$SITEMETRICS_REL" ]; then
  tar_paths+=("$SITEMETRICS_REL")
  echo "-- including $SITEMETRICS_REL"
else
  echo "-- WARNING: $REPO_ROOT/$SITEMETRICS_REL not found — metrics timer will stay broken until it exists" >&2
fi

echo "-- uploading ${tar_paths[*]} to $DROPLET_HOST:$REMOTE_TMP"
ssh "${SSH_OPTS[@]}" "$DROPLET_HOST" "rm -rf '$REMOTE_TMP' && mkdir -p '$REMOTE_TMP'"
# COPYFILE_DISABLE / --no-xattrs: no macOS ._* resource forks or xattr headers in the archive
COPYFILE_DISABLE=1 tar --no-xattrs -C "$REPO_ROOT" -cf - "${tar_paths[@]}" \
  | ssh "${SSH_OPTS[@]}" "$DROPLET_HOST" "tar -xf - -C '$REMOTE_TMP'"

echo "-- running install.sh on the droplet"
ssh "${SSH_OPTS[@]}" "$DROPLET_HOST" \
  "SRC_DIR='$REMOTE_TMP/$DEPLOY_REL' SITEMETRICS_SRC='$REMOTE_TMP/$SITEMETRICS_REL' bash '$REMOTE_TMP/$DEPLOY_REL/install.sh'"

echo "== done. Check: ssh ${SSH_OPTS[*]} $DROPLET_HOST 'systemctl list-timers agent-team-\\*; tail -n 20 /var/log/agent-team/publish.log'"
