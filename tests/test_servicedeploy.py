"""Service deploy (game profile, slice 5; owner 20.11). deploy-service.sh runs against a fake systemd (a python
`systemctl` that starts/stops svc-run.sh itself), a fake nginx and a python http.server as the game; the dispatcher side
(team/servicedeploy.py, Cycle.deploy_after_merge, team deploy) runs against a fake deploy command. No root, no network."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from team import cli, config as C, gitops, servicedeploy
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = ROOT / "deploy" / "service"
RUN_CMD = "python3 -m http.server $PORT --bind 127.0.0.1"


def _free_ports(n: int) -> list[int]:
    out = []
    socks = []
    for _ in range(n):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); socks.append(s); out.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return out


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _exe(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


FAKE_SYSTEMCTL = '''\
    #!/usr/bin/env python3
    """fake systemd: start/stop/is-active of agent-team-svc@<slug>-<colour> run svc-run.sh in a process group."""
    import os, signal, subprocess, sys
    d = os.environ["FAKE_SYSTEMD_DIR"]; os.makedirs(d, exist_ok=True)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verb, unit = args[0], (args[1] if len(args) > 1 else "")
    open(os.path.join(d, "calls.log"), "a").write(" ".join(sys.argv[1:]) + "\\n")
    if unit in ("nginx", "") or "@" not in unit:
        sys.exit(0)
    inst = unit.split("@", 1)[1]
    pidf = os.path.join(d, inst + ".pid")
    def alive():
        try:
            pid = int(open(pidf).read()); os.kill(pid, 0); return pid
        except Exception:
            return None
    if verb == "start":
        if alive(): sys.exit(0)
        log = open(os.path.join(d, inst + ".log"), "ab")
        p = subprocess.Popen([os.environ["SVC_RUN"], inst], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        open(pidf, "w").write(str(p.pid))
    elif verb == "stop":
        pid = alive()
        if pid:
            try: os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError: pass
        if os.path.exists(pidf): os.remove(pidf)
    elif verb == "is-active":
        pid = alive()
        if pid:
            try:
                if os.waitpid(pid, os.WNOHANG) != (0, 0): pid = None
            except ChildProcessError:
                pass
        sys.exit(0 if pid else 3)
    sys.exit(0)
    '''


class Host:
    """A fake droplet in tmp: etc, var, state, nginx conf.d, fake binaries, a bare origin with one game commit."""

    def __init__(self, tmp: Path, run_cmd: str = RUN_CMD, build_cmd: str = "", nginx_ok: bool = True):
        self.tmp = tmp
        self.bin = tmp / "fakebin"; self.bin.mkdir()
        self.etc = tmp / "etc"; (self.etc / "services").mkdir(parents=True)
        self.var = tmp / "var"; self.state = tmp / "state"; self.confd = tmp / "confd"; self.services = tmp / "opt"
        self.systemd = tmp / "systemd"
        self.log = tmp / "deploy.log"
        self.svc_run = DEPLOY_DIR / "svc-run.sh"
        _exe(self.bin / "systemctl", FAKE_SYSTEMCTL)
        _exe(self.bin / "nginx", "#!/usr/bin/env bash\n" + ("exit 0\n" if nginx_ok else "echo 'nginx: [emerg] bad upstream' >&2; exit 1\n"))
        _exe(self.bin / "journalctl", "#!/usr/bin/env bash\necho '-- journal (fake) --'\n")
        self.ports = _free_ports(2)
        # origin: a bare repo the script clones; the game is a folder with healthz
        src = tmp / "src"; src.mkdir()
        gitops.git(src, "init", "-q", "-b", "main")
        gitops.git(src, "config", "user.email", "t@t"); gitops.git(src, "config", "user.name", "t")
        (src / "healthz").write_text('{"version": "one"}\n'); (src / "index.html").write_text("<h1>v1</h1>")
        gitops.git(src, "add", "-A"); gitops.git(src, "commit", "-q", "-m", "v1")
        self.src = src
        self.origin = tmp / "origin.git"
        gitops.git(tmp, "clone", "-q", "--bare", str(src), str(self.origin))
        (self.etc / "services" / "fleets.conf").write_text(
            f"HOST=fleets.example.test\nREPO={self.origin}\nBRANCH=main\nRUN_CMD='{run_cmd}'\nBUILD_CMD='{build_cmd}'\n"
            f"HEALTH_PATH=/healthz\nPORT_A={self.ports[0]}\nPORT_B={self.ports[1]}\n")

    def commit(self, version: str) -> str:
        (self.src / "healthz").write_text(f'{{"version": "{version}"}}\n'); (self.src / "index.html").write_text(f"<h1>{version}</h1>")
        gitops.git(self.src, "add", "-A"); gitops.git(self.src, "commit", "-q", "-m", version)
        gitops.git(self.src, "push", "-q", str(self.origin), "main")
        return gitops.head(self.src)

    def env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "ETC_DIR": str(self.etc), "SERVICES_ETC": str(self.etc / "services"),
                "SERVICES_DIR": str(self.services), "VAR_DIR": str(self.var), "STATE_DIR": str(self.state), "LOG_FILE": str(self.log),
                "LOCK_DIR": str(self.tmp / "lock"), "NGINX_CONFD": str(self.confd), "REQUIRE_ROOT": "0", "DRAIN_SEC": "0",
                "START_TIMEOUT_SEC": "20", "HEALTH_POLL_SEC": "0.3", "SVC_USER": os.environ.get("USER", "nobody"),
                "FAKE_SYSTEMD_DIR": str(self.systemd), "SVC_RUN": str(self.svc_run)}

    def deploy(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets", *args], capture_output=True, text=True, env=self.env(), timeout=120)

    def state_json(self) -> dict:
        return json.loads((self.state / "svc-fleets.json").read_text())

    def stop_all(self) -> None:
        for colour in ("a", "b"):
            subprocess.run([str(self.bin / "systemctl"), "stop", f"agent-team-svc@fleets-{colour}"], env=self.env(), check=False)

    def upstream_port(self) -> int | None:
        f = self.confd / "agent-team-svc-fleets.conf"
        if not f.exists():
            return None
        return int(f.read_text().split("server 127.0.0.1:")[1].split(";")[0])


@pytest.fixture
def host(tmp_path: Path):
    h = Host(tmp_path)
    yield h
    h.stop_all()


# ------------------------------------------------------------------ deploy-service.sh

def test_first_deploy_starts_colour_a_and_switches_nginx(host: Host):
    cp = host.deploy()
    assert cp.returncode == 0, cp.stdout + cp.stderr
    last = cp.stdout.strip().splitlines()[-1]
    assert last.startswith("== deployed slug=fleets sha=") and f"colour=a port={host.ports[0]}" in last and "url=https://fleets.example.test/" in last
    st = host.state_json()
    assert st["colour"] == "a" and st["port"] == host.ports[0] and st["previous"] is None and st["health_http_code"] == "200"
    assert _port_open(host.ports[0]) and host.upstream_port() == host.ports[0]
    rel = Path(st["release"])
    assert (rel / ".built").exists() and (rel / ".release-sha").read_text().strip() == st["sha"] and not (rel / ".git").exists()
    calls = (host.systemd / "calls.log").read_text()
    assert "start agent-team-svc@fleets-a" in calls and "reload nginx" in calls
    assert "action=deployed" in host.log.read_text()
    env = (host.var / "fleets" / "instances" / "fleets-a.env").read_text()
    assert f"PORT={host.ports[0]}" in env and "NODE_ENV=production" in env and "DATA_DIR=" in env


def test_second_deploy_goes_blue_green_and_the_same_sha_is_skipped(host: Host):
    assert host.deploy().returncode == 0
    first = host.state_json()
    cp = host.deploy()
    assert cp.returncode == 10 and "nothing to do" in cp.stdout            # same commit live and healthy
    sha2 = host.commit("two")
    cp = host.deploy("--sha", sha2)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    st = host.state_json()
    assert st["sha"] == sha2 and st["colour"] == "b" and st["port"] == host.ports[1]
    assert st["previous"]["sha"] == first["sha"] and st["previous"]["colour"] == "a"
    assert host.upstream_port() == host.ports[1] and _port_open(host.ports[1])
    time.sleep(0.5)
    assert not _port_open(host.ports[0])                                    # old colour retired after the drain
    assert "stop agent-team-svc@fleets-a" in (host.systemd / "calls.log").read_text()


def _busy(host: Host, colour_release: Path, n: int) -> None:
    """The fake game is a python http.server over the release directory: the busy document is a file in it."""
    d = colour_release / "_team"; d.mkdir(exist_ok=True)
    (d / "metrics.json").write_text(json.dumps({"now": {"rooms_playing": n, "online": n}}))


def test_deploy_waits_for_the_old_colour_to_finish_its_matches(host: Host):
    """ai-battles 22.11 (F42): a deploy stopped the old colour 5 s after the switch and froze the running battles. The old
    colour now stays until it reports no running match; the cap bounds the wait."""
    import threading
    assert host.deploy().returncode == 0
    old_release = Path(host.state_json()["release"]); old_port = host.state_json()["port"]
    _busy(host, old_release, 1)                                   # a match is running on the live colour
    host.commit("two")
    threading.Timer(2.0, _busy, args=(host, old_release, 0)).start()   # ... and ends two seconds into the drain
    env = {**host.env(), "DRAIN_POLL_SEC": "0.5", "DRAIN_MAX_SEC": "30"}
    t0 = time.time()
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env=env, timeout=120)
    took = time.time() - t0
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "still serves 1 match(es): waiting up to 30s" in cp.stdout and "finished its matches after" in cp.stdout
    assert 1.5 < took < 25                                        # it waited for the match, far short of the cap
    assert host.upstream_port() != old_port and "action=drained" in host.log.read_text()
    assert not _port_open(old_port)                              # only then was the old colour stopped


def test_deploy_drain_is_capped_and_a_colour_without_a_busy_document_is_not_waited_for(host: Host):
    assert host.deploy().returncode == 0
    old_release = Path(host.state_json()["release"]); old_port = host.state_json()["port"]
    _busy(host, old_release, 2)                                   # the matches never end
    host.commit("two")
    env = {**host.env(), "DRAIN_POLL_SEC": "0.5", "DRAIN_MAX_SEC": "2"}
    t0 = time.time()
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env=env, timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert 1.5 < time.time() - t0 < 20 and "stopping it anyway" in cp.stderr and "action=drain_cap" in host.log.read_text()
    assert not _port_open(old_port)
    # the new live colour has no busy document at all (a service that does not report matches): the third deploy does not wait
    host.commit("three")
    t0 = time.time()
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True,
                        env={**host.env(), "DRAIN_MAX_SEC": "60"}, timeout=120)
    assert cp.returncode == 0 and "does not report running matches" in cp.stdout and time.time() - t0 < 20


def test_rollback_returns_to_the_previous_release(host: Host):
    assert host.deploy().returncode == 0
    sha1 = host.state_json()["sha"]
    sha2 = host.commit("two")
    assert host.deploy("--sha", sha2).returncode == 0
    cp = host.deploy("--rollback")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    st = host.state_json()
    assert st["sha"] == sha1 and st["colour"] == "a" and st["previous"]["sha"] == sha2       # roll-forward stays possible
    assert "action=rolled_back" in host.log.read_text() and host.upstream_port() == host.ports[0]
    cp = host.deploy("--rollback")
    assert cp.returncode == 0 and host.state_json()["sha"] == sha2


def test_health_failure_leaves_the_live_release_untouched(host: Host):
    assert host.deploy().returncode == 0
    live = host.state_json()
    (host.etc / "services" / "fleets.conf").write_text((host.etc / "services" / "fleets.conf").read_text().replace(RUN_CMD, 'python3 -c "import sys; sys.exit(5)"'))
    sha2 = host.commit("broken")
    env = {**host.env(), "START_TIMEOUT_SEC": "4"}
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets", "--sha", sha2], capture_output=True, text=True, env=env, timeout=60)
    assert cp.returncode == 20 and "health" in cp.stderr and "live colour untouched" in cp.stderr
    assert host.state_json() == live and host.upstream_port() == host.ports[0] and _port_open(host.ports[0]) and not _port_open(host.ports[1])
    assert "action=health_failed" in host.log.read_text() and "stop agent-team-svc@fleets-b" in (host.systemd / "calls.log").read_text()


def test_build_runs_and_a_failed_build_stops_before_start(host: Host):
    conf = host.etc / "services" / "fleets.conf"
    conf.write_text(conf.read_text().replace("BUILD_CMD=''", "BUILD_CMD='echo built > built.txt'"))
    cp = host.deploy()
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (Path(host.state_json()["release"]) / "built.txt").read_text().strip() == "built"
    conf.write_text(conf.read_text().replace("BUILD_CMD='echo built > built.txt'", "BUILD_CMD='echo nope >&2; exit 2'"))
    sha2 = host.commit("two")
    cp = host.deploy("--sha", sha2)
    assert cp.returncode == 1 and "build failed" in cp.stderr and "nope" in cp.stderr
    assert "start agent-team-svc@fleets-b" not in (host.systemd / "calls.log").read_text() and host.state_json()["colour"] == "a"


def test_nginx_failure_restores_the_upstream_and_stops_the_new_colour(tmp_path: Path):
    h = Host(tmp_path, nginx_ok=True)
    try:
        assert h.deploy().returncode == 0
        _exe(h.bin / "nginx", "#!/usr/bin/env bash\necho 'nginx: [emerg] bad' >&2; exit 1\n")
        sha2 = h.commit("two")
        cp = h.deploy("--sha", sha2)
        assert cp.returncode == 21 and "nginx -t failed" in cp.stderr
        assert h.upstream_port() == h.ports[0] and h.state_json()["colour"] == "a"
        time.sleep(0.3)
        assert not _port_open(h.ports[1])
    finally:
        h.stop_all()


# ---- who runs the repository's code (scheme audit 21.11: install and build ran as root, with scripts from the product repository)

FAKE_RUNUSER = '''\
    #!/usr/bin/env bash
    # fake runuser: `runuser -u <user> -- env -i K=V … cmd …`. Logs the call and runs the command as the current user;
    # VIA_RUNUSER=1 goes in right after `env -i`, so a command can tell that it was started through runuser.
    echo "$*" >> "$FAKE_RUNUSER_LOG"
    [ "$1" = "-u" ] && [ "$3" = "--" ] && [ "$4" = "env" ] && [ "$5" = "-i" ] || { echo "fake runuser: unexpected call: $*" >&2; exit 99; }
    shift 5
    exec env -i VIA_RUNUSER=1 "$@"
    '''

FAKE_NPM = '''\
    #!/usr/bin/env python3
    """fake npm: `npm install|ci` runs scripts.postinstall of ./package.json, `npm run <name>` runs scripts.<name>."""
    import json, subprocess, sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    name = "postinstall" if args[0] in ("install", "ci") else args[1]
    cmd = json.load(open("package.json")).get("scripts", {}).get(name)
    sys.exit(subprocess.call(cmd, shell=True) if cmd else 0)
    '''

MARK = "echo via=${VIA_RUNUSER:-0} token=${GITHUB_TOKEN:-none} tg=${TELEGRAM_BOT_TOKEN:-none} extra=${LEAK_ME:-none} node_env=$NODE_ENV user=$USER"


def _game_with_package_scripts(host: Host) -> str:
    """The product repository carries the commands: postinstall and build of package.json leave a marker each."""
    (host.src / "package.json").write_text(json.dumps({"name": "fleets", "scripts": {
        "postinstall": f"{MARK} > postinstall.marker", "build": f"{MARK} > build.marker"}}))
    gitops.git(host.src, "add", "-A"); gitops.git(host.src, "commit", "-q", "-m", "package scripts")
    gitops.git(host.src, "push", "-q", str(host.origin), "main")
    conf = host.etc / "services" / "fleets.conf"
    conf.write_text(conf.read_text().replace("BUILD_CMD=''", "BUILD_CMD='npm run build'"))
    _exe(host.bin / "npm", FAKE_NPM)
    return gitops.head(host.src)


def test_clone_install_and_build_run_through_runuser_with_a_clean_environment(host: Host):
    """The deploy is called as root (sudo); everything that reads or executes the product repository goes through
    `runuser -u <service user> -- env -i …`: the scripts of package.json are started by runuser and see nothing of the
    caller's environment."""
    _game_with_package_scripts(host)
    _exe(host.bin / "runuser", FAKE_RUNUSER)
    log = host.tmp / "runuser.log"
    user = host.env()["SVC_USER"]
    env = {**host.env(), "SVC_VIA_RUNUSER": "1", "FAKE_RUNUSER_LOG": str(log),
           "GITHUB_TOKEN": "tok-must-not-leak", "TELEGRAM_BOT_TOKEN": "tg-must-not-leak", "LEAK_ME": "anything"}
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env=env, timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert f"repo-steps-as={user} (runuser)" in cp.stdout and f"install as {user}: npm install" in cp.stdout and f"build as {user}: npm run build" in cp.stdout
    calls = log.read_text().splitlines()
    assert calls and all(c.startswith(f"-u {user} -- env -i HOME=") for c in calls)          # nothing of these went another way
    for needle in (" git clone ", " rsync -a ", "npm install --no-audit --no-fund", "npm run build", " checkout --quiet --detach "):
        assert any(needle in c for c in calls), (needle, calls)
    assert not any("tok-must-not-leak" in c or "LEAK_ME" in c for c in calls)
    rel = Path(host.state_json()["release"])
    post, build = (rel / "postinstall.marker").read_text(), (rel / "build.marker").read_text()
    assert "via=1 token=none tg=none extra=none node_env=development" in post and f"user={user}" in post
    assert "via=1 token=none tg=none extra=none node_env=production" in build
    assert (rel / ".install.log").exists() and (rel / ".build.log").exists() and (rel / ".built").exists()
    # a second commit: fetch, staging and pruning go the same way
    host.commit("two")
    log.write_text("")
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env=env, timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    calls = log.read_text().splitlines()
    assert any(" fetch --quiet origin " in c for c in calls) and all(c.startswith(f"-u {user} -- env -i HOME=") for c in calls)


