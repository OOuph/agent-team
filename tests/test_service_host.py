"""deploy/service/service-host.sh: a second address of a service, then that address as the main one (owner 27.11:
ai-battles becomes Clankodrome — clankodrome.com must answer with the game beside battles.bymachines.ai before the
canonical address is switched, and the old address then redirects with the path)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "service" / "service-host.sh"
TEMPLATE = ROOT / "deploy" / "service" / "service.conf.template"
OLD, NEW = "battles.example.test", "clanko.example.test"


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A game host in tmp dirs: the service `ai-battles` registered on OLD with its live vhost and certificate; fakes of
    nginx, systemctl, certbot, curl, flock and render-vhost.sh that record what they were asked to do."""
    etc, na, ne, le, fake = tmp_path / "etc", tmp_path / "na", tmp_path / "ne", tmp_path / "le", tmp_path / "fakebin"
    for d in (etc / "services", na, ne, le / "live" / OLD, fake, tmp_path / "log"):
        d.mkdir(parents=True)
    (etc / "service.conf.template").write_text(TEMPLATE.read_text())
    (etc / "indexnow.key").write_text("abcdef0123456789\n")
    (etc / "env").write_text("CERTBOT_EMAIL=ops@example.test\n")
    (etc / "services" / "ai-battles.conf").write_text(f"# ai-battles\nHOST={OLD}\nHEALTH_PATH=/healthz\nPORT_A=8101\nPORT_B=8102\n")
    (le / "live" / OLD / "fullchain.pem").write_text("old cert\n")
    (na / "ai-battles.conf").write_text(f"live vhost\n    server_name {OLD};\n")
    (ne / "ai-battles.conf").symlink_to(na / "ai-battles.conf")
    calls = tmp_path / "calls.txt"
    _exe(fake / "nginx", f"#!/usr/bin/env bash\necho \"nginx $*\" >> {calls}\n[ -f {tmp_path}/nginx-fails ] && exit 1\nexit 0\n")
    _exe(fake / "systemctl", f"#!/usr/bin/env bash\necho \"systemctl $*\" >> {calls}\nexit 0\n")
    _exe(fake / "curl", f"#!/usr/bin/env bash\necho \"curl $*\" >> {calls}\nexit 0\n")
    _exe(fake / "flock", "#!/usr/bin/env bash\nexit 0\n")
    _exe(fake / "certbot", f"#!/usr/bin/env bash\necho \"certbot $*\" >> {calls}\n"
                           f"echo \"during-certbot main=$(head -1 {na}/ai-battles.conf) enabled=$(ls {ne} | tr '\\n' ' ')\" >> {calls}\n"
                           f"[ -f {tmp_path}/certbot-fails ] && exit 1\n"
                           f"host=\"\"; while [ $# -gt 0 ]; do [ \"$1\" = -d ] && host=\"$2\"; shift; done\n"
                           f"mkdir -p {le}/live/$host && echo new > {le}/live/$host/fullchain.pem\n")
    _exe(fake / "render-vhost.sh", f"#!/usr/bin/env bash\necho \"render $*\" >> {calls}\nprintf 'main vhost\\n    server_name %s;\\n' \"$2\" > {na}/$1.conf\n"
                                   f"ln -sfn {na}/$1.conf {ne}/$1.conf\nexit 3\n")
    env = {**os.environ, "REQUIRE_ROOT": "0", "SKIP_DNS": "1", "ETC_DIR": str(etc), "NGINX_AVAILABLE": str(na), "NGINX_ENABLED": str(ne),
           "LETSENCRYPT_DIR": str(le), "ACME_ROOT": str(tmp_path / "acme"), "LOG_DIR": str(tmp_path / "log"),
           "LOCK_FILE": str(tmp_path / "lock"), "RENDER_VHOST": str(fake / "render-vhost.sh"), "PATH": f"{fake}:{os.environ['PATH']}"}
    return env, calls


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_syntax_and_usage(tmp_path: Path):
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    env, _ = _env(tmp_path)
    assert _run(env).returncode == 64 and _run(env, "alias", "ai-battles").returncode == 64
    assert _run(env, "alias", "ai-battles", "not a host").returncode == 64 and _run(env, "alias", "Bad_Slug", NEW).returncode == 64
    assert _run(env, "alias", "ai-battles", NEW + ";rm -rf /").returncode == 64
    assert _run(env, "alias", "nosuch", NEW).returncode == 1
    assert _run(env, "frobnicate", "ai-battles", NEW).returncode == 64


