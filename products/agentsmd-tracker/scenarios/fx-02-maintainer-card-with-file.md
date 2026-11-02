---
id: fx-02-maintainer-card-with-file
persona: maintainer
kind: fixed
title: 'Card of rust-lang/rust (AGENTS.md present): read presence, size, checked time and the GitHub hop'
entry: https://agentsmd.formachines.ai/repo/rust-lang/rust/
inputs:
  repository: rust-lang/rust (Rust)
  reason: a colleague sent the card link and asked "what exactly do they show about us?"
success: 'Reports from the card that AGENTS.md is present with its size in bytes and the last-checked timestamp, that the
  other seven paths are "no", fetches the hop /go/<id>/ next to AGENTS.md (on 2026-11-02: /go/883/) and names its target
  https://github.com/rust-lang/rust/blob/main/AGENTS.md, and confirms the card shows no file contents. Size and
  timestamp change daily; the success is in reading them, not in a fixed value.'
expected_gaps:
- 'The hop page is a meta-refresh to github.com; team-fetch does not follow it and github.com may be a refused host.
  The target URL on the hop page is the click.'
- 'The card has no history (when the file appeared) and no neighbours list; it links back to the home page only.'
max_steps: 4
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

A colleague sends you https://agentsmd.formachines.ai/repo/rust-lang/rust/ and asks: "What exactly do they show
about us?" Open it and answer: which of the tracked files the site found in rust-lang/rust, how large each is,
when the check ran, where the "open on GitHub" link leads, and whether any text from the file is reproduced on the
page. Stop after max_steps fetches or when done. Report every URL you fetched, where you stopped, and classify
the outcome.
