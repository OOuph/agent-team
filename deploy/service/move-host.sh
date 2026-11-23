#!/usr/bin/env bash
# move-host.sh — move a registered service to a new host name without stopping it. Run as root on the droplet.
#
# Usage: move-host.sh <slug> <new-host>
#   slug       a service registered by add-service.sh (/etc/agent-team/services/<slug>.conf)
#   new-host   FQDN; the owner's DNS A record must already point here (checked first; exit 2 otherwise, nothing changed)
# Env: CERTBOT_EMAIL (or CERTBOT_EMAIL= in /etc/agent-team/env) — required for a new certificate.
#
# Why (owner 23.11, ai-battles → battles.bymachines.ai): add-service.sh with a new host would overwrite the live vhost
# with the http-only bootstrap block while certbot runs — every running match would lose its WebSocket — and it would
# leave the old name answering with another site's certificate.
#
# Steps (idempotent; a repeated run changes nothing; the running server is never restarted):
#   0. DNS check of the new host
#   a. certificate for the new host: a SEPARATE http-only bootstrap vhost (<slug>-acme.conf) serves the ACME path, the
#      live vhost is not touched; certbot --webroot; the bootstrap file is removed (skipped when the certificate exists)
#   b. the service vhost is rendered for the new host (render-vhost.sh, same template, same upstream)
#   c. the old host keeps answering: <slug>-old-host.conf redirects http and https of the old name to the new one (301)
#      with the old certificate, and still serves the ACME path so that certificate keeps renewing
#   d. one `nginx -t` + reload (graceful: open WebSockets stay on the old workers until they close); on a failed test
#      the previous vhost comes back and nothing is reloaded
#   e. HOST= in the service definition becomes the new host; the old one is kept in OLD_HOST=; deploy journal line
# Not done here (the dispatcher's side): products/<slug>/product.yaml host / site_base, and the repository's own texts.
set -euo pipefail
umask 022

# ---------------------------------------------------------------- settings
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
SERVICES_ETC="${SERVICES_ETC:-$ETC_DIR/services}"
ENV_FILE="${ENV_FILE:-$ETC_DIR/env}"
TEMPLATE="${TEMPLATE:-$ETC_DIR/service.conf.template}"
RENDER_VHOST="${RENDER_VHOST:-/opt/agent-team/bin/render-vhost.sh}"
NGINX_AVAILABLE="${NGINX_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled}"
LETSENCRYPT_LIVE="${LETSENCRYPT_LIVE:-/etc/letsencrypt/live}"
ACME_ROOT="${ACME_ROOT:-/var/www/acme}"
PUBLIC_IP_URL="${PUBLIC_IP_URL:-https://ifconfig.me}"
LOG_DIR="${LOG_DIR:-/var/log/agent-team}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-10}"
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"
SKIP_DNS="${SKIP_DNS:-0}"
SKIP_CERT="${SKIP_CERT:-0}"
SKIP_HEALTH="${SKIP_HEALTH:-0}"

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; }
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1 move-host: $2"; }
journal() { mkdir -p "$LOG_DIR"; echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) slug=$SLUG $*" >> "$LOG_DIR/deploy.log" 2>/dev/null || true; }