def test_print_changes_nothing_and_shows_the_vhosts(tmp_path: Path):
    env, calls = _env(tmp_path)
    env = {**env, "REQUIRE_ROOT": "1"}                                   # --print needs no root
    before = sorted(p.name for p in (tmp_path / "na").iterdir())
    cp = _run(env, "alias", "ai-battles", NEW, "--print")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout
    assert out.count(f"server_name {NEW};") == 2 and "proxy_pass http://svc_ai_battles;" in out and "proxy_set_header Upgrade $http_upgrade;" in out
    assert "proxy_set_header Connection $svc_ai_battles_connection;" in out and "proxy_read_timeout 3600s;" in out
    assert "\nmap " not in out and "__" not in out                       # the map stays in the main vhost; every placeholder is filled
    assert f"/live/{NEW}/fullchain.pem" in out and "location /.well-known/acme-challenge/" in out
    cp = _run(env, "primary", "ai-battles", NEW, "--print")
    out = cp.stdout
    assert cp.returncode == 0 and out.count(f"server_name {OLD};") == 2 and out.count(f"return 301 https://{NEW}$request_uri;") == 2
    assert "location = /healthz {" in out and "location /_team/ {" in out and out.count("proxy_pass http://svc_ai_battles;") == 2
    assert _run(env, "unalias", "ai-battles", NEW, "--print").returncode == 0
    assert _run(env, "list", "ai-battles").stdout.strip() == f"main: {OLD}"
    assert sorted(p.name for p in (tmp_path / "na").iterdir()) == before and not calls.exists()
    assert _run(env, "alias", "ai-battles", NEW).returncode == 1          # without --print and without root: refused


def test_alias_adds_the_second_address_and_leaves_the_main_one_alone(tmp_path: Path):
    env, calls = _env(tmp_path)
    na, ne = tmp_path / "na", tmp_path / "ne"
    main_before = (na / "ai-battles.conf").read_text()
    cp = _run(env, "alias", "ai-battles", NEW)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    log = calls.read_text()
    assert "certbot certonly --webroot" in log and f"-d {NEW}" in log and "-m ops@example.test" in log
    assert "during-certbot main=live vhost" in log and f"ai-battles-alias-{NEW}.conf" in log.split("during-certbot")[1]   # http-only block of its own file
    alias = (na / f"ai-battles-alias-{NEW}.conf").read_text()
    assert alias.count(f"server_name {NEW};") == 2 and "proxy_pass http://svc_ai_battles;" in alias and "listen 443 ssl http2;" in alias
    assert (ne / f"ai-battles-alias-{NEW}.conf").is_symlink()
    assert (na / "ai-battles.conf").read_text() == main_before             # the main vhost is byte for byte the same
    conf = (tmp_path / "etc" / "services" / "ai-battles.conf").read_text()
    assert f"HOST={OLD}\n" in conf and "OLD_HOST" not in conf
    assert "action=host-alias" in (tmp_path / "log" / "deploy.log").read_text() and f"https://{NEW}/healthz" in log
    assert _run(env, "list", "ai-battles").stdout.split() == ["main:", OLD, "alias:", NEW]
    calls.write_text("")
    cp = _run(env, "alias", "ai-battles", NEW)                              # again: nothing to do
    assert cp.returncode == 0 and "certbot" not in calls.read_text() and "reload" not in calls.read_text()
    assert _run(env, "alias", "ai-battles", OLD).returncode == 64          # the main address cannot be its own alias


def test_alias_failures_leave_everything_as_it_was(tmp_path: Path):
    env, calls = _env(tmp_path)
    na, ne = tmp_path / "na", tmp_path / "ne"
    (tmp_path / "certbot-fails").write_text("x")
    cp = _run(env, "alias", "ai-battles", NEW)
    assert cp.returncode == 1 and "certbot did not issue" in cp.stderr
    assert sorted(p.name for p in na.iterdir()) == ["ai-battles.conf"] and sorted(p.name for p in ne.iterdir()) == ["ai-battles.conf"]
    (tmp_path / "certbot-fails").unlink()
    (tmp_path / "le" / "live" / NEW).mkdir(parents=True); (tmp_path / "le" / "live" / NEW / "fullchain.pem").write_text("c")
    (tmp_path / "nginx-fails").write_text("x")
    calls.write_text("")
    cp = _run(env, "alias", "ai-battles", NEW)
    assert cp.returncode == 1 and "nginx -t failed" in cp.stderr and "systemctl reload" not in calls.read_text()
    assert sorted(p.name for p in na.iterdir()) == ["ai-battles.conf"]
    (tmp_path / "nginx-fails").unlink()
    (na / "other.conf").write_text(f"server {{\n    server_name {NEW};\n}}\n"); (ne / "other.conf").symlink_to(na / "other.conf")
    cp = _run(env, "alias", "ai-battles", NEW)
    assert cp.returncode == 1 and "already a server_name" in cp.stderr      # another vhost answers on that name


