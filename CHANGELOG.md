# Changelog

The living description of the loop is [SCHEME.md](SCHEME.md); this file records when it changed and why. Rule: a pull
request that changes behaviour (roles, phases, states, tools, policies) updates SCHEME.md and adds a line here in the same
PR, citing the friction records (F-xxxx) or the owner decision behind it. The retro role proposes, the developer writes
the PR, a human merges.

## 2026-11-02

- Self-improvement loop: friction register (`team friction`), `framework-retro` role (`team retro`), PR mode for the
  framework repository (`repo.mode: pr`, `test_cmd`), `friction` field in task-result and review-verdict. Owner
  decision 17. SCHEME.md §9.
- `not_before` on tasks: the PO schedules daily posts, the dispatcher waits (friction: Luz marketer task started a day
  early and blocked).
- Public export for the diary site (`team export-public`, `publish-diary.sh`); fourth product `agent-team-diary`.
- Cron on the server invokes scripts through `bash` (friction: execute bit lost on sync, 21:07 UTC cycle did not run).
- Evening: channel-specific AI disclosure, L0–L4 ladder in VISION, feedback collectors (GitHub issues, Bluesky
  replies), SEO check every 12 h, GEO probe weekly, IndexNow on publish. Owner decisions 13–15.
