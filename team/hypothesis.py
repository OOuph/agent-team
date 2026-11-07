"""The hypothesis rule (owner 07.11): a feature the PO starts carries a hypothesis — who, which pain, evidence from real
people, the change, the expected effect, the metric, how many people it takes to read it and when it is read. A feature
without real evidence waits in `proposed`; synthetic material (personas, the external critic, the team's own guesses)
alone does not make a feature Ready. Defects, content, marketing and analyst tasks are outside the rule, and so are
tasks the owner or the retro created (the retro's evidence is the friction register).

Evidence items, one string each:
- `voice:<day>:<pain id>` — a pain from a voice-of-user report (`team/voice.py`);
- `OBS-xxxx` — an observation of class `feedback` (words of real people about us) or `voice`;
- an `http(s)://` URL of a real person's words outside our own hosts (an issue, a thread, a review);
- `metric:<what>` — a number from our metrics with real people in it (counts only next to another item, never alone).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger("team.hypothesis")

RULE_TYPES = ("feature",)                       # task types the rule applies to
RULE_CREATORS = ("po",)                         # owner and retro tasks are outside the rule
REQUIRED = ("who", "pain", "evidence", "change", "expected_effect", "metric", "min_sample", "read_on")
SCORE_KEYS = ("reach", "pain", "confidence", "ease")
REAL_OBS_CLASSES = ("feedback", "voice")
READOUT_CLASS = "hypothesis_readout"
READ_STATES = ("accepted", "released", "measured", "done")
_OBS = re.compile(r"^OBS-\d{4}$")
_VOICE = re.compile(r"^voice:\d{4}-\d{2}-\d{2}:[A-Za-z0-9_-]+$")


def applies(task: dict[str, Any]) -> bool:
    return task.get("type") in RULE_TYPES and task.get("created_by", "po") in RULE_CREATORS


def score(h: dict[str, Any] | None) -> float | None:
    """reach × pain × confidence × ease, each 1–5, scaled to 0–100; None when the PO gave no complete score."""
    s = (h or {}).get("score") or {}
    try:
        vals = [int(s[k]) for k in SCORE_KEYS]
    except (KeyError, TypeError, ValueError):
        return None
    if any(not 1 <= v <= 5 for v in vals):
        return None
    prod = 1
    for v in vals:
        prod *= v
    return round(prod / 625 * 100, 1)


def _own_hosts(site_bases: list[str]) -> set[str]:
    return {re.sub(r"^https?://", "", b).split("/")[0].lower() for b in site_bases if b}


def real_evidence(items: list[str], *, voice_ids: set[str], observations: dict[str, dict[str, Any]], own_hosts: set[str]) -> list[str]:
    """The items that count as words of real people (metric items never count alone, so they are not returned)."""
    out = []
    for raw in items or []:
        e = str(raw).strip()
        if _VOICE.match(e) and e in voice_ids:
            out.append(e)
        elif _OBS.match(e) and (observations.get(e) or {}).get("classification") in REAL_OBS_CLASSES:
            out.append(e)
        elif e.startswith(("http://", "https://")):
            host = re.sub(r"^https?://", "", e).split("/")[0].lower()
            if host and not any(host == h or host.endswith("." + h) for h in own_hosts):
                out.append(e)
    return out


def check(task: dict[str, Any], *, voice_ids: set[str], observations: dict[str, dict[str, Any]], site_bases: list[str],
          today: date | None = None) -> list[str]:
    """Problems that keep the task out of Ready; [] when the rule is met or does not apply."""
    if not applies(task):
        return []
    h = task.get("hypothesis")
    if not isinstance(h, dict):
        return ["no hypothesis"]
    problems = [f"no {k}" for k in REQUIRED if h.get(k) in (None, "", [])]
    items = h.get("evidence") if isinstance(h.get("evidence"), list) else []
    if items and not real_evidence(items, voice_ids=voice_ids, observations=observations, own_hosts=_own_hosts(site_bases)):
        problems.append("no evidence from real people (voice:<day>:<pain>, a feedback/voice OBS or an outside URL); "
                        "personas, the critic and metrics alone do not count")
    if h.get("read_on"):
        try:
            d = date.fromisoformat(str(h["read_on"])[:10])
            if today and d <= today and task.get("state") in ("proposed", "ready"):
                problems.append(f"read_on {d} is not in the future")
        except ValueError:
            problems.append(f"read_on {h['read_on']!r} is not YYYY-MM-DD")
    try:
        if h.get("min_sample") not in (None, "") and int(h["min_sample"]) < 1:
            problems.append("min_sample < 1")
    except (TypeError, ValueError):
        problems.append("min_sample is not a number")
    return problems


def readouts_due(tasks: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Finished tasks whose hypothesis is due to be read today or earlier and was not handed to the PO yet."""
    out = []
    for t in tasks:
        h = t.get("hypothesis")
        if t.get("state") not in READ_STATES or not isinstance(h, dict) or t.get("hypothesis_readout"):
            continue
        try:
            if date.fromisoformat(str(h.get("read_on"))[:10]) <= today:
                out.append(t)
        except ValueError:
            continue
    return out


def readout_card(t: dict[str, Any], product_slug: str) -> dict[str, Any]:
    h = t["hypothesis"]
    return {"title": f"Read hypothesis of {t['id']}: {h.get('expected_effect')}"[:140], "classification": READOUT_CLASS,
            "source": "dispatcher", "product": product_slug, "url": "", "task": t["id"],
            "actual": (f"{t['id']} {t.get('title', '')[:80]} — change: {h.get('change')}; expected: {h.get('expected_effect')}; "
                       f"metric: {h.get('metric')}; needs ≥ {h.get('min_sample')} people; due {h.get('read_on')}")[:600],
            "expected": "PO: read the metric in LATEST METRICS; decide confirmed / refuted / not enough people (extend once with a new date)",
            "fingerprint": f"readout-{t['id']}"}


def brief(h: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(h, dict):
        return None
    b = {k: h.get(k) for k in ("who", "pain", "evidence", "expected_effect", "metric", "min_sample", "read_on")}
    s = score(h)
    if s is not None:
        b["score"] = s
    return b


def product_voice_ids(product_dir: Path) -> set[str]:
    from . import voice
    return voice.known_evidence_ids(product_dir)