def test_primary_needs_the_alias_first_then_switches_and_redirects_the_old_address(tmp_path: Path):
    env, calls = _env(tmp_path)
    na, ne, conf = tmp_path / "na", tmp_path / "ne", tmp_path / "etc" / "services" / "ai-battles.conf"
    cp = _run(env, "primary", "ai-battles", NEW)
    assert cp.returncode == 1 and "is not an alias" in cp.stderr and conf.read_text().count("HOST=") == 1
    assert _run(env, "alias", "ai-battles", NEW).returncode == 0
    assert _run(env, "alias", "ai-battles", "www." + NEW).returncode == 0
    calls.write_text("")
    cp = _run(env, "primary", "ai-battles", NEW)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    log = calls.read_text()
    assert f"render ai-battles {NEW} --no-reload" in log and log.count("systemctl reload nginx") == 1
    assert f"server_name {NEW};" in (na / "ai-battles.conf").read_text()
    assert not (na / f"ai-battles-alias-{NEW}.conf").exists() and not (ne / f"ai-battles-alias-{NEW}.conf").exists()   # no second vhost on that name
    assert (na / f"ai-battles-alias-www.{NEW}.conf").exists()               # the other alias goes on answering
    old = (na / "ai-battles-old-host.conf").read_text()
    assert old.count(f"server_name {OLD};") == 2 and old.count(f"return 301 https://{NEW}$request_uri;") == 2
    assert "location = /healthz {" in old and "location /_team/ {" in old and f"/live/{OLD}/fullchain.pem" in old
    assert old.index("location = /healthz") < old.rindex("return 301")     # the kept paths, then the redirect
    assert (ne / "ai-battles-old-host.conf").is_symlink()
    text = conf.read_text()
    assert f"HOST={NEW}\n" in text and f"OLD_HOST={OLD}\n" in text and text.count("\nHOST=") == 1 and "PORT_A=8101" in text
    assert len(list(conf.parent.glob("ai-battles.conf.before-primary-*"))) == 1
    assert f"action=host-primary from={OLD} to={NEW}" in (tmp_path / "log" / "deploy.log").read_text()
    assert f"https://{NEW}/healthz" in log and f"https://{OLD}/healthz" in log
    assert _run(env, "list", "ai-battles").stdout.split() == ["main:", NEW, "previous:", OLD, "alias:", "www." + NEW]
    calls.write_text("")
    assert _run(env, "primary", "ai-battles", NEW).returncode == 0 and calls.read_text() == ""     # again: nothing to do


def test_primary_that_fails_puts_every_vhost_back(tmp_path: Path):
    env, calls = _env(tmp_path)
    na, ne, conf = tmp_path / "na", tmp_path / "ne", tmp_path / "etc" / "services" / "ai-battles.conf"
    assert _run(env, "alias", "ai-battles", NEW).returncode == 0
    main_before, alias_before, conf_before = (na / "ai-battles.conf").read_text(), (na / f"ai-battles-alias-{NEW}.conf").read_text(), conf.read_text()
    (tmp_path / "nginx-fails").write_text("x")
    calls.write_text("")
    cp = _run(env, "primary", "ai-battles", NEW)
    assert cp.returncode == 1 and "previous ones are back" in cp.stderr and "systemctl reload" not in calls.read_text()
    assert (na / "ai-battles.conf").read_text() == main_before and (na / f"ai-battles-alias-{NEW}.conf").read_text() == alias_before
    assert (ne / f"ai-battles-alias-{NEW}.conf").is_symlink() and not (na / "ai-battles-old-host.conf").exists() and conf.read_text() == conf_before


def test_unalias_removes_only_the_alias(tmp_path: Path):
    env, calls = _env(tmp_path)
    na = tmp_path / "na"
    assert _run(env, "alias", "ai-battles", NEW).returncode == 0
    assert _run(env, "unalias", "ai-battles", NEW).returncode == 0
    assert sorted(p.name for p in na.iterdir()) == ["ai-battles.conf"] and (tmp_path / "le" / "live" / NEW / "fullchain.pem").exists()
    assert _run(env, "unalias", "ai-battles", NEW).returncode == 0          # again: nothing to do


def test_installer_and_sudoers_know_the_script():
    inst = (ROOT / "deploy" / "service" / "install.sh").read_text()
    sudo = (ROOT / "deploy" / "service" / "agent-team-deploy.sudoers").read_text()
    assert "service-host.sh" in inst and "__USER__ ALL=(root) NOPASSWD: /opt/agent-team/bin/service-host.sh" in sudo
