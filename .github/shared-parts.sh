#!/usr/bin/env bash
# Does the change BASE..HEAD touch the shared parts, and if so, does the pull request carry the label?
#   bash .github/shared-parts.sh <base sha> <head sha> "<labels, space separated>"
# Exit 0: nothing shared is touched, or the label is there. Exit 1: shared files without the label (they are listed).
# What is NOT shared: products/<slug>/ and products-retired/ (a product session's own ground) and docs/notes/.
set -u
LABEL="${SHARED_LABEL:-core-change}"
OWN_RE='^(products/[^/]+/|products-retired/|docs/notes/)'      # everything else in the repository is shared
base="$1"; head="$2"; labels=" ${3:-} "
files=$(git diff --name-only "$base...$head") || { echo "the change could not be read"; exit 1; }
shared=$(printf '%s\n' "$files" | grep -v '^$' | grep -Ev "$OWN_RE" || true)
if [ -z "$shared" ]; then
  echo "no shared part is touched: no label needed"; exit 0
fi
n=$(printf '%s\n' "$shared" | wc -l | tr -d ' ')
case "$labels" in
  *" $LABEL "*) echo "$n shared file(s), label \`$LABEL\` is set: passes"; exit 0 ;;
esac
echo "This pull request changes $n shared file(s) and has no label \`$LABEL\`:"
printf '%s\n' "$shared" | head -40 | sed 's/^/  /'
echo "The shared parts are changed through the thread that owns them (CONTRIBUTING.md, «Who changes what»)."
echo "A product session: keep the change inside products/<your product>/, or ask that thread to make it."
exit 1