def test_build_script_never_runs_when_root_cannot_become_the_service_user(host: Host):
    """No fallback: when runuser refuses or is missing, the deploy stops before the clone, and the marker a package.json
    script would leave does not appear anywhere — the script was not executed by the caller's (root's) own process."""
    _game_with_package_scripts(host)
    _exe(host.bin / "runuser", "#!/usr/bin/env bash\necho 'runuser: may not be used by non-root users' >&2; exit 1\n")
    for extra in ({}, {"RUNUSER": str(host.tmp / "no-such-runuser")}):
        env = {**host.env(), "SVC_VIA_RUNUSER": "1", **extra}
        cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env=env, timeout=60)
        assert cp.returncode == 1 and "nothing was run as root" in cp.stderr, cp.stdout + cp.stderr
        assert not list(host.tmp.rglob("*.marker")) and not (host.services / "fleets" / "repo").exists()
        assert not list((host.var / "fleets" / "releases").iterdir()) and not (host.state / "svc-fleets.json").exists()
        assert not (host.systemd / "calls.log").exists()                       # nothing was started either
    # the same repository deploys once the user can be changed: the marker comes from the runuser side only
    _exe(host.bin / "runuser", FAKE_RUNUSER)
    env = {**host.env(), "SVC_VIA_RUNUSER": "1", "FAKE_RUNUSER_LOG": str(host.tmp / "runuser.log")}
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env=env, timeout=120)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert [m.read_text().split()[0] for m in sorted(host.tmp.rglob("*.marker"))] == ["via=1", "via=1"]


