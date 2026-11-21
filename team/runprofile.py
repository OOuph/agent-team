"""Run profile: where the time of one role run went (owner 21.11, frictions F23 and F30).

On the first game product a developer run hit the hard limit six times in one day. A manual look into one of them
showed 1241 of the 1252 seconds inside tool calls were 20 Playwright invocations on one spec, and git took nothing —
but nobody saw that until a person read events.jsonl by hand. This module does that reading for every run, with no
model call:

* ``build(events, ...)`` — wall time, time inside tool calls, a breakdown by category (calls and seconds), the longest
  calls (secrets redacted) and simple waste flags. Durations are the time between the stream-json event that carries a
  ``tool_use`` block and the one that carries its ``tool_result``; the runner stamps the receive time on every event
  (``_ts``), because the CLI's events carry no usable timestamp.
* ``line`` / ``render`` — one line for the PO prompt and events, a few lines for ``team task show`` and questions.
* ``po_block`` — the lines of long or killed runs the PO has not seen yet.
* ``task_runs`` / ``task_runs_md`` — the runs of one task from ``data/runs/<slug>/<cycle>/<label>/run.json``.
* ``period_summary`` — per role totals for the role report (``team report``).

Settings: ``run_profile`` in team.yaml, overridable by ``run_profile`` in product.yaml (see DEFAULTS below).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import friction, gitops
from .runner import EVENT_TS_KEY, KILLED_INACTIVITY, KILLED_TIMEOUT, _blocks

log = logging.getLogger("team.runprofile")

# ---------------------------------------------------------------------------------------------------- parameters

# start of a command inside a compound shell line: `git …`, `cd repo && git …`, `a; git …`, `(git …`
_CMD_START = r"(?:^|[;&|(]\s*)\s*"
_PKG = r"(?:npm|pnpm|yarn|bun)"

# First match wins, top to bottom: the expensive kinds stand first, so `npm run build && npx playwright test` is e2e.
# `tools` — tool names the rule applies to (absent = any tool); `pattern` — a regex searched in the call text (the Bash
# command, a file path, a search pattern; absent = any text). Several rules may share one `name`.
DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {"name": "install", "tools": ["Bash"],
     "pattern": rf"\b{_PKG}\s+(?:ci|install|i|add)\b|\bnpx\s+playwright\s+install\b|\bpip3?\s+install\b|\buv\s+(?:sync|pip)\b"
                r"|\bapt(?:-get)?\s+install\b|\bpoetry\s+install\b|\bgo\s+mod\s+download\b|\bcargo\s+fetch\b"},
    {"name": "e2e", "tools": ["Bash"],
     "pattern": rf"\bplaywright\s+test\b|\b(?:npx|exec)\s+playwright\b|\bcypress\s+(?:run|open)\b|\bteam-play\b|\b{_PKG}\s+(?:run\s+)?[\w:.-]*e2e[\w:.-]*"},
    {"name": "unit_tests", "tools": ["Bash"],
     "pattern": rf"\bpytest\b|\bunittest\b|\bvitest\b|\bjest\b|\bmocha\b|\b{_PKG}\s+(?:run\s+)?test\b|\bnode\s+--test\b|\bgo\s+test\b|\bcargo\s+test\b|\btox\b"},
    {"name": "build", "tools": ["Bash"],
     "pattern": rf"\b{_PKG}\s+(?:run\s+)?build\b|\btsc\b|\bvite\s+build\b|\bwebpack\b|\besbuild\b|\bmake\b|\bcargo\s+build\b|\bgo\s+build\b"
                r"|\bbuild\.(?:py|sh)\b|\bdocker\s+build\b"},
    {"name": "git", "tools": ["Bash"], "pattern": _CMD_START + r"git\s"},
    {"name": "read_search", "tools": ["Read", "Glob", "Grep", "LS"]},
    {"name": "read_search", "tools": ["Bash"], "pattern": _CMD_START + r"(?:cat|ls|head|tail|grep|rg|find|wc|tree|stat|file|sed\s+-n)\b"},
    {"name": "edit", "tools": ["Edit", "Write", "MultiEdit", "NotebookEdit"]},
    {"name": "shell", "tools": ["Bash"]},            # any other shell command
    {"name": "other"},                               # any other tool
]

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "event_min_s": 300,          # a run at least this long (or killed by a limit) gets the compact `run.profile` event
    "top_calls": 5,              # how many of the longest calls are kept
    "call_chars": 160,           # characters of a call kept in the profile (after redaction)
    "repeat_threshold": 3,       # waste flag: the same normalised command MORE than this many times
    "repeat_tools": ["Bash"],    # tools whose calls are compared for repeats
    "repeat_flags_max": 3,       # at most this many repeat flags per run (the most repeated first)
    "dominant_share": 0.6,       # waste flag: one category above this share of the time inside tool calls …
    "dominant_min_s": 120,       # … and at least this many seconds (a short run is never flagged)
    "max_full_e2e": 1,           # waste flag: more than this many FULL e2e runs (no spec file, no grep filter)
    "e2e_category": "e2e",
    # an e2e call that names a spec file, a line, a title filter or only lists/installs is not a full run
    "narrow_e2e_pattern": r"\.spec\.|\.test\.|\.cy\.|\s(?:-g|--grep|-k|--last-failed|--list)\b|:\d+\b|\binstall\b|\bshow-report\b",
    "po_max_lines": 8,           # lines of the `# RUN TIME` block in the PO prompt
    "report_top_categories": 3,  # categories per role in the report table
    "extra_categories": [],      # rules put IN FRONT of the list below (same shape), e.g. a product's own e2e command
    "categories": DEFAULT_CATEGORIES,   # a list here REPLACES the defaults
}

IGNORED_TOOLS = ("StructuredOutput",)          # the answer itself, not work
TS_KEYS = (EVENT_TS_KEY, "timestamp", "ts")    # event keys tried for a time, in order (epoch seconds/ms or ISO 8601)
TASK_ID_RE = re.compile(r"(?<![A-Za-z0-9])T-\d{3,}(?!\d)")      # a task id inside a run id / label (`…-dev-T-0042`)
CYCLE_STAMP_RE = re.compile(r"^(\d{8}T\d{4})")            # a cycle directory name starts with its start time
KILLED_TEXT = {KILLED_TIMEOUT: "killed by the time limit", KILLED_INACTIVITY: "killed by the inactivity watchdog"}
# what is cut from a shell command before two calls are compared: where it runs and how its output is trimmed
NORMALISE_DROP = (r"^\s*cd\s+\S+\s*(?:&&|;)\s*", r"\s*\d?>&\d", r"\s*\d?>\s*/dev/null", r"\s*\|\s*(?:tail|head|grep|tee|cat|less)\b[^|;&]*")
NORMALISE_CHARS = 200
# on top of the repository's own redaction (friction.redact, gitops.redact): NAME=value for names that look secret
ENV_SECRET_RE = re.compile(r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY)[A-Z0-9_]*=)(\"[^\"]*\"|'[^']*'|\S+)")
RUN_EVENT = "run.profile"


def settings(team_raw: dict[str, Any] | None = None, product_raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """DEFAULTS ← team.yaml `run_profile` ← product.yaml `run_profile`."""
    out = dict(DEFAULTS)
    for layer in (team_raw, product_raw):
        if isinstance(layer, dict):
            out.update({k: v for k, v in layer.items() if v is not None})
    return out


def settings_for(product: Any) -> dict[str, Any]:
    """Settings of a `config.Product` (its team.yaml and its own product.yaml)."""
    return settings((product.st.raw.get("run_profile") if product is not None else None),
                    (product.raw.get("run_profile") if product is not None else None))


def redact(s: Any) -> str:
    """Secrets out of a call text: the register's patterns (tokens, bearer headers), the git url token, NAME=secret."""
    return ENV_SECRET_RE.sub(r"\1***", gitops.redact(friction.redact(str(s))))


