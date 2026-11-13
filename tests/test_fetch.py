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


def test_tag_count(monkeypatch):
    body = "<html><h1>A</h1><p>x</p><h1 class='b'>B</h1><h2>C</h2></html>"
    assert "2 matches" in run(monkeypatch, body, ["--tag", "h1", "--count"])
    assert "1 matches" in run(monkeypatch, body, ["--tag", "h2", "--count"])
    assert "<h1>A" in run(monkeypatch, body, ["--tag", "h1"])


def test_offset(monkeypatch):
    body = "a" * 30000 + "START" + "z" * 100
    out = run(monkeypatch, body, ["--offset", "30000"])
    assert "START" in out and "aaaa" not in out


def test_json_path_count_and_keys(monkeypatch):
    body = '{"releases": [{"tag": "a"}, {"tag": "b"}], "meta": {"x": 1, "y": 2}}'
    assert run(monkeypatch, body, ["--json-path", "releases", "--count"]).strip().endswith("\n2")
    assert run(monkeypatch, body, ["--json-path", "releases[1].tag"]).strip().endswith("b")
    assert run(monkeypatch, body, ["--json-path", "meta", "--json-keys"]).strip().endswith('["x", "y"]')
    assert run(monkeypatch, body, ["--json-keys"]).strip().endswith('["releases", "meta"]')


def test_grep_plain_words_accepted(monkeypatch):
    body = "not here\nunder there\nother\n"
    assert "1 matching" in run(monkeypatch, body, ["--grep", "not"])
    assert "1 matching" in run(monkeypatch, body, ["--grep", "under"])


def test_grep_double_quote_rejected(monkeypatch, capsys):
    monkeypatch.delenv("TEAM_ALLOWED_HOSTS", raising=False)
    assert fetch.main(["http://x.test/", "--grep", 'class"card']) == 2
    assert "'\"'" in capsys.readouterr().err
