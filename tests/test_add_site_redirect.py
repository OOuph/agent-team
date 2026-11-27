"""deploy/droplet/add-site.sh --redirect (owner 27.11: www.hittheline.com -> hittheline.com as a real 301).

Only the `--print` path runs here: it renders the vhost and changes nothing, so no root, nginx or certbot is needed."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "droplet" / "add-site.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed")


def run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_redirect_print_renders_301_on_http_and_https(tmp_path: Path):
    env = {"PATH": "/usr/bin:/bin", "LE_OPTIONS": str(tmp_path / "none.conf"), "LE_DHPARAMS": str(tmp_path / "none.pem")}
    r = run("--redirect", "hittheline-www", "www.hittheline.com", "hittheline.com", "--print", env=env)
    assert r.returncode == 0, r.stderr
    conf = r.stdout
    assert conf.count("server_name www.hittheline.com;") == 2
    assert conf.count("return 301 https://hittheline.com$request_uri;") == 2
    assert "listen 80;" in conf and "listen 443 ssl http2;" in conf
    assert "location /.well-known/acme-challenge/ { root /var/www/acme; }" in conf      # renewals keep working
    assert "ssl_certificate     /etc/letsencrypt/live/www.hittheline.com/fullchain.pem;" in conf
    assert "root /var/www/" not in conf.replace("root /var/www/acme;", "")              # nothing is served
    assert "include" not in conf                                                        # optional LE files absent


def test_redirect_print_includes_letsencrypt_options_when_present(tmp_path: Path):
    opts, dh = tmp_path / "options-ssl-nginx.conf", tmp_path / "ssl-dhparams.pem"
    opts.write_text("# x\n"); dh.write_text("x\n")
    env = {"PATH": "/usr/bin:/bin", "LE_OPTIONS": str(opts), "LE_DHPARAMS": str(dh)}
    r = run("--redirect", "w", "www.example.test", "example.test", "--print", env=env)
    assert r.returncode == 0, r.stderr
    assert f"include {opts};" in r.stdout and f"ssl_dhparam {dh};" in r.stdout


@pytest.mark.parametrize("args", [
    ("--redirect", "Bad_Slug", "www.example.test", "example.test", "--print"),
    ("--redirect", "w", "not a host", "example.test", "--print"),
    ("--redirect", "w", "www.example.test", "bad_target", "--print"),
    ("--redirect", "w", "example.test", "example.test", "--print"),
    ("--redirect", "w", "www.example.test"),
])
def test_redirect_rejects_bad_input(args):
    r = run(*args)
    assert r.returncode == 64
    assert r.stdout == ""


def test_redirect_without_print_needs_root():
    import os
    if os.geteuid() == 0:
        pytest.skip("running as root")
    r = run("--redirect", "w", "www.example.test", "example.test")
    assert r.returncode == 1 and "run as root" in r.stderr
