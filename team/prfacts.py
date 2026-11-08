"""Facts block for the PR body, computed by the dispatcher from git and the test run, never from the developer's text (T-0060).

The developer's summary is a claim; this block is the checkpoint a reviewer can verify (F-0127-class claim-vs-reality evidence,
voice 2026-11-07 P2). If the files the developer names are not in the diff, a MISMATCH line is added.
"""
from __future__ import annotations

import re
from typing import Any

HEADING = "## Verified by dispatcher"
_EXT = r"(?:py|md|yaml|yml|json|jsonl|sh|txt|toml|html|css|js|cfg|ini)"
_PATH_RE = re.compile(rf"(?<![\w/.:-])((?:[\w.-]+/)*[\w.-]+\.{_EXT})(?![\w/-])")
_URL_RE = re.compile(r"https?://\S+")
_COUNT_RE = {k: re.compile(rf"(\d+)\s+{k}\b") for k in ("passed", "failed", "error", "errors", "skipped")}


def parse_test_counts(output: str) -> dict[str, int] | None:
    """Counts from the last pytest summary line ('3 failed, 120 passed in 4s'); None if there is none."""
    for line in reversed((output or "").strip().splitlines()):
        if re.search(r"\b(passed|failed|errors?)\b", line) and re.search(r"\bin [\d.]+s\b|=====", line):
            c = {k: int(m.group(1)) for k, rx in _COUNT_RE.items() if (m := rx.search(line))}
            c["error"] = c.pop("errors", 0) + c.get("error", 0)
            return {"passed": c.get("passed", 0), "failed": c.get("failed", 0), "error": c["error"], "skipped": c.get("skipped", 0)}
    return None


def claimed_files(summary: str, changes: list[str] | None) -> list[str]:
    text = _URL_RE.sub(" ", "\n".join([str(summary or ""), *[str(c) for c in changes or []]]))
    seen: list[str] = []
    for m in _PATH_RE.finditer(text):
        p = m.group(1)
        if p not in seen:
            seen.append(p)
    return seen


def mismatches(claimed: list[str], diff_files: list[str]) -> list[str]:
    """Claimed paths that match no file in the diff (a bare name matches by basename, a path by suffix)."""
    out = []
    for c in claimed:
        if not any(f == c or f.endswith("/" + c) or (("/" not in c) and f.rsplit("/", 1)[-1] == c) for f in diff_files):
            out.append(c)
    return out


def render(diff_stat: str, diff_files: list[str], test_output: str | None, claimed: list[str], canary: str = "pending") -> str:
    """The markdown section. `canary` is 'pending' at PR open, then the dispatcher edits the PR with the result."""
    lines = [HEADING, "", f"Computed by the dispatcher from `git diff --stat` against the base branch and the test run; not taken from the developer's text.", "",
             f"**Files changed ({len(diff_files)}):**"]
    lines += [f"- `{f}`" for f in diff_files] or ["- (none)"]
    if diff_stat.strip():
        lines += ["", "```", diff_stat.strip(), "```"]
    if test_output is None:
        lines += ["", "**Tests:** not run (no test command configured)"]
    else:
        c = parse_test_counts(test_output)
        lines += ["", "**Tests:** " + (f"{c['passed']} passed, {c['failed']} failed, {c['error']} errors, {c['skipped']} skipped" if c else "counts not parsed from the output")]
    lines += ["", f"**Canary:** {canary}"]
    bad = mismatches(claimed, diff_files)
    if bad:
        lines += ["", "**MISMATCH:** the developer's summary names files that are not in the diff: " + ", ".join(f"`{b}`" for b in bad)]
    return "\n".join(lines)


def build(diff_stat: str, diff_files: list[str], test_output: str | None, res: dict[str, Any], canary: str = "pending") -> tuple[str, list[str]]:
    """(markdown, mismatched files)."""
    claimed = claimed_files(res.get("summary", ""), res.get("changes"))
    return render(diff_stat, diff_files, test_output, claimed, canary), mismatches(claimed, diff_files)


def with_canary(body: str, canary: str) -> str:
    return body.replace("**Canary:** pending", f"**Canary:** {canary}", 1)
