"""Git operations on a product's repository: the clone (data/repos/<slug>), one worktree per task
(data/work/<slug>/wt/<task id>), merge to the base branch, push.

Since 21.11 (parallel lanes, team/lanes.py) a repository role does not work in the clone itself: every task gets its own
`git worktree` on `task/<id>`, so the developer and the designer of one product can work at the same time. The clone
stays on the base branch and is where the dispatcher merges; everything that touches the clone (fetch, worktree add /
remove, merge, push) is called under the cycle's repository lock — nothing in this module takes that lock itself.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
import subprocess
from pathlib import Path

log = logging.getLogger("team.git")

CONFLICT_DIFF_CHARS = 4000              # the other side's diff of the conflicting files, as shown to the task's author
CONFLICT_FILES_MAX = 20                 # conflicting files listed in a merge-conflict record
MESSAGES_ENV = {"LC_ALL": "C", "LANGUAGE": "C"}     # for a command whose output is parsed or shown to a role: git's own words, not the host's locale


class GitError(RuntimeError):
    pass


class MergeConflict(GitError):
    """`task/<id>` does not merge into the base branch: both changed the same lines. The merge was aborted, the base
    branch is untouched; `files` are the conflicting paths, `text` is what the task's author gets (git's CONFLICT lines
    and what the base branch changed in those files since the task branched off)."""

    def __init__(self, task_id: str, base: str, files: list[str], text: str):
        super().__init__(f"merge conflict: task/{task_id} into {base}: {', '.join(files[:CONFLICT_FILES_MAX]) or '?'}")
        self.task_id, self.base, self.files, self.text = task_id, base, list(files), text


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
    """A task branch from the latest base, or the earlier attempt's branch with base merged in; uncommitted leftovers are
    discarded. The branch is checked out IN `repo_dir` itself — the layout before 21.11, kept for a repository that is
    used by one role at a time; the cycle uses `start_task_worktree`."""
    br = f"task/{task_id}"
    git(repo_dir, "reset", "--hard", check=False)
    git(repo_dir, "clean", "-fdq", check=False)
    if has_remote(repo_dir):
        git(repo_dir, "fetch", "origin")
        git(repo_dir, "checkout", "-q", "-B", base, f"origin/{base}")
    else:
        git(repo_dir, "checkout", "-q", base)
    base_ref = f"origin/{base}" if has_remote(repo_dir) else base
    return _checkout_task_branch(repo_dir, br, base, base_ref)


def _checkout_task_branch(repo_dir: Path, br: str, base: str, base_ref: str, start_point: str | None = None) -> str:
    """In the checkout `repo_dir` (the clone or a task's worktree; clean): stand on `br` — the earlier attempt's branch
    with the base merged in, or a new branch from `start_point` (default: the latest base)."""
    # A retry continues the earlier attempt: reuse task/<id> (local, else origin) when it has commits ahead of base.
    for cand in (br, f"origin/{br}"):
        if git(repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{br}" if cand == br else f"refs/remotes/{cand}", check=False) \
                and git(repo_dir, "rev-list", "--count", f"{base_ref}..{cand}", check=False) not in ("", "0"):
            git(repo_dir, "checkout", "-q", "-B", br, cand)
            try:
                git(repo_dir, "merge", "-q", "--no-edit", base_ref)
                return br
            except GitError:
                # ai-battles 21.11 (F39): the fallback used to be `merge -X ours` (then `-s ours`) — the base's side of every
                # conflicting hunk was dropped WITHOUT a trace, the branch then merged cleanly into the base and the base lost
                # the other tasks' work (T-0038 erased 45 lines of CSS, 99 lines of tests and a client change that T-0068,
                # T-0071 and a hotfix had released hours before). A conflict is never resolved silently any more: the
                # branch stays as the earlier attempt left it, WITHOUT the base. The merge into the base at the end of the
                # run then raises MergeConflict, nothing is merged, and the role gets the MERGE CONFLICT block and merges
                # the base itself, by hand, keeping both sides.
                log.warning("task branch %s conflicts with %s: left without the base merge — the role reconciles it, "
                            "nothing is resolved automatically", br, base)
                git(repo_dir, "merge", "--abort", check=False)
                git(repo_dir, "reset", "--hard", check=False)
                return br
    git(repo_dir, "checkout", "-q", "-B", br, start_point or base_ref)
    return br


# ------------------------------------------------------------------ one worktree per task (owner 21.11, parallel lanes)

def current_branch(repo_dir: Path) -> str:
    """The branch the checkout stands on; `HEAD` when detached, "" when it cannot be read."""
    return git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD", check=False)


def worktrees(repo_dir: Path) -> dict[Path, str]:
    """Registered worktrees of the clone, the clone itself included: resolved path → branch ("" when detached)."""
    out: dict[Path, str] = {}
    path: Path | None = None
    for ln in git(repo_dir, "worktree", "list", "--porcelain", check=False).splitlines():
        if ln.startswith("worktree "):
            path = Path(ln[len("worktree "):]).resolve()
            out[path] = ""
        elif ln.startswith("branch ") and path is not None:
            out[path] = ln[len("branch "):].replace("refs/heads/", "", 1)
    return out


def park_on_base(repo_dir: Path, base: str = "main") -> bool:
    """Bring the clone itself to a clean checkout of the base branch; returns True when something had to be changed.

    In the worktree layout the clone only ever stands on the base branch. It does not when (a) the product was mid-task
    at the update from the layout before 21.11 — `task/<id>` is checked out in the clone, possibly with a wip commit on
    it; the branch and its commits stay, only the clone leaves it, so the task's worktree can take the branch — or
    (b) a merge was cut short by a killed process. Uncommitted leftovers are discarded, as `start_task_branch` always
    did at the next start. With a remote the clone's base branch is then set to what origin has (as of the last fetch),
    again as `start_task_branch` did: the owner's own pushes reach the clone's tree, and a merge whose push failed —
    its task is blocked, not released — does not ride along with the next release. Never called between a merge and
    its push."""
    remote = has_remote(repo_dir)
    cur = current_branch(repo_dir)
    changed = False
    if cur != base or dirty_tracked(repo_dir):
        left = [ln for ln in git(repo_dir, "status", "--porcelain", check=False).splitlines() if ln.strip()]
        log.warning("clone %s stands on %s with %d uncommitted path(s): parked on %s (old layout or an interrupted merge; "
                    "the branch and its commits are kept)", repo_dir, cur or "?", len(left), base)
        git(repo_dir, "merge", "--abort", check=False)
        git(repo_dir, "reset", "--hard", check=False)
        git(repo_dir, "clean", "-fdq", check=False)
        if git(repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{base}", check=False):
            git(repo_dir, "checkout", "-q", base)
        else:
            git(repo_dir, "checkout", "-q", "-B", base, f"origin/{base}" if remote else "HEAD")
        changed = True
    theirs = git(repo_dir, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{base}", check=False) if remote else ""
    if theirs and head(repo_dir) != theirs:
        log.info("clone %s: %s set to origin/%s (%s)", repo_dir, base, base, theirs[:10])
        git(repo_dir, "checkout", "-q", "-B", base, f"origin/{base}")
    return changed


def start_task_worktree(repo_dir: Path, wt_dir: Path, task_id: str, base: str = "main", start_point: str | None = None) -> str:
    """The task's own checkout: a `git worktree` of the clone `repo_dir` at `wt_dir`, standing on `task/<id>` — a new
    branch from the latest base (or from `start_point`), or the earlier attempt's branch with the base merged in (same
    rules as `start_task_branch`). An existing worktree is reused: its commits (a wip commit of a stopped run included)
    and its ignored files (node_modules, build output) stay, uncommitted leftovers are discarded. Returns the branch.
    Call under the cycle's repository lock: it fetches and may park the clone on the base branch."""
    br = f"task/{task_id}"
    wt_dir = Path(wt_dir)
    remote = has_remote(repo_dir)
    if remote:
        git(repo_dir, "fetch", "origin")
    base_ref = f"origin/{base}" if remote else base
    if park_on_base(repo_dir, base):
        log.info("worktree %s: the clone %s was parked on %s first", task_id, repo_dir, base)
    git(repo_dir, "worktree", "prune", check=False)                  # forget worktrees whose directory is gone
    known = worktrees(repo_dir)
    if wt_dir.exists() and wt_dir.resolve() not in known:
        log.warning("worktree %s: %s exists but is not a worktree of %s — removed and created again", task_id, wt_dir, repo_dir)
        shutil.rmtree(wt_dir, ignore_errors=True)
    if not wt_dir.exists():
        wt_dir.parent.mkdir(parents=True, exist_ok=True)
        git(repo_dir, "worktree", "add", "--detach", str(wt_dir), base_ref)
        log.info("worktree %s: created at %s from %s", task_id, wt_dir, base_ref)
    else:
        git(wt_dir, "reset", "--hard", check=False)                  # also ends a merge the role left half-way
        git(wt_dir, "clean", "-fdq", check=False)
        log.info("worktree %s: reused %s (on %s)", task_id, wt_dir, current_branch(wt_dir) or "?")
    return _checkout_task_branch(wt_dir, br, base, base_ref, start_point)