def test_clone_and_releases_of_another_owner_are_handed_to_the_service_user(host: Host):
    """Before 22.11 root made the clone and the releases. Rollback, --force, a repeated sha and the next fetch must work
    on such trees: the script chowns a tree whose top directory is not the service user's before that user touches it.
    Here the service user is `nobody`, so every tree of the test's own user counts as «another owner»; chown is a fake."""
    _exe(host.bin / "runuser", FAKE_RUNUSER)
    _exe(host.bin / "chown", '#!/usr/bin/env bash\necho "$*" >> "$FAKE_CHOWN_LOG"\n')
    chown_log = host.tmp / "chown.log"
    env = {**host.env(), "SVC_USER": "nobody", "SVC_HOME": str(host.tmp), "SVC_VIA_RUNUSER": "1",
           "FAKE_RUNUSER_LOG": str(host.tmp / "runuser.log"), "FAKE_CHOWN_LOG": str(chown_log)}

    def deploy(*args: str) -> subprocess.CompletedProcess:
        chown_log.write_text("")
        return subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets", *args], capture_output=True, text=True, env=env, timeout=120)

    def chowned(path: Path, recursive: bool) -> bool:
        return any(ln.startswith("-R -h nobody:" if recursive else "-h nobody:") and ln.endswith(f" {path}") for ln in chown_log.read_text().splitlines())

    cp = deploy()
    assert cp.returncode == 0, cp.stdout + cp.stderr
    rel1 = Path(host.state_json()["release"]); sha1 = host.state_json()["sha"]
    # the directories root makes are given to the service user before the clone: the node only, never recursively
    for d in (host.services / "fleets", host.var / "fleets" / "releases", host.var / "fleets" / "data"):
        assert chowned(d, recursive=False) and not chowned(d, recursive=True)
    assert not any(str(host.var / "fleets" / "instances") in ln or str(host.state) in ln for ln in chown_log.read_text().splitlines())
    sha2 = host.commit("two")
    cp = deploy("--sha", sha2)                                   # next deploy: the existing clone is adopted before the fetch
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert chowned(host.services / "fleets" / "repo", recursive=True) and "handing the tree over" in cp.stdout
    rel2 = Path(host.state_json()["release"])
    cp = deploy("--rollback")                                    # the previous release is adopted before its server starts
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert chowned(rel1, recursive=True) and host.state_json()["sha"] == sha1
    cp = deploy("--sha", sha2)                                   # a built release deployed again: reused, adopted
    assert cp.returncode == 0 and "already built — reusing" in cp.stdout, cp.stdout + cp.stderr
    assert chowned(rel2, recursive=True) and host.state_json()["sha"] == sha2
    cp = deploy("--sha", sha2, "--force")                        # --force rebuilds the live release: adopted, removed, staged again
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert chowned(rel2, recursive=True) and (rel2 / ".built").exists() and host.state_json()["sha"] == sha2
    assert "action=adopted" in host.log.read_text()


