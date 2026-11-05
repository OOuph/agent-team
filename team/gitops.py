"""Git operations on a product's site repository checkout (data/repos/<slug>): clone, task branch, merge, push."""
from __future__ import annotations

import logging
import os
import re
import time
import subprocess
from pathlib import Path

log = logging.getLogger("team.git")


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True, timeout: int = 300) -> str:
    cp = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and cp.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {cp.stderr.strip()[-500:]}")
    return cp.stdout.strip()


def _auth_url(url: str) -> str:
    """https://github.com/o/r.git → with the token from GITHUB_TOKEN when set; ssh urls untouched."""
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and url.startswith("https://github.com/"):
        return url.replace("https://github.com/", f"https://x-access-token:{tok}@github.com/", 1)
    return url


def redact(s: str) -> str:
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", s)


def ensure_clone(repo_dir: Path, url: str, branch: str = "main") -> Path:
    if (repo_dir / ".git").exists():
        git(repo_dir, "remote", "set-url", "origin", _auth_url(url))
        git(repo_dir, "fetch", "--prune", "origin")
        return repo_dir
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(["git", "clone", "--branch", branch, _auth_url(url), str(repo_dir)], capture_output=True, text=True, timeout=600)
    if cp.returncode != 0:
        raise GitError("git clone: " + redact(cp.stderr.strip()[-500:]))
    git(repo_dir, "config", "user.name", os.environ.get("TEAM_GIT_NAME", "agent-team"))
    git(repo_dir, "config", "user.email", os.environ.get("TEAM_GIT_EMAIL", "agent-team@formachines.ai"))
    return repo_dir


