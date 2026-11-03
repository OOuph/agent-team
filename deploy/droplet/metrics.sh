#!/usr/bin/env bash
# metrics.sh — hourly wrapper around sitemetrics.py (written by another agent, lives in
# the repo at team/sitemetrics.py, installed to /opt/agent-team/bin/sitemetrics.py).
# Reads the shared nginx access.log (log_format full carries $host), writes
# /var/lib/agent-team/metrics/<host>/metrics.json, which nginx serves at
# https://<host>/_team/metrics.json (alias in site.conf.template) — nothing is copied
# into the published release. Runs from agent-team-metrics.timer.
set -euo pipefail
umask 022

SITEMETRICS="${SITEMETRICS:-/opt/agent-team/bin/sitemetrics.py}"
METRICS_DIR="${METRICS_DIR:-/var/lib/agent-team/metrics}"
LOG_FILE="${LOG_FILE:-/var/log/agent-team/metrics.log}"
ACCESS_LOGS="${ACCESS_LOGS:-/var/log/nginx/access.log /var/log/nginx/access.log.1}"
PYTHON="${PYTHON:-python3}"
# Only the team's own hosts get a report: scanners send random Host headers and --all-hosts would create a
# directory per probe. Targets are the union of products/*/product.yaml `targets:` (a target absent on a host just
# counts 0). Both can be overridden from /etc/agent-team/metrics.env (HOSTS=..., TARGETS=...).
HOSTS="${HOSTS:-releases.formachines.ai,agentsmd.formachines.ai,luz.bymachines.ai,agentteam.formachines.ai}"
TARGETS="${TARGETS:-/go/ /go/telegram/ /go/github/ /go/vote/ /go/vote/*/up/ /go/vote/*/down/ /try/ /today.json /tomorrow.json /stats.json /feed.xml /day/ /d/ /repo/ /pkg/ /llms.txt}"
OWN_IPS="${OWN_IPS:-95.179.139.151}"
OWN_UA="${OWN_UA:-agent-team/ FormachinesExternalCritic/}"   # roles and the external critic are the team, not demand   # reservememes: the dispatcher's and the ops session's manual curl checks count as 'own'
[ -r /etc/agent-team/metrics.env ] && . /etc/agent-team/metrics.env

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "== agent-team metrics $(now_iso)"
mkdir -p "$METRICS_DIR" "$(dirname "$LOG_FILE")"

if [ ! -f "$SITEMETRICS" ]; then
  echo "ERROR: $SITEMETRICS not found — re-run install.sh once team/sitemetrics.py exists in the repo" >&2
  echo "$(now_iso) action=error reason=no-sitemetrics" >> "$LOG_FILE"
  exit 1
fi

log_args=()
for f in $ACCESS_LOGS; do
  if [ -r "$f" ]; then
    log_args+=(--log "$f")
    echo "using log $f ($(stat -c %s "$f" 2>/dev/null || echo '?') bytes)"
  else
    echo "skipping missing log $f"
  fi
done
if [ "${#log_args[@]}" -eq 0 ]; then
  echo "ERROR: none of the access logs is readable: $ACCESS_LOGS" >&2
  echo "$(now_iso) action=error reason=no-logs" >> "$LOG_FILE"
  exit 1
fi

target_args=()
for t in $TARGETS; do target_args+=(--target "$t"); done
for ip in $OWN_IPS; do target_args+=(--own-ip "$ip"); done
for ua in $OWN_UA; do target_args+=(--own-ua "$ua"); done
echo "running: $PYTHON $SITEMETRICS --all-hosts --hosts $HOSTS ${target_args[*]} ${log_args[*]} --out $METRICS_DIR/"
start=$(date +%s)
"$PYTHON" "$SITEMETRICS" --all-hosts --hosts "$HOSTS" "${target_args[@]}" "${log_args[@]}" --out "$METRICS_DIR/"
dur=$(( $(date +%s) - start ))

# nginx (www-data) must be able to read what root just wrote
chmod -R a+rX "$METRICS_DIR"

n_hosts=$(find "$METRICS_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)
echo "done in ${dur}s: $n_hosts host dir(s) under $METRICS_DIR"
echo "$(now_iso) action=ok hosts=$n_hosts duration_s=$dur" >> "$LOG_FILE"
