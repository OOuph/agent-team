# agent-team

A dispatcher that runs a small product with AI roles (product owner, developer, reviewer, analyst, marketer, personas,
GEO probe, voice-of-user researcher) as short headless [Claude Code](https://claude.com/claude-code) runs on a subscription token. A product
description goes in; a static site that keeps improving against measurable goals comes out. People are needed only
for decisions outside the product's own vision.

The full scheme of the loop (roles, phases, task states, feedback, analytics, policies, self-improvement) is in
[SCHEME.md](SCHEME.md). The live diary written by the team about itself: https://agentteam.formachines.ai.

## How it works in one paragraph

Roles never talk to each other; everything is a file in `products/<slug>/` (tasks, questions, observations, reviews,
digests, metrics) plus an SQLite ledger (claims, events, spend, posts). Every hour `team cycle` runs the phases
collect → review → triage → execute → measure → geo → digest for each product: the PO turns observations into tasks
with acceptance criteria, the developer implements one task on a branch, the dispatcher merges and the site deploys
itself from git, the reviewer checks the live site, the analyst reads the server logs, the marketer posts in allowed
channels. Budgets, leases, a STOP file and an inactivity watchdog keep it bounded. The framework also keeps a register
of where it got in its own way and a retro role turns that into issues and pull requests on this repository.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env            # CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), GITHUB_TOKEN, channel keys
.venv/bin/team init my-site --host my-site.example.com --repo https://github.com/you/my-site.git
# edit products/my-site/VISION.md — goals, promises, channels, permissions
.venv/bin/team cycle --product my-site --dry-run
.venv/bin/team cycle --product all          # cron this hourly: see run-cycle.sh
```

Other commands: `team status`, `team tasks <slug>`, `team ask <slug> --text ...`, `team answer`, `team obs`,
`team stop|resume`, `team digest`, `team export-public`, `team friction`, `team retro`.

Deployment of the sites (nginx + git pull timer + metrics from logs) is in `deploy/droplet/`. Tests: `pytest -q`.

## Status

Pilot since 2 November 2026: four sites and the framework itself, hourly, without a human in the loop. First week
(Nov 2–8): 677 cycles, 1,108 role runs, ~260 tasks closed, 53 framework changes merged by the loop itself (tests +
canary, automatic rollback), 98 posts in six channels, ~$172 API-equivalent spend inside one subscription.
What it does not solve: people. Each site gets 2–40 humans a day after launch; agent posting alone brings few.

The sites it runs:
- [AGENTS.md Tracker](https://agentsmd.formachines.ai) — which of 1,922 top GitHub repos have AGENTS.md, CLAUDE.md, .cursorrules, …
- [Release Radar](https://releases.formachines.ai) — PyPI/npm releases with the verbatim "breaking / deprecated / removed" lines
- [Luz Amanhã](https://luz.bymachines.ai) — tomorrow's hourly electricity price in Portugal (OMIE), with Home Assistant sensors
- [Diary](https://agentteam.formachines.ai) — what the team did each day, written by the team

Expect rough edges; the diary reports them. License: MIT.
