#!/usr/bin/env bash
# service-host.sh — the host names of a registered service: add a second address, make it the main one, drop one.
# Runs as root on the host of the service; the service user may call it through sudo (agent-team-deploy.sudoers).
#
# Usage: service-host.sh alias   <slug> <host> [--print]   the host answers with the service beside the main address:
#                                                         its own certificate, the same upstream, headers and WebSocket
#        service-host.sh primary <slug> <host> [--print]   an alias becomes the main address; the previous main address
#                                                         answers 301 with the same path (see «old host» below)
#        service-host.sh unalias <slug> <host> [--print]   the alias stops answering (its vhost is removed; the
#                                                         certificate stays) — e.g. before add-site.sh --redirect
#        service-host.sh list    <slug>                    the main address, the previous one and the aliases
#   slug    a service registered by add-service.sh (/etc/agent-team/services/<slug>.conf)
#   host    FQDN whose DNS A record already points at this server (checked first; exit 2 otherwise, nothing changed)
#   --print write to stdout what would be installed and change nothing (no root needed; used by the tests)
# Env: CERTBOT_EMAIL (or CERTBOT_EMAIL= in /etc/agent-team/env) — required for a new certificate.
#
# Why (owner 27.11: ai-battles becomes Clankodrome, clankodrome.com beside battles.bymachines.ai): move-host.sh moves a
# service in one step and only root may run it. A rename needs two steps with a look in between — the game must answer
# on both names before its canonical address is switched — and on the game's own server the owner has no root shell,
# only the service user with a narrow sudo rule.
#
# What each command does (idempotent; the running server is never restarted; nginx is reloaded gracefully, so open
# WebSockets stay on the old workers until they close; a failed `nginx -t` puts the previous files back):
#   alias    0. DNS check   a. certificate by certbot --webroot through an http-only block of the alias's own vhost
#            (the main vhost is not touched)   b. <slug>-alias-<host>.conf: the service template without its `map`
#            block (the main vhost defines it), server_name = the alias   c. nginx -t, reload, journal line
#   primary  the host must be an alias with a certificate.   a. its alias vhost is taken out   b. the main vhost is
#            rendered for it (render-vhost.sh, as add-service.sh does)   c. <slug>-old-host.conf for the previous main
#            address   d. nginx -t, reload   e. HOST= / OLD_HOST= in the service definition, journal line
#   unalias  the alias vhost is removed, nginx -t, reload
# Old host: every request gets 301 to https://<new host><same path>, except
#   /.well-known/acme-challenge/   so that its certificate keeps renewing
#   <HEALTH_PATH> and /_team/      proxied to the service as before: the dispatcher's health check after a deploy and its
#                                  metrics fetch read the address in product.yaml, which the operator changes separately —
#                                  a deploy or a metrics run between the two changes must not fail on a redirect
# Not done here (the dispatcher's side): products/<slug>/product.yaml `host` / `site_base`; the service's own setting
# of its canonical address.
# Exit: 0 done or nothing to do · 1 error (previous state restored) · 2 DNS not ready · 64 usage.
set -euo pipefail
umask 022

# ---------------------------------------------------------------- settings
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
SERVICES_ETC="${SERVICES_ETC:-$ETC_DIR/services}"
ENV_FILE="${ENV_FILE:-$ETC_DIR/env}"
TEMPLATE="${TEMPLATE:-$ETC_DIR/service.conf.template}"
INDEXNOW_KEY_FILE="${INDEXNOW_KEY_FILE:-$ETC_DIR/indexnow.key}"
RENDER_VHOST="${RENDER_VHOST:-/opt/agent-team/bin/render-vhost.sh}"
NGINX_AVAILABLE="${NGINX_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt}"
LETSENCRYPT_LIVE="${LETSENCRYPT_LIVE:-$LETSENCRYPT_DIR/live}"
ACME_ROOT="${ACME_ROOT:-/var/www/acme}"
HEADERS_SNIPPET="${HEADERS_SNIPPET:-/etc/nginx/snippets/agent-team-headers.conf}"
PUBLIC_IP_URL="${PUBLIC_IP_URL:-https://ifconfig.me}"
LOG_DIR="${LOG_DIR:-/var/log/agent-team}"
LOCK_FILE="${LOCK_FILE:-/run/lock/agent-team-service-host.lock}"
LOCK_WAIT_SEC="${LOCK_WAIT_SEC:-120}"        # one change of the vhosts at a time
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-10}"
KEEP_ON_OLD_HOST="${KEEP_ON_OLD_HOST:-/_team/}"   # besides the health path: still proxied on the previous main address
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"
SKIP_DNS="${SKIP_DNS:-0}"
SKIP_CERT="${SKIP_CERT:-0}"
SKIP_HEALTH="${SKIP_HEALTH:-0}"
FQDN_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1 service-host: $2"; }
journal() { mkdir -p "$LOG_DIR" 2>/dev/null || true; echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) slug=$SLUG $*" >> "$LOG_DIR/deploy.log" 2>/dev/null || true; }
die() { echo "ERROR: $2" >&2; exit "$1"; }

