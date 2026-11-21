# Game metrics contract: `GET /_team/metrics.json`

A site's metrics come from nginx logs (`team/sitemetrics.py`). A game server knows more than a log line can tell —
rounds, players, latency, rejected moves — so **the game server computes and serves its own metrics** at
`/_team/metrics.json` (nginx proxies the path to the live release, `Cache-Control: no-store`). The dispatcher fetches
it once per measure slot (`Cycle.fetch_metrics`, same code as for sites), stores a day file under
`products/<slug>/metrics/<day>.json`, renders `metrics/latest.md` and shows it to the PO, the analyst, the game designer
and the tester. Slice 6 of the game profile (owner 20.11); the developer gets this file's path in the SERVICE REPOSITORY
block of its prompt.

## Shape

```json
{
  "kind": "game",
  "host": "fleets.formachines.ai",
  "version": "a1b2c3d4e5f6",
  "generated": "2026-11-21T06:00:12Z",
  "now": {"online": 3, "rooms_open": 2, "rooms_playing": 1},
  "days": {
    "2026-11-21": {
      "rounds_started": 14,
      "rounds_finished": 9,
      "rounds_abandoned": 5,
      "players": 11,
      "returning_players": 4,
      "online_peak": 6,
      "round_length_s": {"median": 412, "p90": 900},
      "move_latency_ms": {"p50": 38, "p95": 140, "samples": 620},
      "moves": 610,
      "rejected_moves": 12,
      "disconnects": 7,
      "reconnects": 5,
      "errors": 1,
      "first_mover_wins": 5,
      "by_result": {"win": 9, "draw": 0, "timeout": 3, "resign": 2},
      "balance": {
        "version": "v7",
        "groups": {
          "weapons": {
            "railgun": {"picks": 120, "wins": 76, "kills": 310, "deaths": 190, "damage_dealt": 81200, "damage_taken": 60100},
            "shotgun": {"picks": 100, "wins": 48, "kills": 240, "deaths": 251, "damage_dealt": 70400, "damage_taken": 72900}
          }
        },
        "humans_only": {
          "weapons": {"railgun": {"picks": 40, "wins": 27, "kills": 98, "deaths": 60, "damage_dealt": 26100, "damage_taken": 19800}}
        }
      }
    }
  }
}
```

Rules:

- `kind: "game"` is what tells the dispatcher to render the game table instead of the site table (`Cycle.metrics_md`,
  `prompts.metrics_digest`). `version` = the running release's git sha (the same value as `/healthz`).
- `days` keyed by UTC date, **at least the last 7 days** kept in the server's store (SQLite in `DATA_DIR`): the
  dispatcher reads once per slot and merges days across files, so a missed slot loses nothing.
- Counts are per day; `players` = distinct sessions that joined a room; `returning_players` = of those, seen on an
  earlier day; `online_peak` = max concurrent connected sessions.
- `move_latency_ms` = server-side time from receiving an intent to broadcasting the state, or round-trip of `ping`/
  `pong` as measured by the client and reported back — say which in `GAME-DESIGN.md`; `samples` makes p95 readable.
- `rejected_moves` are intents the server refused (`error {code: illegal_move | not_your_turn}`); a high share means an
  unclear UI, not cheating. `first_mover_wins` / `rounds_finished` is the first balance signal the game designer reads.
- Missing fields are fine (the renderer prints `—`); extra fields are kept in the day file and ignored by the tables.
- No personal data: no names, IPs or user agents here (the file is public).

## The `balance` block (optional; owner 21.11)

Automatic balancing needs numbers per option, not per round. A game whose players pick something — a unit, a weapon, a
class, a level — may add a `balance` object to every day:

