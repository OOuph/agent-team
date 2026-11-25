"""Service deploy for the game profile (slice 5; owner 20.11 «слить, потом деплой»; SCHEME §11).

After a merge of a `game` product whose `deploy.mode` is `service`, the dispatcher deploys the merged commit through
`deploy/service/deploy-service.sh` on the droplet (same host as the dispatcher since 20.11: `sudo -n` via the sudoers
rule install.sh wrote; `deploy.host: user@host` runs it over ssh instead). The script stages a release, installs,
builds, starts the idle colour, waits for the health path, switches the nginx upstream and retires the old colour; a
failed health check leaves the live release untouched (exit 20). This module only runs the command, parses its last
`== deployed …` line and reports; the task's fate (released → tester, or blocked with a PO question) is `Cycle`'s.

A game on its own server (owner 24.11: the agents' runs and the game no longer share one box): `deploy.host: user@host`
plus `deploy.origin: /path/origin.git`. The dispatcher then pushes the commit from its own clone of the product
repository into that bare repository over ssh (`push_command`) and only then calls the deploy script there; the game
host holds no GitHub token and cannot reach the dispatcher.

`team deploy <slug> [--sha S | --rollback]` is the owner's manual entry. No secret is read or passed here; the only git
this module runs is that push (and a fetch of the product's origin when the asked commit is not in the local clone yet).
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Product
from .playtest import clean_env

log = logging.getLogger("team.servicedeploy")

MODE_SERVICE = "service"
DEFAULT_CMD = "sudo -n /opt/agent-team/bin/deploy-service.sh"      # product.yaml deploy.cmd overrides (tests: a fake script)
LOCAL_HOSTS = ("", "local", "localhost", "127.0.0.1")
TIMEOUT_S = 900                                                      # install + build + health on the droplet
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
RESULT_PREFIX = "== deployed "
OUTPUT_TAIL_CHARS = 2500
EXIT_NOTHING_TO_DO = 10
EXIT_HEALTH_FAILED = 20
EXIT_NGINX_FAILED = 21
EXIT_PUSH_FAILED = 30                                                # the commit did not reach the game host's bare repository
PUSH_TIMEOUT_S = 180
GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=15"
EXIT_MEANING = {EXIT_NOTHING_TO_DO: "already live", EXIT_HEALTH_FAILED: "health check failed: the new release was stopped, the live one is untouched",
                EXIT_NGINX_FAILED: "nginx refused the new upstream; previous one restored", 64: "usage error", 1: "install/build/clone failed",
                EXIT_PUSH_FAILED: "the commit could not be pushed to the game host; nothing was deployed"}

RunFn = Callable[..., subprocess.CompletedProcess]


class DeployError(RuntimeError):
    pass


@dataclass
class DeployResult:
    ok: bool
    sha: str = ""
    url: str = ""
    port: int | None = None
    colour: str = ""
    exit_code: int = 0
    output: str = ""
    error: str = ""
    duration_s: float = 0.0
    command: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return self.exit_code == EXIT_NOTHING_TO_DO

    def summary(self) -> str:
        if self.ok:
            return f"deployed {self.sha[:12]} → {self.url} (colour {self.colour}, port {self.port}, {self.duration_s:.0f}s)"
        if self.skipped:
            return "already live, nothing deployed"
        return f"deploy failed (exit {self.exit_code}: {EXIT_MEANING.get(self.exit_code, 'error')}): {self.error}"[:600]


def enabled(product: Product) -> bool:
    """The dispatcher deploys after a merge only for `deploy.mode: service`; `pending` (no host yet) and `git_pull`
    (the static droplet timer) deploy nothing from here."""
    return product.deploy_mode == MODE_SERVICE


def command(product: Product, *, sha: str = "", rollback: bool = False, force: bool = False) -> list[str]:
    """`<deploy.cmd> <slug> [--sha S | --rollback] [--force]`, wrapped in ssh when `deploy.host` names a remote host."""
    base = shlex.split(product.deploy_cmd or DEFAULT_CMD)
    args = base + [product.slug]
    if rollback:
        args.append("--rollback")
    elif sha:
        args += ["--sha", sha]
    if force:
        args.append("--force")
    host = product.deploy_host
    if host not in LOCAL_HOSTS:
        return ["ssh", *SSH_OPTS, host, " ".join(shlex.quote(a) for a in args)]
    return args


def pushes(product: Product) -> bool:
    """True when the commit travels from the dispatcher's clone to a bare repository on a remote game host."""
    return bool(product.deploy_origin) and product.deploy_host not in LOCAL_HOSTS


def push_command(product: Product, *, sha: str = "") -> list[str]:
    """`git -C <clone> push --force <host>:<origin> <sha or branch>:refs/heads/<branch>` — the game host's bare repository
    is a mirror of what the dispatcher decided to deploy, so a forced update is right (a rollback never pushes)."""
    src = sha or f"refs/heads/{product.deploy_branch}"
    return ["git", "-C", str(product.repo_dir), "push", "--force", f"{product.deploy_host}:{product.deploy_origin}", f"{src}:refs/heads/{product.deploy_branch}"]