def remove_task_worktree(repo_dir: Path, wt_dir: Path, task_id: str | None = None, delete_branch: bool = False) -> bool:
    """Remove a task's worktree (uncommitted files with it); with `delete_branch` also `task/<id>`. The branch is kept
    otherwise: commits of an unfinished task stay reachable. Returns True when a directory was removed."""
    wt_dir = Path(wt_dir)
    existed = wt_dir.exists()
    if existed:
        try:
            git(repo_dir, "worktree", "remove", "--force", str(wt_dir))
        except GitError as exc:
            log.warning("worktree %s: `git worktree remove` failed (%s) — directory deleted directly", wt_dir, redact(str(exc))[:200])
            shutil.rmtree(wt_dir, ignore_errors=True)
    git(repo_dir, "worktree", "prune", check=False)
    if delete_branch and task_id:
        git(repo_dir, "branch", "-D", f"task/{task_id}", check=False)
    if existed:
        log.info("worktree %s removed%s", wt_dir, f" with branch task/{task_id}" if delete_branch and task_id else "")
    return existed


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


WIP_PREFIX = "wip:"                     # subject prefix of a service commit the dispatcher makes for an unfinished run
WIP_LOCK_WAIT_S = 2.0                   # a killed run may leave .git/index.lock behind: wait this long, remove it, retry once
WIP_STAT_CHARS = 1500                   # `git show --stat` of a wip commit as shown in prompts and questions


