#!/usr/bin/env bash
# install.sh — install / upgrade the agent-team service stack (long-running game servers) on the droplet. Run as root.
# Idempotent: scripts, the credential helper, the unit template, the nginx template and the sudoers rule are overwritten;
# service definitions (/etc/agent-team/services/*.conf), secrets files, releases, data and state are kept.
#
# Usage (on the droplet): SRC_DIR=/tmp/agent-team-deploy/deploy/service bash install.sh
#   SRC_DIR   directory with these files (default: directory of this script)
#   SVC_USER  the dispatcher's user: owner of the clones, releases and data, runs git / install / build of a deploy and
#             the services themselves, and may sudo the deploy (default ubuntu)
# From the Mac: deploy/service/remote-install.sh (uploads this folder and runs install.sh).
# Prerequisites: deploy/droplet/install.sh already ran (nginx, certbot, render-vhost.sh, headers snippet, IndexNow key)
# and Node 20 is installed (the services are Node processes; npm ci runs on the droplet).
set -euo pipefail
umask 022

SRC_DIR="${SRC_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)}"
SVC_USER="${SVC_USER:-ubuntu}"
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
SERVICES_ETC="${SERVICES_ETC:-$ETC_DIR/services}"
OPT_DIR="${OPT_DIR:-/opt/agent-team}"
BIN_DIR="$OPT_DIR/bin"
SERVICES_DIR="${SERVICES_DIR:-$OPT_DIR/services}"
VAR_DIR="${VAR_DIR:-/var/lib/agent-team/services}"
STATE_DIR="${STATE_DIR:-/var/lib/agent-team/state}"
LOG_DIR="${LOG_DIR:-/var/log/agent-team}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
NGINX_CONFD="${NGINX_CONFD:-/etc/nginx/conf.d}"
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"

