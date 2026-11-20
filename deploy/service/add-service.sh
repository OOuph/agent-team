#!/usr/bin/env bash
# add-service.sh — register a long-running service (game server) on the droplet: service definition, two local ports,
# nginx vhost with WebSocket proxying, Let's Encrypt certificate, secrets file placeholder, first deploy. Run as root.
#
# Usage: add-service.sh <slug> <host> <repo> [--branch main] [--run "npm run start"] [--build "npm run build"]
#                       [--install CMD] [--health /healthz] [--ports A,B] [--no-deploy]
#   slug   [a-z0-9-], the product slug in the dispatcher (products/<slug>/product.yaml)
#   host   FQDN; the owner's DNS A record must already point here (checked first; exit 2 otherwise, nothing changed)
#   repo   https git URL of the game repository (a public one, or one the droplet's deploy key can read)
# Env: CERTBOT_EMAIL (or CERTBOT_EMAIL= in /etc/agent-team/env) — required for a new certificate.
#
# Steps (idempotent, re-running is safe):
#   0. DNS check                     -  /etc/agent-team/services/<slug>.conf (written once; edit by hand to change commands)
#   -  /etc/agent-team/services/<slug>.secrets.env placeholder (root:<user> 0640) — the owner fills values in by hand
#   a. http-only bootstrap vhost (serves /.well-known/acme-challenge/) → certbot certonly --webroot (skipped when the certificate exists)
#   b. empty upstream file (points at PORT_A) + full vhost from service.conf.template via render-vhost.sh
#   c. deploy-service.sh <slug> (unless --no-deploy)
set -euo pipefail
umask 022

ETC_DIR="${ETC_DIR:-/etc/agent-team}"
SERVICES_ETC="${SERVICES_ETC:-$ETC_DIR/services}"
ENV_FILE="${ENV_FILE:-$ETC_DIR/env}"
TEMPLATE="${TEMPLATE:-$ETC_DIR/service.conf.template}"
RENDER_VHOST="${RENDER_VHOST:-/opt/agent-team/bin/render-vhost.sh}"
DEPLOY="${DEPLOY:-/opt/agent-team/bin/deploy-service.sh}"
NGINX_AVAILABLE="${NGINX_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled}"
NGINX_CONFD="${NGINX_CONFD:-/etc/nginx/conf.d}"
LETSENCRYPT_LIVE="${LETSENCRYPT_LIVE:-/etc/letsencrypt/live}"
PUBLIC_IP_URL="${PUBLIC_IP_URL:-https://ifconfig.me}"
PORT_BASE="${PORT_BASE:-8100}"                       # first free pair above this: 8101/8102, 8111/8112, …
SVC_USER="${SVC_USER:-ubuntu}"
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"
SKIP_DNS="${SKIP_DNS:-0}"
SKIP_CERT="${SKIP_CERT:-0}"

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }

BRANCH="main"; RUN_CMD="npm run start"; BUILD_CMD="npm run build"; INSTALL_CMD=""; HEALTH_PATH="/healthz"; PORTS=""; DO_DEPLOY=1
pos=()
while [ $# -gt 0 ]; do
  case "$1" in
    --branch) BRANCH="$2"; shift ;;
    --run) RUN_CMD="$2"; shift ;;
    --build) BUILD_CMD="$2"; shift ;;
    --install) INSTALL_CMD="$2"; shift ;;
    --health) HEALTH_PATH="$2"; shift ;;
    --ports) PORTS="$2"; shift ;;
    --no-deploy) DO_DEPLOY=0 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "ERROR: unknown option $1" >&2; usage >&2; exit 64 ;;
    *) pos+=("$1") ;;
  esac
  shift
done
[ "${#pos[@]}" -eq 3 ] || { usage >&2; exit 64; }
SLUG="${pos[0]}"; HOST="${pos[1]}"; REPO="${pos[2]}"

