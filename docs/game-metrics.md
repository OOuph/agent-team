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
      "by_result": {"win": 9, "draw": 0, "timeout": 3, "resign": 2}
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

## How the team uses it

| Reader | Where | What for |
|---|---|---|
| PO | `# LATEST METRICS` in the triage prompt (`metrics/latest.md`) | stage exit criteria (M1: ≥ N finished rounds/day, M2: returning players), what to fix first |
| analyst | `metrics_digest` (newest day first) | task effects, goal status, `insufficient_data` honestly |
| game-designer | `# GAME METRICS` in its prompt | balance: abandoned share, round length, first-mover share, rejected moves |
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