echo "== agent-team service install from $SRC_DIR (user $SVC_USER)"
if [ "$REQUIRE_ROOT" = 1 ] && [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root" >&2; exit 1; fi
for f in deploy-service.sh add-service.sh svc-run.sh git-credential-dispatcher agent-team-svc@.service service.conf.template agent-team-deploy.sudoers services.conf.example; do
  [ -f "$SRC_DIR/$f" ] || { echo "ERROR: $SRC_DIR/$f missing" >&2; exit 1; }
done
for f in deploy-service.sh add-service.sh svc-run.sh git-credential-dispatcher; do bash -n "$SRC_DIR/$f"; done

echo "-- checking dependencies"
missing=""
for bin in git rsync curl python3 flock nginx runuser; do     # runuser (util-linux): deploy-service.sh runs git, install and build as $SVC_USER
  command -v "$bin" >/dev/null 2>&1 || missing="$missing $bin"
done
[ -z "$missing" ] || { echo "ERROR: missing on this server:$missing" >&2; exit 1; }
if command -v node >/dev/null 2>&1; then echo "   node $(node --version)"; else echo "   WARNING: node not found — install Node 20 (nodesource) before the first deploy" >&2; fi
if [ "$REQUIRE_ROOT" = 1 ] && ! id "$SVC_USER" >/dev/null 2>&1; then echo "ERROR: user $SVC_USER does not exist" >&2; exit 1; fi
[ -x "$BIN_DIR/render-vhost.sh" ] || echo "   WARNING: $BIN_DIR/render-vhost.sh missing — run deploy/droplet/install.sh first (add-service.sh needs it)" >&2

echo "-- directories"
for d in "$ETC_DIR" "$SERVICES_ETC" "$BIN_DIR" "$SERVICES_DIR" "$VAR_DIR" "$STATE_DIR" "$LOG_DIR" "$OPT_DIR/service"; do
  mkdir -p "$d"; echo "   $d"
done
chmod 750 "$SERVICES_ETC"
if [ "$REQUIRE_ROOT" = 1 ]; then
  chgrp "$SVC_USER" "$SERVICES_ETC" || true                       # the service user reads its own secrets file (0640 root:<user>)
  # root's: deploy-service.sh makes <slug>/ below them and writes the instance env files there as root, so the service
  # user must not be able to swap a <slug> directory for a link. What that user owns is <slug>/repo's parent,
  # <slug>/releases and <slug>/data — deploy-service.sh hands those over itself. (Until 22.11 both were the user's.)
  chown root:root "$SERVICES_DIR" "$VAR_DIR"; chmod 755 "$SERVICES_DIR" "$VAR_DIR"
  touch "$LOG_DIR/deploy.log"; chmod 664 "$LOG_DIR/deploy.log"; chgrp "$SVC_USER" "$LOG_DIR/deploy.log" || true
fi

echo "-- scripts -> $BIN_DIR"
for f in deploy-service.sh add-service.sh svc-run.sh; do
  install -m 0755 "$SRC_DIR/$f" "$BIN_DIR/$f"; echo "   $BIN_DIR/$f"
done
install -m 0644 "$SRC_DIR/README.md" "$OPT_DIR/service/README.md" 2>/dev/null || true

echo "-- git credential helper -> $BIN_DIR (no secret inside: it reads the dispatcher's .env when git calls it)"
helper="$BIN_DIR/git-credential-dispatcher"
if [ -f "$helper" ] && ! cmp -s "$SRC_DIR/git-credential-dispatcher" "$helper" && [ ! -e "$helper.before-repo" ]; then
  cp -p "$helper" "$helper.before-repo"                           # the hand-made helper of 20.11 (root, 0700) is kept once
  echo "   kept the previous helper as $helper.before-repo"
fi
install -m 0755 "$SRC_DIR/git-credential-dispatcher" "$helper"; echo "   $helper"

echo "-- templates -> $ETC_DIR"
install -m 0644 "$SRC_DIR/service.conf.template" "$ETC_DIR/service.conf.template"
install -m 0644 "$SRC_DIR/services.conf.example" "$SERVICES_ETC/example.conf.example"
echo "   $ETC_DIR/service.conf.template, $SERVICES_ETC/example.conf.example"

echo "-- systemd unit template"
mkdir -p "$SYSTEMD_DIR"
sed "s|__USER__|$SVC_USER|g" "$SRC_DIR/agent-team-svc@.service" > "$SYSTEMD_DIR/agent-team-svc@.service.tmp"
mv "$SYSTEMD_DIR/agent-team-svc@.service.tmp" "$SYSTEMD_DIR/agent-team-svc@.service"
chmod 644 "$SYSTEMD_DIR/agent-team-svc@.service"
echo "   $SYSTEMD_DIR/agent-team-svc@.service (User=$SVC_USER)"
if [ "$REQUIRE_ROOT" = 1 ]; then systemctl daemon-reload; fi

echo "-- sudoers: $SVC_USER may run deploy-service.sh"
mkdir -p "$SUDOERS_DIR"
sed "s|__USER__|$SVC_USER|g" "$SRC_DIR/agent-team-deploy.sudoers" > "$SUDOERS_DIR/agent-team-deploy.tmp"
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$SUDOERS_DIR/agent-team-deploy.tmp" >/dev/null
fi
install -m 0440 "$SUDOERS_DIR/agent-team-deploy.tmp" "$SUDOERS_DIR/agent-team-deploy"
rm -f "$SUDOERS_DIR/agent-team-deploy.tmp"
echo "   $SUDOERS_DIR/agent-team-deploy"

mkdir -p "$NGINX_CONFD"
n="$(find "$SERVICES_ETC" -maxdepth 1 -name '*.conf' 2>/dev/null | wc -l | tr -d ' ')"
echo "== done: $n service(s) registered in $SERVICES_ETC. Next: add-service.sh <slug> <host> <repo> …; the dispatcher deploys with: sudo -n $BIN_DIR/deploy-service.sh <slug> --sha <commit>"
echo "   git, install and build of a deploy run as $SVC_USER (runuser, clean environment); a clone or release made by root earlier is handed to $SVC_USER by the next deploy"