def test_trees_the_service_user_already_owns_are_not_chowned(host: Host):
    _exe(host.bin / "chown", '#!/usr/bin/env bash\necho "$*" >> "$FAKE_CHOWN_LOG"\n')
    env = {**host.env(), "FAKE_CHOWN_LOG": str(host.tmp / "chown.log")}
    for args in ([], ["--force"]):
        cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets", *args], capture_output=True, text=True, env=env, timeout=120)
        assert cp.returncode == 0, cp.stdout + cp.stderr
    assert not (host.tmp / "chown.log").exists() and "action=adopted" not in host.log.read_text()


def test_credential_helper_gives_the_token_only_to_github_over_https(tmp_path: Path):
    """git-credential-dispatcher (friction F11): the token is read from the dispatcher's .env when git asks; no other host
    or protocol gets it, nothing is stored, and the helper's own messages never carry it."""
    helper = DEPLOY_DIR / "git-credential-dispatcher"
    assert subprocess.run(["bash", "-n", str(helper)]).returncode == 0 and os.access(helper, os.X_OK)
    envf = tmp_path / ".env"
    envf.write_text("TELEGRAM_BOT_TOKEN=tg\nGITHUB_TOKEN=old-value\nGITHUB_TOKEN=\"tok-abc123\"      # push to site repos\nOTHER=1\n")
    before = envf.read_text()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "AGENT_TEAM_DISPATCHER_ENV": str(envf), "AGENT_TEAM_CREDENTIAL_LOG": str(tmp_path / "cred.log")}

    def ask(verb: str, protocol: str, host_: str, e: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(helper), verb], input=f"protocol={protocol}\nhost={host_}\n\n", capture_output=True, text=True, env=e or env)

    cp = ask("get", "https", "github.com")
    assert cp.returncode == 0 and cp.stdout == "username=x-access-token\npassword=tok-abc123\n"
    assert "tok-abc123" not in cp.stderr and "GITHUB_TOKEN" in cp.stderr and str(envf) in cp.stderr
    for verb, protocol, host_ in (("get", "https", "example.com"), ("get", "http", "github.com"), ("get", "https", "github.com.evil.test"),
                                  ("store", "https", "github.com"), ("erase", "https", "github.com")):
        cp = ask(verb, protocol, host_)
        assert cp.returncode == 0 and cp.stdout == "", (verb, protocol, host_, cp.stdout)
    assert envf.read_text() == before and "tok-abc123" not in (tmp_path / "cred.log").read_text()
    cp = ask("get", "https", "github.com", {**env, "AGENT_TEAM_DISPATCHER_ENV": str(tmp_path / "missing.env")})
    assert cp.returncode == 0 and cp.stdout == "" and "cannot read" in cp.stderr
    (tmp_path / "agent-team" / "dispatcher").mkdir(parents=True)                # the default place: ~/agent-team/dispatcher/.env
    (tmp_path / "agent-team" / "dispatcher" / ".env").write_text("GITHUB_TOKEN=from-home\n")
    cp = ask("get", "https", "github.com", {"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert "password=from-home" in cp.stdout
    # the exact options deploy-service.sh gives git: every other helper dropped, this one answers
    cp = subprocess.run(["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}", "credential", "fill"],
                        input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
                        env={**env, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}, cwd=str(tmp_path))
    assert cp.returncode == 0 and "password=tok-abc123" in cp.stdout, cp.stdout + cp.stderr


def test_https_repository_is_cloned_with_the_helper_and_nothing_else(tmp_path: Path):
    h = Host(tmp_path)
    conf = h.etc / "services" / "fleets.conf"
    conf.write_text(conf.read_text().replace(f"REPO={h.origin}", "REPO=https://github.com/example-org/private-game.git"))
    glog = tmp_path / "git.log"                                  # the path is in the fake itself: git runs with a clean environment
    _exe(h.bin / "git", f'#!/usr/bin/env bash\necho "$*" >> {glog}\necho "fatal: could not read Username" >&2; exit 128\n')
    helper = DEPLOY_DIR / "git-credential-dispatcher"
    env = {**h.env(), "GIT_CREDENTIAL_HELPER": str(helper)}
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env=env, timeout=60)
    assert cp.returncode == 1 and "git clone of https://github.com/example-org/private-game.git failed" in cp.stderr and "credential helper" in cp.stderr
    assert glog.read_text().splitlines()[0] == f"-c credential.helper= -c credential.helper={helper} clone --quiet --branch main https://github.com/example-org/private-game.git {h.services}/fleets/repo"
    assert "action=error step=clone" in h.log.read_text()
    glog.write_text("")
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), "fleets"], capture_output=True, text=True, env={**env, "GIT_CREDENTIAL_HELPER": ""}, timeout=60)
    assert cp.returncode == 1 and glog.read_text().startswith("clone --quiet")           # no helper: the user's own git configuration applies


