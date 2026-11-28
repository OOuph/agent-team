"""What a task's text says about how it can be done, read when the task is created (pack 2 of 28.11, pokerbr К1 and К2).

К1: the acceptance of a task named the live site («the page answers 200 on placar.bymachines.ai»), and the developer
was asked to prove it before the merge — on a product where nothing is live until the merge is deployed. Three partial
runs. Such a criterion is the reviewer's, after the deploy; the developer is told so.

К2: a research task went to the analyst, a role with no tools and no network, and was closed with «I have no network».
A task for a role without tools whose text asks for the network or for a file gets a warning when it is created.

Pure functions over the task's text: no store, no model. The words are a list at the top; a miss costs nothing (the task
is treated as before), a false hit costs one line in a prompt.
"""
from __future__ import annotations

import re
from typing import Any

# ------------------------------------------------------------------ parameters
# a criterion that can be seen only on the deployed product
LIVE_WORDS = (r"live site", r"on the live", r"\blive\b.{0,20}\b(page|url|host|server)", r"in production", r"on prod\b", r"after (the )?deploy",
              r"once deployed", r"deployed (site|host|page|version)", r"на живом", r"после выкладки", r"после деплоя", r"на проде", r"в проде",
              r"no site ao vivo", r"em produção", r"após o deploy")
# what a role with no tools cannot do itself
NETWORK_WORDS = (r"https?://", r"\bfetch\b", r"\bdownload", r"\bscrape", r"\bcrawl", r"search the web", r"\bgoogle\b", r"\bserp\b", r"\bapi\b",
                 r"скачай", r"найди в сети", r"в интернете", r"по ссылке")
TOOLLESS_ROLES = ("analyst",)             # roles whose card has `tools: []` and whose answer is a report, not files of the repository
CRITERION_CHARS = 160


def _hit(words: tuple[str, ...], text: str) -> bool:
    return any(re.search(w, text, re.I) for w in words)


def after_deploy(acceptance: list[Any] | None, host: str = "") -> list[str]:
    """The acceptance criteria that can be checked only on the deployed product: they name the live site, the deploy, or
    the product's own host."""
    host = (host or "").strip().lower()
    out = []
    for a in acceptance or []:
        text = " ".join(str(a).split())
        if _hit(LIVE_WORDS, text) or (host and host in text.lower()):
            out.append(text[:CRITERION_CHARS])
    return out


def toolless_warning(role: str, task: dict[str, Any]) -> str:
    """"" or one line: the task goes to a role that has no tools, and its text asks for the network."""
    if role not in TOOLLESS_ROLES or task.get("data_request"):
        return ""
    text = " ".join(str(task.get(k) or "") for k in ("title", "problem", "scope")) + " " + " ".join(str(a) for a in task.get("acceptance") or [])
    if not _hit(NETWORK_WORDS, text):
        return ""
    return (f"the role `{role}` has no tools and no network, and the task's text asks for a page, a download or an API: it will answer "
            "«I cannot reach it». Give the task a `data_request` the dispatcher can fetch, or give it to a role that has the tool.")


def developer_note(criteria: list[str]) -> str:
    """The block for the executor's prompt: which criteria are not its to prove."""
    if not criteria:
        return ""
    return ("\n\n# CRITERIA CHECKED AFTER THE DEPLOY (not yours to prove)\nThese acceptance criteria can be seen only on the deployed "
            "product. Nothing of your branch is live before the merge: do not wait for the site, do not report `partial` because of "
            "them. Do the work, prove what the repository can show (tests, a local run), and in `self_check` mark each of these "
            "`met: true` with the evidence «after deploy: the reviewer checks it on the live site» plus what you did check locally.\n"
            + "\n".join(f"- {c}" for c in criteria) + "\n")
