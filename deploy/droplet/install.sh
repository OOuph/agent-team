#!/usr/bin/env bash
# install.sh — install / upgrade the agent-team publishing stack on the droplet. Run as root.
# Idempotent: scripts, units, template and snippet are overwritten; sites.tsv, env and the
# IndexNow key are kept. Every site already in sites.tsv gets its nginx vhost re-rendered from
# the (possibly new) template via render-vhost.sh (nginx -t, rollback on error, one reload).
#
# Usage (on the droplet): SRC_DIR=/tmp/agent-team-deploy/deploy/droplet bash install.sh
#   SRC_DIR         directory with these deploy files (default: directory of this script)
#   SITEMETRICS_SRC path to team/sitemetrics.py (default: <SRC_DIR>/../../team/sitemetrics.py)
# From the Mac use remote-install.sh, which uploads the files and calls this script.
set -euo pipefail
umask 022

# ---------------------------------------------------------------- settings
SRC_DIR="${SRC_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)}"
SITEMETRICS_SRC="${SITEMETRICS_SRC:-$SRC_DIR/../../team/sitemetrics.py}"
ETC_DIR="${ETC_DIR:-/etc/agent-team}"
OPT_DIR="${OPT_DIR:-/opt/agent-team}"
BIN_DIR="$OPT_DIR/bin"
SITES_DIR="$OPT_DIR/sites"
STATE_DIR="${STATE_DIR:-/var/lib/agent-team/state}"
METRICS_DIR="${METRICS_DIR:-/var/lib/agent-team/metrics}"
LOG_DIR="${LOG_DIR:-/var/log/agent-team}"
WWW_DIR="${WWW_DIR:-/var/www}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
NGINX_SNIPPETS="${NGINX_SNIPPETS:-/etc/nginx/snippets}"
LETSENCRYPT_LIVE="${LETSENCRYPT_LIVE:-/etc/letsencrypt/live}"
INDEXNOW_KEY_FILE="${INDEXNOW_KEY_FILE:-$ETC_DIR/indexnow.key}"               # the key (32 hex), read by publish.sh / render-vhost.sh
INDEXNOW_KEYFILE_TXT="${INDEXNOW_KEYFILE_TXT:-$ETC_DIR/indexnow-keyfile.txt}" # same key, served by nginx as /<key>.txt

echo "== agent-team install from $SRC_DIR"
if [ "$(id -u)" -ne 0 ]; then echo "ERROR: run as root" >&2; exit 1; fi
for f in publish.sh add-site.sh render-vhost.sh metrics.sh site.conf.template nginx-headers.snippet.conf \
         agent-team-publish.service agent-team-publish.timer \
         agent-team-metrics.service agent-team-metrics.timer sites.tsv.example env.example; do
  [ -f "$SRC_DIR/$f" ] || { echo "ERROR: $SRC_DIR/$f missing" >&2; exit 1; }
done

echo "-- checking dependencies"
missing=""
for bin in nginx certbot git rsync curl python3 flock systemctl openssl; do
  if command -v "$bin" >/dev/null 2>&1; then
    echo "   $bin: $(command -v "$bin")"
  else
    missing="$missing $bin"
  fi
done
if [ -n "$missing" ]; then
  echo "ERROR: missing on this server:$missing (apt install them first; Node is NOT required)" >&2
  exit 1
fi
if ! grep -rqs 'log_format[[:space:]]\+full' /etc/nginx/nginx.conf /etc/nginx/snippets /etc/nginx/conf.d 2>/dev/null; then
  echo "WARNING: nginx log_format 'full' not found under /etc/nginx — site vhosts use 'access_log ... full;' and nginx -t will fail without it" >&2
fi

echo "-- directories"
for d in "$ETC_DIR" "$BIN_DIR" "$SITES_DIR" "$STATE_DIR" "$METRICS_DIR" "$LOG_DIR" "$WWW_DIR"; do
  mkdir -p "$d"
  echo "   $d"
done
# nginx (www-data) reads /var/lib/agent-team/metrics/<host>/ and /etc/agent-team/indexnow-keyfile.txt via alias
chmod 755 /var/lib/agent-team "$METRICS_DIR" "$ETC_DIR"

echo "-- scripts -> $BIN_DIR"
for f in publish.sh add-site.sh render-vhost.sh metrics.sh; do
  install -m 0755 "$SRC_DIR/$f" "$BIN_DIR/$f"
  echo "   $BIN_DIR/$f"
done
if [ -f "$SITEMETRICS_SRC" ]; then
  install -m 0755 "$SITEMETRICS_SRC" "$BIN_DIR/sitemetrics.py"
  echo "   $BIN_DIR/sitemetrics.py (from $SITEMETRICS_SRC)"
else
  echo "   WARNING: $SITEMETRICS_SRC not found — metrics timer will fail until team/sitemetrics.py exists and install.sh is re-run" >&2
fi
install -m 0644 "$SRC_DIR/README.md" "$OPT_DIR/README.md" 2>/dev/null || true

echo "-- config -> $ETC_DIR"
install -m 0644 "$SRC_DIR/site.conf.template" "$ETC_DIR/site.conf.template"
echo "   $ETC_DIR/site.conf.template"
install -m 0644 "$SRC_DIR/sites.tsv.example" "$ETC_DIR/sites.tsv.example"
echo "   $ETC_DIR/sites.tsv.example"
if [ ! -f "$ETC_DIR/sites.tsv" ]; then
  grep '^#' "$SRC_DIR/sites.tsv.example" > "$ETC_DIR/sites.tsv"
  echo "   $ETC_DIR/sites.tsv (created empty — add sites with add-site.sh)"