case "${1:-}" in -h|--help|"") usage; [ -n "${1:-}" ] && exit 0 || exit 64 ;; esac
CMD="$1"; shift
PRINT=0
args=()
for a in "$@"; do case "$a" in --print) PRINT=1 ;; *) args+=("$a") ;; esac; done
case "$CMD" in
  alias|primary|unalias) [ "${#args[@]}" -eq 2 ] || { usage >&2; exit 64; } ;;
  list) [ "${#args[@]}" -eq 1 ] || { usage >&2; exit 64; }; PRINT=1 ;;
  *) usage >&2; exit 64 ;;
esac
SLUG="${args[0]}"; NEW="${args[1]:-}"
[[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || die 64 "slug '$SLUG' must match [a-z0-9][a-z0-9-]*"
if [ "$CMD" != list ]; then
  [ "${#NEW}" -le 253 ] && [[ "$NEW" =~ $FQDN_RE ]] || die 64 "host '$NEW' is not a valid FQDN"
fi
if [ "$PRINT" != 1 ] && [ "$REQUIRE_ROOT" = 1 ] && [ "$(id -u)" -ne 0 ]; then die 1 "run as root (sudo -n $0 …), or add --print to see what would be installed"; fi
CONF="$SERVICES_ETC/$SLUG.conf"
[ -f "$CONF" ] || die 1 "$CONF missing — the service is not registered (add-service.sh)"
# shellcheck disable=SC1090
MAIN="$(. "$CONF"; echo "${HOST:-}")"; HEALTH_PATH="$(. "$CONF"; echo "${HEALTH_PATH:-/healthz}")"; OLD_RECORDED="$(. "$CONF"; echo "${OLD_HOST:-}")"
[ -n "$MAIN" ] || die 1 "$CONF has no HOST="
[[ "$HEALTH_PATH" =~ ^/[A-Za-z0-9._/-]*$ ]] || die 1 "HEALTH_PATH '$HEALTH_PATH' in $CONF is not a plain path"
SLUG_U="${SLUG//-/_}"
VHOST="$NGINX_AVAILABLE/$SLUG.conf"
OLD_VHOST="$NGINX_AVAILABLE/$SLUG-old-host.conf"
alias_file() { echo "$NGINX_AVAILABLE/$SLUG-alias-$1.conf"; }
alias_link() { echo "$NGINX_ENABLED/$SLUG-alias-$1.conf"; }
aliases() { local f; for f in "$NGINX_AVAILABLE/$SLUG-alias-"*.conf; do [ -f "$f" ] || continue; f="${f##*/"$SLUG"-alias-}"; echo "${f%.conf}"; done; }

if [ "$CMD" = list ]; then
  echo "main: $MAIN"
  [ -n "$OLD_RECORDED" ] && echo "previous: $OLD_RECORDED"
  for h in $(aliases); do echo "alias: $h"; done
  exit 0
fi

# ---------------------------------------------------------------- the texts that get installed
ssl_extras() {   # certbot's shared ssl snippets exist only when a certificate was ever installed with --nginx
  [ -f "$LETSENCRYPT_DIR/options-ssl-nginx.conf" ] && echo "    include $LETSENCRYPT_DIR/options-ssl-nginx.conf;"
  [ -f "$LETSENCRYPT_DIR/ssl-dhparams.pem" ] && echo "    ssl_dhparam $LETSENCRYPT_DIR/ssl-dhparams.pem;"
  return 0
}

alias_vhost() {   # <host>: the service template for one more server_name — everything of the main vhost but its `map`
  local host="$1" key="none"
  [ -f "$TEMPLATE" ] || die 1 "$TEMPLATE missing — run deploy/service/install.sh first"
  [ -r "$INDEXNOW_KEY_FILE" ] && key="$(tr -d '[:space:]' < "$INDEXNOW_KEY_FILE")"
  [[ "$key" =~ ^[A-Za-z0-9-]{8,128}$ ]] || key="indexnow-key-not-readable"
  echo "# agent-team: $host is a second address of service $SLUG (service-host.sh alias). Same upstream, headers and"
  echo "# WebSocket as the main vhost $SLUG.conf, which also defines the map \$svc_${SLUG_U}_connection used below."
  # the template: leading comment lines and the `map … { … }` block go; the two server blocks stay as they are
  awk 'BEGIN{skip=0; body=0}
       /^map[[:space:]]/ {skip=1}
       skip==1 { if ($0 ~ /^}/) skip=0; next }
       body==0 && (/^#/ || /^[[:space:]]*$/) {next}
       {body=1; print}' "$TEMPLATE" \
    | sed -e "s|__HOST__|$host|g" -e "s|__SLUG_U__|$SLUG_U|g" -e "s|__SLUG__|$SLUG|g" -e "s|__INDEXNOW_KEY__|$key|g" \
    | { if [ -f "$LETSENCRYPT_DIR/options-ssl-nginx.conf" ]; then cat; else sed '\|include /etc/letsencrypt/options-ssl-nginx.conf;|d'; fi; } \
    | { if [ -f "$LETSENCRYPT_DIR/ssl-dhparams.pem" ]; then cat; else sed '\|ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;|d'; fi; }
}

bootstrap_vhost() {   # <host>: http only, for the ACME challenge of a host that has no certificate yet
  cat <<EOF
# agent-team: temporary http-only block for the certificate of $1 (service-host.sh alias); replaced right after certbot.
server {
    listen 80;
    listen [::]:80;
    server_name $1;
    location /.well-known/acme-challenge/ { root $ACME_ROOT; }
    location / { return 404; }
}
EOF
}

old_host_vhost() {   # <old> <new>: the previous main address — 301 with the path, but ACME, health and /_team/ still work
  local old="$1" new="$2" cert="$LETSENCRYPT_LIVE/$1" loc
  cat <<EOF
# agent-team: $old was the main address of service $SLUG until $(date -u +%Y-%m-%d) (service-host.sh primary); requests go to $new.
# Still answered here: the ACME path (the certificate keeps renewing), $HEALTH_PATH and $KEEP_ON_OLD_HOST (the dispatcher's
# health check and metrics fetch, until product.yaml names the new address).
server {
    listen 80;
    listen [::]:80;
    server_name $old;
    access_log /var/log/nginx/access.log full;
    location /.well-known/acme-challenge/ { root $ACME_ROOT; }
    location / { return 301 https://$new\$request_uri; }
}
EOF
  if [ "$SKIP_CERT" = 1 ] || [ "$PRINT" = 1 ] || [ -f "$cert/fullchain.pem" ]; then
    cat <<EOF
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $old;
    access_log /var/log/nginx/access.log full;
    ssl_certificate     $cert/fullchain.pem;
    ssl_certificate_key $cert/privkey.pem;
EOF
    ssl_extras
    echo "    location /.well-known/acme-challenge/ { root $ACME_ROOT; }"
    for loc in "= $HEALTH_PATH" "$KEEP_ON_OLD_HOST"; do
      cat <<EOF
    location $loc {
        proxy_pass http://svc_$SLUG_U;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        add_header Cache-Control "no-store" always;
    }
EOF
    done
    echo "    location / { return 301 https://$new\$request_uri; }"
    echo "}"
  fi
}

# ---------------------------------------------------------------- --print: what would be installed, nothing changed
if [ "$PRINT" = 1 ]; then
  case "$CMD" in
    alias)
      [ "$NEW" != "$MAIN" ] || die 64 "$NEW is the main address of $SLUG already"
      echo "## would write $(alias_file "$NEW") (after a certificate for $NEW; the main vhost $VHOST is not touched):"
      alias_vhost "$NEW" ;;
    primary)
      [ "$NEW" != "$MAIN" ] || { echo "## $NEW is the main address of $SLUG already: nothing to do"; exit 0; }
      echo "## would remove $(alias_file "$NEW"), render $VHOST for $NEW (render-vhost.sh, the service template),"
      echo "## set HOST=$NEW and OLD_HOST=$MAIN in $CONF, and write $OLD_VHOST:"
      old_host_vhost "$MAIN" "$NEW" ;;
    unalias)
      echo "## would remove $(alias_file "$NEW") and its link in $NGINX_ENABLED; the certificate of $NEW stays" ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------- from here on: changes, one caller at a time