def test_usage_and_validation(tmp_path: Path):
    h = Host(tmp_path)
    env = h.env()
    for args, code, needle in ((["Fleets"], 64, "slug"), (["fleets", "--sha", "zz"], 64, "commit id"), (["other"], 1, "add-service.sh"), ([], 64, "Usage")):
        cp = subprocess.run(["bash", str(DEPLOY_DIR / "deploy-service.sh"), *args], capture_output=True, text=True, env=env)
        assert cp.returncode == code and needle in (cp.stderr + cp.stdout), (args, cp.stdout, cp.stderr)


def test_svc_run_substitutes_port_and_sources_secrets(tmp_path: Path):
    inst = tmp_path / "var" / "fleets" / "instances"; inst.mkdir(parents=True)
    rel = tmp_path / "rel"; rel.mkdir()
    secrets = tmp_path / "fleets.secrets.env"; secrets.write_text("SESSION_SECRET=abc\n")
    (inst / "fleets-a.env").write_text(f"RELEASE_DIR={rel}\nRUN_CMD='echo port={{port}} s=$SESSION_SECRET d=$DATA_DIR'\nPORT=8123\nDATA_DIR=/tmp/d\n"
                                        f"RELEASE_SHA=abc\nNODE_ENV=production\nSECRETS_FILE={secrets}\n")
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "svc-run.sh"), "fleets-a"], capture_output=True, text=True, env={**os.environ, "VAR_DIR": str(tmp_path / "var")})
    assert cp.returncode == 0 and "port=8123 s=abc d=/tmp/d" in cp.stdout and "svc-run: fleets-a sha=abc port=8123" in cp.stdout
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "svc-run.sh"), "fleets-c"], capture_output=True, text=True)
    assert cp.returncode == 64


