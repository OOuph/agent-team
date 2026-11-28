#!/usr/bin/env bash
# The dispatcher checkout moves only through the gate (owner 28.11: «первые две меры делаем»).
# 28.11 10:34 UTC: a session fast-forwarded ~/agent-team/dispatcher by hand in the middle of a cycle of ai-battles; the
# role cards and schemas changed under a running cycle and the three-cycle measurement of the game was lost.
#
#   bash gate-lock.sh install   put the hook in place, record the commit, lock          (once per checkout; idempotent)
#   bash gate-lock.sh lock      take the write bit off .git and .git/refs/heads         (update-code.sh, after its merge)
#   bash gate-lock.sh unlock    give it back                                            (update-code.sh, before its fetch)
#   bash gate-lock.sh record    write the commit the gate left the checkout on          (update-code.sh, after its merge)
#   bash gate-lock.sh status    locked or not, the recorded commit, HEAD
#
# Two layers, because every session on the server is the same user:
#   1. no write bit on .git and .git/refs/heads: fetch, pull, merge, reset, checkout fail with «Permission denied»;
#   2. the hook `reference-transaction`: a move of a local branch without TEAM_CODE_GATE=1 is refused with a message
#      that names update-code.sh (this is what answers when somebody gave the write bit back by hand).
# A third one is in the cycle itself (team/codegate.py): HEAD that differs from the recorded commit is said aloud.
# None of this is a wall against intent — the same user can undo it; it is a wall against a mistake.
set -u
cd "$(dirname "$0")" || exit 1
RECORD="${GATE_RECORD:-data/locks/code.version}"      # the commit the gate left the checkout on: "<sha> <time> <who>"
HOOK=.git/hooks/reference-transaction
LOCKED_DIRS=(.git .git/refs/heads)
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) gate-lock: $*"; }

hook_body() {
  cat <<'HOOK'
#!/bin/sh
# Installed by gate-lock.sh: the dispatcher checkout moves only through update-code.sh.
[ "$1" = "prepared" ] || exit 0
[ "${TEAM_CODE_GATE:-}" = "1" ] && exit 0
while read -r old new ref; do
  case "$ref" in
    refs/heads/*|HEAD)
      echo "REFUSED: this checkout is the running dispatcher; its code moves only through the gate." >&2
      echo "  Use: bash update-code.sh   (waits for the cycles to end, updates, records the commit)" >&2
      echo "  A direct pull/merge/reset here changes role cards and schemas under running cycles (28.11)." >&2
      exit 1 ;;
  esac
done
exit 0
HOOK
}

case "${1:-status}" in
  install)
    [ -d .git ] || { log "no .git here; nothing done"; exit 1; }
    chmod u+w "${LOCKED_DIRS[@]}" 2>/dev/null
    mkdir -p .git/hooks
    hook_body > "$HOOK" && chmod 755 "$HOOK" || { log "the hook could not be written"; exit 1; }
    bash "$0" record "${2:-install}" && bash "$0" lock ;;
  record)
    mkdir -p "$(dirname "$RECORD")"
    sha=$(git rev-parse HEAD 2>/dev/null) || { log "HEAD could not be read; nothing recorded"; exit 1; }
    echo "$sha $(date -u +%Y-%m-%dT%H:%M:%SZ) ${2:-update-code.sh}" > "$RECORD.tmp" && mv "$RECORD.tmp" "$RECORD"
    log "recorded $(echo "$sha" | cut -c1-7) (${2:-update-code.sh})" ;;
  lock)
    chmod a-w "${LOCKED_DIRS[@]}" && log "locked: ${LOCKED_DIRS[*]}" ;;
  unlock)
    chmod u+w "${LOCKED_DIRS[@]}" && log "unlocked: ${LOCKED_DIRS[*]}" ;;
  status)
    if [ -w .git ]; then echo "lock: OFF (.git is writable)"; else echo "lock: on"; fi
    [ -x "$HOOK" ] && echo "hook: on" || echo "hook: OFF"
    echo "recorded: $(cat "$RECORD" 2>/dev/null || echo none)"
    echo "HEAD:     $(git rev-parse HEAD 2>/dev/null)" ;;
  *) echo "usage: bash gate-lock.sh install|lock|unlock|record|status" >&2; exit 2 ;;
esac