# ---------------------------------------------------------------------------------------------------- building

def _event_ts(e: dict[str, Any]) -> float | None:
    for k in TS_KEYS:
        v = e.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            return float(v) / 1000.0 if v > 1e11 else float(v)        # milliseconds → seconds
        if isinstance(v, str) and v.strip():
            try:
                d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _call_text(tool: str, inp: Any) -> str:
    """What the call did, in one line: the shell command, the file, the search pattern."""
    if not isinstance(inp, dict):
        return " ".join(str(inp or "").split())
    if tool == "Bash" and inp.get("command"):
        return " ".join(str(inp["command"]).split())
    parts = [str(inp[k]) for k in ("file_path", "path", "pattern", "url", "command") if inp.get(k)]
    return " ".join(" ".join(parts).split()) if parts else " ".join(json.dumps(inp, ensure_ascii=False).split())


def _compile(cfg: dict[str, Any]) -> list[tuple[str, frozenset[str] | None, re.Pattern[str] | None]]:
    rules = []
    for c in list(cfg.get("extra_categories") or []) + list(cfg.get("categories") or DEFAULT_CATEGORIES):
        if not isinstance(c, dict) or not c.get("name"):
            log.warning("run_profile: category rule %r has no name — skipped", c)
            continue
        try:
            rx = re.compile(str(c["pattern"])) if c.get("pattern") else None
        except re.error as exc:
            log.warning("run_profile: category %s has a bad pattern (%s) — rule skipped", c.get("name"), exc)
            continue
        rules.append((str(c["name"]), frozenset(str(t) for t in c["tools"]) if c.get("tools") else None, rx))
    return rules