else
  echo "   $ETC_DIR/sites.tsv kept ($(grep -cv '^\s*\(#\|$\)' "$ETC_DIR/sites.tsv" || true) site(s))"
fi
if [ ! -f "$ETC_DIR/env" ]; then
  install -m 0600 "$SRC_DIR/env.example" "$ETC_DIR/env"
  echo "   $ETC_DIR/env (created — fill CERTBOT_EMAIL before the first add-site.sh)"
else
  echo "   $ETC_DIR/env kept"
fi

echo "-- IndexNow key (one key for every pilot site)"
if [ -s "$INDEXNOW_KEY_FILE" ] && [[ "$(tr -d '[:space:]' < "$INDEXNOW_KEY_FILE")" =~ ^[A-Za-z0-9-]{8,128}$ ]]; then
  echo "   $INDEXNOW_KEY_FILE kept"
else
  # 32 hex chars; the key is public by design (served at https://<host>/<key>.txt), 0644 is fine
  printf '%s\n' "$(openssl rand -hex 16)" > "$INDEXNOW_KEY_FILE"
  echo "   $INDEXNOW_KEY_FILE generated"
fi
chmod 0644 "$INDEXNOW_KEY_FILE"
indexnow_key="$(tr -d '[:space:]' < "$INDEXNOW_KEY_FILE")"
# keyfile = the key itself, no trailing newline (what IndexNow fetches to verify the submitter)
if [ -f "$INDEXNOW_KEYFILE_TXT" ] && [ "$(cat "$INDEXNOW_KEYFILE_TXT")" = "$indexnow_key" ]; then
  echo "   $INDEXNOW_KEYFILE_TXT kept"
else
  printf '%s' "$indexnow_key" > "$INDEXNOW_KEYFILE_TXT.tmp"
  mv -f "$INDEXNOW_KEYFILE_TXT.tmp" "$INDEXNOW_KEYFILE_TXT"
  echo "   $INDEXNOW_KEYFILE_TXT written"
fi
chmod 0644 "$INDEXNOW_KEYFILE_TXT"
echo "   key location on every host: https://<host>/$indexnow_key.txt"

echo "-- nginx snippet"
mkdir -p "$NGINX_SNIPPETS"
install -m 0644 "$SRC_DIR/nginx-headers.snippet.conf" "$NGINX_SNIPPETS/agent-team-headers.conf"
echo "   $NGINX_SNIPPETS/agent-team-headers.conf"
echo "   nginx -t"
nginx -t

echo "-- re-render vhosts of registered sites from the template"
# Sites whose certificate exists have a full vhost: re-render it so template changes (new
# placeholders such as __INDEXNOW_KEY__) reach them. Bootstrap-only sites (no cert yet) are left
# to add-site.sh. render-vhost.sh does nginx -t per site and restores the old file on failure.
n_render_updated=0; n_render_same=0; n_render_skip=0; n_render_err=0; render_err_slugs=""
if [ -f "$ETC_DIR/sites.tsv" ]; then
  exec 3< "$ETC_DIR/sites.tsv"
  while IFS= read -r -u 3 line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    IFS=$'\t' read -r slug host _rest <<< "$line"
    [ -n "$slug" ] && [ -n "$host" ] || { echo "   WARNING: bad sites.tsv line: $line" >&2; continue; }
    if [ ! -f "$LETSENCRYPT_LIVE/$host/fullchain.pem" ]; then
      echo "   [$slug] no certificate in $LETSENCRYPT_LIVE/$host — skipping (add-site.sh will render it)"
      n_render_skip=$((n_render_skip + 1)); continue
    fi
    set +e
    "$BIN_DIR/render-vhost.sh" "$slug" "$host" --no-reload
    rc=$?
    set -e
    case "$rc" in
      0) n_render_same=$((n_render_same + 1)) ;;
      3) n_render_updated=$((n_render_updated + 1)) ;;
      *) n_render_err=$((n_render_err + 1)); render_err_slugs="$render_err_slugs $slug" ;;
    esac
  done
  exec 3<&-
fi
echo "   vhosts: updated=$n_render_updated unchanged=$n_render_same skipped=$n_render_skip errors=$n_render_err${render_err_slugs:+ (${render_err_slugs# })}"
if [ "$n_render_updated" -gt 0 ]; then
  echo "   nginx -t && systemctl reload nginx"
  nginx -t
  systemctl reload nginx
fi
if [ "$n_render_err" -gt 0 ]; then
  echo "WARNING: $n_render_err vhost(s) failed to render and were left as before:${render_err_slugs}" >&2
fi

echo "-- systemd units -> $SYSTEMD_DIR"
for u in agent-team-publish.service agent-team-publish.timer agent-team-metrics.service agent-team-metrics.timer; do
  install -m 0644 "$SRC_DIR/$u" "$SYSTEMD_DIR/$u"
  echo "   $SYSTEMD_DIR/$u"
done
systemctl daemon-reload
systemctl enable --now agent-team-publish.timer
systemctl enable --now agent-team-metrics.timer
echo "   timers enabled:"
systemctl list-timers --no-pager 'agent-team-*' || true

echo "-- first publish (no-op when sites.tsv is empty)"
"$BIN_DIR/publish.sh" || echo "WARNING: publish.sh reported failures — see $LOG_DIR/publish.log" >&2

echo "== install done. Next: fill $ETC_DIR/env (CERTBOT_EMAIL), then add-site.sh <slug> <host> <repo>"
echo "   IndexNow: curl -s https://<host>/$indexnow_key.txt  → must print the key; log: grep indexnow= $LOG_DIR/publish.log"