for bin in nginx systemctl; do command -v "$bin" >/dev/null 2>&1 || die 1 "$bin not found"; done
mkdir -p "$(dirname "$LOCK_FILE")" "$NGINX_AVAILABLE" "$NGINX_ENABLED"
exec 9>>"$LOCK_FILE"
flock -w "$LOCK_WAIT_SEC" 9 || die 1 "another service-host.sh holds $LOCK_FILE after ${LOCK_WAIT_SEC}s; nothing changed"
log INFO "start: $CMD slug=$SLUG host=$NEW (main address now $MAIN)"
reload_nginx() { nginx -t && systemctl reload nginx; }

dns_check() {
  [ "$SKIP_DNS" = 1 ] && return 0
  local resolved local_ips public_ip ip
  resolved="$(getent ahosts "$NEW" | awk '{print $1}' | sort -u || true)"
  [ -n "$resolved" ] || die 2 "$NEW does not resolve yet. Nothing changed."
  local_ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' | sort -u || true)"
  public_ip="$(curl -s --max-time 5 "$PUBLIC_IP_URL" 2>/dev/null | tr -d '[:space:]' || true)"
  while IFS= read -r ip; do
    [ -n "$ip" ] || continue
    if echo "$local_ips" | grep -qxF "$ip" || { [ -n "$public_ip" ] && [ "$ip" = "$public_ip" ]; }; then
      log INFO "DNS ok: $NEW -> $(echo "$resolved" | tr '\n' ' ')"; return 0
    fi
  done <<< "$resolved"
  die 2 "$NEW resolves to [$(echo "$resolved" | tr '\n' ' ')], not to this server. Nothing changed."
}