def wip_message(task_id: str, why: str) -> str:
    return f"{WIP_PREFIX} {task_id} — {why}"


def save_wip(repo_dir: Path, task_id: str, why: str) -> dict | None:
    """Commit everything in the working tree of the checkout `repo_dir` (the task's worktree; before the worktree
    layout the clone itself) to `task/<id>` as a service commit (owner 21.11, F23: a run stopped by the
    time limit left its work uncommitted and the next attempt's `reset --hard` destroyed it). Returns
    `{commit, files, stat}`, or None when the tree is clean. Only ever commits on the task branch — a checkout that
    stands on any other branch is refused (GitError), so a wip can never land on the base branch — and never pushes."""
    br = f"task/{task_id}"
    cur = git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if cur != br:
        raise GitError(f"wip not saved: the checkout is on {cur or '?'}, not on {br}")
    if not dirty(repo_dir):
        log.info("wip %s: working tree is clean, nothing to save", task_id)
        return None
    for attempt in (1, 2):
        try:
            git(repo_dir, "add", "-A")
            git(repo_dir, "commit", "-q", "--no-verify", "-m", wip_message(task_id, why))   # no hooks: the tree may not even build
            break
        except GitError as exc:
            lock = _index_lock(repo_dir)
            if attempt == 2 or "index.lock" not in str(exc):
                raise
            log.warning("wip %s: %s — waiting %.0fs, removing the stale lock and retrying", task_id, redact(str(exc))[:200], WIP_LOCK_WAIT_S)
            time.sleep(WIP_LOCK_WAIT_S)
            if lock.exists():
                lock.unlink()
    sha = head(repo_dir)
    files = [ln for ln in git(repo_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha, check=False).splitlines() if ln.strip()]
    log.info("wip %s: %d file(s) saved in %s on %s", task_id, len(files), sha[:10], br)
    return {"commit": sha, "files": len(files), "stat": commit_stat(repo_dir, sha)}


def _index_lock(repo_dir: Path) -> Path:
    """The index lock of a checkout: `.git/index.lock` in a clone, `<clone>/.git/worktrees/<name>/index.lock` in a worktree."""
    rel = git(repo_dir, "rev-parse", "--git-path", "index.lock", check=False)
    p = Path(rel) if rel else Path(".git") / "index.lock"
    return p if p.is_absolute() else Path(repo_dir) / p