if [ "$REQUIRE_ROOT" = 1 ] && [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root" >&2; exit 1; fi
[[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || { echo "ERROR: slug '$SLUG' must match [a-z0-9][a-z0-9-]*" >&2; exit 64; }
[[ "$HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "ERROR: host '$HOST' is not a valid FQDN" >&2; exit 64; }
[[ "$REPO" =~ ^(https://|git@)[^[:space:]]+$ ]] || { echo "ERROR: repo '$REPO' must be an https or git@ URL" >&2; exit 64; }
case "$HEALTH_PATH" in /*) ;; *) HEALTH_PATH="/$HEALTH_PATH" ;; esac
[ -f "$TEMPLATE" ] || { echo "ERROR: $TEMPLATE missing — run deploy/service/install.sh first" >&2; exit 1; }
[ -x "$DEPLOY" ] || { echo "ERROR: $DEPLOY missing — run deploy/service/install.sh first" >&2; exit 1; }
if [ -z "${CERTBOT_EMAIL:-}" ] && [ -f "$ENV_FILE" ]; then
  CERTBOT_EMAIL="$(sed -n 's/^CERTBOT_EMAIL=[[:space:]]*//p' "$ENV_FILE" | tail -n 1 | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' | tr -d '"'"'" || true)"
fi
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"

CONF="$SERVICES_ETC/$SLUG.conf"
SECRETS="$SERVICES_ETC/$SLUG.secrets.env"
VHOST="$NGINX_AVAILABLE/$SLUG.conf"
VHOST_LINK="$NGINX_ENABLED/$SLUG.conf"
UPSTREAM="$NGINX_CONFD/agent-team-svc-$SLUG.conf"
CERT_DIR="$LETSENCRYPT_LIVE/$HOST"
echo "== add-service $SLUG host=$HOST repo=$REPO branch=$BRANCH run='$RUN_CMD' build='$BUILD_CMD' health=$HEALTH_PATH"

# ---------------------------------------------------------------- 0. DNS (same check as add-site.sh)
if [ "$SKIP_DNS" != 1 ]; then
  resolved="$(getent ahosts "$HOST" | awk '{print $1}' | sort -u || true)"
  [ -n "$resolved" ] || { echo "ERROR: $HOST does not resolve yet. Ask the owner for the DNS A record, then re-run." >&2; exit 2; }
  local_ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' | sort -u || true)"
  public_ip="$(curl -s --max-time 5 "$PUBLIC_IP_URL" 2>/dev/null | tr -d '[:space:]' || true)"
  dns_ok=0
  while IFS= read -r ip; do
    [ -n "$ip" ] || continue
    if echo "$local_ips" | grep -qxF "$ip" || { [ -n "$public_ip" ] && [ "$ip" = "$public_ip" ]; }; then dns_ok=1; break; fi
  done <<< "$resolved"
  [ "$dns_ok" = 1 ] || { echo "ERROR: $HOST resolves to [$(echo "$resolved" | tr '\n' ' ')], this server is [$(echo "$local_ips" | tr '\n' ' ')${public_ip:+ public $public_ip}]. Nothing changed." >&2; exit 2; }
  echo "DNS: ok ($HOST → $(echo "$resolved" | tr '\n' ' '))"
fi

# ---------------------------------------------------------------- service definition + ports
mkdir -p "$SERVICES_ETC" "$NGINX_CONFD"
if [ -f "$CONF" ]; then
  echo "conf: $CONF exists — keeping it (edit by hand to change commands or ports)"
  # shellcheck disable=SC1090
  . "$CONF"
else
  if [ -n "$PORTS" ]; then
    PORT_A="${PORTS%%,*}"; PORT_B="${PORTS##*,}"
  else
    used="$(cat "$SERVICES_ETC"/*.conf 2>/dev/null | sed -n 's/^PORT_[AB]=//p' | tr -d '"' || true)"
    n=0; PORT_A=""; PORT_B=""
    while [ "$n" -lt 100 ]; do
      a=$((PORT_BASE + 1 + n * 10)); b=$((a + 1))
      if ! echo "$used" | grep -qx "$a" && ! echo "$used" | grep -qx "$b"; then PORT_A="$a"; PORT_B="$b"; break; fi
      n=$((n + 1))
    done
    [ -n "$PORT_A" ] || { echo "ERROR: no free port pair above $PORT_BASE" >&2; exit 1; }
  fi
  [[ "$PORT_A" =~ ^[0-9]{2,5}$ && "$PORT_B" =~ ^[0-9]{2,5}$ ]] || { echo "ERROR: bad --ports '$PORTS'" >&2; exit 64; }
  {
    echo "# agent-team service $SLUG — written by add-service.sh $(date -u +%Y-%m-%dT%H:%M:%SZ); sourced by deploy-service.sh"
    printf 'HOST=%q\n' "$HOST"; printf 'REPO=%q\n' "$REPO"; printf 'BRANCH=%q\n' "$BRANCH"
    printf 'RUN_CMD=%q\n' "$RUN_CMD"; printf 'BUILD_CMD=%q\n' "$BUILD_CMD"; printf 'INSTALL_CMD=%q\n' "$INSTALL_CMD"
    printf 'HEALTH_PATH=%q\n' "$HEALTH_PATH"; printf 'PORT_A=%q\n' "$PORT_A"; printf 'PORT_B=%q\n' "$PORT_B"
  } > "$CONF"
  chmod 644 "$CONF"
  echo "conf: wrote $CONF (ports $PORT_A/$PORT_B)"
fi
if [ ! -f "$SECRETS" ]; then
  {
    echo "# $SLUG secrets — filled in by the owner by hand, sourced by svc-run.sh before the server starts (KEY=value per line)."
    echo "# Never in git, never read by the dispatcher. Example: SESSION_SECRET=…"
  } > "$SECRETS"
  chmod 640 "$SECRETS"
  if [ "$REQUIRE_ROOT" = 1 ]; then chgrp "$SVC_USER" "$SECRETS" || true; fi
  echo "secrets: placeholder $SECRETS (root:$SVC_USER 0640)"
fi

reload_nginx() { nginx -t; systemctl reload nginx; }

# ---------------------------------------------------------------- a. certificate
if [ "$SKIP_CERT" = 1 ] || [ -f "$CERT_DIR/fullchain.pem" ]; then
  echo "cert: ${SKIP_CERT:+skipped}${CERT_DIR:+ }$CERT_DIR"
else
  [ -n "$CERTBOT_EMAIL" ] || { echo "ERROR: CERTBOT_EMAIL is empty — set it in $ENV_FILE or in the environment" >&2; exit 1; }
  echo "a. http-only bootstrap vhost $VHOST"
  mkdir -p /var/www/acme
  cat > "$VHOST" <<EOF
# agent-team bootstrap vhost for $HOST — replaced by the full service config right after certbot.
server {
    listen 80;
    listen [::]:80;
    server_name $HOST;
    root /var/www/acme;
    access_log /var/log/nginx/access.log full;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
    location / { return 404; }
}
EOF
  ln -sfn "$VHOST" "$VHOST_LINK"
  reload_nginx
  echo "a. certbot certonly --webroot /var/www/acme -d $HOST (email $CERTBOT_EMAIL)"
  # webroot, not the nginx plugin: python3-certbot-nginx is not installed on the droplet and the full vhost
  # (service.conf.template) already carries the ssl and redirect lines; renewals keep working through /var/www/acme.
  certbot certonly --webroot -w /var/www/acme -d "$HOST" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --keep-until-expiring
  [ -f "$CERT_DIR/fullchain.pem" ] || { echo "ERROR: certbot finished but $CERT_DIR/fullchain.pem is missing" >&2; exit 1; }
fi

# ---------------------------------------------------------------- b. upstream + full vhost
if [ ! -f "$UPSTREAM" ]; then
  {
    echo "# agent-team service $SLUG — placeholder until the first deploy; deploy-service.sh rewrites this file"
    echo "upstream svc_${SLUG//-/_} {"
    echo "    server 127.0.0.1:$PORT_A;"
    echo "}"
  } > "$UPSTREAM"
  echo "b. upstream placeholder $UPSTREAM → :$PORT_A"
fi
echo "b. vhost from $TEMPLATE via $RENDER_VHOST"
set +e
TEMPLATE="$TEMPLATE" "$RENDER_VHOST" "$SLUG" "$HOST"
rc=$?
set -e
case "$rc" in 0|3) ;; *) echo "ERROR: render-vhost.sh failed (rc=$rc); the previous vhost (if any) is back in place" >&2; exit 1 ;; esac

# ---------------------------------------------------------------- c. first deploy
if [ "$DO_DEPLOY" = 1 ]; then
  echo "c. first deploy: $DEPLOY $SLUG"
  if "$DEPLOY" "$SLUG"; then
    echo "== $SLUG is live: https://$HOST/ — set product.yaml deploy: {mode: service, host: local}; the dispatcher redeploys after every merge"
  else
    echo "== $SLUG registered, but the first deploy failed — see /var/log/agent-team/deploy.log and the output above" >&2; exit 1
  fi
else
  echo "== $SLUG registered (no deploy). Deploy with: $DEPLOY $SLUG"
fi
