"""A ready `node_modules` per lock file instead of `npm ci` in every run and every check (27.11, night analysis of
ai-battles: `npm ci` took 198 s per call with six runs at once, 1.5 calls per developer run, and once more in every
review lane; the load stood at 9–10 on 8 cores).

The dependencies of a commit are decided by its `package-lock.json`. For each lock file seen, the dispatcher installs
once into `data/cache/node_modules/<slug>/<sha256 of the lock>/` (only `package.json`, the lock and `.npmrc` are copied
there, so the install cannot read the task's code) and from then on COPIES that folder into a checkout that has none.
A copy, not hard links: a test or a build tool that writes inside `node_modules` changes only its own checkout. A
changed lock file is another key — the cache is built again by itself; the newest `KEEP` caches of a product stay.

Commands of product.yaml that begin with the install (`npm ci && npm run build && npm test`) are run without it when
the checkout was seeded (`strip_install`). Anything that goes wrong — no lock file, a failed install, a failed copy —
leaves the checkout as it was and the commands unchanged: the old path."""
from __future__ import annotations

import fcntl
import hashlib
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("team.nodecache")

# ------------------------------------------------------------------ parameters
LOCK_FILE = "package-lock.json"            # what decides the content of node_modules
MANIFESTS = ("package.json", LOCK_FILE, ".npmrc")   # copied into the cache folder for the install; nothing else of the checkout
MODULES = "node_modules"
MARK = ".team-node-cache"                  # file inside a seeded node_modules: the key it was copied from
DEFAULT_INSTALL = "npm ci --no-audit --no-fund"
INSTALL_TIMEOUT_S = 900
KEEP = 3                                   # caches kept per product (newest by use)
FAILED_RETRY_S = 1800                      # a lock file whose install failed is not tried again for this long
INSTALL_RE = re.compile(r"^\s*npm\s+(ci|install|i)\b[^&;|]*$")


def key_of(checkout: Path) -> str | None:
    """sha256 of the checkout's lock file (first 20 hex), None without one."""
    f = Path(checkout) / LOCK_FILE
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest()[:20] if f.is_file() else None
    except OSError:
        return None


def strip_install(cmd: str) -> str:
    """`npm ci && npm run build && npm test` → `npm run build && npm test`: the `&&` segments that only install are
    dropped. A command that is nothing but the install becomes `true`; anything not joined by `&&` is left alone."""
    if not cmd or ";" in cmd or "||" in cmd:
        return cmd
    parts = [p.strip() for p in cmd.split("&&")]
    kept = [p for p in parts if not INSTALL_RE.match(p)]
    return " && ".join(kept) if kept else "true"


def seeded(checkout: Path) -> bool:
    """Was this checkout's node_modules copied from the cache for the lock file it has NOW?"""
    try:
        return (Path(checkout) / MODULES / MARK).read_text(encoding="utf-8").strip() == (key_of(checkout) or "-")
    except OSError:
        return False


def ensure(cache_root: Path, slug: str, checkout: Path, install_cmd: str = "", env: dict[str, str] | None = None) -> Path | None:
    """The cache folder for the checkout's lock file, built now when it does not exist (one builder at a time per key,
    across processes). None: no lock file, or the install failed (logged; not tried again for FAILED_RETRY_S)."""
    key = key_of(checkout)
    if not key:
        return None
    base = Path(cache_root) / slug
    dest = base / key
    if (dest / MODULES / MARK).is_file():
        return dest
    failed = base / f"{key}.failed"
    try:
        if failed.is_file() and time.time() - failed.stat().st_mtime < FAILED_RETRY_S:
            return None
    except OSError:
        pass
    base.mkdir(parents=True, exist_ok=True)
    with (base / f"{key}.lock").open("a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            if (dest / MODULES / MARK).is_file():          # another lane or process built it while we waited
                return dest
            tmp = base / f"{key}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            for name in MANIFESTS:
                if (Path(checkout) / name).is_file():
                    shutil.copy2(Path(checkout) / name, tmp / name)
            cmd = install_cmd or DEFAULT_INSTALL
            t0 = time.time()
            try:
                cp = subprocess.run(cmd, shell=True, cwd=str(tmp), capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, env=env)
                ok, tail = cp.returncode == 0 and (tmp / MODULES).is_dir(), (cp.stdout + cp.stderr)[-400:]
            except subprocess.TimeoutExpired:
                ok, tail = False, f"timed out after {INSTALL_TIMEOUT_S}s"
            if not ok:
                log.warning("%s: node_modules cache %s not built (`%s`): %s", slug, key, cmd, tail.strip()[-300:])
                shutil.rmtree(tmp, ignore_errors=True)
                failed.write_text(tail, encoding="utf-8")
                return None
            (tmp / MODULES / MARK).write_text(key, encoding="utf-8")
            shutil.rmtree(dest, ignore_errors=True)
            tmp.rename(dest)
            log.info("%s: node_modules cache %s built in %.0fs (`%s`)", slug, key, time.time() - t0, cmd)
            _prune(base, keep=dest)
            return dest
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _prune(base: Path, keep: Path) -> None:
    dirs = sorted((d for d in base.iterdir() if d.is_dir() and not d.name.endswith(".tmp")), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP:]:
        if d != keep:
            shutil.rmtree(d, ignore_errors=True)
            log.info("node_modules cache %s removed (more than %d kept)", d.name, KEEP)


def _ignored(checkout: Path) -> bool:
    """True when the checkout is not a git work tree, or git ignores node_modules there. A repository that does not
    ignore it would get the copied folder committed by the dispatcher's «leftover changes» commit."""
    if not (checkout / ".git").exists():
        return True
    try:
        cp = subprocess.run(["git", "check-ignore", "-q", f"{MODULES}/.probe"], cwd=str(checkout), capture_output=True, timeout=20)
        return cp.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def seed(cache_root: Path, slug: str, checkout: Path, install_cmd: str = "", env: dict[str, str] | None = None) -> bool:
    """Give the checkout a node_modules for its lock file. True when it has one from the cache after the call (copied
    now, or seeded earlier for the same lock). A node_modules that is there and was not seeded for this lock — the
    role installed something itself, or the lock changed — is left alone and the answer is False. Never raises."""
    try:
        checkout = Path(checkout)
        if seeded(checkout):
            return True
        if (checkout / MODULES).exists():
            return False
        if not _ignored(checkout):
            log.warning("%s: %s does not git-ignore %s: not seeded (the dispatcher commits leftovers of a checkout)", slug, checkout, MODULES)
            return False
        src = ensure(cache_root, slug, checkout, install_cmd, env)
        if src is None:
            return False
        t0 = time.time()
        tmp = checkout / f"{MODULES}.seeding"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src / MODULES, tmp, symlinks=True)
        tmp.rename(checkout / MODULES)
        try:
            (src / MODULES / MARK).touch()                 # «used now», for the pruning order
        except OSError:
            pass
        log.info("%s: node_modules copied from the cache %s into %s in %.1fs", slug, src.name, checkout, time.time() - t0)
        return True
    except Exception as exc:  # noqa: BLE001 — the cache is an optimisation: without it the commands install as before
        log.warning("%s: node_modules not seeded into %s: %s", slug, checkout, str(exc)[:300])
        return False
