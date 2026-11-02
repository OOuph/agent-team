#!/usr/bin/env bash
# daily snapshot of product state (backlog, questions, observations, reviews, digests, metrics, team.db) and .env; keeps 14
cd "$(dirname "$0")" || exit 1
mkdir -p data/backups
tar czf "data/backups/products-$(date -u +%Y%m%d-%H%M).tgz" products .env 2>/dev/null
ls -1t data/backups/products-*.tgz | tail -n +15 | xargs -r rm -f