def categorise(rules: list[tuple[str, frozenset[str] | None, re.Pattern[str] | None]], tool: str, text: str) -> str:
    for name, tools, rx in rules:
        if tools is not None and tool not in tools:
            continue
        if rx is not None and not rx.search(text):
            continue
        return name
    return "other"


def normalise(text: str) -> str:
    """A shell command without its `cd … &&` prefix and output trimming, so re-runs of one command compare equal."""
    s = " ".join(str(text).split())
    for rx in NORMALISE_DROP:
        s = re.sub(rx, "", s)
    return s.strip()[:NORMALISE_CHARS]


def _union_s(spans: list[tuple[float, float]]) -> float:
    """Total length of the union of [start, end] spans: parallel tool calls are not counted twice."""
    total, cur_a, cur_b = 0.0, None, None
    for a, b in sorted(spans):
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


def build(events: list[dict[str, Any]], *, wall_s: float | None = None, started: float | None = None, killed: str | None = None,
          limit_s: int | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The profile of one run from its stream-json events.

    `wall_s` / `started` — the run's duration and start (epoch seconds) when the caller knows them; without them the
    first and last event times are used. `killed` — "timeout" / "inactivity" when the dispatcher stopped the run: a
    call still without a result then lasted until the kill. `limit_s` — the hard limit the run had (kept for the text).
    Events without any time give counts only (`timed: false`)."""
    cfg = cfg if cfg is not None else settings()
    rules = _compile(cfg)
    calls: dict[str, dict[str, Any]] = {}
    order: list[dict[str, Any]] = []
    first_ts: float | None = None
    last_ts: float | None = None
    for e in events or []:
        if not isinstance(e, dict):
            continue
        ts = _event_ts(e)
        if ts is not None:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        kind = e.get("type")
        if kind == "assistant":
            for b in _blocks(e.get("message")):
                if b.get("type") != "tool_use" or str(b.get("name")) in IGNORED_TOOLS:
                    continue
                c = {"tool": str(b.get("name") or "?"), "text": _call_text(str(b.get("name") or ""), b.get("input")), "t0": ts, "t1": None}
                calls[str(b.get("id"))] = c
                order.append(c)
        elif kind == "user":
            for b in _blocks(e.get("message")):
                if b.get("type") != "tool_result":
                    continue
                c = calls.get(str(b.get("tool_use_id")))
                if c is not None and not c.get("done"):
                    c["t1"], c["done"] = ts, True
    end_ts = (started + wall_s) if started and wall_s else last_ts
    if wall_s is None:
        wall_s = (last_ts - first_ts) if first_ts is not None and last_ts is not None else 0.0
    timed = any(c["t0"] is not None for c in order)
    spans: list[tuple[float, float]] = []
    cats: dict[str, dict[str, Any]] = {}
    for c in order:
        c["category"] = categorise(rules, c["tool"], c["text"])
        if c["t0"] is None:
            c["s"] = 0.0
        elif c.get("done") and c["t1"] is not None:
            c["s"] = max(0.0, c["t1"] - c["t0"])
        elif killed and end_ts is not None:
            c["s"], c["unfinished"] = max(0.0, end_ts - c["t0"]), True      # the call the kill interrupted
        else:
            c["s"] = 0.0
        if c["t0"] is not None and c["s"] > 0:
            spans.append((c["t0"], c["t0"] + c["s"]))
        agg = cats.setdefault(c["category"], {"name": c["category"], "calls": 0, "s": 0.0})
        agg["calls"] += 1
        agg["s"] += c["s"]
    sum_s = sum(c["s"] for c in cats.values())
    tool_s = min(_union_s(spans), float(wall_s)) if wall_s else _union_s(spans)
    cat_list = sorted(cats.values(), key=lambda c: (-c["s"], -c["calls"], c["name"]))
    for c in cat_list:
        c["share"] = round(c["s"] / sum_s, 3) if sum_s > 0 else 0.0
        c["s"] = round(c["s"], 1)
    n_chars = int(cfg.get("call_chars", 160))
    longest = [{"s": round(c["s"], 1), "category": c["category"], "tool": c["tool"], "call": redact(c["text"])[:n_chars],
                **({"unfinished": True} if c.get("unfinished") else {})}
               for c in sorted((c for c in order if c["s"] > 0), key=lambda c: -c["s"])[:int(cfg.get("top_calls", 5))]]
    prof = {"wall_s": round(float(wall_s or 0.0), 1), "tool_s": round(tool_s, 1), "model_s": round(max(0.0, float(wall_s or 0.0) - tool_s), 1),
            "calls": len(order), "timed": timed, "killed": killed, "limit_s": limit_s, "categories": cat_list, "longest": longest,
            "flags": _flags(order, cat_list, timed, cfg)}
    log.debug("run profile: wall %.0fs, tools %.0fs, %d calls, categories %s, flags %s", prof["wall_s"], prof["tool_s"], prof["calls"],
              [(c["name"], c["calls"], c["s"]) for c in cat_list], [f["kind"] for f in prof["flags"]])
    return prof


def _flags(order: list[dict[str, Any]], cat_list: list[dict[str, Any]], timed: bool, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    n_chars = int(cfg.get("call_chars", 160))
    # the same command again and again
    rep: dict[str, dict[str, Any]] = {}
    tools = set(cfg.get("repeat_tools") or [])
    for c in order:
        if c["tool"] not in tools:
            continue
        r = rep.setdefault(normalise(c["text"]), {"n": 0, "s": 0.0})
        r["n"] += 1
        r["s"] += c["s"]
    limit = int(cfg.get("repeat_threshold", 3))
    for key, r in sorted(rep.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["s"]))[:int(cfg.get("repeat_flags_max", 3))]:
        if key and r["n"] > limit:
            flags.append({"kind": "repeat", "count": r["n"], "s": round(r["s"], 1),
                          "text": f"same command {r['n']}× ({r['s']:.0f} s): `{redact(key)[:n_chars]}`"})
    # one category ate the run
    if timed and cat_list:
        top = cat_list[0]
        if top["share"] > float(cfg.get("dominant_share", 0.6)) and top["s"] >= float(cfg.get("dominant_min_s", 120)):
            flags.append({"kind": "dominant", "category": top["name"], "share": top["share"],
                          "text": f"{top['name']} took {_pct(top['share'])} of the time inside tool calls ({top['s']:.0f} s in {top['calls']} calls)"})
    # the whole e2e suite more than once
    e2e = str(cfg.get("e2e_category") or "e2e")
    try:
        narrow = re.compile(str(cfg.get("narrow_e2e_pattern") or r"$^"))
    except re.error as exc:
        log.warning("run_profile: bad narrow_e2e_pattern (%s) — full e2e runs not counted", exc)
        narrow = None
    if narrow is not None:
        full = [c for c in order if c["category"] == e2e and not narrow.search(c["text"])]
        if len(full) > int(cfg.get("max_full_e2e", 1)):
            flags.append({"kind": "full_e2e", "count": len(full), "s": round(sum(c["s"] for c in full), 1),
                          "text": f"{len(full)} full e2e runs ({sum(c['s'] for c in full):.0f} s)"})
    return flags


# ---------------------------------------------------------------------------------------------------- text

def _pct(share: float) -> str:
    return f"{round(100 * float(share or 0))} %"


def killed_text(prof: dict[str, Any]) -> str:
    k = prof.get("killed")
    if not k:
        return ""
    return KILLED_TEXT.get(str(k), f"killed ({k})") + (f" ({prof['limit_s']} s)" if prof.get("limit_s") and k == KILLED_TIMEOUT else "")


def _cat_text(c: dict[str, Any], timed: bool) -> str:
    return f"{c['name']} {_pct(c['share'])} in {c['calls']} calls" if timed and c.get("s") else f"{c['name']} {c['calls']} calls"


def line(prof: dict[str, Any], *, role: str = "", task: str = "", run_id: str = "", top: int = 2) -> str:
    """One line: «developer T-0042: 1500 s (tools 1252 s), e2e 99 % in 20 calls, killed by the time limit (1500 s)»."""
    who = " ".join(x for x in (role, task or run_id) if x)
    parts = [f"{prof.get('wall_s', 0):.0f} s" + (f" (tools {prof.get('tool_s', 0):.0f} s)" if prof.get("timed") else "")]
    parts += [_cat_text(c, bool(prof.get("timed"))) for c in (prof.get("categories") or [])[:top]]
    if prof.get("killed"):
        parts.append(killed_text(prof))
    out = (f"{who}: " if who else "") + ", ".join(parts)
    flags = [f["text"] for f in prof.get("flags") or [] if f.get("kind") != "dominant"]      # the dominant share is already in the line
    return out + (f"; flags: {'; '.join(flags)}" if flags else "")


def render(prof: dict[str, Any], header: str = "") -> str:
    """A few lines: totals, every category, the longest calls, the flags."""
    timed = bool(prof.get("timed"))
    head = (f"{header}: " if header else "") + f"{prof.get('wall_s', 0):.0f} s wall"
    if timed:
        head += f", {prof.get('tool_s', 0):.0f} s inside tool calls, {prof.get('model_s', 0):.0f} s model"
    head += f", {prof.get('calls', 0)} calls" + (f" — {killed_text(prof)}" if prof.get("killed") else "")
    out = [head]
    cats = prof.get("categories") or []
    if cats:
        out.append("  by category: " + " · ".join(
            (f"{c['name']} {c['s']:.0f} s ({_pct(c['share'])}) in {c['calls']} calls" if timed else f"{c['name']} {c['calls']} calls") for c in cats))
    for c in prof.get("longest") or []:
        out.append(f"  {c['s']:.0f} s {c['category']}{' (cut off by the kill)' if c.get('unfinished') else ''}: `{c['call']}`")
    for f in prof.get("flags") or []:
        out.append(f"  flag: {f['text']}")
    if not timed and prof.get("calls"):
        out.append("  (no event times in this run: counts only)")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------- events, PO prompt

def task_of(run_id: str) -> str:
    m = TASK_ID_RE.search(str(run_id or ""))
    return m.group(0) if m else ""


def worth_event(prof: dict[str, Any], cfg: dict[str, Any]) -> bool:
    return bool(prof.get("killed")) or float(prof.get("wall_s") or 0) >= float(cfg.get("event_min_s", 300))


def event_detail(prof: dict[str, Any], *, role: str, run_id: str) -> dict[str, Any]:
    """The compact `run.profile` event: the ready line plus the numbers behind it."""
    task = task_of(run_id)
    return {"task": task or None, "wall_s": prof.get("wall_s"), "tool_s": prof.get("tool_s"), "killed": prof.get("killed"),
            "limit_s": prof.get("limit_s"), "top": [[c["name"], c["s"], c["calls"]] for c in (prof.get("categories") or [])[:3]],
            "flags": [f["kind"] for f in prof.get("flags") or []], "line": line(prof, role=role, task=task, run_id=run_id)}


def po_lines(store: Any, cfg: dict[str, Any]) -> list[str]:
    """Lines of the `run.profile` events written after the PO's last run (what it has not seen yet), oldest first."""
    row = store.db.execute("SELECT id FROM events WHERE kind='run' AND actor='po' ORDER BY id DESC LIMIT 1").fetchone()
    after = int(row[0]) if row else 0
    rows = store.db.execute("SELECT detail FROM events WHERE kind=? AND id > ? ORDER BY id DESC LIMIT ?",
                            (RUN_EVENT, after, int(cfg.get("po_max_lines", 8)))).fetchall()
    out = []
    for (detail,) in reversed(rows):
        try:
            d = json.loads(detail) if detail else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(d, dict) and d.get("line"):
            out.append(str(d["line"]))
    return out


def po_block(store: Any, product: Any) -> str:
    """`# RUN TIME` for the PO prompt, or "" when no run was long or killed since its last triage."""
    cfg = settings_for(product)
    if not cfg.get("enabled", True):
        return ""
    try:
        lines = po_lines(store, cfg)
    except Exception as exc:  # noqa: BLE001 — a broken profile must never cost the PO its prompt
        log.warning("%s: run profile lines for the PO failed: %s", getattr(product, "slug", "?"), exc)
        return ""
    if not lines:
        return ""
    n = getattr(product, "max_continuations", 3)
    return ("# RUN TIME (runs since your last triage that took over "
            f"{int(cfg.get('event_min_s', 300))} s or were stopped by a limit; % = share of the time inside tool calls)\n"
            + "\n".join(f"- {ln}" for ln in lines)
            + f"\nA run stopped by the time limit is continued on the same branch (up to {n} times per task), then the task is blocked "
              "and you are asked to split it. A task whose runs keep hitting the limit is too big for one run: split it or cut its "
              "scope before that happens; a flag names what ate the time (the same command again and again, the full e2e suite "
              "run several times) — say it in the task's scope.")


# ---------------------------------------------------------------------------------------------------- run.json readers

def _read_run(path: Path) -> dict[str, Any] | None:
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.debug("unreadable %s: %s", path, exc)
        return None
    return r if isinstance(r, dict) else None


def task_runs(product: Any, task_id: str) -> list[dict[str, Any]]:
    """run.json of every run whose label names the task (developer, designer, reviewer, tester …), oldest first."""
    rx = re.compile(rf"(?<![A-Za-z0-9]){re.escape(task_id)}(?!\d)")
    out = []
    for rj in sorted(product.runs_dir.glob(f"*/*{task_id}*/run.json")):
        if not rx.search(rj.parent.name):
            continue
        r = _read_run(rj)
        if r is not None:
            out.append(r)
    out.sort(key=lambda r: str(r.get("finished") or ""))
    return out


def run_header(r: dict[str, Any]) -> str:
    lim = (r.get("limits") or {}).get("timeout_s")
    return f"{r.get('run_id')} ({r.get('role')}" + (f", limit {lim} s" if lim else "") + ")"


def task_runs_md(product: Any, task_id: str) -> str:
    """`# RUN PROFILES` for `team task <slug> show --id T`: one profile per run of the task."""
    runs = task_runs(product, task_id)
    if not runs:
        return ""
    out = [f"# RUN PROFILES of {task_id} ({len(runs)} runs; % = share of the time inside tool calls)"]
    for r in runs:
        prof = r.get("profile")
        if isinstance(prof, dict):
            out.append(render(prof, run_header(r)))
        else:
            out.append(f"{run_header(r)}: {float(r.get('duration_s') or 0):.0f} s wall, no profile recorded"
                       + (f" — {str(r.get('error'))[:120]}" if r.get("error") else ""))
    return "\n".join(out)


def period_summary(product: Any, since: datetime, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Per role: runs, wall and tool seconds, seconds by category and the number of killed runs since `since`."""
    cfg = cfg if cfg is not None else settings_for(product)
    roles: dict[str, dict[str, Any]] = {}
    total = {"runs": 0, "wall_s": 0.0, "tool_s": 0.0, "killed": 0}
    since_iso = since.isoformat(timespec="seconds")
    for cdir in sorted(product.runs_dir.iterdir()) if product.runs_dir.exists() else []:
        m = CYCLE_STAMP_RE.match(cdir.name)
        if m:                                   # a cycle that started more than a day before the window cannot hold a run of it
            try:
                if (since - datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)).total_seconds() > 86400:
                    continue
            except ValueError:
                pass
        for rj in cdir.glob("*/run.json"):
            r = _read_run(rj)
            if r is None or str(r.get("finished") or "") < since_iso:
                continue
            prof = r.get("profile") if isinstance(r.get("profile"), dict) else {}
            agg = roles.setdefault(str(r.get("role") or "?"), {"runs": 0, "wall_s": 0.0, "tool_s": 0.0, "killed": 0, "categories": {}, "flags": {}})
            wall = float(prof.get("wall_s") or r.get("duration_s") or 0.0)
            agg["runs"] += 1
            agg["wall_s"] += wall
            agg["tool_s"] += float(prof.get("tool_s") or 0.0)
            agg["killed"] += 1 if r.get("killed") else 0
            for c in prof.get("categories") or []:
                agg["categories"][c["name"]] = agg["categories"].get(c["name"], 0.0) + float(c.get("s") or 0.0)
            for f in prof.get("flags") or []:
                agg["flags"][f["kind"]] = agg["flags"].get(f["kind"], 0) + 1
            total["runs"] += 1
            total["wall_s"] += wall
            total["tool_s"] += float(prof.get("tool_s") or 0.0)
            total["killed"] += 1 if r.get("killed") else 0
    top_n = int(cfg.get("report_top_categories", 3))
    for agg in roles.values():
        agg["wall_s"], agg["tool_s"] = round(agg["wall_s"], 1), round(agg["tool_s"], 1)
        agg["categories"] = [[k, round(v, 1)] for k, v in sorted(agg["categories"].items(), key=lambda kv: -kv[1]) if v > 0][:top_n]
    return {**{k: (round(v, 1) if isinstance(v, float) else v) for k, v in total.items()},
            "roles": dict(sorted(roles.items(), key=lambda kv: -kv[1]["wall_s"]))}
