# PROTOCOL — client ↔ server messages

The server is authoritative: it owns the room state, validates every intent and broadcasts the resulting state. The
client renders `state` and sends intents; it never computes rules. Transport: one WebSocket per player, JSON text frames.
Change this file only in a task that names it in scope, and change both sides in the same commit.

## Client → server (intents)
| type | fields | preconditions | server answer |
|---|---|---|---|
| `join` | `room` (code or `new`), `name?` | room exists and has a free seat | `state` to everyone in the room, or `error` |
| `move` | `turn`, `from`, `to`, `action` | it is this player's turn, the move is legal | `state` to everyone, or `error {code: not_your_turn | illegal_move}` |
| `resign` | — | the round is running | `state` with `result` |
| `ping` | `t` | — | `pong {t}` (latency measurement) |

## Server → client
| type | fields | when |
|---|---|---|
| `state` | `room`, `turn`, `you`, `players[]`, `board`, `status` (`waiting | playing | finished`), `result?`, `version` | after every accepted intent and on (re)connect |
| `error` | `code`, `message`, `intent?` | an intent was rejected; the state did not change |
| `pong` | `t` | answer to `ping` |

## Reconnect
A player who reconnects with the same `session` token within the grace period (GAME-DESIGN.md: «what happens on a
disconnect») receives the current `state` and keeps the seat. `version` increases with every accepted intent; a client
that sees a gap asks for `state` again.

## Health and metrics (read by the dispatcher)
`GET /healthz` → 200 `{ "version": "<git sha or package version>" }`. `GET /_team/metrics.json` → rounds started and
finished today, players online, median and p95 move latency, server errors (contract in the framework's SCHEME.md).