def _push(product: Product, sha: str, runner: RunFn) -> tuple[int, str, list[str]]:
    """Fetch the product's origin when the commit is not in the local clone yet (a manual deploy of a commit pushed by a
    human), then push it to the game host. Returns (exit code, output, the push command)."""
    env = {**clean_env(), "GIT_SSH_COMMAND": GIT_SSH_COMMAND, "GIT_TERMINAL_PROMPT": "0"}
    out = ""
    if sha:
        have = runner(["git", "-C", str(product.repo_dir), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, text=True, timeout=PUSH_TIMEOUT_S, env=env)
        if int(have.returncode) != 0:
            fetched = runner(["git", "-C", str(product.repo_dir), "fetch", "--quiet", "origin"], capture_output=True, text=True, timeout=PUSH_TIMEOUT_S, env=env)
            out += f"(commit {sha[:12]} not in the local clone: fetched origin, exit {fetched.returncode})\n" + (fetched.stderr or "")
    cmd = push_command(product, sha=sha)
    log.info("%s: push to the game host: %s", product.slug, " ".join(cmd))
    try:
        cp = runner(cmd, capture_output=True, text=True, timeout=PUSH_TIMEOUT_S, env=env)
        return int(cp.returncode), out + (cp.stdout or "") + (cp.stderr or ""), cmd
    except subprocess.TimeoutExpired:
        return 124, out + f"(push timed out after {PUSH_TIMEOUT_S}s)", cmd
    except OSError as exc:
        return 127, out + f"(push could not start: {exc})", cmd


def parse_result_line(output: str) -> dict[str, str]:
    """The script's last stdout line: `== deployed slug=… sha=… colour=… port=… url=…` → dict (empty when absent)."""
    for line in reversed((output or "").splitlines()):
        if line.startswith(RESULT_PREFIX):
            out: dict[str, str] = {}
            for tok in line[len(RESULT_PREFIX):].split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    out[k] = v
            return out
    return {}


def run(product: Product, *, sha: str = "", rollback: bool = False, force: bool = False, runner: RunFn = subprocess.run,
        timeout_s: int = TIMEOUT_S, log_path: Path | None = None) -> DeployResult:
    """Run the deploy command with a secret-free environment; never raises on a failed deploy (the result says why)."""
    cmd = command(product, sha=sha, rollback=rollback, force=force)
    t0 = time.monotonic()
    push_out = ""
    if pushes(product) and not rollback:                              # a rollback redeploys a release the host already has
        code, push_out, push_cmd = _push(product, sha, runner)
        if code != 0:
            err = f"push to {product.deploy_host}:{product.deploy_origin} failed (exit {code})"
            log.error("%s: %s", product.slug, err)
            res = DeployResult(ok=False, sha=sha, exit_code=EXIT_PUSH_FAILED, output=push_out[-OUTPUT_TAIL_CHARS:], error=err,
                               duration_s=time.monotonic() - t0, command=push_cmd)
            if log_path is not None:
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(push_out, encoding="utf-8")
                except OSError:
                    pass
            return res
    log.info("%s: deploy %s", product.slug, " ".join(cmd))
    try:
        cp = runner(cmd, capture_output=True, text=True, timeout=timeout_s, env=clean_env())
        out = (cp.stdout or "") + (("\n" + cp.stderr) if cp.stderr else "")
        code = int(cp.returncode)
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")) + f"\n(timeout after {timeout_s}s)"
        code = 124
    except OSError as exc:
        out, code = f"cannot run {cmd[0]}: {exc}", 127
    dur = time.monotonic() - t0
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"$ {' '.join(cmd)}\n{out}\n(exit {code}, {dur:.1f}s)\n", encoding="utf-8")
    tail = out[-OUTPUT_TAIL_CHARS:]
    parsed = parse_result_line(out)
    if code == 0 and parsed.get("sha"):
        port = parsed.get("port")
        return DeployResult(True, sha=parsed["sha"], url=parsed.get("url", ""), port=int(port) if port and port.isdigit() else None,
                            colour=parsed.get("colour", ""), exit_code=0, output=tail, duration_s=dur, command=cmd)
    err_lines = [ln for ln in out.splitlines() if "ERROR" in ln or "error" in ln.lower()]
    err = (err_lines[-1] if err_lines else (out.strip().splitlines() or ["no output"])[-1]).strip()
    if code == 0:
        err = "exit 0 but no `== deployed` line in the output"
    return DeployResult(False, exit_code=code, output=tail, error=err[:400], duration_s=dur, command=cmd)


def describe(product: Product) -> str:
    """One line for `team status` / prompts: where this product deploys."""
    if not enabled(product):
        return f"deploy.mode={product.deploy_mode} (no dispatcher deploy)"
    where = "this host via sudo" if product.deploy_host in LOCAL_HOSTS else f"ssh {product.deploy_host}"
    if pushes(product):
        where += f" (the commit is pushed to {product.deploy_origin} first)"
    when = (f"; batch: once per cycle, on the last merged commit, at most {product.deploy_batch_max_wait_min:g} min after a merge"
            if product.deploy_batch else "; after every merge")
    return f"deploy.mode=service → {where}: {product.deploy_cmd or DEFAULT_CMD} {product.slug} --sha <merged>{when}"


def result_card(product: Product, task: dict[str, Any], res: DeployResult) -> dict[str, Any]:
    """Observation for the PO when a deploy fails (classification environment_error: the host, not the game, unless the
    output says build/health, which the PO reads in `actual`)."""
    return {"title": f"deploy of {task['id']} failed ({EXIT_MEANING.get(res.exit_code, f'exit {res.exit_code}')})"[:140],
            "classification": "environment_error", "source": "dispatcher", "task": task["id"], "url": product.site_base,
            "expected": "merged commit live on the host, health 200", "actual": (res.error + "\n" + res.output[-800:])[:1200]}