case "${1:-}" in -h|--help) usage; exit 0 ;; esac
[ $# -eq 2 ] || { usage >&2; exit 64; }
SLUG="$1"; NEW="$2"
if [ "$REQUIRE_ROOT" = 1 ] && [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root" >&2; exit 1; fi
[[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || { echo "ERROR: slug '$SLUG' must match [a-z0-9][a-z0-9-]*" >&2; exit 64; }
[[ "$NEW" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo "ERROR: host '$NEW' is not a valid FQDN" >&2; exit 64; }
CONF="$SERVICES_ETC/$SLUG.conf"
[ -f "$CONF" ] || { echo "ERROR: $CONF missing — the service is not registered (add-service.sh)" >&2; exit 1; }
[ -f "$TEMPLATE" ] || { echo "ERROR: $TEMPLATE missing — run deploy/service/install.sh first" >&2; exit 1; }
# shellcheck disable=SC1090
OLD="$(. "$CONF"; echo "${HOST:-}")"; HEALTH_PATH="$(. "$CONF"; echo "${HEALTH_PATH:-/healthz}")"
[ -n "$OLD" ] || { echo "ERROR: $CONF has no HOST=" >&2; exit 1; }
VHOST="$NGINX_AVAILABLE/$SLUG.conf"
ACME_VHOST="$NGINX_AVAILABLE/$SLUG-acme.conf"
OLD_VHOST="$NGINX_AVAILABLE/$SLUG-old-host.conf"
log INFO "start: slug=$SLUG, host now $OLD, wanted $NEW"

reload_nginx() { nginx -t && systemctl reload nginx; }

# ---------------------------------------------------------------- 0. DNS (same check as add-service.sh)
if [ "$SKIP_DNS" != 1 ]; then
  resolved="$(getent ahosts "$NEW" | awk '{print $1}' | sort -u || true)"
  [ -n "$resolved" ] || { echo "ERROR: $NEW does not resolve yet. Nothing changed." >&2; exit 2; }
  local_ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' | sort -u || true)"
  public_ip="$(curl -s --max-time 5 "$PUBLIC_IP_URL" 2>/dev/null | tr -d '[:space:]' || true)"
  dns_ok=0
  while IFS= read -r ip; do
    [ -n "$ip" ] || continue
    if echo "$local_ips" | grep -qxF "$ip" || { [ -n "$public_ip" ] && [ "$ip" = "$public_ip" ]; }; then dns_ok=1; break; fi
  done <<< "$resolved"
  [ "$dns_ok" = 1 ] || { echo "ERROR: $NEW resolves to [$(echo "$resolved" | tr '\n' ' ')], not to this server. Nothing changed." >&2; exit 2; }
  log INFO "DNS ok: $NEW -> $(echo "$resolved" | tr '\n' ' ')"
fi

# ---------------------------------------------------------------- a. certificate of the new host, the live vhost untouched
CERT_DIR="$LETSENCRYPT_LIVE/$NEW"
if [ "$SKIP_CERT" = 1 ] || [ -f "$CERT_DIR/fullchain.pem" ]; then
  log INFO "certificate: $( [ "$SKIP_CERT" = 1 ] && echo skipped || echo "exists in $CERT_DIR" )"
else
  if [ -z "${CERTBOT_EMAIL:-}" ] && [ -f "$ENV_FILE" ]; then
    CERTBOT_EMAIL="$(sed -n 's/^CERTBOT_EMAIL=[[:space:]]*//p' "$ENV_FILE" | tail -n 1 | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' | tr -d '"'"'" || true)"
  fi
  [ -n "${CERTBOT_EMAIL:-}" ] || { echo "ERROR: CERTBOT_EMAIL is empty — set it in $ENV_FILE or in the environment. Nothing changed." >&2; exit 1; }
  log INFO "a. bootstrap vhost $ACME_VHOST for the ACME challenge of $NEW (the live vhost of $OLD is not touched)"
  mkdir -p "$ACME_ROOT" "$NGINX_AVAILABLE" "$NGINX_ENABLED"
  cat > "$ACME_VHOST" <<EOF
# agent-team: temporary http-only vhost for the certificate of $NEW (move-host.sh); removed right after certbot.
server {
    listen 80;
    listen [::]:80;
    server_name $NEW;
    location /.well-known/acme-challenge/ { root $ACME_ROOT; }
    location / { return 404; }
}
EOF
  ln -sfn "$ACME_VHOST" "$NGINX_ENABLED/$SLUG-acme.conf"
  if ! reload_nginx; then rm -f "$NGINX_ENABLED/$SLUG-acme.conf" "$ACME_VHOST"; echo "ERROR: nginx refused the bootstrap vhost; it is removed, nothing else changed" >&2; exit 1; fi
  rc=0
  certbot certonly --webroot -w "$ACME_ROOT" -d "$NEW" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --keep-until-expiring || rc=$?
  rm -f "$NGINX_ENABLED/$SLUG-acme.conf" "$ACME_VHOST"
  if [ "$rc" -ne 0 ] || [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    reload_nginx || true
    echo "ERROR: certbot did not issue a certificate for $NEW (rc=$rc); the bootstrap vhost is removed, the service is as it was" >&2; exit 1
  fi
  log INFO "a. certificate issued: $CERT_DIR"
fi

if [ "$OLD" = "$NEW" ]; then
  log INFO "the service definition already names $NEW: the vhost is rendered again, no redirect of an old host"
fi

# ---------------------------------------------------------------- b. the service vhost for the new host (no reload yet)
backup=""
if [ -f "$VHOST" ]; then backup="$(mktemp)"; cp -p "$VHOST" "$backup"; fi
restore() {
  if [ -n "$backup" ]; then cp -p "$backup" "$VHOST"; else rm -f "$VHOST" "$NGINX_ENABLED/$SLUG.conf"; fi
  rm -f "$NGINX_ENABLED/$SLUG-old-host.conf" "$OLD_VHOST.new"
}
set +e
TEMPLATE="$TEMPLATE" "$RENDER_VHOST" "$SLUG" "$NEW" --no-reload
rc=$?
set -e
case "$rc" in 0|3) log INFO "b. vhost for $NEW rendered (render-vhost rc=$rc)" ;; *) echo "ERROR: render-vhost.sh failed (rc=$rc); the previous vhost is back in place" >&2; exit 1 ;; esac

# ---------------------------------------------------------------- c. the old host redirects to the new one
if [ "$OLD" != "$NEW" ]; then
  old_cert="$LETSENCRYPT_LIVE/$OLD"
  {
    echo "# agent-team: $OLD is the previous host of service $SLUG (move-host.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)); every request goes to $NEW."
    echo "server {"
    echo "    listen 80;"
    echo "    listen [::]:80;"
    echo "    server_name $OLD;"
    echo "    location /.well-known/acme-challenge/ { root $ACME_ROOT; }"
    echo "    location / { return 301 https://$NEW\$request_uri; }"
    echo "}"
    if [ "$SKIP_CERT" = 1 ] || [ -f "$old_cert/fullchain.pem" ]; then
      echo "server {"
      echo "    listen 443 ssl http2;"
      echo "    listen [::]:443 ssl http2;"
      echo "    server_name $OLD;"
      echo "    ssl_certificate     $old_cert/fullchain.pem;"
      echo "    ssl_certificate_key $old_cert/privkey.pem;"
      echo "    return 301 https://$NEW\$request_uri;"
      echo "}"
    fi
  } > "$OLD_VHOST.new"
  mv "$OLD_VHOST.new" "$OLD_VHOST"
  ln -sfn "$OLD_VHOST" "$NGINX_ENABLED/$SLUG-old-host.conf"
  log INFO "c. redirect vhost $OLD_VHOST: $OLD -> $NEW"
fi

# ---------------------------------------------------------------- d. one test, one graceful reload
if ! nginx -t; then
  restore
  echo "ERROR: nginx -t failed with the new vhosts; the previous vhost is back, nginx was not reloaded" >&2; exit 1
fi
systemctl reload nginx
[ -n "$backup" ] && rm -f "$backup"
log INFO "d. nginx reloaded"

# ---------------------------------------------------------------- e. the service definition and the journal
if [ "$OLD" != "$NEW" ]; then
  cp -p "$CONF" "$CONF.before-move-$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="$(mktemp)"
  { grep -v -E '^(HOST|OLD_HOST)=' "$CONF"; printf 'HOST=%q\n' "$NEW"; printf 'OLD_HOST=%q\n' "$OLD"; } > "$tmp"
  install -m 0644 "$tmp" "$CONF"; rm -f "$tmp"
  journal "action=host-moved from=$OLD to=$NEW"
  log INFO "e. $CONF: HOST=$NEW, OLD_HOST=$OLD"
fi
if [ "$SKIP_HEALTH" != 1 ]; then
  if curl -fsS --max-time "$HEALTH_TIMEOUT_SEC" "https://$NEW$HEALTH_PATH" >/dev/null; then log INFO "health: https://$NEW$HEALTH_PATH answers"
  else log WARNING "health: https://$NEW$HEALTH_PATH did not answer in ${HEALTH_TIMEOUT_SEC}s — check the service and DNS"; fi
fi
echo "== $SLUG now lives at https://$NEW/ ; $OLD redirects there. Next: products/$SLUG/product.yaml host and site_base (the dispatcher's side)."
