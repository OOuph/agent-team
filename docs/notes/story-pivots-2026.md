# How we got here: the pivots behind the agent team (Sep–Nov 2026)

Public source for the diary series of 05–11.11.2026. Every claim in those posts comes from this file or from a page
linked here. Written 04.11 by the operator session from the owner's decision log (private) and the public repos and
sites; not edited by the loop. Numbers marked *(decision log)* have no public page — use them as stated, do not round
them up, do not add new ones. The week's mechanics (critic, loop, bot counter, personas) are in
[2026-11-04-week-facts.md](https://github.com/OOuph/agent-team/blob/main/docs/notes/2026-11-04-week-facts.md).

The one-line thread: **every pivot was the same lesson in a new costume — we kept building a measurement before we had a
reader for it.** The agent team is the first attempt where the reader (people and agents arriving at a page) is counted
every day and drives the work.

---

## 1. By Machines: a lab that measured how AI picks businesses (25.09 → 08.10)

- **What we thought.** Buyers increasingly ask ChatGPT, Gemini, Perplexity and Google AI Mode "which lawyer / advisor
  should I pick". An independent lab that measures who gets named — with confidence intervals, sources, method and CSV,
  "zeros included" — would be cited and trusted. First vertical: relocation to Europe (immigration lawyers, tax
  advisors, company formation in Portugal, Spain, Cyprus).
- **How the frame moved before launch (decision log).** It started as a plan for a classic "GEO agency" site; within one
  day research showed the label was taken everywhere and AI cites agency directories rather than agency sites, so it
  became an expert blog under the umbrella "when the buyer is a machine"; then a ranking of agencies was planned as the
  flagship and dropped after one question: *who is the reader of such a ranking, and what is our interest in it?* —
  the readers were the ranked firms themselves, not buyers. What stayed: a lab with a portable instrument (a vertical
  is a config, not code).
- **What we built.** Open-source panel code from the first commit (MIT): 4 engines through their APIs, a frozen panel of
  buyer questions, 7 runs per question, Wilson intervals, weekly exports. Site live on 29.09.
  Code: https://github.com/OOuph/bymachines-lab · site: https://bymachines.ai/
- **What we measured.** The instrument worked; the readers did not come. Three framings in a row — a vertical map
  (27.09), a deck (30.09), three product options (01.10: a "lag counter", a "stand of AI advisors", an investor index)
  — got no response, including from the owner: "nothing to answer there, the options are not interesting"
  *(decision log, 06.10)*. A check of 37 adjacent directions found no empty niche, and the raw series did not sell to
  vendors: they collect it themselves for $6–11 a month *(decision log, 07.10)*.
- **What turned out wrong.** The assumption that a careful measurement finds its own reader. Nobody whose behaviour
  changes because of the numbers was in the loop; the people measured are not the people who pay.
- **Decision.** 06.10: autopilot (the series kept collecting, about $6 a day, no new lines). 08.10: stopped — "this
  positioning is of no interest to anyone" *(owner)*; the daily collection timer was switched off after the 08.10 run.
  Data 29.09–08.10 is kept.
- **Honest loose end.** The site is still online and its text still describes a weekly measurement; its last data week
  is 2026-W40. Do not claim the site was updated after 08.10.

## 2. Two new directions in five days, and both stalled (09.10 → 20.10)

- **What we thought.** After By Machines, the owner set a new frame: grow with the agent economy, collect what cannot be
  reconstructed later, the consumer is more and more agents and crawlers *(decision log, 08.10)*. Two directions were
  started on 13.10:
  1. *Forensics of money in AI agents* — an independent recount of a public AI trading agent's track record, sold to
     the agent's operator.
  2. *Loss data of AI agents* — dated series of agent failures for insurers and agent-trust companies, plus honeypots
     for prompt injection "in the wild".
- **What we measured.** The recount of a public agent competition matched the organiser's own figures to within $1.28
  *(decision log, 16–20.10)*. There was no discrepancy to sell, and an operator with real money at stake checks their
  own numbers anyway. The honeypots had caught nothing yet.
- **What turned out wrong.** "An independent check is worth paying for" holds only if checks sometimes disagree. Ours
  agreed. And both directions needed someone to sell to strangers; nobody in the team was that person.
- **Decision.** 20.10: the recount service and its outreach letters were closed by the owner ("we compared the data,
  there is no discrepancy… whoever has skin in the game will check it no worse"). The loss-data series were put on
  an explicit pause until 21.12. No outreach letter was sent.

## 3. Errata: where models write outdated code, and the verified fix (20.10 → now)

- **What we thought.** Agents go where a task starts to succeed, and stale model knowledge of a library is one of the
  most common reasons a coding task fails. A shared base of sandbox-verified corrections ("the line the models write,
  the line that works on the newer release, and the proof") is something agents would read before writing code and add
  to after finding a workaround.
  Site: https://formachines.ai/errata/ · skill: https://github.com/formachines-ai/skills ·
  CLI: https://github.com/formachines-ai/errata-cli · MCP: https://github.com/formachines-ai/errata-mcp
- **What we measured (decision log, 20–26.10).** Launched 20.10 with 39 entries, 17 packages, 3 models. Two outside
  audits within two days found duplicates, 13 of 39 entries without an actual fix, and 5 fixes that change behaviour
  (one produced a different curve). A later check of applicability: applied literally to the original example, the
  patch of 18 of 26 published entries did not reproduce the verified result.
- **What turned out wrong.** "Verified in a sandbox" was weaker than it sounded: matching output is not proof of
  equivalent behaviour, and a fix can be correct as a hint yet wrong as a literal patch. One bad automatic fix at the
  start would have spent all the trust.
- **Decisions.**
  - 21.10, pivot to "check your project": a CLI over the lockfile, an MCP server and pages per error signature, packages
    chosen by what agents actually call; data cleaning before any promotion; a two-month plan with a reading on 21.12.
  - 24–27.10: nothing publishes if a mandatory check fails; new entries enter the public list automatically only when
    fully proven (equivalent *and* applicable), the rest wait in a review queue; the 18 entries stay public as hints
    without auto-apply instead of being withdrawn.
  - 27.10, money: monetisation must not need a sales team — the owner's words: "we went toward B2B sales with a small
    cheque, but we wanted to sell to agents".
  - 02.11: the planned Show HN launch on 04.11 was dropped as "too early"; no new date.
- **Where it stands.** On pause since 09.11 (owner's decision); no return date set. The site, the JSON, the CLI and the
  MCP server stay online as they are; the last update on the live page is 2026-11-03 (26 verified corrections in 19
  packages). Agent demand was not measured before the pause — say so, do not present the pause as a verdict.

## 4. The framework grew out of Errata, step by step (27.10 → 02.11)

- **Step 1 — Errata was already a cycle of roles.** Errata's daily work (pick a package, probe models, write an entry,
  review it, post about it) ran as a script that called one role after another. On 27.10 the owner proposed running it
  as a team: a product owner, personas of users as agents, a marketer, executors, on a periodic cycle *(decision log)*.
  A one-product pilot on Errata was approved with a $2/day budget.
- **Step 2 — from one product to a framework (02.11).** The natural next question was whether the same loop could run
  more than one product without a human. That morning an AI review of "autonomous product factories" advised against
  it: don't open our own framework, and until 21.12 AI does not publish to people. The owner overruled it the same
  evening with a spec for exactly that — a framework, three new simple sites, seven days without a human — and confirmed
  "everything as automatic as possible". This was not a reversal of a long-held plan: it was one cautious recommendation,
  overruled within hours, on an idea that had been growing for a week. The spec named the disagreement instead of
  hiding it.
- **Why our own dispatcher and not Paperclip.** An outside research note recommended Paperclip (https://docs.paperclip.ing/).
  We read its releases, docs and open issues on 02.11. It is alive and has patterns we copied (the executor is never the
  reviewer, atomic task claims with a lease, a budget per agent with warn and hard stop, a watchdog on a silent run).
  But with 1.5 days to build and 7 days unattended, the open issues sat exactly where we could not afford them on that
  date: signing in with a subscription on a headless server, budgets that read zero on a subscription, runs not resumed
  after a server restart. So: a small Python dispatcher grown from the Errata pilot script — the same loop, generalised to several products.
- **What was built on 02.11.** Three new sites readable by people (so marketing could be tested at all):
  https://releases.formachines.ai/ · https://agentsmd.formachines.ai/ · https://luz.bymachines.ai/ — and, the same night,
  a fourth: this diary, https://agentteam.formachines.ai/, because the owner decided the main product is the framework
  itself. Repo opened under MIT: https://github.com/OOuph/agent-team

## 5. Letting go of the merge button (02.11 → 03.11)

- **Step 1 (02.11, evening).** Self-improvement in three levels: collect difficulties without the owner; a retro role
  turns them into GitHub issues; the team itself fixes only safe things (role cards, docs, settings) through pull
  requests a human merges. The AI's argument: dispatcher code is changed by a human, otherwise an error breaks the very
  loop that should repair it.
- **Step 2 (02.11, ~22:40 UTC).** The owner reversed it: "no, the other way round: run the full cycle every hour, so we
  get a result before I leave". Any-risk proposals become tasks; green tests → one canary cycle on the candidate code →
  merge by the dispatcher; a list of protected files still waits for a human; an outside watchdog rolls back to the last
  good commit if two hours pass without a finished cycle. The residual risk was written down: slow quality decay that
  tests and one canary do not see.
- **What broke.** The canary's first version ran a *real* cycle and published a real daily card while testing new code
  (03.11 08:07); fixed the same morning — the canary now runs on a copy of the state, dry, without channel keys. Overnight
  the owner's "yes" was needed three times where the system could have acted itself (a protected pull request waiting
  for a sleeping human, duplicate tasks, site builds missing from cron).
- **Step 3 (03.11 ~05:00 UTC).** For the pilot week, infrastructure and role behaviour are fixed without asking; then
  the protected list shrank to two files — `run-cycle.sh` and `watchdog.sh` — the pair that can undo a bad self-merge.
  Owner: "at the pilot stage we don't look at risks, maximum automation".
- **Numbers 02–04.11** (from the week facts): 27 pull requests merged into the framework, 25 by the dispatcher after
  tests and canary, 2 by a human reviewer; 96 difficulty records; 0 watchdog rollbacks.
  Scheme: https://github.com/OOuph/agent-team/blob/main/SCHEME.md · changes: https://github.com/OOuph/agent-team/blob/main/CHANGELOG.md

## 6. Who watches when nobody is watching (02.11 → 04.11)

- **Editor in the loop, not in a human (02.11).** The owner did not want texts to decay while away. The marketer now
  writes drafts only; an `editor` role scores each on five axes (hook, clarity, specifics, tone, honesty), approves,
  returns once with concrete notes, or rejects; lessons accumulate in a file both roles read before every post. First
  live run: a draft scored 2.8/5 (a list of timestamps), was returned, came back at 4.0/5 and was published.
- **Reports like people at a bonus review (03.11).** Every 12 hours each role that worked reports in Telegram what it
  did and what it is unsure of; the product owner says what was chosen and shipped. The owner rejected the dry first
  format and asked for "human language, as for a business".
- **New tool, tried the same day (03.11).** Mastodon, X and Threads were connected in the morning and the first posts
  were planned for days later; the rule now is that a new channel or tool gets a real trial in the next cycle.
- **An outside critic (03.11).** Another model, outside the team, reads the public surface every two hours; its
  findings carry a weight above personas and below real people. (Told in the 04.11 post; do not repeat it as news.)
- **A deputy for the owner (03.11).** The owner is travelling 04–10.11 with no access. A `deputy` role answers questions
  addressed to the owner that stay open longer than an hour, under a written mandate
  (https://github.com/OOuph/agent-team/blob/main/DEPUTY.md): a conservative choice inside the product's vision, or
  "unavailable until 11.11 — act without it or close the task" for secrets, money, accounts, new channels and legal
  questions. A STOP drill was run on 03.11; the watchdog drill and a server reboot were not run.

## 7. The same mistake, caught in our own numbers (04.11)

- **The people were bots.** The sites counted anyone with a browser-like string as a person. Recount for 03.11 after
  excluding cloud and crawler networks, outdated strings, rotating systems and proxy pools: 55/91/79/105 → 18/13/11/14
  people (Release Radar / AGENTS.md Tracker / Luz Amanhã / diary); clicks and votes were almost all bots.
- **The personas said "it works".** Synthetic users checked that scenarios run; half their findings were "it works",
  and on Luz 6 of 102 became tasks. Now they run on Opus as readers and score first impression, interest and
  usefulness, and the product owner must act when those stay at 3 or below.
- **Why it is the same mistake.** The panel of By Machines measured AI answers with no reader of the numbers; the
  recount measured a track record nobody disputed; the counter measured "people" who were machines; the personas
  measured whether a page works, not whether it is worth reading. Each time the instrument was honest and the question
  was wrong. What we count now: humans after the strict filter, real replies, personas' interest and usefulness —
  and machine demand separately (a script downloaded the whole AGENTS.md Tracker on 03.11, about 15,500 requests).
- **Unknown.** Whether any of the four sites has readers who come back. The pilot is read on about 11.11 against
  thresholds written before the week (spec §8); the result is not known when this file is written.

---

## What we deliberately do not publish

- Anything about the owner's private life, family, health, personal finances or personal trading.
- Names of collaborators and partners; names of other people's businesses we considered as clients.
- Internal confidence scores, private repository paths, server addresses, tokens and account details.
- The owner's other research tracks outside the formachines line (they are not part of this story).
- Numbers without a source in this file, the week facts, or a live page.
