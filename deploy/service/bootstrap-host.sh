#!/usr/bin/env bash
# bootstrap-host.sh — prepare a FRESH server for one game (owner 24.11: the game gets its own machine, the agents keep
# the dispatcher's one). Run as root on Ubuntu 24.04, once, before deploy/droplet/install.sh and deploy/service/install.sh.
# Idempotent: every step checks what is already there; nothing that exists is overwritten except the two small nginx
# files this script owns.
#
# What it does:
#   1. packages: nginx, certbot, git, rsync, curl, python3, openssl, ufw (apt), Node from the NodeSource apt repository
#   2. the service user (owner of clones, releases and data; the game process runs as this user) and the dispatcher's
#      public ssh key in its authorized_keys — the only way in for deploys (push to the bare origin + sudo deploy-service.sh)
#   3. nginx: the access-log format `full` the vhost templates use, a connection limit sized for WebSockets
#   4. TLS files the vhost template includes: options-ssl-nginx.conf and ssl-dhparams.pem, /var/www/acme for certbot
#   5. firewall: ssh, http, https only
# What it does NOT do: no service is registered (add-service.sh), no certificate is asked, no secret is written.
#
# Usage (on the new server, as root):
#   DEPLOY_PUBKEY="ssh-ed25519 AAAA… dispatcher" CERTBOT_EMAIL=owner@example.org bash bootstrap-host.sh
# Env (parameters):
#   SVC_USER            service user, default ubuntu (the same name the dispatcher's host uses)
#   DEPLOY_PUBKEY       one public key line of the dispatcher's user; empty = keep authorized_keys as it is
#   CERTBOT_EMAIL       written to /etc/agent-team/env when that file has no CERTBOT_EMAIL yet; empty = skip
#   NODE_MAJOR          Node major version, default 20
#   WORKER_CONNECTIONS  nginx worker_connections, default 8192 (a WebSocket player holds two connections)
#   ENABLE_UFW          1 (default) = allow OpenSSH, 80, 443 and enable ufw; 0 = leave the firewall alone
#   DH_BITS             size of the DH parameters, default 2048
#   DRY_RUN             1 = print the steps, change nothing (also what the tests run)
set -euo pipefail
umask 022

# ---------------------------------------------------------------- parameters
SVC_USER="${SVC_USER:-ubuntu}"
DEPLOY_PUBKEY="${DEPLOY_PUBKEY:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
NODE_MAJOR="${NODE_MAJOR:-20}"
WORKER_CONNECTIONS="${WORKER_CONNECTIONS:-8192}"
ENABLE_UFW="${ENABLE_UFW:-1}"
DH_BITS="${DH_BITS:-2048}"
DRY_RUN="${DRY_RUN:-0}"
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"
APT_PACKAGES="${APT_PACKAGES:-nginx certbot git rsync curl python3 openssl ufw ca-certificates gnupg}"
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt}"
ACME_ROOT="${ACME_ROOT:-/var/www/acme}"
KEYRING="${KEYRING:-/etc/apt/keyrings/nodesource.gpg}"
NODESOURCE_LIST="${NODESOURCE_LIST:-/etc/apt/sources.list.d/nodesource.list}"
NODESOURCE_KEY_URL="${NODESOURCE_KEY_URL:-https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1 bootstrap-host: $2"; }
run() { if [ "$DRY_RUN" = 1 ]; then log INFO "dry-run: $*"; else "$@"; fi; }

