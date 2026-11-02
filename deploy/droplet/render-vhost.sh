#!/usr/bin/env bash
# render-vhost.sh — render the full nginx vhost of one pilot site from
# /etc/agent-team/site.conf.template and install it safely. Run as root on the droplet.
# Used by add-site.sh (step c) and by install.sh (re-render of every site in sites.tsv,
# so a template change — e.g. the IndexNow key location — reaches existing sites).
#
# Usage: render-vhost.sh <slug> <host> [--no-reload]
#   Placeholders: __HOST__ -> host, __SLUG__ -> slug, __INDEXNOW_KEY__ -> /etc/agent-team/indexnow.key.
#   Writes /etc/nginx/sites-available/<slug>.conf (+ symlink in sites-enabled) only when the
#   rendered text differs; then nginx -t; on failure the previous file is restored (exit 1).
#   --no-reload  do not `systemctl reload nginx` after a successful change (caller reloads once).
#
# Exit codes: 0 unchanged · 3 updated (nginx -t ok) · 1 error (previous vhost restored) · 64 usage.
set -euo pipefail
umask 022

# ---------------------------------------------------------------- settings
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
TEMPLATE="${TEMPLATE:-$ETC_DIR/site.conf.template}"
INDEXNOW_KEY_FILE="${INDEXNOW_KEY_FILE:-$ETC_DIR/indexnow.key}"
NGINX_AVAILABLE="${NGINX_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt}"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

RELOAD=1
args=()
for a in "$@"; do
  case "$a" in
    --no-reload) RELOAD=0 ;;
    -h|--help) usage; exit 0 ;;
    *) args+=("$a") ;;
  esac
done
if [ "${#args[@]}" -ne 2 ]; then usage >&2; exit 64; fi
SLUG="${args[0]}"; HOST="${args[1]}"

if [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root" >&2; exit 1; fi
if ! [[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then echo "ERROR: bad slug '$SLUG'" >&2; exit 64; fi
if ! [[ "$HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
  echo "ERROR: bad host '$HOST'" >&2; exit 64
fi
[ -f "$TEMPLATE" ] || { echo "ERROR: $TEMPLATE missing — run install.sh first" >&2; exit 1; }
[ -s "$INDEXNOW_KEY_FILE" ] || { echo "ERROR: $INDEXNOW_KEY_FILE missing/empty — run install.sh first (it generates the IndexNow key)" >&2; exit 1; }
INDEXNOW_KEY="$(tr -d '[:space:]' < "$INDEXNOW_KEY_FILE")"
if ! [[ "$INDEXNOW_KEY" =~ ^[A-Za-z0-9-]{8,128}$ ]]; then
  echo "ERROR: $INDEXNOW_KEY_FILE does not look like an IndexNow key ([A-Za-z0-9-]{8,128})" >&2; exit 1
fi

CONF="$NGINX_AVAILABLE/$SLUG.conf"
CONF_LINK="$NGINX_ENABLED/$SLUG.conf"
mkdir -p "$NGINX_AVAILABLE" "$NGINX_ENABLED"

echo "[$SLUG] rendering $TEMPLATE -> $CONF (host=$HOST)"
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed -e "s|__HOST__|$HOST|g" -e "s|__SLUG__|$SLUG|g" -e "s|__INDEXNOW_KEY__|$INDEXNOW_KEY|g" "$TEMPLATE" > "$rendered"
# certbot's shared ssl snippets exist when any cert was ever installed with --nginx; drop the lines otherwise
if [ ! -f "$LETSENCRYPT_DIR/options-ssl-nginx.conf" ]; then
  echo "[$SLUG] note: $LETSENCRYPT_DIR/options-ssl-nginx.conf missing — dropping its include from the vhost"
  sed -i '\|include /etc/letsencrypt/options-ssl-nginx.conf;|d' "$rendered"
fi
if [ ! -f "$LETSENCRYPT_DIR/ssl-dhparams.pem" ]; then
  echo "[$SLUG] note: $LETSENCRYPT_DIR/ssl-dhparams.pem missing — dropping ssl_dhparam from the vhost"
  sed -i '\|ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;|d' "$rendered"
fi

if [ -f "$CONF" ] && cmp -s "$rendered" "$CONF"; then
  echo "[$SLUG] vhost unchanged"
  ln -sfn "$CONF" "$CONF_LINK"
  exit 0
fi

backup=""
if [ -f "$CONF" ]; then backup="$(mktemp)"; cp "$CONF" "$backup"; fi
install -m 0644 "$rendered" "$CONF"
ln -sfn "$CONF" "$CONF_LINK"
if ! nginx -t; then
  echo "[$SLUG] ERROR: nginx -t failed with the new vhost; restoring the previous one" >&2
  if [ -n "$backup" ]; then mv "$backup" "$CONF"; else rm -f "$CONF" "$CONF_LINK"; fi
  nginx -t || true
  exit 1
fi
[ -n "$backup" ] && rm -f "$backup"
echo "[$SLUG] vhost updated (nginx -t ok)"
if [ "$RELOAD" = 1 ]; then
  echo "[$SLUG] systemctl reload nginx"
  systemctl reload nginx
fi
exit 3
