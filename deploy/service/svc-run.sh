#!/usr/bin/env bash
# svc-run.sh <instance> — ExecStart of agent-team-svc@<instance>.service (instance = <slug>-<colour>). Sources the
# instance env written by deploy-service.sh (RELEASE_DIR, RUN_CMD, PORT, DATA_DIR, RELEASE_SHA, NODE_ENV, SECRETS_FILE),
# then the owner's secrets file when it exists (values never pass through git or the dispatcher), cd's into the
# release and exec's RUN_CMD under bash so `$PORT` and `{port}` both work. One file, one process: systemd owns it.
set -euo pipefail
VAR_DIR="${VAR_DIR:-/var/lib/agent-team/services}"
INSTANCE="${1:-}"
[ -n "$INSTANCE" ] || { echo "usage: svc-run.sh <slug>-<colour>" >&2; exit 64; }
[[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9-]*-(a|b)$ ]] || { echo "ERROR: bad instance '$INSTANCE'" >&2; exit 64; }
SLUG="${INSTANCE%-*}"
ENV_FILE="${ENV_FILE:-$VAR_DIR/$SLUG/instances/$INSTANCE.env}"
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE missing — deploy-service.sh writes it" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
if [ -n "${SECRETS_FILE:-}" ] && [ -r "$SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  . "$SECRETS_FILE"
fi
set +a
[ -d "${RELEASE_DIR:-}" ] || { echo "ERROR: RELEASE_DIR '$RELEASE_DIR' missing" >&2; exit 1; }
cd "$RELEASE_DIR"
cmd="${RUN_CMD//\{port\}/$PORT}"
echo "svc-run: $INSTANCE sha=${RELEASE_SHA:-?} port=$PORT cwd=$RELEASE_DIR cmd=$cmd"
exec bash -c "$cmd"
