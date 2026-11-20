# Server: roles, dispatcher and sites on one host

Since 2026-11-20 the dispatcher runs on the same droplet that serves the sites (`ubuntu@68.183.2.193`, 4 vCPU / 8 GB,
Ubuntu 24.04), user `ubuntu`, paths `/home/ubuntu/agent-team/…` — the same layout as the previous host, so nothing in the
code or crontab changed.

- `crontab.txt` — the `ubuntu` crontab (hourly cycle at :07, state backup 02:00, watchdog, site builds, reports, autonomy
  check, GSC). Install: `crontab deploy/server/crontab.txt`.
- `formachines-external-critic.{service,timer}` — the external critic (code in `~/formachines-external-critic`, Codex CLI
  in its `runtime/`). Install as root into `/etc/systemd/system/`, then `systemctl enable --now formachines-external-critic.timer`.
- Setup once: Node 20 (nodesource), `npm install` in `~/agent-team` (Claude Code) and in the critic's `runtime/`,
  `python3 -m venv .venv && .venv/bin/pip install -e .` in `dispatcher/`; `.env`, `secrets/`, `data/x_oauth2.json` and
  `~/.codex/auth.json` are copied from the old host, never through git.
- Metrics: the dispatcher's own requests now come from this host, so `/etc/agent-team/metrics.env` lists its addresses in
  `OWN_IPS`.
