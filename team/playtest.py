"""Play-test server for the tester role (game profile, slice 4; owner 20.11).

The reviewer of a static site reads the live pages. A game has nothing to read until the server runs, so for a released
task the dispatcher starts the service itself from the merged commit: a writable copy of `released_sha` under
data/work/<slug>/<cycle>/playtest-<task>/, `install` (npm ci / npm install when package.json is there and node_modules
is not), `repo.build_cmd`, then `repo.run_cmd` on a free port with PORT in the environment (and `{port}` substituted),
and waits until `repo.health_path` answers 200. The tester then plays against http://127.0.0.1:<port> with
`team-play`. When the product already has a deployed host (`deploy.mode` not `pending` and a `site_base`), that URL is
used instead and no local server is started. Secrets never reach the server's environment.

Every timeout and default lives at the top of this module; product.yaml `repo.*` overrides the commands.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops
from .config import Product

log = logging.getLogger("team.playtest")

SECRET_ENV_PREFIXES = ("TELEGRAM_", "BLUESKY_", "DEVTO_", "MASTODON_", "THREADS_", "X_API_", "X_ACCESS_", "X_CLIENT_", "X_REFRESH_",
                       "X_OAUTH2_", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GSC_")
START_TIMEOUT_S = 90            # health must answer within this
HEALTH_POLL_S = 0.5
INSTALL_TIMEOUT_S = 600
BUILD_TIMEOUT_S = 600
STOP_GRACE_S = 5
LOG_TAIL_CHARS = 2500
HOST = "127.0.0.1"
DEFAULT_HEALTH_PATH = "/"
AUTO_INSTALL = {"package-lock.json": "npm ci --no-audit --no-fund", "package.json": "npm install --no-audit --no-fund"}


class PlaytestError(RuntimeError):
    pass


@dataclass
class Server:
    base_url: str
    port: int | None
    dest: Path
    log_path: Path | None
    proc: subprocess.Popen | None = None
    local: bool = True
    notes: list[str] = field(default_factory=list)

    def log_tail(self, n: int = LOG_TAIL_CHARS) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        t = self.log_path.read_text(encoding="utf-8", errors="replace")
        return t[-n:]


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_ENV_PREFIXES)}
    env.update({"TEAM_PUBLISH_DRY_RUN": "1", "NODE_ENV": "test", "CI": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def prepare_copy(repo_dir: Path, sha: str, dest: Path) -> Path:
    """A writable detached copy of `sha` (the server writes node_modules, SQLite files, logs)."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    gitops.git(repo_dir, "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(repo_dir), str(dest))
    gitops.git(dest, "checkout", "--quiet", "--detach", sha)
    gitops.git(dest, "remote", "remove", "origin", check=False)
    return dest


def install_cmd_for(dest: Path, explicit: str = "") -> str:
    if explicit:
        return explicit
    if (dest / "node_modules").exists():
        return ""
    for marker, cmd in AUTO_INSTALL.items():
        if (dest / marker).exists():
            return cmd
    return ""


def _run(cmd: str, cwd: Path, timeout: int, log_fh: Any) -> int:
    log_fh.write(f"\n$ {cmd}\n")
    log_fh.flush()
    cp = subprocess.run(cmd, shell=True, cwd=str(cwd), stdout=log_fh, stderr=subprocess.STDOUT, timeout=timeout, env=clean_env())
    return cp.returncode


def health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:   # noqa: S310 — 127.0.0.1 or the product's own host
            return 200 <= r.status < 400
    except Exception:  # noqa: BLE001
        return False


def live_base_url(product: Product) -> str | None:
    """The deployed host when there is one: `deploy.mode` not pending and a site_base."""
    if product.deploy_mode != "pending" and product.site_base:
        return product.site_base
    return None


def start(product: Product, repo_dir: Path, sha: str, work: Path, *, install_cmd: str = "", start_timeout_s: int = START_TIMEOUT_S) -> Server:
    """Copy `sha`, install, build, run, wait for health. Raises PlaytestError with the log tail when the server does not come up."""
    live = live_base_url(product)
    if not live and not product.run_cmd:
        raise PlaytestError("repo.run_cmd is not set in product.yaml: the dispatcher cannot start the server")
    dest = prepare_copy(repo_dir, sha, work / "checkout")       # the tester's Playwright runs from here even against a deployed host
    log_path = work / "server.log"
    port = free_port()
    log_fh = log_path.open("w", encoding="utf-8")
    try:
        inst = install_cmd_for(dest, install_cmd or str((product.raw.get("repo") or {}).get("install_cmd") or ""))
        if inst:
            rc = _run(inst, dest, INSTALL_TIMEOUT_S, log_fh)
            if rc != 0:
                raise PlaytestError(f"install failed (exit {rc}): {inst}\n" + _tail(log_path))
        if live:
            log_fh.write(f"\ndeployed host {live} in use; no local server\n")
            log_fh.close()
            return Server(base_url=live, port=None, dest=dest, log_path=log_path, proc=None, local=False, notes=[f"deployed host {live} used; no local server"])
        if product.build_cmd:
            rc = _run(product.build_cmd, dest, BUILD_TIMEOUT_S, log_fh)
            if rc != 0:
                raise PlaytestError(f"build failed (exit {rc}): {product.build_cmd}\n" + _tail(log_path))
        cmd = product.run_cmd.replace("{port}", str(port))
        env = clean_env()
        env.update({"PORT": str(port), "HOST": HOST, "BASE_URL": f"http://{HOST}:{port}"})
        log_fh.write(f"\n$ PORT={port} {cmd}\n")
        log_fh.flush()
        proc = subprocess.Popen(cmd, shell=True, cwd=str(dest), stdout=log_fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except subprocess.TimeoutExpired as exc:
        log_fh.close()
        raise PlaytestError(f"timed out: {exc.cmd}") from exc
    except PlaytestError:
        log_fh.close()
        raise
    base = f"http://{HOST}:{port}"
    health = base + (product.health_path or DEFAULT_HEALTH_PATH)
    t0 = time.time()
    server = Server(base_url=base, port=port, dest=dest, log_path=log_path, proc=proc)
    while time.time() - t0 < start_timeout_s:
        if proc.poll() is not None:
            log_fh.close()
            raise PlaytestError(f"server exited with code {proc.returncode} before answering {health}\n" + _tail(log_path))
        if health_ok(health):
            log.info("%s: play-test server up at %s (%.1fs)", product.slug, base, time.time() - t0)
            server.notes.append(f"health {health} answered after {time.time() - t0:.1f}s")
            return server
        time.sleep(HEALTH_POLL_S)
    stop(server)
    log_fh.close()
    raise PlaytestError(f"server did not answer {health} within {start_timeout_s}s\n" + _tail(log_path))


def stop(server: Server) -> None:
    p = server.proc
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(p.pid), 15)
    except (ProcessLookupError, PermissionError, OSError):
        p.terminate()
    try:
        p.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            p.kill()
        p.wait(timeout=STOP_GRACE_S)


def _tail(p: Path, n: int = LOG_TAIL_CHARS) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""
