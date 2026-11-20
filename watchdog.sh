#!/usr/bin/env bash
# External watchdog (cron every 30 min). Never edited by the dispatcher (protected path): it is the thing that survives a
# bad self-merge. If no cycle completed within MAX_SILENCE_S and no cycle is running and no STOP file is set, the checkout
# is rolled back to data/framework/last-good, a friction record is written, the owner is told, and a cycle is started.
#
# data/cycle.lock (SCHEME.md §3): every running `team cycle` holds it shared, so the exclusive try below fails while any
# product's cycle runs. The lock is kept for the whole rollback — no cycle can start on a checkout that is being reset —
# and released before the pass is started (the pass takes it shared).
set -u
cd "$(dirname "$0")" || exit 1
MAX_SILENCE_S="${MAX_SILENCE_S:-7200}"
LOG=data/cron.log
LOCK=data/cycle.lock
[ -f STOP ] && exit 0
mkdir -p data/framework

silence() {            # seconds since the last completed cycle of any product in $LOG (since 1970 when there is none)
  local last last_s
  last=$(grep -E "cycle .*: phases" "$LOG" 2>/dev/null | tail -1 | cut -c1-19 | tr ',' ' ')
  if [ -n "$last" ]; then
    last_s=$(date -u -d "$last" +%s 2>/dev/null || echo 0)
  else
    last_s=0
  fi
  echo $(( $(date -u +%s) - last_s ))
}

[ "$(silence)" -lt "$MAX_SILENCE_S" ] && exit 0        # the usual case, decided without touching the lock
exec 9>>"$LOCK"
flock -n 9 2>/dev/null || exit 0                       # a cycle is running, or the code is being updated
age=$(silence)                                         # again under the lock: no cycle can finish while it is held
[ "$age" -lt "$MAX_SILENCE_S" ] && exit 0
set -a; . ./.env 2>/dev/null; set +a
ts=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
msg="watchdog: no completed cycle for ${age}s"
good=$(cat data/framework/last-good 2>/dev/null || true)
cur=$(git rev-parse HEAD 2>/dev/null || true)
if [ -n "$good" ] && [ -n "$cur" ] && [ "$good" != "$cur" ]; then
  # 9>&-: the children do not inherit the lock (a detached `git gc --auto` left by fetch would keep it held)
  git fetch -q origin 2>/dev/null 9>&-
  if git reset -q --hard "$good" 9>&-; then msg="$msg; rolled back $cur -> $good"; else msg="$msg; rollback to $good FAILED"; fi
else
  msg="$msg; code unchanged ($cur), restarting the cycle"
fi
printf '{"ts":"%s","product":"framework","role":"watchdog","kind":"watchdog_rollback","summary":"%s","task":null,"run_id":null,"fingerprint":"watchdog:%s"}\n' "$ts" "$msg" "$ts" >> data/framework/friction.jsonl
echo "$ts $msg" >> data/watchdog.log
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_OPS_CHAT_ID:-}" ]; then
  curl -s -o /dev/null --data-urlencode "chat_id=${TELEGRAM_OPS_CHAT_ID}" --data-urlencode "text=🐕 ${msg}" "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" 9>&-
fi
flock -u 9
exec 9>&-
bash run-cycle.sh