def init_local(repo_dir: Path, branch: str = "main") -> Path:
    """An empty local repository (tests and products without a remote yet)."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        git(repo_dir, "init", "-q", "-b", branch)
        git(repo_dir, "config", "user.name", "agent-team")
        git(repo_dir, "config", "user.email", "agent-team@formachines.ai")
    return repo_dir


def has_remote(repo_dir: Path) -> bool:
    return bool(git(repo_dir, "remote", check=False))


def start_task_branch(repo_dir: Path, task_id: str, base: str = "main") -> str:
    """A task branch from the latest base, or the earlier attempt's branch with base merged in; uncommitted leftovers are discarded."""
    br = f"task/{task_id}"
    git(repo_dir, "reset", "--hard", check=False)
    git(repo_dir, "clean", "-fdq", check=False)
    if has_remote(repo_dir):
        git(repo_dir, "fetch", "origin")
        git(repo_dir, "checkout", "-q", "-B", base, f"origin/{base}")
    else:
        git(repo_dir, "checkout", "-q", base)
    base_ref = f"origin/{base}" if has_remote(repo_dir) else base
    # A retry continues the earlier attempt: reuse task/<id> (local, else origin) when it has commits ahead of base.
    for cand in (br, f"origin/{br}"):
        if git(repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{br}" if cand == br else f"refs/remotes/{cand}", check=False) \
                and git(repo_dir, "rev-list", "--count", f"{base_ref}..{cand}", check=False) not in ("", "0"):
            git(repo_dir, "checkout", "-q", "-B", br, cand)
            try:
                git(repo_dir, "merge", "-q", "--no-edit", base_ref)
                return br
            except GitError:
                log.warning("task branch %s conflicts with %s; keeping the earlier attempt's side", br, base)
                git(repo_dir, "merge", "--abort", check=False)
                git(repo_dir, "reset", "--hard", check=False)
            try:
                git(repo_dir, "merge", "-q", "--no-edit", "-X", "ours", base_ref)
                return br
            except GitError:
                git(repo_dir, "merge", "--abort", check=False)
                git(repo_dir, "reset", "--hard", check=False)
            # last resort (e.g. modify/delete): base content, but the earlier commit stays in the history of the branch
            git(repo_dir, "checkout", "-q", "-B", br, base_ref)
            git(repo_dir, "merge", "-q", "--no-edit", "-s", "ours", cand)
            return br
    git(repo_dir, "checkout", "-q", "-B", br)
    return br


def readonly_checkout(repo_dir: Path, sha: str, dest: Path) -> Path:
    """A detached copy of `sha` in `dest` with every file made read-only (reviewer of repo_only tasks). No remote, no .git
    write access, so the role can neither change the checkout nor fetch anything."""
    import shutil
    import stat
    if dest.exists():
        os.chmod(dest, 0o700)
        for r, ds, fs in os.walk(dest):
            for n in ds:
                os.chmod(os.path.join(r, n), 0o700)
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(repo_dir, "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(repo_dir), str(dest))
    git(dest, "checkout", "--quiet", "--detach", sha)
    git(dest, "remote", "remove", "origin", check=False)
    for r, ds, fs in os.walk(dest):
        for n in fs:
            fp = os.path.join(r, n)
            if not os.path.islink(fp):
                os.chmod(fp, stat.S_IMODE(os.lstat(fp).st_mode) & ~0o222)
        for n in ds:
            os.chmod(os.path.join(r, n), 0o555)
    os.chmod(dest, 0o555)
    return dest


def head(repo_dir: Path) -> str:
    return git(repo_dir, "rev-parse", "HEAD")


def commits_ahead(repo_dir: Path, base: str = "main") -> int:
    ref = f"origin/{base}" if has_remote(repo_dir) else base
    out = git(repo_dir, "rev-list", "--count", f"{ref}..HEAD", check=False)
    return int(out) if out.isdigit() else 0


def dirty(repo_dir: Path) -> bool:
    return bool(git(repo_dir, "status", "--porcelain", check=False))


def dirty_tracked(repo_dir: Path) -> bool:
    """Modified tracked files only: untracked state files (team.db-wal, decisions.md) must not block a fast-forward."""
    return bool(git(repo_dir, "status", "--porcelain", "--untracked-files=no", check=False))


def merge_and_push(repo_dir: Path, task_id: str, base: str = "main") -> str:
    """task/<id> → base with a merge commit, push when a remote exists. Returns the new base head."""
    br = f"task/{task_id}"
    if dirty(repo_dir):
        git(repo_dir, "add", "-A")
        git(repo_dir, "commit", "-q", "-m", f"{task_id}: leftover changes committed by the dispatcher")
    git(repo_dir, "checkout", "-q", base)
    if has_remote(repo_dir):
        git(repo_dir, "pull", "-q", "--ff-only", "origin", base)
    git(repo_dir, "merge", "--no-ff", "-q", "-m", f"Merge {br}: released by the dispatcher", br)
    if has_remote(repo_dir):
        git(repo_dir, "push", "-q", "origin", base)
    sha = head(repo_dir)
    git(repo_dir, "branch", "-D", br, check=False)
    return sha


def revert_merge(repo_dir: Path, sha: str, base: str = "main") -> str:
    git(repo_dir, "checkout", "-q", base)
    git(repo_dir, "revert", "-q", "-m", "1", "--no-edit", sha)
    if has_remote(repo_dir):
        git(repo_dir, "push", "-q", "origin", base)
    return head(repo_dir)


def log_since(repo_dir: Path, n: int = 10) -> str:
    return git(repo_dir, "log", f"-{n}", "--oneline", check=False)


RETRIES = 3
MAX_TRANSIENT = 3  # consecutive transient push/PR failures (each already retried RETRIES times) before the task is blocked (F-0100)
# "The requested URL returned error: 503", "HTTP Error 502", "HTTP 502", "HTTP/2 504", "status 500", "remote: ... 503"
_TRANSIENT_RE = re.compile(
    r"(?:returned error:|\berror:|\bstatus(?: code)?:?|\bHTTP(?:/[\d.]+)?(?: Error:?)?)\s*5\d\d\b"
    r"|\bremote:[^\n]*\b5\d\d\b", re.I | re.M)


def is_transient(exc: BaseException) -> bool:
    """HTTP 5xx from GitHub (API or git over https): the commit is intact, trying again later is safe (F-0100)."""
    return bool(_TRANSIENT_RE.search(str(exc)))


def push_branch(repo_dir: Path, task_id: str) -> str:
    """Push task/<id> to origin (force-with-lease: a retry replaces the previous attempt). Returns the branch head."""
    br = f"task/{task_id}"
    if dirty(repo_dir):
        git(repo_dir, "add", "-A")
        git(repo_dir, "commit", "-q", "-m", f"{task_id}: leftover changes committed by the dispatcher")
    for attempt in range(RETRIES):
        try:
            git(repo_dir, "push", "-q", "--force-with-lease", "-u", "origin", br)
            break
        except GitError as exc:
            if attempt == RETRIES - 1 or not is_transient(exc):
                raise
            time.sleep(5 * 3 ** attempt)
    return head(repo_dir)


def _gh_repo(url: str) -> str:
    m = re.search(r"github\.com[/:]([^/]+/[^/.]+)", url)
    if not m:
        raise GitError(f"not a GitHub url: {url}")
    return m.group(1)


def _gh(method: str, path: str, body: dict | None = None) -> dict:
    import json
    import urllib.request
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise GitError("GITHUB_TOKEN not set")
    req = urllib.request.Request(f"https://api.github.com{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "agent-team"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        raise GitError(f"github {method} {path}: {redact(str(exc))[:300]}") from exc


def open_pull_request(repo_url: str, task_id: str, base: str, title: str, body: str) -> str:
    """One PR per task branch; an existing open PR for the branch is reused. Returns its html url."""
    repo = _gh_repo(repo_url)
    owner = repo.split("/")[0]
    existing = _gh("GET", f"/repos/{repo}/pulls?state=open&head={owner}:task/{task_id}")
    if isinstance(existing, list) and existing:
        return str(existing[0].get("html_url"))
    last: GitError | None = None
    for attempt in range(RETRIES):
        try:
            r = _gh("POST", f"/repos/{repo}/pulls", {"title": title[:200], "head": f"task/{task_id}", "base": base, "body": body[:60000]})
            break
        except GitError as exc:
            # 04.11 T-0035: GitHub answered 502 to the POST yet opened PR #65; the task went blocked (F-0100).
            # Look the branch up, then retry on 5xx with backoff (5 s, 15 s, 45 s).
            last = exc
            time.sleep(5 * 3 ** attempt)
            again = _gh("GET", f"/repos/{repo}/pulls?state=open&head={owner}:task/{task_id}")
            if isinstance(again, list) and again:
                log.warning("POST /pulls failed for task/%s but the PR exists: %s", task_id, again[0].get("html_url"))
                return str(again[0].get("html_url"))
            if not is_transient(exc):
                raise
    else:
        raise last  # type: ignore[misc]
    return str(r.get("html_url"))


def pull_request_state(pr_url: str) -> str:
    """open / merged / closed for a PR html url."""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not m:
        return "unknown"
    r = _gh("GET", f"/repos/{m.group(1)}/pulls/{m.group(2)}")
    return "merged" if r.get("merged") else str(r.get("state") or "unknown")


def changed_files(repo_dir: Path, base: str = "main") -> list[str]:
    ref = f"origin/{base}" if has_remote(repo_dir) else base
    out = git(repo_dir, "diff", "--name-only", f"{ref}...HEAD", check=False)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _pr_parts(pr_url: str) -> tuple[str, str]:
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not m:
        raise GitError(f"not a pull request url: {pr_url}")
    return m.group(1), m.group(2)


def comment_pull_request(pr_url: str, body: str) -> None:
    repo, num = _pr_parts(pr_url)
    _gh("POST", f"/repos/{repo}/issues/{num}/comments", {"body": body[:60000]})


def merge_pull_request(pr_url: str, method: str = "merge") -> str:
    """Merge via the API (the dispatcher never force-pushes main). Returns the merge sha."""
    repo, num = _pr_parts(pr_url)
    r = _gh("PUT", f"/repos/{repo}/pulls/{num}/merge", {"merge_method": method})
    if not r.get("merged"):
        raise GitError(f"merge refused: {r.get('message')}")
    return str(r.get("sha") or "")


def update_running_checkout(root: Path, branch: str = "main") -> str | None:
    """Fast-forward the dispatcher's own checkout to origin/<branch> when it is a clean git clone; else leave it."""
    if not (root / ".git").exists():
        log.warning("running checkout %s is not a git clone; code not updated", root)
        return None
    if dirty_tracked(root):
        log.warning("running checkout %s has local changes to tracked files; code not updated", root)
        return None
    git(root, "fetch", "-q", "origin", branch)
    git(root, "merge", "-q", "--ff-only", f"origin/{branch}")
    return head(root)
