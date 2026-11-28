"""`claude -p` as a role run: agent definition, JSON schema, budget, inactivity watchdog, full transcript.

Adapted from errata/scripts/am_cycle.py (AM pilot, 27.10). New here: the watchdog — no stream-json line for
`inactivity_s` kills the process (Paperclip issue #18586 lesson: a silent run otherwise burns the timeout), and the
auth modes oauth_token / api_key for a server without an interactive login.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import RUN_TIMEOUT_CEILING_S

log = logging.getLogger("team.runner")
ROLE_ENV_DENY_PREFIXES = ("DATAFORSEO_", "BING_WEBMASTER_")      # never in a role's environment (team/keywords.py)

# never available to a role: the dispatcher routes everything through files and the allowed Bash commands
AGENTS_ARG_MAX = 100_000   # bytes; the kernel limit for a single argv string is 128 KiB
EVENT_TS_KEY = "_ts"       # receive time (epoch seconds) the runner stamps on every stream-json event: the CLI's events carry
                           # no usable timestamp, and the run profile (team/runprofile.py) needs the time between a tool call and its result
KILLED_TIMEOUT, KILLED_INACTIVITY = "timeout", "inactivity"     # RunResult.killed: which limit stopped the run
KILLED_LOOP = "loop"                       # … or the loop guard (rework slice 4): the same tool call with the same output, N times in a row
LOOP_SHOWN_CHARS = 160                     # of the repeated call in the error text and the register
_KILLED_RE = ((re.compile(r"^timeout after \d+s"), KILLED_TIMEOUT), (re.compile(r"^inactive for \d+s"), KILLED_INACTIVITY),
              (re.compile(r"^loop: \d+ identical"), "loop"))
DISALLOWED_DEFAULT = ["WebFetch", "WebSearch", "Agent", "Task", "TodoWrite", "NotebookEdit", "KillShell", "BashOutput"]


def front_matter(text: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    return (yaml.safe_load(m.group(1)) or {}), m.group(2)


def load_roles(roles_dir: Path) -> dict[str, dict[str, Any]]:
    """roles/*.md → the JSON object `claude --agents` takes: {name: {description, prompt, model}}. `tools:` in the
    front matter is kept apart (it is applied through --allowedTools; a tools list on the agent breaks --json-schema)."""
    roles: dict[str, dict[str, Any]] = {}
    for p in sorted(roles_dir.glob("*.md")):
        meta, body = front_matter(p.read_text(encoding="utf-8"))
        name = str(meta.get("name") or p.stem)
        roles[name] = {"description": str(meta.get("description", "")), "prompt": body.strip(),
                       **({"model": str(meta["model"])} if meta.get("model") else {}),
                       "_tools": meta.get("tools") or [], "_disallowed": meta.get("disallowed") or []}
    return roles


@dataclass
class RunResult:
    ok: bool
    structured: dict[str, Any] | None
    raw: dict[str, Any] | None
    cost_usd: float | None
    num_turns: int
    error: str | None
    denials: list[Any] = field(default_factory=list)
    session_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    transcript: str = ""
    duration_s: float = 0.0
    limit_info: dict[str, Any] | None = None      # last rate_limit_info of the stream (plan window, credits)
    killed: str | None = None                     # "timeout" / "inactivity" when the dispatcher stopped the run, else None
    started: float = 0.0                          # epoch seconds of the run start (0 = unknown: fake runners)


def killed_kind(rr: "RunResult | None") -> str | None:
    """Which limit stopped the run: `timeout`, `inactivity` or None. Reads `RunResult.killed`, else the error text the
    runner writes for a kill (results built by hand or read back from run.json carry only the text)."""
    if rr is None or getattr(rr, "ok", False):
        return None
    if getattr(rr, "killed", None):
        return str(rr.killed)
    for rx, kind in _KILLED_RE:
        if rx.match(str(getattr(rr, "error", "") or "")):
            return kind
    return None


class LoopGuard:
    """Sees a run that goes round in a circle (owner 26.11, point 6 of the guide review: «петли лечить»): `repeats`
    finished tool calls in a row with the same tool, the same input AND the same output. A role that waits for a
    background command repeats the call too, but the output changes — that is not a loop. The role's own calls and
    each subagent's are counted apart; `StructuredOutput` is never counted. `repeats` < 2 switches the guard off."""

    def __init__(self, repeats: int):
        self.repeats = int(repeats or 0)
        self._open: dict[str, tuple[str, str, str]] = {}          # tool_use id → (stream, name, input)
        self._last: dict[str, tuple[tuple[str, str, str], int]] = {}   # stream → (the last finished call, how many in a row)

    def feed(self, e: Any) -> str | None:
        """One stream event. Returns the description of the loop at the moment the limit is reached, else None."""
        if self.repeats < 2 or not isinstance(e, dict):
            return None
        stream = str(e.get("parent_tool_use_id") or "")
        if e.get("type") == "assistant":
            for b in _blocks(e.get("message")):
                if b.get("type") == "tool_use" and b.get("name") != "StructuredOutput":
                    self._open[str(b.get("id"))] = (stream, str(b.get("name")), json.dumps(b.get("input"), sort_keys=True, ensure_ascii=False, default=str))
            return None
        if e.get("type") != "user":
            return None
        for b in _blocks(e.get("message")):
            if b.get("type") != "tool_result":
                continue
            call = self._open.pop(str(b.get("tool_use_id")), None)
            if call is None:
                continue
            stream, name, inp = call
            c = b.get("content")
            out = "\n".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in c) if isinstance(c, list) else str(c or "")
            key = (name, inp, out)
            prev, n = self._last.get(stream, (None, 0))
            n = n + 1 if prev == key else 1
            self._last[stream] = (key, n)
            if n >= self.repeats:
                try:
                    shown = json.loads(inp).get("command") or inp
                except (ValueError, AttributeError):
                    shown = inp
                return f"{n} identical tool calls in a row with the same output ({name} `{' '.join(str(shown).split())[:LOOP_SHOWN_CHARS]}`)"
        return None


class Runner:
    def __init__(self, claude_cfg: dict[str, Any], roles: dict[str, dict[str, Any]]):
        self.cfg, self.roles = claude_cfg, roles
        self.binary = str(claude_cfg.get("binary") or "claude")

    def run(self, role: str, prompt: str, schema: dict[str, Any] | None, *, model: str | None = None, max_usd: float,
            max_turns: int | None = None, cwd: Path, allowed_tools: list[str] | None = None, env_extra: dict[str, str] | None = None,
            timeout_s: int | None = None, inactivity_s: int | None = None, disallowed_tools: list[str] | None = None,
            add_dirs: list[Path] | None = None, run_dir: Path | None = None, run_id: str = "", loop_repeats: int = 0) -> RunResult:
        if role not in self.roles:
            return RunResult(False, None, None, 0.0, 0, f"role {role!r} is not defined")
        rdef = self.roles[role]
        agent_def = {k: v for k, v in rdef.items() if not k.startswith("_")}
        model = model or str(self.cfg.get("models", {}).get(role) or agent_def.get("model") or "sonnet")
        agent_def["model"] = model
        agents_json = json.dumps({role: agent_def})
        if len(agents_json.encode("utf-8")) > AGENTS_ARG_MAX:
            # one argv string is capped at 128 KB (Errno 7): send a stub definition, the role prompt goes ahead of the task on stdin
            log.warning("run %s: agent definition is %d bytes (> %d): role prompt moved to stdin", run_id, len(agents_json), AGENTS_ARG_MAX)
            prompt = f"# Role instructions ({role})\n\n{agent_def.get('prompt', '')}\n\n# Task\n\n{prompt}"
            agent_def = {**agent_def, "prompt": f"You are the {role} role. Your full role instructions are at the start of the user message; follow them."}
            agents_json = json.dumps({role: agent_def})
        argv = [self.binary, "-p", "--output-format", "stream-json", "--verbose",
                "--agents", agents_json, "--agent", role, "--model", model,
                "--max-turns", str(max_turns or self.cfg.get("max_turns", 60)), "--max-budget-usd", f"{max_usd:.2f}",
                "--permission-mode", "default"]
        if schema:
            argv += ["--json-schema", json.dumps({k: v for k, v in schema.items() if k != "$schema"})]
        tools = list(allowed_tools if allowed_tools is not None else rdef.get("_tools") or [])
        if tools:
            argv += ["--allowedTools", *tools]
        dis = list(DISALLOWED_DEFAULT) + list(rdef.get("_disallowed") or []) + list(disallowed_tools or [])
        dis = [d for d in sorted(set(dis)) if d not in set(tools)]   # a tool the role card allows explicitly wins over the default deny
        if dis:
            argv += ["--disallowedTools", *dis]
        for d in add_dirs or []:
            argv += ["--add-dir", str(d)]
        # owner 28.11: the keyword-data credentials are the dispatcher's alone (team/keywords.py); no role process gets them
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT") and not k.startswith(ROLE_ENV_DENY_PREFIXES)}
        env.update(env_extra or {})
        auth = str(self.cfg.get("auth", "oauth_token"))
        if auth == "oauth_token":
            env.pop("ANTHROPIC_API_KEY", None)            # the OAuth token (owner's plan) wins; the key stays for api_key mode
            if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
                log.warning("claude.auth=oauth_token but CLAUDE_CODE_OAUTH_TOKEN is not set: the CLI login will be used")
        elif auth == "api_key":
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            if not env.get("ANTHROPIC_API_KEY"):
                log.warning("claude.auth=api_key but ANTHROPIC_API_KEY is not set")
        elif auth == "login":                             # the CLI's own interactive login on this machine (a Mac)
            env.pop("ANTHROPIC_API_KEY", None)
        cwd.mkdir(parents=True, exist_ok=True)
        timeout_s = int(timeout_s or self.cfg.get("timeout_s", 1500))
        inactivity_s = int(inactivity_s or self.cfg.get("inactivity_s", 300))
        if timeout_s > RUN_TIMEOUT_CEILING_S:              # the last line of defence: callers that bypass config.run_limits
            log.warning("run %s: timeout_s=%d is above the hard ceiling %d s — clamped", run_id, timeout_s, RUN_TIMEOUT_CEILING_S)
            timeout_s = RUN_TIMEOUT_CEILING_S
        log.info("run %s: role=%s model=%s auth=%s cap=$%.2f turns=%s timeout=%ds inactivity=%ds cwd=%s prompt=%d chars", run_id, role,
                 model, auth, max_usd, max_turns or self.cfg.get("max_turns"), timeout_s, inactivity_s, cwd, len(prompt))
        t0 = time.time()
        events: list[dict[str, Any]] = []
        killed_reason: str | None = None
        killed: str | None = None
        try:
            # the prompt goes through stdin, not argv: argv is capped by the OS (issues #147/#149, "Argument list too long")
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        except FileNotFoundError:
            return RunResult(False, None, None, 0.0, 0, f"{self.binary}: not found")

        def _feed() -> None:
            try:
                if proc.stdin:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                log.warning("run %s: writing the prompt to stdin failed: %s", run_id, exc)

        threading.Thread(target=_feed, daemon=True).start()
        last = time.time()
        stderr_buf: list[str] = []
        guard = LoopGuard(loop_repeats)                    # off unless the caller asks (cycle.guards.loop)

        def _drain_err() -> None:
            for ln in proc.stderr or []:
                stderr_buf.append(ln)

        threading.Thread(target=_drain_err, daemon=True).start()
        live = run_dir / "events.jsonl" if run_dir else None
        if live:
            live.parent.mkdir(parents=True, exist_ok=True)
        lf = live.open("a", encoding="utf-8") if live else None
        try:
            while True:
                if proc.poll() is not None and not _readable(proc.stdout):
                    break
                now = time.time()
                if now - t0 > timeout_s:
                    killed_reason, killed = f"timeout after {timeout_s}s", KILLED_TIMEOUT
                    proc.kill()
                    break
                if now - last > inactivity_s:
                    killed_reason, killed = f"inactive for {inactivity_s}s", KILLED_INACTIVITY
                    proc.kill()
                    break
                ln = _readline(proc.stdout, 1.0)
                if ln is None:
                    continue
                if not ln:
                    if proc.poll() is not None:
                        break
                    continue
                last = time.time()
                s = ln.strip()
                if not s.startswith("{"):
                    continue
                try:
                    e = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if isinstance(e, dict):
                    e[EVENT_TS_KEY] = round(last, 3)          # when the line arrived; the run profile derives call durations from it
                events.append(e)
                if lf:
                    lf.write((json.dumps(e, ensure_ascii=False) if isinstance(e, dict) else s) + "\n")
                    lf.flush()
                loop = guard.feed(e)
                if loop:
                    killed_reason, killed = f"loop: {loop}", KILLED_LOOP
                    proc.kill()
                    break
        finally:
            if lf:
                lf.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        dur = time.time() - t0
        from .billing import limit_info as _limit_info
        lim = _limit_info(events)
        raw = next((e for e in reversed(events) if e.get("type") == "result"), None)
        session_id = next((e.get("session_id") for e in events if e.get("session_id")), None)
        transcript = render_transcript(role, model, events, raw)
        if run_dir:
            (run_dir / "transcript.md").write_text(transcript, encoding="utf-8")
        if killed_reason:
            log.warning("run %s killed: %s (%.0fs, %d events)", run_id, killed_reason, dur, len(events))
            return RunResult(False, None, None, None, 0, killed_reason, session_id=session_id, events=events, transcript=transcript,
                             duration_s=dur, limit_info=lim, killed=killed, started=t0)
        if raw is None:
            cli_error = proc.returncode != 0 and not events and dur < 15
            err = "".join(stderr_buf)[-500:]
            return RunResult(False, None, None, 0.0 if cli_error else None, 0,
                             f"{'cli error' if cli_error else 'no result event'} (exit {proc.returncode}): {err}",
                             session_id=session_id, events=events, transcript=transcript, duration_s=dur, limit_info=lim, started=t0)
        cost = raw.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
        structured = raw.get("structured_output") if isinstance(raw.get("structured_output"), dict) else None
        err = None
        if raw.get("is_error"):
            err = str(raw.get("result") or raw.get("subtype") or "is_error")[:600]
        elif schema and structured is None:
            err = "no structured_output in the result"
        log.info("run %s done: %.0fs turns=%s cost=%s session=%s error=%s", run_id, dur, raw.get("num_turns"),
                 f"${cost:.3f}" if cost is not None else "unknown", session_id, err)
        return RunResult(err is None, structured, raw, cost, int(raw.get("num_turns") or 0), err,
                         list(raw.get("permission_denials") or []), session_id, events, transcript, dur, limit_info=lim, started=t0)


def _readable(stream: Any) -> bool:
    try:
        import select
        r, _, _ = select.select([stream], [], [], 0)
        return bool(r)
    except Exception:  # noqa: BLE001
        return False


def _readline(stream: Any, timeout: float) -> str | None:
    """A line, "" at EOF, None when nothing arrived within timeout."""
    import select
    r, _, _ = select.select([stream], [], [], timeout)
    if not r:
        return None
    return stream.readline()


def _blocks(msg: Any) -> list[dict[str, Any]]:
    content = (msg or {}).get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


SHELL_RULE = ("Shell rule: only a bare `team-fetch <url> [flags]` (or team-publish) is allowed in Bash — no pipes, redirects, heredocs "
              "or other commands. Write files with the Write tool. Replace pipes with `team-fetch --json-path a.b[0].c` "
              "(add `--count` for a length), `--json-keys`, or `--grep`.")


def denial_hint(denial: Any) -> str:
    """The allowed form for a denied call, so a role (and a reader of the transcript) sees what to do instead."""
    d = denial if isinstance(denial, dict) else {"tool_name": str(denial)}
    inp = d.get("tool_input") or {}
    cmd = str(inp.get("command") or "") if isinstance(inp, dict) else ""
    if d.get("tool_name") == "Bash":
        if cmd.lstrip().startswith("team-fetch") and re.search(r"<|>", cmd):
            return ("A `<` or `>` inside a team-fetch argument looks like a shell redirect to the permission check, even in quotes. "
                    "Grep for text without it (`--grep 'item' --around 1`, or `--grep 'class=\"item'`); `--around` takes N>=1 "
                    "(`--around 0` itself works in team-fetch, but use 1). " + SHELL_RULE)
        if re.search(r"<<|>|\btee\b|\bcat\b", cmd):
            return "Use the Write tool to create files, not shell redirects or heredocs. " + SHELL_RULE
        return SHELL_RULE
    if d.get("tool_name") == "Edit":
        return "Edit is not available to this role; rewrite the whole file with Write."
    return ""


def render_transcript(role: str, model: str, events: list[dict[str, Any]], result: dict[str, Any] | None,
                      max_result_chars: int = 1500) -> str:
    sid = next((e.get("session_id") for e in events if e.get("session_id")), None)
    lines = [f"# {role} — run transcript", "", f"model: {model} · session: {sid or '—'}"
             + (f" · `claude --resume {sid}`" if sid else ""), ""]
    turn = 0
    names: dict[str, str] = {}
    for e in events:
        t = e.get("type")
        if t == "assistant":
            body: list[str] = []
            for b in _blocks(e.get("message")):
                if b.get("type") == "text" and b.get("text", "").strip():
                    body += ["", b["text"].strip(), ""]
                elif b.get("type") == "tool_use":
                    names[str(b.get("id"))] = str(b.get("name"))
                    inp = b.get("input") or {}
                    if b.get("name") == "StructuredOutput":
                        body += ["", "**StructuredOutput:** submitted", ""]
                    else:
                        cmd = inp.get("command") if isinstance(inp, dict) and "command" in inp else json.dumps(inp, ensure_ascii=False)[:500]
                        body += ["", f"**{b.get('name')}:** `{cmd}`", ""]
            if body:
                turn += 1
                lines += [f"## Turn {turn}"] + body
        elif t == "user":
            for b in _blocks(e.get("message")):
                if b.get("type") != "tool_result" or names.get(str(b.get("tool_use_id"))) == "StructuredOutput":
                    continue
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                c = str(c or "")
                cut = c[:max_result_chars] + (f"\n… [{len(c) - max_result_chars} more chars]" if len(c) > max_result_chars else "")
                lines += [f"**result of {names.get(str(b.get('tool_use_id')), 'tool')}{' (error)' if b.get('is_error') else ''}:**",
                          "", "```", cut, "```", ""]
    if result is not None and result.get("permission_denials"):
        lines += ["## Denied calls", ""]
        for dn in result["permission_denials"][:10]:
            lines += [f"- {dn.get('tool_name') if isinstance(dn, dict) else dn}: {denial_hint(dn)}".rstrip(": ")]
        lines += [""]
    if result is not None:
        lines += ["## Result", "", f"turns: {result.get('num_turns')} · cost: {result.get('total_cost_usd')} · is_error: {result.get('is_error')}", ""]
        so = result.get("structured_output")
        if isinstance(so, dict):
            lines += ["```yaml", yaml.safe_dump(so, sort_keys=False, allow_unicode=True, width=110).rstrip(), "```", ""]
        elif result.get("result"):
            lines += [str(result.get("result"))[:4000], ""]
    return "\n".join(lines) + "\n"
