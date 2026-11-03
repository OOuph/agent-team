#!/usr/bin/env bash
# report-roles.sh — the 12-hour role report to the owner's Telegram (cron 07:30 and 19:30 UTC).
# Owner 03.11: "every 12 hours every role that did tasks reports in Telegram; the PO says what was chosen and implemented".
# Ledger from the product stores → one `reporter` model run → ops chat; the dry ledger is sent if the model fails.
set -u
cd "$(dirname "$0")" || exit 1
[ -f STOP ] && exit 0
set -a; . ./.env 2>/dev/null; set +a
mkdir -p data
echo "== report-roles $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> data/report.log
.venv/bin/team report --hours "${REPORT_HOURS:-12}" --send >> data/report.log 2>&1
echo "rc=$?" >> data/report.log
