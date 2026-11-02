#!/usr/bin/env bash
# add-site.sh — register a pilot site on the droplet: sites.tsv line, nginx vhost,
# Let's Encrypt certificate, first publish. Run as root on the droplet.
#
# Usage: add-site.sh <slug> <host> <repo> [branch] [public_dir]
#   slug        [a-z0-9-], e.g. foo
#   host        FQDN, e.g. foo.formachines.ai (DNS A record must already point here)
#   repo        public https git URL
#   branch      default: main
#   public_dir  default: public (prebuilt, committed in the repo)
# Env: CERTBOT_EMAIL (or CERTBOT_EMAIL= in /etc/agent-team/env) — required for a new certificate.
#
# Steps (each one idempotent, re-running is safe):
#   0. DNS check: host must resolve to this server, else exit 2 (nothing changed)
#   -  sites.tsv line (added once)
#   a. http-only vhost (:80, root /var/www/<slug>) → nginx -t → reload   [skipped if cert exists]
#   b. certbot --nginx -d <host> … --redirect                             [skipped if cert exists]
#   c. full vhost from /etc/agent-team/site.conf.template → nginx -t → reload
#   d. publish.sh --site <slug>
set -euo pipefail
umask 022

# ---------------------------------------------------------------- settings
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
SITES_FILE="${SITES_FILE:-$ETC_DIR/sites.tsv}"
ENV_FILE="${ENV_FILE:-$ETC_DIR/env}"
TEMPLATE="${TEMPLATE:-$ETC_DIR/site.conf.template}"
HEADERS_SNIPPET="${HEADERS_SNIPPET:-/etc/nginx/snippets/agent-team-headers.conf}"
NGINX_AVAILABLE="${NGINX_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled}"
LETSENCRYPT_LIVE="${LETSENCRYPT_LIVE:-/etc/letsencrypt/live}"
WWW_DIR="${WWW_DIR:-/var/www}"
PUBLISH="${PUBLISH:-/opt/agent-team/bin/publish.sh}"
PUBLIC_IP_URL="${PUBLIC_IP_URL:-https://ifconfig.me}"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

if [ $# -lt 3 ] || [ $# -gt 5 ]; then usage >&2; exit 64; fi
case "$1" in -h|--help) usage; exit 0 ;; esac

SLUG="$1"; HOST="$2"; REPO="$3"; BRANCH="${4:-main}"; PUBLIC_DIR="${5:-public}"