log INFO "start: user=$SVC_USER node=$NODE_MAJOR worker_connections=$WORKER_CONNECTIONS ufw=$ENABLE_UFW dry_run=$DRY_RUN"
if [ "$REQUIRE_ROOT" = 1 ] && [ "$DRY_RUN" != 1 ] && [ "$(id -u)" -ne 0 ]; then log ERROR "run as root"; exit 1; fi
[[ "$SVC_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { log ERROR "SVC_USER '$SVC_USER' is not a valid user name"; exit 64; }
[[ "$NODE_MAJOR" =~ ^[0-9]{2}$ ]] || { log ERROR "NODE_MAJOR '$NODE_MAJOR' must be two digits"; exit 64; }
[[ "$WORKER_CONNECTIONS" =~ ^[0-9]{3,6}$ ]] || { log ERROR "WORKER_CONNECTIONS '$WORKER_CONNECTIONS' must be a number"; exit 64; }
if [ -n "$DEPLOY_PUBKEY" ] && ! [[ "$DEPLOY_PUBKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256)\ [A-Za-z0-9+/=]+(\ [^[:cntrl:]]*)?$ ]]; then
  log ERROR "DEPLOY_PUBKEY is not one public key line (ssh-ed25519 AAAA… comment)"; exit 64
fi

# ---------------------------------------------------------------- 1. packages and Node
log INFO "1. packages: $APT_PACKAGES"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -q
# shellcheck disable=SC2086
run apt-get install -y -q $APT_PACKAGES
if command -v node >/dev/null 2>&1 && [ "$(node --version | sed 's/^v\([0-9]*\).*/\1/')" = "$NODE_MAJOR" ]; then
  log INFO "   node $(node --version) is installed"
else
  log INFO "   installing Node $NODE_MAJOR from the NodeSource apt repository (signed-by $KEYRING)"
  if [ "$DRY_RUN" != 1 ]; then
    install -d -m 0755 "$(dirname "$KEYRING")"
    curl -fsSL "$NODESOURCE_KEY_URL" | gpg --dearmor --yes -o "$KEYRING"
    echo "deb [signed-by=$KEYRING] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > "$NODESOURCE_LIST"
    apt-get update -q
    apt-get install -y -q nodejs
    log INFO "   node $(node --version)"
  fi
fi

# ---------------------------------------------------------------- 2. service user and the dispatcher's key
log INFO "2. service user $SVC_USER"
if id "$SVC_USER" >/dev/null 2>&1; then log INFO "   user exists"; else run useradd --create-home --shell /bin/bash "$SVC_USER"; fi
if [ -n "$DEPLOY_PUBKEY" ]; then
  home="$( (getent passwd "$SVC_USER" 2>/dev/null || true) | cut -d: -f6)"; home="${home:-/home/$SVC_USER}"
  keys="$home/.ssh/authorized_keys"
  if [ -f "$keys" ] && grep -qxF "$DEPLOY_PUBKEY" "$keys"; then
    log INFO "   the dispatcher's key is already in $keys"
  elif [ "$DRY_RUN" = 1 ]; then
    log INFO "dry-run: add the dispatcher's public key to $keys"
  else
    install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$home/.ssh"
    printf '%s\n' "$DEPLOY_PUBKEY" >> "$keys"
    chown "$SVC_USER:$SVC_USER" "$keys"; chmod 0600 "$keys"
    log INFO "   added the dispatcher's public key to $keys"
  fi
else
  log WARNING "   DEPLOY_PUBKEY is empty: authorized_keys of $SVC_USER is left as it is; the dispatcher cannot deploy until its key is there"
fi

# ---------------------------------------------------------------- 3. nginx: log format and connection limit
log INFO "3. nginx: log format 'full', worker_connections $WORKER_CONNECTIONS"
if [ "$DRY_RUN" != 1 ]; then
  install -d -m 0755 "$NGINX_DIR/conf.d"
  # http-level file of this script: conf.d is included inside http {} by the stock nginx.conf of Ubuntu
  cat > "$NGINX_DIR/conf.d/00-agent-team-log-format.conf" <<'NGX'
# written by deploy/service/bootstrap-host.sh — the access-log format the agent-team vhost templates use
log_format full '$remote_addr - - [$time_iso8601] "$request" $status $body_bytes_sent '
                '"$http_referer" "$http_user_agent" $host $request_time';
NGX
  if grep -qE '^[[:space:]]*worker_connections[[:space:]]+[0-9]+;' "$NGINX_DIR/nginx.conf"; then
    sed -i -E "s/^([[:space:]]*worker_connections[[:space:]]+)[0-9]+;/\1${WORKER_CONNECTIONS};/" "$NGINX_DIR/nginx.conf"
  else
    log WARNING "   no worker_connections line in $NGINX_DIR/nginx.conf: set it by hand"
  fi
  install -d -m 0755 "$ACME_ROOT"
fi

# ---------------------------------------------------------------- 4. TLS files the vhost template includes
log INFO "4. TLS options and DH parameters in $LETSENCRYPT_DIR"
if [ "$DRY_RUN" != 1 ]; then
  install -d -m 0755 "$LETSENCRYPT_DIR"
  if [ ! -f "$LETSENCRYPT_DIR/options-ssl-nginx.conf" ]; then
    cat > "$LETSENCRYPT_DIR/options-ssl-nginx.conf" <<'TLS'
# written by deploy/service/bootstrap-host.sh (certbot's nginx plugin is not used; the vhost template includes this file)
ssl_session_cache shared:le_nginx_SSL:10m;
ssl_session_timeout 1440m;
ssl_session_tickets off;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_ciphers "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384";
TLS
    log INFO "   wrote options-ssl-nginx.conf"
  fi
  if [ ! -s "$LETSENCRYPT_DIR/ssl-dhparams.pem" ]; then
    log INFO "   generating $DH_BITS-bit DH parameters (about a minute)"
    openssl dhparam -out "$LETSENCRYPT_DIR/ssl-dhparams.pem" "$DH_BITS" 2>/dev/null
  fi
  if [ -n "$CERTBOT_EMAIL" ]; then
    install -d -m 0755 "$ETC_DIR"
    if [ -f "$ETC_DIR/env" ] && grep -q '^CERTBOT_EMAIL=.\+' "$ETC_DIR/env"; then log INFO "   $ETC_DIR/env already has CERTBOT_EMAIL"
    else printf 'CERTBOT_EMAIL=%s\n' "$CERTBOT_EMAIL" >> "$ETC_DIR/env"; log INFO "   CERTBOT_EMAIL written to $ETC_DIR/env"; fi
  fi
  nginx -t && systemctl reload nginx
fi

# ---------------------------------------------------------------- 5. firewall
if [ "$ENABLE_UFW" = 1 ]; then
  log INFO "5. firewall: OpenSSH, 80, 443"
  run ufw allow OpenSSH
  run ufw allow 80/tcp
  run ufw allow 443/tcp
  run ufw --force enable
else
  log INFO "5. firewall left alone (ENABLE_UFW=0)"
fi

log INFO "done. Next, from the Mac: deploy/droplet/remote-install.sh and deploy/service/remote-install.sh with DROPLET_HOST=root@<this server>,"
log INFO "then on this server: /opt/agent-team/bin/add-service.sh <slug> <host> local --run '…' --build '…'"
echo "BOOTSTRAPPED user=$SVC_USER node=$NODE_MAJOR"