def test_install_and_add_service_scripts_parse_and_render(tmp_path: Path):
    for f in ("install.sh", "add-service.sh", "deploy-service.sh", "svc-run.sh", "git-credential-dispatcher"):
        assert subprocess.run(["bash", "-n", str(DEPLOY_DIR / f)]).returncode == 0
    unit = (DEPLOY_DIR / "agent-team-svc@.service").read_text()
    assert "ExecStart=/opt/agent-team/bin/svc-run.sh %i" in unit and "User=__USER__" in unit
    # F36 (ai-battles 21.11): a role's `pkill -f` stopped the live server, which exits 0 on SIGTERM — it must come back by itself
    assert "\nRestart=always\n" in unit and "\nRestart=on-failure" not in unit
    assert "pkill" in (DEPLOY_DIR.parent.parent / "team" / "roles" / "developer.md").read_text()
    sudoers = (DEPLOY_DIR / "agent-team-deploy.sudoers").read_text()
    assert "NOPASSWD: /opt/agent-team/bin/deploy-service.sh" in sudoers
    # scheme audit 21.11: the rule's comment claimed «no command text comes from the caller», while package.json scripts ran as root
    assert "no command text comes from the caller" not in sudoers and "runuser" in sudoers and "package.json" in sudoers
    assert [ln for ln in sudoers.splitlines() if ln and not ln.startswith("#")] == ["__USER__ ALL=(root) NOPASSWD: /opt/agent-team/bin/deploy-service.sh"]
    tpl = (DEPLOY_DIR / "service.conf.template").read_text()
    assert "proxy_pass http://svc___SLUG_U__;" in tpl and "Upgrade $http_upgrade" in tpl and "/_team/metrics.json" in tpl
    assert "svc___SLUG__" not in tpl   # a slug with "-" must become a valid nginx identifier (F: ai-battles)
    # install.sh without root into tmp dirs: files land where deploy-service.sh expects them
    env = {**os.environ, "REQUIRE_ROOT": "0", "ETC_DIR": str(tmp_path / "etc"), "OPT_DIR": str(tmp_path / "opt"), "VAR_DIR": str(tmp_path / "var"),
           "STATE_DIR": str(tmp_path / "state"), "LOG_DIR": str(tmp_path / "log"), "SYSTEMD_DIR": str(tmp_path / "systemd"),
           "SUDOERS_DIR": str(tmp_path / "sudoers"), "NGINX_CONFD": str(tmp_path / "confd"), "SVC_USER": "gamer", "PATH": os.environ["PATH"]}
    fake = tmp_path / "fakebin"; fake.mkdir()
    _exe(fake / "nginx", "#!/usr/bin/env bash\nexit 0\n"); _exe(fake / "flock", "#!/usr/bin/env bash\nexit 0\n")
    _exe(fake / "runuser", "#!/usr/bin/env bash\nexit 0\n")              # install.sh refuses a host where root cannot become the service user
    env["PATH"] = f"{fake}:{env['PATH']}"
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "install.sh")], capture_output=True, text=True, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (tmp_path / "opt" / "bin" / "deploy-service.sh").exists() and (tmp_path / "etc" / "service.conf.template").exists()
    helper = tmp_path / "opt" / "bin" / "git-credential-dispatcher"
    assert helper.read_text() == (DEPLOY_DIR / "git-credential-dispatcher").read_text() and stat.S_IMODE(helper.stat().st_mode) == 0o755
    assert "GITHUB_TOKEN" in helper.read_text() and not (tmp_path / "opt" / "bin" / "git-credential-dispatcher.before-repo").exists()
    helper.write_text("#!/bin/sh\n# hand-made\n")                         # a helper put there by hand is kept once, then replaced
    for _ in range(2):
        assert subprocess.run(["bash", str(DEPLOY_DIR / "install.sh")], capture_output=True, text=True, env=env).returncode == 0
    assert helper.read_text() == (DEPLOY_DIR / "git-credential-dispatcher").read_text()
    assert (tmp_path / "opt" / "bin" / "git-credential-dispatcher.before-repo").read_text() == "#!/bin/sh\n# hand-made\n"
    no_runuser = {**env, "PATH": f"{fake}:/usr/bin:/bin"}
    (fake / "runuser").rename(fake / "runuser.off")
    if subprocess.run(["bash", "-c", "command -v runuser"], capture_output=True, env=no_runuser).returncode != 0:
        cp = subprocess.run(["bash", str(DEPLOY_DIR / "install.sh")], capture_output=True, text=True, env=no_runuser)
        assert cp.returncode == 1 and "runuser" in cp.stderr
    (fake / "runuser.off").rename(fake / "runuser")
    assert "User=gamer" in (tmp_path / "systemd" / "agent-team-svc@.service").read_text()
    assert (tmp_path / "sudoers" / "agent-team-deploy").read_text().startswith("#") and "gamer ALL=(root) NOPASSWD" in (tmp_path / "sudoers" / "agent-team-deploy").read_text()
    # add-service.sh --no-deploy with DNS and cert skipped and a fake render-vhost: conf, secrets placeholder, upstream placeholder
    _exe(fake / "render-vhost.sh", "#!/usr/bin/env bash\necho rendered $1 $2 template=$TEMPLATE; exit 3\n")
    env2 = {**env, "SKIP_DNS": "1", "SKIP_CERT": "1", "RENDER_VHOST": str(fake / "render-vhost.sh"), "TEMPLATE": str(tmp_path / "etc" / "service.conf.template"),
            "SERVICES_ETC": str(tmp_path / "etc" / "services"), "DEPLOY": str(tmp_path / "opt" / "bin" / "deploy-service.sh"),
            "NGINX_AVAILABLE": str(tmp_path / "na"), "NGINX_ENABLED": str(tmp_path / "ne"), "LETSENCRYPT_LIVE": str(tmp_path / "le")}
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "add-service.sh"), "fleets", "fleets.example.test", "https://github.com/x/fleets.git",
                         "--run", "node server.js", "--no-deploy"], capture_output=True, text=True, env=env2)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    conf = (tmp_path / "etc" / "services" / "fleets.conf").read_text()
    assert "HOST=fleets.example.test" in conf and "RUN_CMD=node\\ server.js" in conf and "PORT_A=8101" in conf and "PORT_B=8102" in conf
    assert (tmp_path / "etc" / "services" / "fleets.secrets.env").exists()
    assert "server 127.0.0.1:8101" in (tmp_path / "confd" / "agent-team-svc-fleets.conf").read_text()
    assert "rendered fleets fleets.example.test template=" in cp.stdout
    cp = subprocess.run(["bash", str(DEPLOY_DIR / "add-service.sh"), "other", "other.example.test", "https://github.com/x/o.git", "--no-deploy"],
                        capture_output=True, text=True, env=env2)
    assert cp.returncode == 0 and "PORT_A=8111" in (tmp_path / "etc" / "services" / "other.conf").read_text()     # next free pair


