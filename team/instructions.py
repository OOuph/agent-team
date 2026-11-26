"""Instructions of the roles change only through the owner (rework slice 8, owner 26.11, point 9 of the guide review:
«инструкции, которые агенты пишут себе сами: разовая чистка + правка только через владельца, пачкой раз в неделю»).

What counts as an instruction: the role cards (`team/roles/`), the deputy's mandate (`DEPUTY.md`) and each product's
`editorial-lessons.md` — text a role reads as a rule on every run. Before 26.11 the loop wrote these itself: the retro
role proposed a role-card change, the developer made it and the dispatcher merged it; the editor appended lessons.

With `framework.instructions.owner_only: true` (team.yaml, off by default) nothing of that takes effect by itself:

  proposal   the agent's change is kept as a file «было → стало» under data/framework/instruction-proposals/
             (`propose`) — the retro's role-card proposal instead of a task, the editor's lesson instead of the
             appended line, the diff of a pull request that touches an instruction path;
  protection a pull request touching an instruction path waits for the owner (`protected` — on top of
             `framework.protected_paths`);
  digest     once in `digest_every_days` (7) the owner gets ONE message with everything pending (`send_digest`), and
             marks each with `team instructions --done P-0003 --as accepted|rejected` after acting on it.

The one-time inventory of what the agents already wrote is not done here (it is the owner's review)."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import Settings

log = logging.getLogger("team.instructions")

# ------------------------------------------------------------------ parameters (team.yaml `framework.instructions`)
DEFAULTS: dict[str, Any] = {
    "owner_only": False,                        # true = instructions change only through the owner
    "paths": ["team/roles/", "DEPUTY.md"],      # repository paths that are instructions (a folder ends with /)
    "digest_every_days": 7,                     # the owner's list of pending proposals, at most this often (1..90)
}
PROPOSALS_DIR = "framework/instruction-proposals"    # under the dispatcher's data directory
STAMP = "last-digest"                           # ISO time of the last list sent, in that folder
PENDING, ACCEPTED, REJECTED = "pending", "accepted", "rejected"
STATUSES = (PENDING, ACCEPTED, REJECTED)
TEXT_MAX = 6000                                 # characters of «было» / «стало» kept in a proposal
DIGEST_SNIPPET = 280                            # characters of each side shown in the owner's list
DIGEST_MAX_ITEMS = 30                           # proposals in one list; the rest is counted
_ID = re.compile(r"^P-(\d{4})")


def settings(st: Settings) -> dict[str, Any]:
    """`framework.instructions` with the defaults; a value of the wrong type is reported and the default stays."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    src = (st.raw.get("framework") or {}).get("instructions")
    if src is None:
        return out
    if not isinstance(src, dict):
        log.warning("team.yaml framework.instructions=%r is not a mapping — ignored", src)
        return out
    if "owner_only" in src:
        if isinstance(src["owner_only"], bool):
            out["owner_only"] = src["owner_only"]
        else:
            log.warning("team.yaml framework.instructions.owner_only=%r is not true/false — false in use", src["owner_only"])
    if isinstance(src.get("paths"), list) and all(isinstance(x, str) and x.strip() for x in src["paths"]):
        out["paths"] = [x.strip() for x in src["paths"]]
    elif "paths" in src:
        log.warning("team.yaml framework.instructions.paths=%r is not a list of paths — the default in use", src["paths"])
    try:
        n = int(src.get("digest_every_days", out["digest_every_days"]))
        if not 1 <= n <= 90:
            raise ValueError
        out["digest_every_days"] = n
    except (TypeError, ValueError):
        log.warning("team.yaml framework.instructions.digest_every_days=%r is not 1..90 — %s in use", src.get("digest_every_days"), out["digest_every_days"])
    return out


def protected(files: list[str], paths: list[str]) -> list[str]:
    """The files of a change that are instructions: equal to a path, or under a path that ends with `/`."""
    return [f for f in files if any(f == p or (p.endswith("/") and f.startswith(p)) or f.startswith(p.rstrip("/") + "/") for p in paths)]


def proposals_dir(st: Settings) -> Path:
    return Path(st.data_dir) / PROPOSALS_DIR


def _load(p: Path) -> dict[str, Any] | None:
    try:
        text = p.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return None
        head, _, body = text[4:].partition("\n---\n")
        meta = yaml.safe_load(head) or {}
        if not isinstance(meta, dict):
            return None
        was, _, becomes = body.partition("\n## Стало\n")
        meta["was"] = was.split("## Было\n", 1)[-1].strip()
        meta["becomes"] = becomes.strip()
        meta["path"] = str(p)
        return meta
    except (OSError, yaml.YAMLError):
        return None


def all_proposals(st: Settings) -> list[dict[str, Any]]:
    d = proposals_dir(st)
    out = [m for m in (_load(p) for p in sorted(d.glob("P-*.md"))) if m] if d.exists() else []
    return out