taken_elsewhere() {   # is the host a server_name of a vhost that is not this service's alias file for it?
  local f own; own="$(alias_file "$NEW")"
  for f in "$NGINX_ENABLED"/*; do
    [ -e "$f" ] || continue
    [ "$(readlink -f "$f" 2>/dev/null || echo "$f")" = "$(readlink -f "$own" 2>/dev/null || echo "$own")" ] && continue
    if grep -Eq "^[[:space:]]*server_name[[:space:]]+([^;]*[[:space:]])?${NEW//./\\.}([[:space:]][^;]*)?;" "$f" 2>/dev/null; then echo "$f"; return 0; fi
  done
  return 1
}

case "$CMD" in
# ================================================================ alias
alias)
  [ "$NEW" != "$MAIN" ] || die 64 "$NEW is the main address of $SLUG already"
  AF="$(alias_file "$NEW")"; AL="$(alias_link "$NEW")"; CERT_DIR="$LETSENCRYPT_LIVE/$NEW"
  if other="$(taken_elsewhere)"; then die 1 "$NEW is already a server_name in $other; remove that vhost first. Nothing changed."; fi
  dns_check
  backup=""
  if [ -f "$AF" ]; then backup="$(mktemp)"; cp -p "$AF" "$backup"; fi
  restore() { if [ -n "$backup" ]; then cp -p "$backup" "$AF"; else rm -f "$AF" "$AL"; fi; }
  if [ "$SKIP_CERT" = 1 ] || [ -f "$CERT_DIR/fullchain.pem" ]; then
    log INFO "a. certificate: $( [ "$SKIP_CERT" = 1 ] && echo skipped || echo "exists in $CERT_DIR" )"
  else
    command -v certbot >/dev/null 2>&1 || die 1 "certbot not found"
    if [ -z "${CERTBOT_EMAIL:-}" ] && [ -f "$ENV_FILE" ]; then
      CERTBOT_EMAIL="$(sed -n 's/^CERTBOT_EMAIL=[[:space:]]*//p' "$ENV_FILE" | tail -n 1 | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' | tr -d '"'"'" || true)"
    fi
    [ -n "${CERTBOT_EMAIL:-}" ] || die 1 "CERTBOT_EMAIL is empty — set it in $ENV_FILE. Nothing changed."
    log INFO "a. http-only block in $AF for the ACME challenge of $NEW (the main vhost of $MAIN is not touched)"
    mkdir -p "$ACME_ROOT"
    bootstrap_vhost "$NEW" > "$AF"
    ln -sfn "$AF" "$AL"
    if ! reload_nginx; then restore; die 1 "nginx refused the http-only block; it is removed, nothing else changed"; fi
    rc=0
    certbot certonly --webroot -w "$ACME_ROOT" -d "$NEW" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --keep-until-expiring || rc=$?
    if [ "$rc" -ne 0 ] || [ ! -f "$CERT_DIR/fullchain.pem" ]; then
      restore; reload_nginx || true
      die 1 "certbot did not issue a certificate for $NEW (rc=$rc); the http-only block is removed, the service is as it was"
    fi
    log INFO "a. certificate issued: $CERT_DIR"
  fi
  tmp="$(mktemp)"
  alias_vhost "$NEW" > "$tmp"
  if [ -f "$AF" ] && cmp -s "$tmp" "$AF" && [ -L "$AL" ]; then
    rm -f "$tmp"; [ -n "$backup" ] && rm -f "$backup"
    log INFO "b. $AF is up to date: nothing to do"
  else
    install -m 0644 "$tmp" "$AF"; rm -f "$tmp"
    ln -sfn "$AF" "$AL"
    if ! nginx -t; then restore; nginx -t || true; die 1 "nginx -t failed with the alias vhost; the previous state is back, nginx was not reloaded"; fi
    systemctl reload nginx
    [ -n "$backup" ] && rm -f "$backup"
    journal "action=host-alias host=$NEW main=$MAIN"
    log INFO "b. $AF installed, nginx reloaded"
  fi
  if [ "$SKIP_HEALTH" != 1 ]; then
    if curl -fsS --max-time "$HEALTH_TIMEOUT_SEC" "https://$NEW$HEALTH_PATH" >/dev/null; then log INFO "health: https://$NEW$HEALTH_PATH answers"
    else log WARNING "health: https://$NEW$HEALTH_PATH did not answer in ${HEALTH_TIMEOUT_SEC}s — check the service and DNS"; fi
  fi
  echo "== $SLUG also answers at https://$NEW/ ; the main address is still https://$MAIN/"
  ;;