# ------------------------------------------------------------------ dispatcher side

def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_n_cycles": 0, "digest_hours_utc": [0]}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _game(st: C.Settings, deploy: dict | None = None, site_base: str = "https://fleets.example.test") -> C.Product:
    d = st.products_dir / "fleets"; d.mkdir(parents=True)
    (d / "product.yaml").write_text(yaml.safe_dump({
        "slug": "fleets", "name": "Fleets", "host": "", "site_base": site_base, "profile": "game", "stage": "M0",
        "repo": {"url": "", "branch": "main", "kind": "service", "run_cmd": "npm start", "health_path": "/healthz"},
        "deploy": deploy if deploy is not None else {"mode": "pending"}}, sort_keys=False), encoding="utf-8")
    (d / "VISION.md").write_text("# Fleets\n")
    p = C.load_product(st, d)
    r = gitops.init_local(p.repo_dir)
    (r / "README.md").write_text("game\n")
    gitops.git(r, "add", "-A"); gitops.git(r, "commit", "-q", "-m", "init")
    return p


def _fake_deploy(tmp_path: Path, mode: str = "ok") -> Path:
    body = {"ok": 'echo "[$1] staging"; echo "== deployed slug=$1 sha=$3 colour=b port=8102 url=https://fleets.example.test/"; exit 0',
            "health": 'echo "[$1] ERROR: health http://127.0.0.1:8102/healthz → 000 after 4s; stopping, live colour untouched" >&2; exit 20',
            "skip": 'echo "[$1] abc is live and healthy — nothing to do"; exit 10',
            "args": 'echo "== deployed slug=$1 sha=deadbeef colour=a port=8101 url=https://x/"; echo "ARGS: $*"; env | grep -c TELEGRAM_BOT_TOKEN || true; exit 0'}[mode]
    return _exe(tmp_path / f"deploy-{mode}.sh", "#!/usr/bin/env bash\n" + body + "\n")