def commit_stat(repo_dir: Path, sha: str) -> str:
    """`git show --stat` of one commit without the message, cut to WIP_STAT_CHARS; empty when the commit is unknown."""
    out = git(repo_dir, "show", "--stat", "--format=", sha, check=False).strip()
    return out if len(out) <= WIP_STAT_CHARS else out[:WIP_STAT_CHARS] + "\n… [cut]"


def is_ancestor(repo_dir: Path, sha: str, ref: str = "HEAD") -> bool:
    """True when `sha` is in the history of `ref` (a wip commit that the task branch still holds)."""
    if not sha:
        return False
    cp = subprocess.run(["git", "merge-base", "--is-ancestor", sha, ref], cwd=str(repo_dir), capture_output=True, text=True, timeout=60)
    return cp.returncode == 0


def _conflict_text(repo_dir: Path, base: str, br: str, files: list[str], merge_out: str) -> str:
    """What the author of `br` needs to reconcile: git's CONFLICT lines and the base branch's own diff of those files."""
    lines = [ln.strip() for ln in merge_out.splitlines() if ln.startswith("CONFLICT")]
    out = "\n".join(lines) or "conflicting files: " + ", ".join(files)
    mb = git(repo_dir, "merge-base", base, br, check=False)
    if mb and files:
        theirs = git(repo_dir, "diff", f"{mb}..{base}", "--", *files[:CONFLICT_FILES_MAX], check=False)
        if len(theirs) > CONFLICT_DIFF_CHARS:
            theirs = theirs[:CONFLICT_DIFF_CHARS] + "\n… [cut]"
        log_ = git(repo_dir, "log", "--oneline", "-8", f"{mb}..{base}", "--", *files[:CONFLICT_FILES_MAX], check=False)
        out += (f"\n\ncommits on {base} that changed these files since the branch started:\n{log_ or '(none listed)'}"
                f"\n\nwhat {base} changed in them:\n{theirs or '(no diff)'}")
    return out


def merge_and_push(repo_dir: Path, task_id: str, base: str = "main", worktree: Path | None = None) -> str:
    """task/<id> → base with a merge commit in the clone `repo_dir`, push when a remote exists. Returns the new base head.

    `worktree`: the task's own checkout — its leftovers are committed there, the clone is parked on the base branch,
    and after the push the worktree and the branch are removed. Without it the branch is checked out in `repo_dir`
    itself (the layout before the worktrees). A merge that conflicts is aborted and raises `MergeConflict` (the base
    branch, the task branch and the worktree are left as they were); a push that fails undoes the local merge, so the
    base branch of the clone never holds what is not on origin. Call under the cycle's repository lock."""
    br = f"task/{task_id}"
    src = Path(worktree) if worktree is not None else repo_dir
    if dirty(src):
        git(src, "add", "-A")
        git(src, "commit", "-q", "-m", f"{task_id}: leftover changes committed by the dispatcher")
    if worktree is not None:
        park_on_base(repo_dir, base)
    else:
        git(repo_dir, "checkout", "-q", base)
    remote = has_remote(repo_dir)
    if remote:
        git(repo_dir, "pull", "-q", "--ff-only", "origin", base)
    before = head(repo_dir)
    # no -q: git's «CONFLICT (content): Merge conflict in <file>» lines are what the task's author is shown
    cp = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {br}: released by the dispatcher", br], cwd=str(repo_dir),
                        capture_output=True, text=True, timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **MESSAGES_ENV})
    if cp.returncode != 0:
        out = ((cp.stdout or "") + "\n" + (cp.stderr or "")).strip()
        files = [ln.strip() for ln in git(repo_dir, "diff", "--name-only", "--diff-filter=U", check=False).splitlines() if ln.strip()]
        git(repo_dir, "merge", "--abort", check=False)
        git(repo_dir, "reset", "--hard", before, check=False)
        if files:
            log.warning("merge %s into %s: conflict in %d file(s): %s", br, base, len(files), ", ".join(files[:CONFLICT_FILES_MAX]))
            raise MergeConflict(task_id, base, files, _conflict_text(repo_dir, base, br, files, out))
        raise GitError(f"git merge {br}: {out[-500:]}")
    if remote:
        try:
            git(repo_dir, "push", "-q", "origin", base)
        except GitError:
            git(repo_dir, "reset", "--hard", before, check=False)      # not released: the clone's base goes back to what origin has
            raise
    sha = head(repo_dir)
    if worktree is not None:
        remove_task_worktree(repo_dir, Path(worktree))
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


