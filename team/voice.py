"""Voice of the user: twice a week a role reads what real people say in public about the problem the product exists for
(GitHub issues, Reddit, HN, forums) and returns pains with quotes, links and dates plus hypotheses. The PO ranks and
starts hypotheses on this material (owner 07.11: "голос пользователя — часть фреймворка, нужен для любого продукта").
This module is the pure part — topics, prompt, report files, observation cards, the PO block; the run is in the cycle.

Configuration: `product.yaml: voice_topics` (a list of search topics) — missing → `DEFAULT_TOPICS[slug]`, else the
product name. `team.yaml: cycle.voice_every_h` (default 84, twice a week).
Files: `products/<slug>/voice/<YYYY-MM-DD>.json` (the report) and `.md` (summary + pains, for people).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("team.voice")

ROLE = "voice"
SCHEMA = "voice-report"
EVERY_H_DEFAULT = 84               # twice a week (owner 08.11; was weekly)
RETRY_H = 24                       # a run that failed or is still going is not started again for this long
ATTEMPT_FILE = "last-attempt"      # products/<slug>/voice/last-attempt: ISO time of the last start
CYCLE_FILE = "voice-cycle"         # data/voice-cycle: the cycle id that already ran one voice run (one product per cycle)
MAX_TOPICS = 8
OBS_TITLE_MAX = 140
OBS_ACTUAL_MAX = 600
PREV_SUMMARY_MAX = 1500
BLOCK_MAX_PAINS = 10
CLASSIFICATION = "voice"           # PO weighting: after `feedback` (words about us), before facts on the live site

DEFAULT_TOPICS: dict[str, list[str]] = {
    "agentsmd-tracker": [
        "how to write AGENTS.md / CLAUDE.md / copilot-instructions for a repository",
        "maintainers deciding whether to add agent instruction files",
        "AI coding agents ignoring or misreading repo instructions",
    ],
    "release-radar": [
        "a dependency update broke my build (npm, PyPI)",
        "how people track breaking changes in their dependencies",
        "release notes hard to read or missing for popular packages",
    ],
    "luz-amanha": [
        "preço da eletricidade por hora em Portugal, tarifa bi-horária / indexada",
        "quando carregar o carro elétrico mais barato em Portugal",
        "dynamic electricity tariff Portugal OMIE hourly prices",
    ],
    "agent-team": [
        "running a team of AI coding agents unattended for days",
        "autonomous agents breaking their own code, self-improving agent loops",
        "keeping AI agents within a budget and getting human-readable reports from them",
    ],
    "agent-team-diary": [
        "people building or watching autonomous AI agent teams in production",
        "what fails when AI agents run a product without humans",
    ],
}


def topics_for(product_raw: dict[str, Any]) -> list[str]:
    raw = product_raw.get("voice_topics")
    ts = [str(t).strip() for t in raw if str(t).strip()] if isinstance(raw, list) else []
    if not ts:
        ts = list(DEFAULT_TOPICS.get(str(product_raw.get("slug") or ""), []))
    if not ts:
        ts = [str(product_raw.get("name") or product_raw.get("slug") or "")]
        log.warning("voice: no voice_topics for %s, searching by the product name", product_raw.get("slug"))
    return ts[:MAX_TOPICS]


def voice_dir(product_dir: Path) -> Path:
    return Path(product_dir) / "voice"


def reports(product_dir: Path) -> list[Path]:
    return sorted(voice_dir(product_dir).glob("????-??-??.json"))


def latest(product_dir: Path) -> dict[str, Any] | None:
    files = reports(product_dir)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("voice: cannot read %s: %s", files[-1], e)
        return None


def mark_attempt(product_dir: Path, now: datetime) -> None:
    voice_dir(product_dir).mkdir(parents=True, exist_ok=True)
    (voice_dir(product_dir) / ATTEMPT_FILE).write_text(now.isoformat(timespec="seconds"), encoding="utf-8")


def _recent_attempt(product_dir: Path, now: datetime) -> bool:
    f = voice_dir(product_dir) / ATTEMPT_FILE
    try:
        prev = datetime.fromisoformat(f.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return now - prev < timedelta(hours=RETRY_H)


def claim_cycle(data_dir: Path, cycle_id: str) -> bool:
    """One voice run per dispatcher cycle across all products, so the runs spread over hours instead of
    stretching one hourly cycle past the next. True when this cycle had none yet (and now has one)."""
    f = Path(data_dir) / CYCLE_FILE
    try:
        if f.read_text(encoding="utf-8").strip() == cycle_id:
            return False
    except OSError:
        pass
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(cycle_id, encoding="utf-8")
    return True


def due(product_dir: Path, every_h: int, now: datetime | None = None) -> bool:
    if every_h <= 0:
        return False
    now = now or datetime.now(timezone.utc)
    if _recent_attempt(product_dir, now):
        return False
    rep = latest(product_dir)
    if not rep:
        return True
    try:
        prev = datetime.fromisoformat(str(rep.get("generated")))
    except ValueError:
        return True
    return now - prev >= timedelta(hours=every_h)


def build_prompt(product_name: str, site_base: str, vision_md: str, topics: list[str], previous: dict[str, Any] | None) -> str:
    prev = (str(previous.get("summary_md") or "")[:PREV_SUMMARY_MAX] + f"\n(report of {previous.get('day')})") if previous else "(first report)"
    return (f"# VOICE OF THE USER — product `{product_name}` ({site_base})\n\n"
            "# VISION (audience, promises, goals)\n" + (vision_md.strip() or "(not given)")
            + "\n\n# TOPICS TO SEARCH (the problem, in the audience's words)\n" + "\n".join(f"- {t}" for t in topics)
            + "\n\n# YOUR PREVIOUS REPORT (look for what is new)\n" + prev + "\n")


def evidence_id(day: str, pain_id: str) -> str:
    """How a task cites a pain: `voice:<day>:<pain id>` (the hypothesis rule accepts it as real evidence)."""
    return f"voice:{day}:{pain_id}"


def fingerprint(pain: str) -> str:
    return hashlib.sha1(f"voice|{' '.join(pain.lower().split())[:120]}".encode()).hexdigest()[:12]


def save(product_dir: Path, result: dict[str, Any], day: str, generated: str) -> Path:
    d = voice_dir(product_dir)
    d.mkdir(parents=True, exist_ok=True)
    rep = {**result, "day": day, "generated": generated}
    path = d / f"{day}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# Голос пользователя {day}", "", str(result.get("summary_md") or ""), "", "## Pains", ""]
    for p in result.get("pains") or []:
        lines.append(f"- **{evidence_id(day, str(p.get('id')))}** ({p.get('frequency')}) {p.get('who')}: {p.get('pain')}")
        for q in p.get("quotes") or []:
            lines.append(f"  - «{q.get('quote')}» — {q.get('url')} ({q.get('date')})")
        lines.append(f"  - today: {p.get('workarounds')} · our fit: {p.get('fit')}")
    lines += ["", "## Hypotheses", ""]
    for h in result.get("hypotheses") or []:
        lines.append(f"- **{h.get('id')}** ({', '.join(h.get('pain_ids') or [])}): {h.get('change')} → {h.get('expected_effect')} · metric: {h.get('metric')}")
    (d / f"{day}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("voice: report %s saved, %d pains, %d hypotheses", path, len(result.get("pains") or []), len(result.get("hypotheses") or []))
    return path


def to_observations(result: dict[str, Any], product_slug: str, day: str) -> list[dict[str, Any]]:
    """One card per pain that has at least one quote with a URL; the PO answers each like any observation."""
    hyps = result.get("hypotheses") or []
    cards: list[dict[str, Any]] = []
    for p in result.get("pains") or []:
        quotes = [q for q in (p.get("quotes") or []) if str(q.get("url", "")).startswith("http")]
        if not quotes or not str(p.get("pain", "")).strip():
            continue
        pid = str(p.get("id"))
        hs = [h for h in hyps if pid in (h.get("pain_ids") or [])]
        q0 = quotes[0]
        actual = (f"{p.get('who')} ({p.get('frequency')}): «{q0.get('quote')}» ({q0.get('date')}) · today: {p.get('workarounds')}"
                  f" · our fit: {p.get('fit')}" + (f" · hypotheses: " + "; ".join(f"{h.get('id')} {h.get('change')}" for h in hs) if hs else ""))
        cards.append({"title": f"Voice: {p.get('pain')}"[:OBS_TITLE_MAX], "classification": CLASSIFICATION, "source": ROLE,
                      "product": product_slug, "url": str(q0.get("url")), "evidence": [str(q.get("url")) for q in quotes],
                      "evidence_id": evidence_id(day, pid), "actual": actual[:OBS_ACTUAL_MAX], "expected": "",
                      "fingerprint": fingerprint(str(p.get("pain")))})
    return cards


def prompt_block(product_dir: Path) -> str:
    """The latest report for the PO: pains with their evidence ids, quotes and links, and the role's hypotheses."""
    rep = latest(product_dir)
    if not rep:
        return "(no voice-of-user report yet: features wait in `proposed` until one gives them real evidence)"
    day = str(rep.get("day"))
    out = [f"Report {day} (generated {rep.get('generated')}). Cite a pain in a task's hypothesis as `{evidence_id(day, 'P1')}`."]
    for p in (rep.get("pains") or [])[:BLOCK_MAX_PAINS]:
        q = (p.get("quotes") or [{}])[0]
        out.append(f"- {evidence_id(day, str(p.get('id')))} [{p.get('frequency')}] {p.get('who')}: {p.get('pain')} — «{q.get('quote')}» {q.get('url')}"
                   f" · today: {p.get('workarounds')} · fit: {p.get('fit')}")
    hyps = rep.get("hypotheses") or []
    if hyps:
        out.append("Hypotheses proposed by the researcher (unranked):")
        out += [f"- {h.get('id')} ({', '.join(h.get('pain_ids') or [])}): {h.get('change')} → {h.get('expected_effect')} · metric: {h.get('metric')}" for h in hyps]
    return "\n".join(out)


def known_evidence_ids(product_dir: Path) -> set[str]:
    ids: set[str] = set()
    for f in reports(product_dir):
        try:
            rep = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ids |= {evidence_id(str(rep.get("day")), str(p.get("id"))) for p in rep.get("pains") or []}
    return ids