def pending(st: Settings) -> list[dict[str, Any]]:
    return [m for m in all_proposals(st) if m.get("status") == PENDING]


def propose(st: Settings, *, source: str, file: str, was: str, becomes: str, why: str, evidence: list[str] | None = None,
            product: str = "", ref: str = "") -> dict[str, Any]:
    """Keep an agent's change of an instruction as a proposal file. Idempotent: the same file and the same «стало» give
    the proposal that already exists (whatever its status — a rejected text is not proposed again)."""
    was, becomes = str(was or "").strip()[:TEXT_MAX], str(becomes or "").strip()[:TEXT_MAX]
    fp = hashlib.sha256(f"{file}\n{' '.join(becomes.split())}".encode("utf-8")).hexdigest()[:16]
    have = all_proposals(st)
    for m in have:
        if m.get("fingerprint") == fp:
            return m
    d = proposals_dir(st)
    d.mkdir(parents=True, exist_ok=True)
    n = max([int(_ID.match(str(m.get("id") or "")).group(1)) for m in have if _ID.match(str(m.get("id") or ""))] or [0]) + 1
    pid = f"P-{n:04d}"
    now = datetime.now(timezone.utc)
    meta = {"id": pid, "status": PENDING, "file": file, "source": source, "product": product, "why": " ".join(str(why or "").split())[:600],
            "evidence": [str(e) for e in (evidence or [])][:12], "ref": ref, "created": now.isoformat(timespec="seconds"), "fingerprint": fp}
    path = d / f"{pid}-{now.strftime('%Y%m%d')}.md"
    path.write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n## Было\n"
                    + (was or "(не указано: предложение добавляет текст)") + "\n\n## Стало\n" + becomes + "\n", encoding="utf-8")
    log.info("instruction proposal %s: %s by %s (%s)", pid, file, source, path)
    return {**meta, "was": was, "becomes": becomes, "path": str(path)}


def decide(st: Settings, pid: str, status: str, by: str = "owner") -> dict[str, Any]:
    """Mark a proposal accepted or rejected (the owner applies an accepted text himself; this only records his word)."""
    if status not in (ACCEPTED, REJECTED):
        raise ValueError(f"status is {ACCEPTED} or {REJECTED}")
    m = next((x for x in all_proposals(st) if x.get("id") == pid), None)
    if m is None:
        raise KeyError(pid)
    p = Path(m["path"])
    text = p.read_text(encoding="utf-8")
    head, sep, body = text[4:].partition("\n---\n")
    meta = yaml.safe_load(head) or {}
    meta.update(status=status, decided=datetime.now(timezone.utc).isoformat(timespec="seconds"), decided_by=by)
    p.write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body, encoding="utf-8")
    log.info("instruction proposal %s: %s by %s", pid, status, by)
    return {**m, **meta}


def _cut(s: str, n: int = DIGEST_SNIPPET) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def digest(st: Settings) -> str:
    """The owner's list: every pending proposal as «файл — почему — было → стало». "" when nothing is pending."""
    items = pending(st)
    if not items:
        return ""
    lines = [f"Инструкции ролей: {len(items)} предложений ждут вашего решения. Файлы: {proposals_dir(st)}"]
    for m in items[:DIGEST_MAX_ITEMS]:
        lines.append(f"\n{m['id']} · {m.get('file')} · от {m.get('source')}" + (f" ({m.get('product')})" if m.get("product") else "")
                     + (f"\nПочему: {_cut(m.get('why'))}" if m.get("why") else "")
                     + f"\nБыло: {_cut(m.get('was')) or '—'}\nСтало: {_cut(m.get('becomes'))}")
    if len(items) > DIGEST_MAX_ITEMS:
        lines.append(f"\n… и ещё {len(items) - DIGEST_MAX_ITEMS}.")
    lines.append("\nОтметить решение: team instructions --done P-0001 --as accepted (или rejected). Принятый текст вы вносите сами.")
    return "\n".join(lines)


def digest_due(st: Settings, now: datetime | None = None) -> bool:
    cfg = settings(st)
    if not cfg["owner_only"] or not pending(st):
        return False
    stamp = proposals_dir(st) / STAMP
    try:
        last = datetime.fromisoformat(stamp.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return (now or datetime.now(timezone.utc)) - last >= timedelta(days=int(cfg["digest_every_days"]))


def send_digest(st: Settings, notify: Callable[[str], Any], *, force: bool = False, now: datetime | None = None) -> str | None:
    """Send the list when it is due (or `force`). The stamp is written after the send, so a failed send is tried again
    by the next pass. Returns the text sent, None when nothing was due or pending."""
    if not force and not digest_due(st, now):
        return None
    text = digest(st)
    if not text:
        return None
    notify(text)
    d = proposals_dir(st)
    d.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    (d / STAMP).write_text(stamp, encoding="utf-8")
    (d / f"digest-{stamp[:10]}.md").write_text(text + "\n", encoding="utf-8")
    log.info("instruction digest sent: %d pending proposal(s)", len(pending(st)))
    return text
