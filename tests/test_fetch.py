import io
import contextlib
import urllib.request

from team import fetch


class R:
    def __init__(self, body):
        self.status, self.headers, self._b = 200, {}, body.encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def run(monkeypatch, body, args):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R(body))
    monkeypatch.setenv("TEAM_PAUSE", "0")
    monkeypatch.delenv("TEAM_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("TEAM_TRACE_FILE", raising=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert fetch.main(["http://x.test/"] + args) == 0
    return buf.getvalue()


def test_around_one_line(monkeypatch):
    body = "<p>" + "a" * 3000 + " NEEDLE one " + "b" * 500 + " NEEDLE two " + "c" * 500 + " NEEDLE three tail-end</p>"
    out = run(monkeypatch, body, ["--grep", "needle", "--around", "200"])
    assert out.count("--- match") == 3 and "3 matches" in out
    assert "NEEDLE three" in out and body.index("NEEDLE three") > 500


def test_count(monkeypatch):
    out = run(monkeypatch, "<tr>x</tr><tr>x</tr>", ["--grep", "<tr>", "--count"])
    assert "2 matches" in out


def test_offset(monkeypatch):
    body = "a" * 30000 + "START" + "z" * 100
    out = run(monkeypatch, body, ["--offset", "30000"])
    assert "START" in out and "aaaa" not in out
