"""The canary's report and the guard of the code that judges a framework change (rework slice 7, owner 26.11, points 2
and 3 of the guide review: «пустой отчёт пробного прогона не должен означать „всё хорошо“»; «новые тесты сливаются
сами; изменение и удаление существующих тестов и кода пробного прогона ждёт владельца»).

The canary is a dry-run cycle of one product on the CANDIDATE code (`Cycle.canary`). Its verdict used to live in two
return values of that call. Here it becomes a file the running dispatcher — not the candidate — writes, and the merge
reads it back and refuses whatever it cannot stand on: no report, a report that does not parse, a report of another
revision, a report older than this run, a report that says «failed» or names no result.

`protected_functions_changed` finds a change of named functions between the base branch and the candidate by comparing
their source text (Python `ast`): the canary and the merge gate live inside team/cycle.py, the file every dispatcher
change touches, so a path cannot protect them."""
from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("team.canary")

# ------------------------------------------------------------------ parameters
REPORT_VERSION = 1
REPORT_DIR = "framework/canary-reports"    # under the dispatcher's data directory: one <cycle>.json per canary run
REPORT_KEEP = 200                          # newest reports kept
REQUIRED = ("version", "cycle", "product", "sha", "started", "finished", "ok", "note", "artefacts")
# functions whose change waits for the owner (team.yaml `framework.protected_functions` replaces the list): the canary
# itself and the gate that reads its verdict. "*" = any change of the file.
PROTECTED_FUNCTIONS: dict[str, list[str]] = {
    "team/cycle.py": ["canary", "_canary_artefacts", "_canary_snapshot", "release_as_pull_request", "tests_guard"],
    "team/canary.py": ["*"],
}
GUARD_DEFAULTS: dict[str, bool] = {"canary_report": False, "tests_strict": False}   # team.yaml `framework.guard`


def guard_settings(framework: dict[str, Any] | None) -> dict[str, bool]:
    """`framework.guard` of team.yaml: only a real true/false counts, anything else leaves the default (off)."""
    out = dict(GUARD_DEFAULTS)
    src = (framework or {}).get("guard")
    if isinstance(src, dict):
        for k, v in src.items():
            if k in out and isinstance(v, bool):
                out[k] = v
            elif k in out:
                log.warning("team.yaml framework.guard.%s=%r is not true/false — %s in use", k, v, out[k])
    elif src is not None:
        log.warning("team.yaml framework.guard=%r is not a mapping — ignored", src)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def report_path(data_dir: Path, cycle: str) -> Path:
    return Path(data_dir) / REPORT_DIR / f"{cycle}.json"


def write_report(path: Path, *, cycle: str, product: str, sha: str, started: str, ok: bool, note: str, artefacts: list[str],
                 phases: list[str] | None = None, rc: int | None = None) -> dict[str, Any]:
    """Write the canary's report (atomically: a half-written file must read as «does not parse», never as a pass)."""
    rep = {"version": REPORT_VERSION, "cycle": cycle, "product": product, "sha": sha, "started": started, "finished": _now(),
           "ok": bool(ok), "note": str(note)[:2000], "artefacts": list(artefacts)[:50], "phases": list(phases or []), "rc": rc}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    old = sorted(path.parent.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-REPORT_KEEP]
    for p in old:
        try:
            p.unlink()
        except OSError:
            pass
    log.info("canary report %s: ok=%s sha=%s artefacts=%d", path.name, rep["ok"], sha[:10], len(rep["artefacts"]))
    return rep


def check_report(path: Path | None, sha: str, started: str) -> tuple[bool, str]:
    """May a change at revision `sha` be merged on this report? `started`: ISO time this release began its canary — a
    report finished before it belongs to an earlier run. Returns (ok, why); every doubt is a «no»."""
    if path is None or not Path(path).exists():
        return False, "no canary report"
    try:
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"the canary report does not parse: {str(exc)[:120]}"
    if not isinstance(rep, dict) or any(k not in rep for k in REQUIRED):
        missing = [k for k in REQUIRED if not isinstance(rep, dict) or k not in rep]
        return False, f"the canary report does not parse: no {', '.join(missing[:4])}"
    if not sha or str(rep.get("sha")) != str(sha):
        return False, f"the canary report is for another revision ({str(rep.get('sha'))[:10]}, the candidate is {str(sha)[:10] or '?'})"
    try:
        fin, beg = datetime.fromisoformat(str(rep["finished"])), datetime.fromisoformat(str(started))
    except ValueError:
        return False, "the canary report does not parse: bad time"
    if fin < beg:
        return False, f"the canary report is older than this run (finished {rep['finished']}, the run began {started})"
    if rep.get("ok") is not True:
        return False, f"the canary failed: {str(rep.get('note'))[:200]}"
    if not rep.get("artefacts"):
        return False, "the canary report names no result artefact"
    return True, f"canary report {Path(path).name}: revision {str(sha)[:10]}, {len(rep['artefacts'])} artefact(s)"


def _functions(source: str) -> dict[str, str]:
    """name → source text of every function and method of a module (a method under its bare name; the last one wins)."""
    out: dict[str, str] = {}
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out[node.name] = "\n".join(lines[start - 1:node.end_lineno])
    return out


def protected_functions_changed(base_text: dict[str, str | None], head_text: dict[str, str | None],
                                protected: dict[str, list[str]] | None = None) -> list[str]:
    """`path:function` for every protected function whose source differs between the base and the candidate (added,
    removed and unparsable count as changed). `*_text`: path → file text, None when the file is absent on that side."""
    out: list[str] = []
    for path, names in (protected or PROTECTED_FUNCTIONS).items():
        a, b = base_text.get(path), head_text.get(path)
        if a == b:
            continue
        if "*" in names:
            out.append(f"{path}:*")
            continue
        try:
            fa, fb = _functions(a or ""), _functions(b or "")
        except SyntaxError:
            out.append(f"{path}:(does not parse)")
            continue
        out += [f"{path}:{n}" for n in names if fa.get(n) != fb.get(n)]
    return out
