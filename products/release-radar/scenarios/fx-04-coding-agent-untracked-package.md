---
id: fx-04-coding-agent-untracked-package
persona: coding-agent
kind: fixed
title: 'Error in a package the site does not track (pydantic-ai): report "not tracked", do not confuse it with pydantic'
entry: https://releases.formachines.ai/llms.txt
inputs:
  traceback: 'ImportError: cannot import name ''RunContext'' from ''pydantic_ai'''
  versions: pydantic-ai==1.4.0 after `uv lock --upgrade-package pydantic-ai`
  project: 'agent.py: from pydantic_ai import Agent, RunContext'
success: 'Checks the package list in llms.txt (pydantic-ai is absent; pydantic is present and is a different package),
  optionally fetches /pkg/pypi/pydantic-ai/ and reads the 404, and reports "pydantic-ai is not tracked on Release
  Radar" without proposing a correction as if it came from the site. Quoting the 404 line about demand
  ("this 404 is how we notice demand") is a plus. Reading pydantic''s page as if it were pydantic-ai is a failure.'
expected_gaps:
- 'There is no "package not tracked" page; the signal is absence from llms.txt and a 404 at /pkg/pypi/<name>/. The 404
  page says untracked requests are how new packages get added.'
max_steps: 5
budget_usd: 0.3
version: 1
created: '2026-11-02'
baseline: null
---

Your user's agent code fails on pydantic-ai 1.4.0 with:

    ImportError: cannot import name 'RunContext' from 'pydantic_ai'

Your user asks you to check https://releases.formachines.ai/llms.txt before you change anything. Do so. If the site
tracks pydantic-ai, report its releases this week and any flagged quote; if it does not, say so plainly, name the
evidence, and do not present your own guess as something the site verified. Stop after max_steps fetches or when
done. Report every URL you fetched, where you stopped, and classify the outcome.
