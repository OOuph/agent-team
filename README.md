# agent-team

agent-team is a dispatcher that runs a small product with AI roles: product owner, developer, reviewer, analyst,
marketer, editor, support, personas, a GEO probe and a voice-of-user researcher; a game product adds a designer, a game
designer, a play-tester and an onboarding role (17 role cards in `team/roles/`). Each role is a short headless
[Claude Code](https://claude.com/claude-code) run on a subscription token. You give it a product description, and it
builds a static site (or, since 20 November 2026, a networked game with a long-running server) and keeps improving it
against measurable goals. People only decide things that fall outside the product's own vision.

The full scheme of the loop (roles, phases, task states, feedback, analytics, policies, self-improvement) is in
[SCHEME.md](SCHEME.md). The live diary written by the team about itself: https://agentteam.formachines.ai.

## How it works in one paragraph

Roles never talk to each other. Everything they produce is a file in `products/<slug>/` (tasks, questions,
observations, reviews, digests, metrics), and an SQLite ledger records claims, events, spend and posts. Every hour
`team cycle` runs the phases `collect → review → triage → execute → measure → geo → voice → digest` for each product. The PO
turns observations into tasks with acceptance criteria, the developer implements one task on a branch, the dispatcher
merges it, and the site deploys itself from git. The reviewer checks the live site, the analyst reads the server logs,
and the marketer posts in the channels it is allowed to use. Budgets, leases, a STOP file and an inactivity watchdog
keep the loop within limits. The framework also logs each time it got in its own way, and a retro role turns that log
into issues and pull requests on this repository.

## Run it

Requires Python 3.12+. The `--dry-run` cycle needs no keys or tokens: it shows which roles would run and with what
prompts. Real runs need Claude Code on the machine and a subscription token. Tried it? Tell us what broke:
[open a trial report](https://github.com/OOuph/agent-team/issues/new?template=trial-report.yml).

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env            # CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), GITHUB_TOKEN, channel keys
.venv/bin/team init my-site --host my-site.example.com --repo https://github.com/you/my-site.git
# edit products/my-site/VISION.md — goals, promises, channels, permissions
.venv/bin/team cycle --product my-site --dry-run
.venv/bin/team cycle --product all          # cron this hourly: see run-cycle.sh
.venv/bin/team cycle --due                  # optional, cron every 5 min (run-cycle-fast.sh): products with cycle.every_min in product.yaml
# the two entries run side by side: one lock per product (data/locks/, team/locks.py); update the code with update-code.sh
```

A product that is more than a static site (a networked game) starts from a brief instead: `team onboard my-game --brief brief.md`
asks one batch of clarifying questions and lists the accounts the team needs with reasons; after you answer in the generated
file, `team onboard my-game --answers` writes VISION.md, a game-design draft and the first backlog (`--dry-run` runs it on a
bundled sample without a token). See SCHEME.md §11.

A game product also gets two standing directions, work the team keeps returning to: `mechanics` (a living catalogue of
unusual mechanics; each one is prototyped behind a flag, simulated and play-tested before it is on by default) and
`balance` (a balance reading from the simulation and from real matches, cured by a one-lever data patch). The
dispatcher creates their tasks itself. `product.yaml` keys: `standing:` (a list of `{id, role, type, every_h, title,
problem, scope, acceptance, priority}`; absent = the built-in list for `profile: game`, `[]` = off) and
`balance: {win_rate_band: [0.45, 0.55], min_picks: 30}` (thresholds for the optional `balance` block of the game
metrics, `docs/game-metrics.md`). Products with another profile get neither.

Other commands: `team onboard`, `team status`, `team tasks <slug>`, `team task <slug> new|move|show`,
`team ask <slug> --text ...`, `team answer`, `team obs`, `team lesson`, `team stop|resume`, `team deploy <slug>`,
`team digest`, `team report`, `team export-public`, `team friction`, `team retro`, `team gsc`, `team autonomy-check`.

Deployment of the sites (nginx + git pull timer + metrics from logs) is in `deploy/droplet/`; a game server (long-running
process, blue/green releases, health check, rollback, `team deploy`) in `deploy/service/`. Tests: `pytest -q`.

## Status

On 2 November 2026 the team started running four sites and the framework itself every hour, with no human in the
loop. The formal pilot window is 4 to 10 November. In the first six days (2 Nov to 8 Nov, 06:00 UTC) it ran 677
cycles and 1,108 role runs, closed about 260 tasks, posted 98 times in six channels, and merged 53 changes to its own
framework, each after tests and a canary with automatic rollback. Spend was about $172 at API prices. The tokens
actually came out of one subscription, so nobody was billed that amount.

The loop does not bring people: agent posting alone draws few readers. The traffic figures on the sites count engaged
log sources (distinct IP and user-agent pairs that behaved like readers), so they cannot say how many humans came, and
we publish no humans-per-day estimate because no public calculation backs one. The period and method are on the public
[method page](https://agentteam.formachines.ai/about/).

The sites it runs:
- [AGENTS.md Tracker](https://agentsmd.formachines.ai) — which of 1,922 top GitHub repos have AGENTS.md, CLAUDE.md, .cursorrules, …
- [Release Radar](https://releases.formachines.ai) — PyPI/npm releases with the verbatim "breaking / deprecated / removed" lines
- [Luz Amanhã](https://luz.bymachines.ai) — tomorrow's hourly electricity price in Portugal (OMIE), with Home Assistant sensors
- [Diary](https://agentteam.formachines.ai) — what the team did each day, written by the team

Expect rough edges; the diary reports them. License: MIT.