def diff_stat(repo_dir: Path, base: str = "main") -> str:
    ref = f"origin/{base}" if has_remote(repo_dir) else base
    return git(repo_dir, "diff", "--stat", f"{ref}...HEAD", check=False)


def tests_modified_or_deleted(repo_dir: Path, base: str = "main") -> list[str]:
    """Files under tests/ whose existing lines the branch changes: deleted, renamed, type-changed, or modified with at
    least one removed line. A new test file, or new tests appended to an existing file (0 lines removed), is not listed
    (owner 15.11: «M» alone flagged almost every PR — 13 sat waiting for a human for adding a test)."""
    ref = f"origin/{base}" if has_remote(repo_dir) else base
    out = set()
    st = git(repo_dir, "diff", "--name-status", "--diff-filter=DRT", f"{ref}...HEAD", "--", "tests/", check=False)
    for ln in st.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            out.add(parts[1].strip())
    ns = git(repo_dir, "diff", "--numstat", "--diff-filter=M", f"{ref}...HEAD", "--", "tests/", check=False)
    for ln in ns.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3 and (parts[1] == "-" or (parts[1].isdigit() and int(parts[1]) > 0)):
            out.add(parts[2].strip())
    return sorted(x for x in out if x.startswith("tests/"))


TESTS_REASON_RE = re.compile(r"(?m)^[ \t]*(?:[-*][ \t]+)?tests-change-reason:[ \t]*F-\d{4,}\b")


def has_tests_change_reason(body: str) -> bool:
    """The PR body has a line 'tests-change-reason: F-xxxx ...' (T-0058); a line without an F-id does not count."""
    return bool(TESTS_REASON_RE.search(body or ""))


def _pr_parts(pr_url: str) -> tuple[str, str]:
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not m:
        raise GitError(f"not a pull request url: {pr_url}")
    return m.group(1), m.group(2)


def comment_pull_request(pr_url: str, body: str) -> None:
    repo, num = _pr_parts(pr_url)
    _gh("POST", f"/repos/{repo}/issues/{num}/comments", {"body": body[:60000]})


def update_pull_request_body(pr_url: str, body: str) -> None:
    repo, num = _pr_parts(pr_url)
    _gh("PATCH", f"/repos/{repo}/pulls/{num}", {"body": body[:60000]})


AUTO_MERGE_LABEL = "auto-merged"          # owner 10.11: an automatic merge is visible from outside (label + commit text)


def merge_pull_request(pr_url: str, method: str = "merge", *, auto: bool = False) -> str:
    """Merge via the API (the dispatcher never force-pushes main). Returns the merge sha.

    ``auto=True``: the dispatcher merged without a human (tests + canary) — the merge commit says so and the PR gets the
    ``auto-merged`` label, so "merged without a human" can be checked on GitHub, where every merge shows the owner's token."""
    repo, num = _pr_parts(pr_url)
    body: dict = {"merge_method": method}
    if auto:
        body["commit_message"] = "Merged automatically by the agent-team dispatcher (tests and canary passed, no human review)."
    r = _gh("PUT", f"/repos/{repo}/pulls/{num}/merge", body)
    if not r.get("merged"):
        raise GitError(f"merge refused: {r.get('message')}")
    if auto:
        try:
            _gh("POST", f"/repos/{repo}/issues/{num}/labels", {"labels": [AUTO_MERGE_LABEL]})
        except GitError as exc:
            log.warning("could not label %s as %s: %s", pr_url, AUTO_MERGE_LABEL, redact(str(exc)))
    return str(r.get("sha") or "")


def closes_lines(links: list[str] | None, repo_url: str) -> list[str]:
    """"Closes #N" for every link to an issue of the same repository: GitHub then closes the friction issue on merge
    (owner 10.11: 75 of 76 friction issues stayed open although merged PRs had fixed 60 of them)."""
    try:
        repo = _gh_repo(repo_url).lower()
    except GitError:
        return []
    out: list[str] = []
    for l in links or []:
        m = re.search(r"github\.com/([^/]+/[^/#?]+)/issues/(\d+)", str(l))
        if m and m.group(1).lower() == repo and f"Closes #{m.group(2)}" not in out:
            out.append(f"Closes #{m.group(2)}")
    return out


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
