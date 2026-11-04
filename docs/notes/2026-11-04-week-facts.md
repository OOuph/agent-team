# Facts for the posts of the pilot week (04–10.11.2026)

Public source for the marketer and the editor: every number in the week's posts about the framework comes from here or
from the pages linked here. Written 04.11 by the operator session; not edited by the loop.

## 1. An outside critic (since 03.11)

- A separate system — another model (Codex), its own prompt and context, outside this repository and outside the team —
  reads the public surface of the four sites, their channels and repositories every two hours and writes a report.
- Each finding has a stable id (`RR-01`, `LU-01` …), a priority, a status (new / confirmed / fixed / unverified), the fact,
  a proposal and a criterion checkable from outside. Contract and place in the loop: [SCHEME.md §10](https://github.com/OOuph/agent-team/blob/main/SCHEME.md).
- The dispatcher turns each finding into an observation for the product owner, who must answer with a task or a
  reasoned reject; after every cycle the critic gets a file with what the team did with each of its ids.
- First intake, 03.11 16:07 UTC: 4 reports → 25 findings across 5 products → 9 tasks; 5 of them were released or done
  within the hour (honest counters on the diary, "23/24 hours" completeness on Luz, a plainer first screen on Release Radar).
- Why: the team's own reviewer uses the same prompts as the team. A `fixed` from the critic on its own id is the first
  outside confirmation that a change reached the reader.

## 2. The loop that fixes the framework (since 02.11)

- Register of difficulties (every refused command, failed run, blocked task) → retro of the framework every cycle →
  GitHub issue → task for the framework's developer → pull request → tests → canary cycle → merge by the dispatcher
  → the server fast-forwards. A watchdog outside the dispatcher rolls back to the last good commit if cycles stop.
- 02.11–04.11: **27 pull requests merged** into the framework; 2 of them by a human reviewer (they touched protected files),
  25 by the dispatcher after tests and canary. 96 difficulty records in the register. 0 rollbacks by the watchdog.
- What broke and was fixed (honestly): the canary first ran a *real* cycle of a product, and on 03.11 08:07 it published
  a real daily card while testing new code. Fix the same morning: the canary now runs on a copy of the product's state,
  in dry-run, without channel keys. Another: a task kept returning "partial, continues next cycle" three times on a report
  that did not exist; now a repeated partial blocks the task and asks the product owner.
- Code: [github.com/OOuph/agent-team](https://github.com/OOuph/agent-team) · scheme: [SCHEME.md](https://github.com/OOuph/agent-team/blob/main/SCHEME.md) · changes: [CHANGELOG.md](https://github.com/OOuph/agent-team/blob/main/CHANGELOG.md).

## 3. Our "people" were mostly bots (04.11)

- The sites counted a visitor as a person if the browser string looked like a browser. On 04.11 we checked the logs:
  Google's mobile crawler was "clicking" the useful / not useful links, cloud machines (Tencent, Azure, Google Cloud)
  and a scanner that changed its operating system every request looked like readers.
- The counter now ignores published cloud and crawler networks (refreshed weekly), outdated browser strings, addresses
  that rotate their system and proxy pools. Recount for 03.11, people per site:

  | Site | before | after | engaged before | engaged after |
  |---|---|---|---|---|
  | Release Radar | 55 | 18 | 19 | 5 |
  | AGENTS.md Tracker | 91 | 13 | 13 | 4 |
  | Luz Amanhã | 79 | 11 | 14 | 3 |
  | Diary | 105 | 14 | 11 | 2 |

- Clicks on outbound links and votes were almost entirely bots (Tracker votes 9 → 0). A phone that looked like the one
  real reader of Luz turned out to be inside Google Cloud. The threshold is strict on purpose: missing a person is
  cheaper than selling a bot as demand.
- Machine demand is real, though: on 03.11 a script downloaded the whole AGENTS.md Tracker (about 15,500 requests).

## 4. Turning to the reader (04.11)

- Synthetic personas used to check that a scenario works; half of their findings were "it works" and almost none became
  a task (Luz: 6 of 102). Now they run on Opus as readers, not testers: after each scenario they give a first impression,
  how many steps until they understood what the page gives them, interest and usefulness 1–5, whether they would come
  back or share, why, and one wish. The product owner sees the scores and must put work on interest and usefulness
  when they stay at 3 or below. Personas and the critic are still not demand — the people counter and real replies are.
- First run, Luz Amanhã, 04.11: two personas, interest 4 and 4, usefulness 4 and 3, both would come back, neither would
  share yet. Wishes: "next to the Telegram link, one line saying what I'll get and when", and "say when tomorrow's prices
  usually appear, and compare tonight's 00:00–06:00 with the cheapest window".

## Where to look live

- Diary: https://agentteam.formachines.ai/ · today: https://agentteam.formachines.ai/today.json
- Sites: https://releases.formachines.ai/ · https://agentsmd.formachines.ai/ · https://luz.bymachines.ai/
