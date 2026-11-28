#!/usr/bin/env bash
# Update the dispatcher code on the server: fast-forward this checkout to origin/<branch> while no cycle is running.
#   bash update-code.sh            (sync.sh calls it over ssh; by hand on the server it is the same command)
# Cycles of several products run side by side and hold data/cycle.lock shared (team/locks.py, SCHEME.md §3). This script
# first closes data/locks/code-update.gate — from that moment no new cycle starts (`team cycle` exits 75) — then waits
# for the running cycles to end, takes data/cycle.lock exclusively, updates, and lets the cycles go on.
# Exit 75: the gate or the lock was not free within UPDATE_WAIT_S (nothing was changed). Product state is never touched.
main() {
  cd "$(dirname "$0")" || exit 1
  local BRANCH="${UPDATE_BRANCH:-main}"            # branch of origin the checkout follows
  local WAIT="${UPDATE_WAIT_S:-3600}"              # how long to wait for the running cycles, seconds
  local PIP="${UPDATE_PIP:-.venv/bin/pip}"         # reinstalls the package entry points (bin/ scripts, new dependencies)
  local GATE=data/locks/code-update.gate LOCK=data/cycle.lock
  log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) update-code: $*"; }
  mkdir -p data/locks
  # the request to drain (team/drain.py): the running cycles finish their runs in flight and end, instead of being waited
  # for to the end of their refill (27.11: 33–41 minutes with the gate closed for every product). Removed on any exit; a
  # request left behind by a killed script is forgotten by the cycles after `drain.max_min`.
  local PENDING=data/locks/update.pending T0
  T0=$(date +%s)
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) by update-code.sh (pid $$)" > "$PENDING"
  trap 'rm -f data/locks/update.pending' EXIT
  exec 8>>"$GATE"
  flock -w "$WAIT" 8 || { log "another code update holds $GATE after ${WAIT}s; nothing changed"; exit 75; }
  log "gate closed: no new cycle starts; waiting up to ${WAIT}s for the running cycles"
  exec 9>>"$LOCK"
  flock -w "$WAIT" 9 || { log "cycles still running after ${WAIT}s; nothing changed"; exit 75; }
  log "the cycles ended $(( $(date +%s) - T0 ))s after the request to drain"
  local before after
  before=$(git rev-parse --short HEAD)
  # the checkout is locked between updates (gate-lock.sh): this script is the one that may move it. Locked again on any exit.
  if [ -f gate-lock.sh ] && [ -f data/locks/code.version ]; then
    bash gate-lock.sh unlock 8>&- 9>&- || { log "the checkout could not be unlocked; nothing changed"; exit 1; }
    trap 'rm -f data/locks/update.pending; bash gate-lock.sh lock >/dev/null 2>&1' EXIT
  fi
  export TEAM_CODE_GATE=1
  # 8>&- 9>&-: the children do not inherit the locks (a detached `git gc --auto` left by fetch would keep them held)
  git fetch -q origin "$BRANCH" 8>&- 9>&- || { log "git fetch origin $BRANCH failed"; exit 1; }
  git merge -q --ff-only "origin/$BRANCH" 8>&- 9>&- || { log "fast-forward to origin/$BRANCH failed (local commits or changes?)"; exit 1; }
  after=$(git rev-parse --short HEAD)
  if [ -f gate-lock.sh ] && [ -f data/locks/code.version ]; then
    bash gate-lock.sh record update-code.sh 8>&- 9>&- || log "the new commit was not recorded (the cycles will report a mismatch)"
  fi
  if [ -x "$PIP" ]; then
    "$PIP" install -q -e . >/dev/null 2>&1 8>&- 9>&- || log "pip install -e . failed (the code itself is updated)"
  fi
  log "$before -> $after"
}
# bash reads a script as it runs it and this script replaces itself: the call and the exit sit on one line, read before the merge
main "$@"; exit $?