| Field | Meaning |
|---|---|
| `"version"` | the version of the balance tables (`design/balance/*.json`) that produced the day's matches. It changes with every balance patch; the dispatcher never mixes numbers of two versions |
| `"groups"` | `{"<group name>": {"<option key>": {...}}}` — a group is one choice a player makes (weapons, chassis, levels); an option is one thing that can be picked |
| `"picks"` | how many times the option was picked in a finished match (integer) |
| `"wins"` | of those, how many times the picker won (integer) |
| `"kills"`, `"deaths"` | totals for the pickers of the option (integers; print 0 or leave out when the game has no such notion) |
| `"damage_dealt"`, `"damage_taken"` | totals for the pickers of the option (numbers) |
| `"humans_only"` | optional, the same shape as `groups`: the same counts over matches of human players only. Bots can hide or fake an imbalance, so both are shown |

Rules of the block:

- Per day, like every other count; a day without matches may leave the block out. A server that sends no `balance`
  block at all changes nothing: no table, no observation (every product before 21.11).
- The dispatcher (`team/balance.py`) sums the block over the last `window_days` days (default 7) **that carry the newest
  `version`**, and renders one table per group: option, picks, win rate (`wins / picks`), deviation (win rate − the
  centre of the band), K/D, damage dealt/taken, verdict. It closes `metrics/latest.md` (PO, game designer) and the
  analyst's digest.
- Thresholds live in the product's `product.yaml`, defaults in code:

  ```yaml
  balance:
    win_rate_band: [0.45, 0.55]   # an option is balanced while its win rate stays inside (default)
    min_picks: 30                 # fewer picks = «few picks», shown but never raised (default)
    window_days: 7                # days summed (default)
    reraise_h: 24                 # the same finding is raised again after this many hours (default)
    sim_report: design/reports/simulation.md   # the repository's simulation report shown to the game designer (default)
  ```

  The default band fits a duel (expected win rate 0.5). A free-for-all of N players has an expected win rate of 1/N:
  set the band around it.
- In every measure phase the dispatcher raises **one observation per option** whose win rate is outside the band with
  at least `min_picks` picks — class `balance`, source `dispatcher`, title `Balance: <group>/<key> win rate 0.63 over
  120 picks (band 0.45–0.55)` (`…, humans only` when only the human numbers are out). The same option of the same
  tables version is not raised again for `reraise_h` hours; a new `version` (a patch went live) or the passed interval
  raises it again. At most 6 new cards per check, the strongest deviations first. The PO turns each into a
  game-designer task at once (`standing: balance`, `team/standing.py`); the cure is a data patch of one lever.
- The same numbers from the product's own simulation are a file in the repository (`balance.sim_report`), written by
  the developer's simulation tool; the game designer reads both and says where they disagree.

## How the team uses it

| Reader | Where | What for |
|---|---|---|
| PO | `# LATEST METRICS` in the triage prompt (`metrics/latest.md`) | stage exit criteria (M1: ≥ N finished rounds/day, M2: returning players), what to fix first; the Balance table and `balance` observations |
| analyst | `metrics_digest` (newest day first) | task effects, goal status, `insufficient_data` honestly; the effect of a balance patch across tables versions |
| game-designer | `# GAME METRICS` in its prompt | balance: abandoned share, round length, first-mover share, rejected moves; the Balance table per option for the standing `balance` task |
| tester | `team-fetch <base>/_team/metrics.json` during a play-test or a load task | before/after a load run: errors, latency, online |

## Load tasks (type `load`, role `tester`)

The PO creates a `load` task with a threshold in the acceptance («50 clients for 60 s: p95 move latency ≤ 300 ms,
0 server errors»). The dispatcher starts the server from the current commit (or uses the deployed host when
`deploy.mode` is not `pending`) and the tester runs `team-play load --clients N --seconds S [--script tests/load/clients.ts]`:
the repository's load script (starter in `team/templates/service-repo/tests/load/clients.ts`) opens N WebSocket
clients, pairs them into rooms, sends intents at a steady rate and prints one JSON line — `connected`, `messages`,
`p50_ms`, `p95_ms`, `errors`, `disconnects` — which `team-play` summarises and files in the review folder. The tester
reads `/_team/metrics.json` and the server log after the run and returns a `test-report` whose `latency_ms` holds the
numbers; pass = every threshold met.
