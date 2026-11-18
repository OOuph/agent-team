"""AI-tell catalogue for public English text: the editor reads the full humanizer catalogue, writers get a checklist.

Why: owner 18.11 — new public texts (posts, replies) should not read as machine-written. The catalogue is the
humanizer skill v4.0.0 by blader (MIT, github.com/blader/humanizer, commit b48c832), vendored verbatim in
``docs/style/humanizer.md`` with its licence next to it; it was audited by hand before vendoring. Update it only by
re-auditing a new commit and replacing the file.

- ``catalogue()`` — the skill body without its YAML front matter, for the editor (one role run per post).
- ``checklist()`` — one line per pattern (number, name, watch-for list), extracted from the same file so the two
  never drift; for the marketer and support, whose prompts should stay short.
- ``OVERRIDES`` — where this project's formats outrank the catalogue (numeric ranges, Portuguese, the disclosure).

A missing file is not an error: every function returns "" and the prompts read as before.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("team.style")

CATALOGUE_FILE = Path(__file__).resolve().parent.parent / "docs" / "style" / "humanizer.md"
SOURCE = "humanizer v4.0.0 (blader/humanizer, MIT)"
CHECKLIST_WATCH_CHARS = 160

OVERRIDES = (
    "Project overrides (they outrank the catalogue):\n"
    "- Apply it to English prose. Portuguese text (Luz Amanhã): only §1–§5 and §22; never translate or restyle it.\n"
    "- Keep numeric ranges and data exactly as the site prints them: `10–13h`, `4–10 Nov`, `540 → 563`, prices, "
    "percentages, version numbers. §8 is about dashes that join clauses, not ranges.\n"
    "- Quotes from release notes, file names, URLs, `?src=` links and the VISION tone section stay as they are.\n"
    "- The AI disclosure is appended by team-publish after review; it is not a tell and not part of the draft.\n"
    "- Facts first: a rewrite that drops or changes a number, date, name or quote is wrong even if it reads better."
)


def _read() -> str:
    try:
        return CATALOGUE_FILE.read_text(encoding="utf-8")
    except OSError:
        log.warning("style catalogue missing: %s", CATALOGUE_FILE)
        return ""


def _strip_front_matter(text: str) -> str:
    m = re.match(r"\A---\n.*?\n---\n", text, re.DOTALL)
    return text[m.end():] if m else text


def catalogue() -> str:
    body = _strip_front_matter(_read()).strip()
    if not body:
        return ""
    return f"Source: {SOURCE}.\n\n{OVERRIDES}\n\n{body}"


def checklist() -> str:
    """`§N name: watch for …` per pattern; patterns without a watch list get their first problem sentence."""
    body = _strip_front_matter(_read())
    if not body:
        return ""
    lines: list[str] = []
    for m in re.finditer(r"(?m)^### ([0-9]+)\. ([^\n]+)\n(.*?)(?=^### |^## |\Z)", body, re.DOTALL):
        num, name, sect = m.group(1), m.group(2).strip(), m.group(3)
        w = re.search(r"\*\*Watch for:\*\*\s*(.+)", sect)
        hint = w.group(1) if w else (re.search(r"\*\*(?:Problem|Rule):\*\*\s*(.+?\.)(?=\s|$)", sect) or [None, ""])[1]
        hint = " ".join(str(hint).split())
        if len(hint) > CHECKLIST_WATCH_CHARS:
            hint = hint[:CHECKLIST_WATCH_CHARS].rsplit(" ", 1)[0] + " …"
        lines.append(f"- §{num} {name}: {hint}" if hint else f"- §{num} {name}")
    if not lines:
        return ""
    return (f"Source: {SOURCE}. The editor checks every draft against the full catalogue; §1–§5 are sent back on one "
            "sighting.\n" + OVERRIDES + "\n" + "\n".join(lines))