def test_modes_and_commands(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    assert p.deploy_mode == "pending" and not servicedeploy.enabled(p) and "no dispatcher deploy" in servicedeploy.describe(p)
    p.raw["deploy"] = {"mode": "service", "host": "local"}
    assert servicedeploy.enabled(p)
    assert servicedeploy.command(p, sha="abc123") == ["sudo", "-n", "/opt/agent-team/bin/deploy-service.sh", "fleets", "--sha", "abc123"]
    assert servicedeploy.command(p, rollback=True, force=True)[-2:] == ["--rollback", "--force"]
    p.raw["deploy"] = {"mode": "service", "host": "ubuntu@10.0.0.5", "cmd": "/opt/x/deploy.sh"}
    cmd = servicedeploy.command(p, sha="abc")
    assert cmd[0] == "ssh" and cmd[-2] == "ubuntu@10.0.0.5" and cmd[-1] == "/opt/x/deploy.sh fleets --sha abc" and "BatchMode=yes" in cmd
    assert "ssh ubuntu@10.0.0.5" in servicedeploy.describe(p)
    site = C.load_product(st, tmp_path / "products" / "fleets")
    site.raw["profile"] = "site"; site.raw.pop("deploy", None)
    assert site.deploy_mode == "git_pull" and not servicedeploy.enabled(site)
    assert servicedeploy.parse_result_line("x\n== deployed slug=f sha=abc colour=a port=8101 url=https://h/\n") == {"slug": "f", "sha": "abc", "colour": "a", "port": "8101", "url": "https://h/"}
    assert servicedeploy.parse_result_line("nothing") == {}


def test_run_parses_success_failure_and_strips_secrets(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, {"mode": "service", "cmd": str(_fake_deploy(tmp_path, "args"))})
    os.environ["TELEGRAM_BOT_TOKEN"] = "secret"                      # conftest restores the environment
    res = servicedeploy.run(p, sha="abc", log_path=tmp_path / "d.log")
    assert res.ok and res.sha == "deadbeef" and res.port == 8101 and res.colour == "a" and res.url == "https://x/"
    assert "ARGS: fleets --sha abc" in res.output and "\n0\n" in res.output         # no TELEGRAM_BOT_TOKEN in the script's environment
    assert (tmp_path / "d.log").read_text().startswith("$ ") and "deployed" in res.summary()
    p.raw["deploy"]["cmd"] = str(_fake_deploy(tmp_path, "health"))
    res = servicedeploy.run(p, sha="abc")
    assert not res.ok and res.exit_code == 20 and "health" in res.error and "live one is untouched" in res.summary()
    p.raw["deploy"]["cmd"] = str(_fake_deploy(tmp_path, "skip"))
    res = servicedeploy.run(p, sha="abc")
    assert not res.ok and res.skipped and res.summary() == "already live, nothing deployed"
    p.raw["deploy"]["cmd"] = str(tmp_path / "missing.sh")
    res = servicedeploy.run(p, sha="abc")
    assert not res.ok and res.exit_code == 127 and "cannot run" in res.error


class DevRunner:
    def __init__(self):
        self.calls: list[str] = []

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        self.calls.append(role)
        f = Path(cwd) / "src.ts"; f.write_text("work\n")
        gitops.git(Path(cwd), "add", "-A"); gitops.git(Path(cwd), "commit", "-q", "-m", "dev: work")
        out = {"status": "done", "summary": "done", "changes": ["src.ts"], "how_to_verify": ["src.ts"], "self_check": [{"criterion": "f", "met": True, "evidence": "e"}]}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


def _ready_task(p: C.Product) -> str:
    s = Store(p.dir)
    t = s.create_task(title="rooms", type_="milestone", problem="x", scope="src/", acceptance=["two tabs"], goal="M0", created_by="po", role="developer", state="ready")
    s.close()
    return t["id"]


def test_merge_deploys_and_the_task_stays_released(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, {"mode": "service", "host": "local", "cmd": str(_fake_deploy(tmp_path, "ok"))})
    tid = _ready_task(p)
    cyc = Cycle(st, p, DevRunner(), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "released" and t["deployed_sha"] == t["released_sha"] == gitops.head(p.repo_dir) and t["deployed_url"] == "https://fleets.example.test/"
    ev = [e for e in s.events() if e["kind"] == "deploy.ok"]
    assert len(ev) == 1 and ev[0]["detail"]["port"] == 8102 and ev[0]["detail"]["skipped"] is False
    assert not s.questions(["open"]) and not s.observations("new")
    s.close()
    assert (st.data_dir / "work" / "fleets" / "c1" / f"deploy-{tid}.log").read_text().startswith("$ ")


def test_failed_deploy_blocks_the_task_with_a_po_question(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, {"mode": "service", "cmd": str(_fake_deploy(tmp_path, "health"))})
    tid = _ready_task(p)
    cyc = Cycle(st, p, DevRunner(), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "blocked" and "deployed_sha" not in t and t["released_sha"]
    q = s.questions(["open"])
    assert len(q) == 1 and q[0]["to"] == "po" and "did not deploy" in q[0]["question"] and "previous release stays live" in q[0]["question"]
    obs = s.observations("new")
    assert len(obs) == 1 and obs[0]["classification"] == "environment_error" and "health check failed" in obs[0]["title"]
    assert [e["kind"] for e in s.events() if e["kind"].startswith("deploy.")] == ["deploy.failed"]
    s.close()


def test_pending_and_dry_run_deploy_nothing(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, {"mode": "pending", "cmd": str(_fake_deploy(tmp_path, "ok"))})
    tid = _ready_task(p)
    cyc = Cycle(st, p, DevRunner(), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    s = Store(p.dir)
    assert s.task(tid)["state"] == "released" and "deployed_sha" not in s.task(tid) and not [e for e in s.events() if e["kind"].startswith("deploy.")]
    s.close()
    p2 = _game(_settings(tmp_path / "b"), {"mode": "service", "cmd": str(_fake_deploy(tmp_path, "ok"))})
    cyc = Cycle(p2.st, p2, None, "c1", dry_run=True)
    assert cyc.deploy_after_merge({"id": "T-0001"}, "abc") is False
    cyc.store.close()


def test_team_deploy_cli(tmp_path: Path, monkeypatch, capsys):
    st = _settings(tmp_path)
    p = _game(st, {"mode": "service", "host": "local", "cmd": str(_fake_deploy(tmp_path, "ok"))})
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    assert cli.main(["deploy", "fleets", "--show", "--sha", "abc"]) == 0
    out = capsys.readouterr().out
    assert "deploy.mode=service" in out and out.strip().endswith("fleets --sha abc")
    assert cli.main(["deploy", "fleets", "--sha", "abc"]) == 0
    assert "deployed abc" in capsys.readouterr().out
    p.raw["deploy"]["cmd"] = str(_fake_deploy(tmp_path, "health"))
    (p.dir / "product.yaml").write_text(yaml.safe_dump(p.raw, sort_keys=False))
    assert cli.main(["deploy", "fleets"]) == 1
    assert "deploy failed (exit 20" in capsys.readouterr().out
    pend = _game(_settings(tmp_path / "c"))
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: pend.st)
    with pytest.raises(SystemExit):
        cli.main(["deploy", "fleets"])


def test_vhost_template_renders_valid_nginx_identifiers_for_hyphenated_slug():
    """render-vhost.sh substitutes __SLUG_U__ (slug with '-' → '_') before __SLUG__: the upstream name matches what
    add-service.sh / deploy-service.sh write (`svc_${SLUG//-/_}`) and the map variable has no '-' (nginx forbids it)."""
    render = (ROOT / "deploy" / "droplet" / "render-vhost.sh").read_text()
    assert '-e "s|__SLUG_U__|${SLUG//-/_}|g" -e "s|__SLUG__|$SLUG|g"' in render
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / "droplet" / "render-vhost.sh")]).returncode == 0
    slug, host = "ai-battles", "ai-battles.formachines.ai"
    tpl = (DEPLOY_DIR / "service.conf.template").read_text()
    rendered = tpl.replace("__HOST__", host).replace("__SLUG_U__", slug.replace("-", "_")).replace("__SLUG__", slug).replace("__INDEXNOW_KEY__", "k")
    assert "proxy_pass http://svc_ai_battles;" in rendered and "$svc_ai_battles_connection" in rendered
    assert "svc_ai-battles" not in rendered and "__SLUG" not in rendered
    upstream_line = [ln for ln in (DEPLOY_DIR / "deploy-service.sh").read_text().splitlines() if "echo \"upstream svc_" in ln][0]
    assert "${SLUG//-/_}" in upstream_line
    add = (DEPLOY_DIR / "add-service.sh").read_text()
    assert "certbot certonly --webroot -w /var/www/acme" in add and "location /.well-known/acme-challenge/ { root /var/www/acme; }" in add
