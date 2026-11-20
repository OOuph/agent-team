# Server: roles, dispatcher and sites on one host

Since 2026-11-20 the dispatcher runs on the same droplet that serves the sites (`ubuntu@68.183.2.193`, 4 vCPU / 8 GB,
Ubuntu 24.04), user `ubuntu`, paths `/home/ubuntu/agent-team/…` — the same layout as the previous host, so nothing in the
code or crontab changed.

- `crontab.txt` — the `ubuntu` crontab (hourly cycle at :07, fast cycle check every 5 min, state backup 02:00, watchdog,
  site builds, reports, autonomy check, GSC). Install: `crontab deploy/server/crontab.txt`.
- Cycle cadence (SCHEME.md §3): `run-cycle.sh` at :07 runs every product. `run-cycle-fast.sh` is one line for all
  products — `*/5 * * * * bash /home/ubuntu/agent-team/dispatcher/run-cycle-fast.sh` — and runs `team cycle --due`: a cycle
  for each product whose `products/<slug>/product.yaml` has `cycle: {every_min: N}` (whole minutes, 5..60), no STOP file,
  and whose last finished cycle is at least N minutes old. Both take `flock -n data/cycle.lock`, so they never overlap.
  To speed a product up or slow it down, edit its product.yaml; the crontab does not change. `team status <slug>` prints
  the cadence (`cycle every 10 min · next in 4 min` / `hourly`). A per-product cron line is not needed any more.
- `formachines-external-critic.{service,timer}` — the external critic (code in `~/formachines-external-critic`, Codex CLI
  in its `runtime/`). Install as root into `/etc/systemd/system/`, then `systemctl enable --now formachines-external-critic.timer`.
- Setup once: Node 20 (nodesource), `npm install` in `~/agent-team` (Claude Code) and in the critic's `runtime/`,
  `python3 -m venv .venv && .venv/bin/pip install -e .` in `dispatcher/`; `.env`, `secrets/`, `data/x_oauth2.json` and
  `~/.codex/auth.json` are copied from the old host, never through git.
- Metrics: the dispatcher's own requests now come from this host, so `/etc/agent-team/metrics.env` lists its addresses in
  `OWN_IPS`.
