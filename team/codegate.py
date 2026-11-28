"""The dispatcher checkout moves only through the gate (owner 28.11: «первые две меры делаем»).

28.11 10:34 UTC a session fast-forwarded the checkout on the server by hand, in the middle of a cycle of ai-battles:
role cards and schemas changed under a running cycle, nothing recorded who did it, and the three-cycle measurement of
the game was lost. `gate-lock.sh` makes such a move fail (no write bit on .git, a hook that refuses); this module is
the third layer, for a move that happened all the same: the gate records the commit it left the checkout on
(`data/locks/code.version`, written by `update-code.sh`), every cycle compares HEAD with it, and a difference is said
aloud — a log line, an event, one line to the owner's channel per commit. It never stops a cycle.

Nothing is checked until the record exists (`bash gate-lock.sh install`): a checkout that has no gate behaves as before.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from . import flow

log = logging.getLogger("team.codegate")

# ------------------------------------------------------------------ parameters
RECORD_FILE = "locks/code.version"            # under the data directory: "<sha> <time> <who>", written by gate-lock.sh
TOLD_FILE = "locks/code.outside-gate"         # the HEAD the owner was told about last: one line per commit, not per cycle
EVENT = "code.outside_gate"


def recorded(data_dir: Path) -> tuple[str, str]:
    """(commit, the rest of the line) the gate left the checkout on; ("", "") when there is no record."""
    try:
        parts = (data_dir / RECORD_FILE).read_text(encoding="utf-8").split(None, 1)
    except OSError:
        return "", ""
    return (parts[0], parts[1].strip() if len(parts) > 1 else "") if parts else ("", "")


def mismatch(root: Path, data_dir: Path) -> dict[str, str] | None:
    """None when the checkout is where the gate left it, when there is no record, or when HEAD cannot be read."""
    want, how = recorded(data_dir)
    head = flow.code_version(root)
    if not want or not head or head == want:
        return None
    return {"head": head, "recorded": want, "how": how}


def check(st: Any, slug: str, event: Callable[..., Any] | None = None, tell: Callable[[str], Any] | None = None) -> dict[str, str] | None:
    """Called at the start of a cycle. Says a mismatch aloud; the owner's channel gets one line per commit. Never raises."""
    try:
        bad = mismatch(st.root, st.data_dir)
        if bad is None:
            return None
        text = (f"dispatcher code is {bad['head'][:7]}, the gate left it on {bad['recorded'][:7]} ({bad['how']}): the checkout was moved "
                "outside update-code.sh. Find who did it; `bash gate-lock.sh record <who>` accepts the commit, `bash update-code.sh` moves on.")
        log.error("%s: %s", slug, text)
        if event is not None:
            event(EVENT, bad["head"][:12], "dispatcher", {"head": bad["head"][:12], "recorded": bad["recorded"][:12], "how": bad["how"][:120]})
        told = st.data_dir / TOLD_FILE
        try:
            seen = told.read_text(encoding="utf-8").strip()
        except OSError:
            seen = ""
        if tell is not None and seen != bad["head"]:
            told.parent.mkdir(parents=True, exist_ok=True)
            told.write_text(bad["head"] + "\n", encoding="utf-8")       # written first: two products' cycles start side by side
            tell(f"agent-team: {text}")
        return bad
    except Exception as exc:  # noqa: BLE001 — a check must never cost a cycle
        log.warning("%s: the code gate was not checked: %s", slug, str(exc)[:200])
        return None
