"""Editorial loop for the marketer: draft → editor → (one revision) → publish, with scores that accumulate.

Why: the owner reviewed the first Telegram post as dry and boring; a human will not be around for a week, so the
check moves into the loop. The `editor` role reads every draft before it is sent, scores it 1–5 on five axes, and
either approves (optionally with a light final edit), sends it back once with concrete issues, or rejects it.

What accumulates:
- ``data/editorial/scores.jsonl`` — one line per reviewed draft (product, task, channel, scores, verdict, round);
  the daily digest reports the rolling mean, and a falling mean becomes an observation for the PO;
- ``products/<slug>/editorial-lessons.md`` — lessons the editor (or the owner via `team lesson`) wants every next
  post to respect; both marketer and editor prompts carry it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Product, Settings

log = logging.getLogger("team.editorial")

AXES = ("hook", "clarity", "specificity", "tone", "honesty")
LESSONS_FILE = "editorial-lessons.md"
ROLLING_N = 10
DEGRADE_BELOW = 3.5
MAX_LESSONS_PER_REVIEW = 2
MAX_LESSONS = 40


def lessons_path(product: Product) -> Path:
    return product.dir / LESSONS_FILE


def read_lessons(product: Product) -> list[str]:
    p = lessons_path(product)
    if not p.exists():
        return []
    return [ln[2:].strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]


_LABEL_RE = re.compile(r"\bAI[- ](label|disclosure|generated|agents?)\b|\bdisclosure\b|\bdisclos|\bAI label|\blabel\b.*\b(AI|agents?)\b|\b(AI|agents?)\b.*\blabel\b|made by AI", re.I)


def is_label_lesson(text: str) -> bool:
    """team-publish appends the AI disclosure itself; a lesson about it only makes the marketer duplicate it (F-0121, F-0122)."""
    return bool(_LABEL_RE.search(text))


def add_lessons(product: Product, lessons: list[str], by: str) -> int:
    have = {x.lower() for x in read_lessons(product)}
    p = lessons_path(product)
    if not p.exists():
        p.write_text("# Editorial lessons (read by the marketer and the editor before every post)\n\n", encoding="utf-8")
    n = 0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with p.open("a", encoding="utf-8") as fh:
        for ls in lessons:
            t = " ".join(str(ls).split())[:300]
            if not t or is_label_lesson(t) or t.lower() in have or len(have) >= MAX_LESSONS:
                continue
            fh.write(f"- {t} _({by}, {day})_\n")
            have.add(t.lower())
            n += 1
    return n


def scores_path(st: Settings) -> Path:
    return st.data_dir / "editorial" / "scores.jsonl"


def record_score(st: Settings, rec: dict[str, Any]) -> None:
    p = scores_path(st)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **rec}, ensure_ascii=False) + "\n")


def load_scores(st: Settings, product: str | None = None) -> list[dict[str, Any]]:
    p = scores_path(st)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if product is None or r.get("product") == product:
            out.append(r)
    return out


def mean_score(scores: dict[str, Any]) -> float:
    vals = [float(scores.get(a) or 0) for a in AXES if scores.get(a) is not None]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def rolling(st: Settings, product: str, n: int = ROLLING_N) -> tuple[float | None, int, list[str]]:
    """(mean of the last n final-round reviews, count, most frequent issues)"""
    recs = [r for r in load_scores(st, product) if r.get("final")][-n:]
    if not recs:
        return None, 0, []
    issues: dict[str, int] = {}
    for r in recs:
        for i in r.get("issues") or []:
            issues[str(i)[:120]] = issues.get(str(i)[:120], 0) + 1
    top = [k for k, _ in sorted(issues.items(), key=lambda kv: -kv[1])[:3]]
    return round(sum(float(r.get("mean") or 0) for r in recs) / len(recs), 2), len(recs), top


def degrading(st: Settings, product: str) -> bool:
    m, n, _ = rolling(st, product)
    return m is not None and n >= 5 and m < DEGRADE_BELOW


SERIES_DEFAULTS: dict[str, Any] = {"title_prefixes": ["Pivots "], "editor_model": "opus", "marketer_model": "opus", "rounds": 3,
                                   "min_axis": 4, "min_mean": 4.3, "editor_cap": 1.5, "marketer_cap": 2.5,
                                   "rubric_file": "docs/notes/series-editorial-rubric.md"}


def series_mode(st: Settings, task: dict[str, Any]) -> dict[str, Any] | None:
    """Owner 04.11: the daily article series is edited very critically (Opus editor, written rubric, up to two returns,
    a numeric bar enforced in code). A task belongs to a series when its title starts with a configured prefix."""
    cfg = {**SERIES_DEFAULTS, **((st.raw.get("editorial") or {}).get("series") or {})}
    if cfg.get("enabled") is False:
        return None
    title = str(task.get("title") or "")
    if not any(title.startswith(str(px)) for px in cfg.get("title_prefixes") or []):
        return None
    rf = Path(str(cfg["rubric_file"]))
    rf = rf if rf.is_absolute() else st.root / rf
    cfg["rubric"] = rf.read_text(encoding="utf-8").strip() if rf.exists() else "(rubric file missing: apply the RULES below strictly)"
    return cfg


def enforce_series_bar(review: dict[str, Any], series: dict[str, Any], last_round: bool) -> dict[str, Any]:
    """An approve under the series bar (any axis < min_axis or mean < min_mean) is not an approve: it goes back while
    rounds remain, and is rejected in the last round. Nothing below the bar is published."""
    r = dict(review)
    if r.get("verdict") != "approve":
        if last_round and r.get("verdict") == "revise":
            r["verdict"] = "reject"
        return r
    sc = r.get("scores") or {}
    vals = [float(sc[a]) for a in AXES if isinstance(sc.get(a), (int, float))]
    low = [a for a in AXES if isinstance(sc.get(a), (int, float)) and float(sc[a]) < float(series["min_axis"])]
    if len(vals) == len(AXES) and not low and sum(vals) / len(vals) >= float(series["min_mean"]):
        return r
    why = f"series bar not met (every axis ≥ {series['min_axis']}, mean ≥ {series['min_mean']}): " + (
        f"low {', '.join(low)}" if low else f"mean {round(sum(vals) / len(vals), 2) if vals else 'n/a'}")
    r["verdict"] = "reject" if last_round else "revise"
    r["issues"] = list(r.get("issues") or []) + [why]
    r["final_text"] = None
    return r


def editor_prompt(product: Product, task: dict[str, Any], drafts: list[tuple[str, dict[str, Any]]], lessons: list[str],
                  recent_issues: list[str], round_no: int, previous_issues: list[str] | None = None,
                  series: dict[str, Any] | None = None) -> str:
    parts = [f"# EDITOR — product `{product.slug}` ({product.site_base}) — task {task.get('id')} — review round {round_no}",
             "You review the marketer's drafts BEFORE they are published. Score each on five axes 1–5 (hook, clarity, specificity, "
             "tone, honesty), then approve (optionally with a light final_text of your own), send back once with concrete issues "
             "(revise), or reject. Check facts against the live site with `team-fetch` when a draft states numbers or events.",
             "## TASK\n" + json.dumps({k: task.get(k) for k in ("id", "title", "problem", "scope", "acceptance")}, ensure_ascii=False, indent=1),
             "## VISION (tone section is binding)\n" + product.vision()[:9000],
             "## LESSONS (every draft must respect these)\n" + ("\n".join(f"- {x}" for x in lessons) if lessons else "(none yet)"),
             "## MOST FREQUENT ISSUES OF THE LAST REVIEWS\n" + ("\n".join(f"- {x}" for x in recent_issues) if recent_issues else "(none)")]
    if series:
        parts.append("## SERIES RUBRIC — this task is an article of the daily series; read every draft against it line by line\n"
                     + series["rubric"])
    if previous_issues:
        parts.append(f"## ISSUES YOU RAISED IN ROUND {round_no - 1} (check they are fixed)\n" + "\n".join(f"- {x}" for x in previous_issues))
    for name, d in drafts:
        parts.append(f"## DRAFT `{name}` — channel {d.get('channel')} → {d.get('target') or ''}\nurl: {d.get('url') or ''}\ntitle: {d.get('title') or ''}\n\n```\n{d.get('text', '')}\n```")
    if series:
        parts.append(f"## RULES (series mode)\n- You may send back (revise) in rounds 1–{int(series['rounds']) - 1}; in round {series['rounds']} approve "
                     "or reject — never revise.\n"
                     f"- Approve only if EVERY axis is ≥ {series['min_axis']} and the mean ≥ {series['min_mean']}; the dispatcher turns "
                     "anything lower into revise/reject anyway. A 5 is rare and means you would sign it yourself.\n"
                     "- Before scoring, re-fetch the source file(s) named in the task and check every number, date and quote against "
                     "them; one untraceable number is honesty ≤ 2.\n"
                     "- Read as a stranger who never heard of this team: name every place a word, abbreviation or reference "
                     "assumes inside knowledge.\n"
                     "- Channel formats are the ones in the task scope (Telegram up to 4000, Threads 500 per post, X 280 per post, "
                     "Bluesky 300 with the link, dev.to 900–1500 words), not the short-post limits.\n"
                     "- Issues must be concrete edits (quote the sentence, say what replaces it). final_text only for a light edit.\n"
                     "- lessons: at most 2 per review, phrased as a rule for the next article.")
        return "\n\n".join(parts)
    parts.append("## RULES\n- Round 1 may send back (revise) at most once; in round 2 approve with a final_text or reject — never revise again.\n"
                 "- approve only when the first sentence is a concrete event or fact, every number is traceable to the site, the tone "
                 "section of VISION is met, the channel length holds (Bluesky ≤ 250 chars before the tag, Telegram ≤ 600), and there is a hook.\n"
                 "- lessons: at most 2 per review, only for mistakes likely to repeat, phrased as a rule for the next post.\n"
                 "- Terse English; the marketer reads your issues as instructions.")
    return "\n\n".join(parts)