# ================================================================ unalias
unalias)
  AF="$(alias_file "$NEW")"; AL="$(alias_link "$NEW")"
  if [ ! -f "$AF" ] && [ ! -L "$AL" ]; then log INFO "$NEW is not an alias of $SLUG: nothing to do"; exit 0; fi
  backup="$(mktemp)"; had_file=0
  if [ -f "$AF" ]; then cp -p "$AF" "$backup"; had_file=1; fi
  rm -f "$AL" "$AF"
  if ! nginx -t; then
    if [ "$had_file" = 1 ]; then cp -p "$backup" "$AF"; ln -sfn "$AF" "$AL"; fi
    rm -f "$backup"; die 1 "nginx -t failed without the alias vhost; it is back, nginx was not reloaded"
  fi
  systemctl reload nginx
  rm -f "$backup"
  journal "action=host-unalias host=$NEW main=$MAIN"
  echo "== $NEW no longer answers for $SLUG (its certificate stays in $LETSENCRYPT_LIVE/$NEW)"
  ;;

# ================================================================ primary
primary)
  if [ "$NEW" = "$MAIN" ]; then log INFO "$NEW is the main address of $SLUG already: nothing to do"; exit 0; fi
  AF="$(alias_file "$NEW")"; AL="$(alias_link "$NEW")"
  [ -f "$AF" ] || die 1 "$NEW is not an alias of $SLUG yet — run: service-host.sh alias $SLUG $NEW, look at the service there, then primary. Nothing changed."
  [ "$SKIP_CERT" = 1 ] || [ -f "$LETSENCRYPT_LIVE/$NEW/fullchain.pem" ] || die 1 "no certificate in $LETSENCRYPT_LIVE/$NEW — run the alias step first. Nothing changed."
  [ -f "$TEMPLATE" ] || die 1 "$TEMPLATE missing — run deploy/service/install.sh first"
  OLD="$MAIN"
  keep="$(mktemp -d)"
  [ -f "$VHOST" ] && cp -p "$VHOST" "$keep/vhost"
  cp -p "$AF" "$keep/alias"
  [ -f "$OLD_VHOST" ] && cp -p "$OLD_VHOST" "$keep/old"
  had_old_link=0; [ -L "$NGINX_ENABLED/$SLUG-old-host.conf" ] && had_old_link=1
  restore() {
    if [ -f "$keep/vhost" ]; then cp -p "$keep/vhost" "$VHOST"; fi
    cp -p "$keep/alias" "$AF"; ln -sfn "$AF" "$AL"
    if [ -f "$keep/old" ]; then cp -p "$keep/old" "$OLD_VHOST"; else rm -f "$OLD_VHOST"; fi
    [ "$had_old_link" = 1 ] || rm -f "$NGINX_ENABLED/$SLUG-old-host.conf"
    rm -rf "$keep"
  }
  # a. the alias vhost of the new main address goes: the main vhost takes that server_name
  rm -f "$AL" "$AF"
  # b. the main vhost for the new address (no reload yet). The previous main address is in no vhost for a moment of
  #    configuration only: nothing is reloaded before step d.
  set +e
  TEMPLATE="$TEMPLATE" "$RENDER_VHOST" "$SLUG" "$NEW" --no-reload
  rc=$?
  set -e
  case "$rc" in
    0|3) log INFO "b. main vhost rendered for $NEW (render-vhost rc=$rc)" ;;
    *) restore; die 1 "render-vhost.sh failed (rc=$rc); the previous vhosts are back, nginx was not reloaded" ;;
  esac
  # c. the previous main address: 301 with the path; ACME, health and /_team/ still answered
  old_host_vhost "$OLD" "$NEW" > "$OLD_VHOST.new"
  mv "$OLD_VHOST.new" "$OLD_VHOST"
  ln -sfn "$OLD_VHOST" "$NGINX_ENABLED/$SLUG-old-host.conf"
  log INFO "c. $OLD_VHOST: $OLD -> $NEW (kept: ACME, $HEALTH_PATH, $KEEP_ON_OLD_HOST)"
  # d. one test, one graceful reload
  if ! nginx -t; then restore; nginx -t || true; die 1 "nginx -t failed with the new vhosts; the previous ones are back, nginx was not reloaded"; fi
  systemctl reload nginx
  rm -rf "$keep"
  log INFO "d. nginx reloaded"
  # e. the service definition and the journal
  cp -p "$CONF" "$CONF.before-primary-$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="$(mktemp)"
  { grep -v -E '^(HOST|OLD_HOST)=' "$CONF"; printf 'HOST=%q\n' "$NEW"; printf 'OLD_HOST=%q\n' "$OLD"; } > "$tmp"
  install -m 0644 "$tmp" "$CONF"; rm -f "$tmp"
  journal "action=host-primary from=$OLD to=$NEW"
  log INFO "e. $CONF: HOST=$NEW, OLD_HOST=$OLD"
  if [ "$SKIP_HEALTH" != 1 ]; then
    for u in "https://$NEW$HEALTH_PATH" "https://$OLD$HEALTH_PATH"; do
      if curl -fsS --max-time "$HEALTH_TIMEOUT_SEC" "$u" >/dev/null; then log INFO "health: $u answers"
      else log WARNING "health: $u did not answer in ${HEALTH_TIMEOUT_SEC}s — check the service"; fi
    done
  fi
  echo "== $SLUG now lives at https://$NEW/ ; $OLD redirects there (its $HEALTH_PATH and $KEEP_ON_OLD_HOST still answer). Next: products/$SLUG/product.yaml host and site_base."
  ;;
esac
