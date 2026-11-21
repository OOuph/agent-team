# Server: roles, dispatcher and sites on one host

Since 2026-11-20 the dispatcher runs on the same droplet that serves the sites (`ubuntu@68.183.2.193`, 4 vCPU / 8 GB,
Ubuntu 24.04), user `ubuntu`, paths `/home/ubuntu/agent-team/…` — the same layout as the previous host, so nothing in the
code or crontab changed.

- `crontab.txt` — the `ubuntu` crontab (hourly cycle at :07, fast cycle check every 5 min, state backup 02:00, watchdog,
  site builds, reports, autonomy check, GSC). Install: `crontab deploy/server/crontab.txt`.
- Cycle cadence (SCHEME.md §3): `run-cycle.sh` at :07 runs every product. `run-cycle-fast.sh` is one line for all
  products — `*/5 * * * * bash /home/ubuntu/agent-team/dispatcher/run-cycle-fast.sh` — and runs `team cycle --due`: a cycle
  for each product whose `products/<slug>/product.yaml` has `cycle: {every_min: N}` (whole minutes, 5..60), no STOP file,
  and whose last finished cycle is at least N minutes old.
  To speed a product up or slow it down, edit its product.yaml; the crontab does not change. `team status <slug>` prints
  the cadence (`cycle every 10 min · next in 4 min` / `hourly`). A per-product cron line is not needed any more.
- Locks (SCHEME.md §3 «Блокировки циклов», `team/locks.py`; friction F17, 2026-11-20). The cron scripts no longer wrap
  `team cycle` in `flock`; `team cycle` takes the locks itself, so the two entries run side by side:
  `data/locks/product-<slug>.lock` — one cycle per product (the hourly pass skips a product whose cycle is running, the
  fast entry does not wait for other products); `data/locks/hourly-pass.lock` — one hourly pass at a time;
  `data/cycle.lock` — held shared by every running `team cycle`, exclusively by whatever replaces the dispatcher code;
  `data/locks/code-update.gate` — closed by a code update while it waits, so no new cycle starts and it gets its turn.
  The crontab lines are the same as before. Exit code 75 of a cron entry = nothing started (code update or another pass).
  `cat data/locks/product-<slug>.lock` shows the pid and cycle id of the holder; `team status <slug>` prints
  `cycle running now`. Lines of parallel cycles in `data/cron.log` carry the pid: `… INFO [12345] team.cycle: …`.
- Updating the dispatcher code: `bash update-code.sh` on the server (or `./sync.sh` from the owner's machine, which pushes
  `main` and runs it over ssh). It closes the gate, waits up to `UPDATE_WAIT_S` (3600 s) for the running cycles, takes
  `data/cycle.lock` exclusively, fast-forwards to `origin/main`, reinstalls the package and opens the gate. While it
  waits, no new cycle starts — so run it when the hourly pass is not in the middle of a long product, or accept the pause.
  `flock data/cycle.lock <command>` by hand still works (it waits for a moment with no cycle at all and does not stop new
  ones from starting, so with a 5-minute cadence it may wait long). Never update the checkout without one of the two.
  First update to this version (the server still has the old scripts, where the cron entries hold `data/cycle.lock`
  exclusively): `flock data/cycle.lock sh -c 'git fetch -q origin main && git merge -q --ff-only origin/main'` — the old
  way, once; `sync.sh` does exactly this when `update-code.sh` is not on the server yet. Nothing else to migrate: the
  lock files are created on first use, crontab and product.yaml files stay as they are.
- `formachines-external-critic.{service,timer}` — the external critic (code in `~/formachines-external-critic`, Codex CLI
  in its `runtime/`). Install as root into `/etc/systemd/system/`, then `systemctl enable --now formachines-external-critic.timer`.
- Setup once: Node 20 (nodesource), `npm install` in `~/agent-team` (Claude Code) and in the critic's `runtime/`,
  `python3 -m venv .venv && .venv/bin/pip install -e .` in `dispatcher/`; `.env`, `secrets/`, `data/x_oauth2.json` and
  `~/.codex/auth.json` are copied from the old host, never through git.
- Metrics: the dispatcher's own requests now come from this host, so `/etc/agent-team/metrics.env` lists its addresses in
  `OWN_IPS`.
