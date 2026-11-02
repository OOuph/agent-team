---
id: ex-01-maintainer-peers-in-language
persona: maintainer
kind: explore
title: 'A Go maintainer looks for peers: which Go repositories in the sample have AGENTS.md, and how would one find them'
entry: https://agentsmd.formachines.ai/
inputs:
  repository: gofiber/fiber (Go)
  reason: deciding whether to add AGENTS.md; wants to see what comparable Go projects did
success: 'Reports the Go row of the home table (repos, AGENTS.md count and share), then an honest account of how far one
  can get towards a list of Go repositories with AGENTS.md: the home page offers none; sitemap.xml with grep or
  guessed cards (gin-gonic/gin, labstack/echo, go-chi/chi, spf13/cobra, kubernetes/kubernetes are in the sample)
  are the only routes. States what each card fetched shows (presence yes/no), including gofiber/fiber itself, and
  names in one or two sentences what a per-language list with presence would have saved. Reaching a partial
  answer within the limit is a success if it is reported as partial.'
expected_gaps:
- 'No per-language repository list, no filter "repos with AGENTS.md in <language>", no search. The sitemap is the
  only enumeration and it is cut at 30 000 characters (use --grep).'
- 'Card addresses are case-sensitive and must match GitHub spelling.'
max_steps: 10
budget_usd: 0.5
version: 1
created: '2026-11-02'
baseline: null
---

You maintain gofiber/fiber and are deciding whether to add an AGENTS.md. You want to see what comparable Go
projects in the sample did. Start at https://agentsmd.formachines.ai/. Report what the site says about Go as a
whole, whether fiber is in the sample and what its card shows, and get as far as you can towards a list of Go
repositories that have an AGENTS.md — opening the cards of Go projects you know is allowed. Then say in one or two
sentences what would have made this easier. Stop after max_steps fetches or when done. Report every URL you
fetched, where you stopped, and classify the outcome.
