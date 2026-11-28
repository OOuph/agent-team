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
import threading
import time
import urllib.request
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops, nodecache
from .config import Product

log = logging.getLogger("team.playtest")

SECRET_ENV_PREFIXES = ("TELEGRAM_", "BLUESKY_", "DEVTO_", "MASTODON_", "THREADS_", "X_API_", "X_ACCESS_", "X_CLIENT_", "X_REFRESH_",
                       "X_OAUTH2_", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GSC_",
                       "DATAFORSEO_", "BING_WEBMASTER_")
START_TIMEOUT_S = 90            # health must answer within this
HEALTH_POLL_S = 0.5
INSTALL_TIMEOUT_S = 600
BUILD_TIMEOUT_S = 600
STOP_GRACE_S = 5
LOG_TAIL_CHARS = 2500
HOST = "127.0.0.1"
DEFAULT_HEALTH_PATH = "/"
PORT_HOLD_S = 180               # a port handed to one play-test is not handed to another of this process for this long
PORT_TRIES = 50                 # asks to the OS for a free port before giving up on finding one that is not held
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


_port_lock = threading.Lock()
_ports_held: dict[int, float] = {}          # port -> monotonic time it was handed out (this process)


def free_port() -> int:
    """A free local port. The OS only says that the port is free NOW: between this call and the server's own bind another
    play-test of the same cycle (parallel lanes, team/lanes.py) could be given the same number. So the choice is made
    under a lock and a port handed out in the last PORT_HOLD_S seconds is not handed out again by this process."""
    with _port_lock:
        now = time.monotonic()
        for port, t in list(_ports_held.items()):
            if now - t > PORT_HOLD_S:
                del _ports_held[port]
        port = 0
        for _ in range(PORT_TRIES):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((HOST, 0))
                port = int(s.getsockname()[1])
            if port not in _ports_held:
                break
            log.debug("port %d was handed to another play-test %.0fs ago — asking for another", port, now - _ports_held[port])
        _ports_held[port] = now
        return port


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


PORT_ASSIGN_RE = __import__("re").compile(r"(?<![\w])PORT=\d+\b")


def command_for_port(run_cmd: str, port: int) -> str:
    """The run command for a server on `port`: `{port}` is filled in, and a port the command assigns itself is replaced
    (`PORT=3101 node dist/server/index.js`, ai-battles: with it the server came up on 3101 whatever PORT the environment
    held, so a locally started server never answered on the port the dispatcher had picked)."""
    return PORT_ASSIGN_RE.sub(f"PORT={port}", str(run_cmd).replace("{port}", str(port)))


def test_env(product: Product, port: int, tmp: Path) -> dict[str, str]:
    """product.yaml `playtest.test_env` for a locally started server (rework slices 5 and 6): `{port}` and `{tmp}` are
    filled in. Empty when the product has none."""
    cfg = product.raw.get("playtest")
    te = cfg.get("test_env") if isinstance(cfg, dict) else None
    if not isinstance(te, dict):
        return {}
    return {str(k): str(v).replace("{port}", str(port)).replace("{tmp}", str(tmp)) for k, v in te.items()
            if str(k).replace("_", "").isalnum() and str(k).upper() == str(k)}


def live_base_url(product: Product) -> str | None:
    """The deployed host when there is one: `deploy.mode` not pending and a site_base."""
    if product.deploy_mode != "pending" and product.site_base:
        return product.site_base
    return None


def start(product: Product, repo_dir: Path, sha: str, work: Path, *, install_cmd: str = "", start_timeout_s: int = START_TIMEOUT_S,
          lock: Any = None, force_local: bool = False, prepare: Any = None) -> Server:
    """Copy `sha`, install, build, run, wait for health. Raises PlaytestError with the log tail when the server does not come up.
    `lock`: the cycle's repository lock (team/lanes.py), held only while the clone is copied — another lane may be merging
    into it; install, build and the server run outside it."""
    # `force_local` (rework slice 5): a commit that is not merged yet is not on the deployed host — always a local server
    live = None if force_local else live_base_url(product)
    if not live and not product.run_cmd:
        raise PlaytestError("repo.run_cmd is not set in product.yaml: the dispatcher cannot start the server")
    with lock if lock is not None else nullcontext():
        dest = prepare_copy(repo_dir, sha, work / "checkout")   # the tester's Playwright runs from here even against a deployed host
    log_path = work / "server.log"
    log_fh = log_path.open("w", encoding="utf-8")
    # `prepare(dest)` (27.11, team/nodecache.py): True = the copy got a ready node_modules — no install, and the build
    # command runs without its own `npm ci`
    seeded = bool(prepare(dest)) if prepare is not None else False
    build_cmd = nodecache.strip_install(product.build_cmd) if seeded and product.build_cmd else product.build_cmd
    if seeded:
        log_fh.write("node_modules copied from the dispatcher's cache for this lock file; no install\n")
    try:
        inst = "" if seeded else install_cmd_for(dest, install_cmd or str((product.raw.get("repo") or {}).get("install_cmd") or ""))
        if inst:
            rc = _run(inst, dest, INSTALL_TIMEOUT_S, log_fh)
            if rc != 0:
                raise PlaytestError(f"install failed (exit {rc}): {inst}\n" + _tail(log_path))
        if live:
            # ai-battles 22.11: generated sources (src/engine/balance.data.ts after T-0133) are not in git, so on the
            # un-built checkout every spec failed at import («No tests found») and the reviews were inconclusive from
            # 19:24 UTC on. The tester's Playwright runs from this checkout, so it is built here as well; a failed build
            # is reported like a failed install.
            if build_cmd:
                rc = _run(build_cmd, dest, BUILD_TIMEOUT_S, log_fh)
                if rc != 0:
                    raise PlaytestError(f"build failed (exit {rc}): {build_cmd}\n" + _tail(log_path))
            log_fh.write(f"\ndeployed host {live} in use; no local server\n")
            log_fh.close()
            return Server(base_url=live, port=None, dest=dest, log_path=log_path, proc=None, local=False, notes=[f"deployed host {live} used; no local server"])
        if build_cmd:
            rc = _run(build_cmd, dest, BUILD_TIMEOUT_S, log_fh)
            if rc != 0:
                raise PlaytestError(f"build failed (exit {rc}): {build_cmd}\n" + _tail(log_path))
        # the port is picked right before the server starts, not before install and build: with cycles of several
        # products running side by side (F17) a port chosen minutes earlier may have been taken by another play-test
        port = free_port()
        cmd = command_for_port(product.run_cmd, port)
        env = clean_env()
        if force_local:
            # a server the review plays INSTEAD of the deployed host gets the product's test settings: without them it
            # would call the real text provider without a key and write into the default data folder
            (work / "server-data").mkdir(parents=True, exist_ok=True)
            env.update(test_env(product, port, work / "server-data"))
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


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_group(p: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(p.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def stop(server: Server) -> None:
    """Stop the server; returns only after the process has exited and the port is closed (bounded by timeouts)."""
    p = server.proc
    if p is None:
        return
    if p.poll() is None:
        _kill_group(p, 15)
        try:
            p.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill_group(p, 9)
            p.wait(timeout=STOP_GRACE_S)
    port = server.port
    if not port:
        return
    deadline = time.monotonic() + STOP_GRACE_S
    while _port_open(port) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _port_open(port):  # a child outside the leader still holds the port
        _kill_group(p, 9)
        deadline = time.monotonic() + STOP_GRACE_S
        while _port_open(port) and time.monotonic() < deadline:
            time.sleep(0.05)


def _tail(p: Path, n: int = LOG_TAIL_CHARS) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""