if [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root" >&2; exit 1; fi
if ! [[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "ERROR: slug '$SLUG' must match [a-z0-9][a-z0-9-]*" >&2; exit 64
fi
if ! [[ "$HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
  echo "ERROR: host '$HOST' is not a valid FQDN" >&2; exit 64
fi
if ! [[ "$REPO" =~ ^https://[^[:space:]]+$ ]]; then
  echo "ERROR: repo '$REPO' must be a public https URL" >&2; exit 64
fi
for bin in nginx certbot getent curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: $bin not found" >&2; exit 1; }
done
[ -f "$TEMPLATE" ] || { echo "ERROR: $TEMPLATE missing — run install.sh first" >&2; exit 1; }
[ -f "$HEADERS_SNIPPET" ] || { echo "ERROR: $HEADERS_SNIPPET missing — run install.sh first" >&2; exit 1; }
[ -x "$PUBLISH" ] || { echo "ERROR: $PUBLISH missing — run install.sh first" >&2; exit 1; }

# CERTBOT_EMAIL: environment wins, then /etc/agent-team/env
if [ -z "${CERTBOT_EMAIL:-}" ] && [ -f "$ENV_FILE" ]; then
  CERTBOT_EMAIL="$(sed -n 's/^CERTBOT_EMAIL=[[:space:]]*//p' "$ENV_FILE" | tail -n 1 \
                   | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' | tr -d '"'"'" || true)"
fi
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"

CONF="$NGINX_AVAILABLE/$SLUG.conf"
CONF_LINK="$NGINX_ENABLED/$SLUG.conf"
CERT_DIR="$LETSENCRYPT_LIVE/$HOST"

echo "== add-site $SLUG host=$HOST repo=$REPO branch=$BRANCH public_dir=$PUBLIC_DIR"

reload_nginx() {
  echo "nginx -t"
  nginx -t
  echo "systemctl reload nginx"
  systemctl reload nginx
}

# ---------------------------------------------------------------- 0. DNS check (before touching anything)
echo "DNS: resolving $HOST"
resolved="$(getent ahosts "$HOST" | awk '{print $1}' | sort -u || true)"
if [ -z "$resolved" ]; then
  echo "ERROR: $HOST does not resolve yet. Ask the owner to create the DNS A record, then re-run." >&2
  exit 2
fi
local_ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' | sort -u || true)"
public_ip="$(curl -s --max-time 5 "$PUBLIC_IP_URL" 2>/dev/null | tr -d '[:space:]' || true)"
echo "DNS: $HOST -> $(echo "$resolved" | tr '\n' ' ')"
echo "DNS: this server: $(echo "$local_ips" | tr '\n' ' ')${public_ip:+(public $public_ip)}"
dns_ok=0
while IFS= read -r ip; do
  [ -n "$ip" ] || continue
  if echo "$local_ips" | grep -qxF "$ip" || { [ -n "$public_ip" ] && [ "$ip" = "$public_ip" ]; }; then
    dns_ok=1; break
  fi
done <<< "$resolved"
if [ "$dns_ok" -ne 1 ]; then
  echo "ERROR: $HOST resolves to [$(echo "$resolved" | tr '\n' ' ')] but this server is [$(echo "$local_ips" | tr '\n' ' ')${public_ip:+ public $public_ip}]." >&2
  echo "       DNS is not set up (yet) by the owner — A record for $HOST must point here. Nothing else changed." >&2
  exit 2
fi
echo "DNS: ok"

# ---------------------------------------------------------------- sites.tsv
mkdir -p "$ETC_DIR"
touch "$SITES_FILE"
existing="$(awk -F'\t' -v s="$SLUG" '$1 == s' "$SITES_FILE" || true)"
if [ -n "$existing" ]; then
  echo "sites.tsv: slug '$SLUG' already registered, keeping existing line:"
  echo "  $existing"
else
  echo "sites.tsv: adding $SLUG"
  printf '%s\t%s\t%s\t%s\t%s\n' "$SLUG" "$HOST" "$REPO" "$BRANCH" "$PUBLIC_DIR" >> "$SITES_FILE"
fi

# ---------------------------------------------------------------- a+b. certificate
if [ -f "$CERT_DIR/fullchain.pem" ]; then
  echo "cert: $CERT_DIR exists — skipping http-only vhost and certbot"
else
  if [ -z "$CERTBOT_EMAIL" ]; then
    echo "ERROR: CERTBOT_EMAIL is empty — set it in $ENV_FILE or pass CERTBOT_EMAIL=... in the environment" >&2
    exit 1
  fi
  echo "a. writing http-only bootstrap vhost $CONF"
  cat > "$CONF" <<EOF
# agent-team bootstrap vhost for $HOST — replaced by the full config right after certbot.
server {
    listen 80;
    listen [::]:80;
    server_name $HOST;
    root  $WWW_DIR/$SLUG;
    index index.html;
    access_log /var/log/nginx/access.log full;
    location / {
        try_files \$uri \$uri/ \$uri.html =404;
    }
}
EOF
  ln -sfn "$CONF" "$CONF_LINK"
  reload_nginx

  echo "b. certbot --nginx -d $HOST (email $CERTBOT_EMAIL)"
  certbot --nginx -d "$HOST" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect --keep-until-expiring
  [ -f "$CERT_DIR/fullchain.pem" ] || { echo "ERROR: certbot finished but $CERT_DIR/fullchain.pem is missing" >&2; exit 1; }
  echo "cert: issued $CERT_DIR"
fi

# ---------------------------------------------------------------- c. full vhost
echo "c. rendering $TEMPLATE -> $CONF"
rendered="$(mktemp)"
sed -e "s|__HOST__|$HOST|g" -e "s|__SLUG__|$SLUG|g" "$TEMPLATE" > "$rendered"
# certbot's shared ssl snippets exist when any cert was ever installed with --nginx; drop the lines otherwise
if [ ! -f /etc/letsencrypt/options-ssl-nginx.conf ]; then
  echo "WARNING: /etc/letsencrypt/options-ssl-nginx.conf missing — dropping its include from the vhost"
  sed -i '\|include /etc/letsencrypt/options-ssl-nginx.conf;|d' "$rendered"
fi
if [ ! -f /etc/letsencrypt/ssl-dhparams.pem ]; then
  echo "WARNING: /etc/letsencrypt/ssl-dhparams.pem missing — dropping ssl_dhparam from the vhost"
  sed -i '\|ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;|d' "$rendered"
fi
if [ -f "$CONF" ] && cmp -s "$rendered" "$CONF"; then
  echo "vhost unchanged"
  rm -f "$rendered"
  ln -sfn "$CONF" "$CONF_LINK"
else
  backup=""
  if [ -f "$CONF" ]; then backup="$(mktemp)"; cp "$CONF" "$backup"; fi
  mv "$rendered" "$CONF"
  chmod 644 "$CONF"
  ln -sfn "$CONF" "$CONF_LINK"
  if ! nginx -t; then
    echo "ERROR: nginx -t failed with the new vhost; restoring the previous one" >&2
    if [ -n "$backup" ]; then mv "$backup" "$CONF"; else rm -f "$CONF" "$CONF_LINK"; fi
    nginx -t || true
    exit 1
  fi
  [ -n "$backup" ] && rm -f "$backup"
  echo "systemctl reload nginx"
  systemctl reload nginx
fi

# ---------------------------------------------------------------- d. first publish
echo "d. first publish: $PUBLISH --site $SLUG"
if "$PUBLISH" --site "$SLUG"; then
  echo "== $SLUG is live: https://$HOST/ (timer republishes every 5 min on new commits)"
else
  echo "== $SLUG registered, but the first publish failed — see /var/log/agent-team/publish.log and the output above" >&2
  exit 1
fi
