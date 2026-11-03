"""Tests for team.sitemetrics: parsing (plain + gz), classification, engaged, returns, targets, privacy, CLI."""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from team import sitemetrics as sm

HOST = "x.formachines.ai"
CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
GPTBOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"
TELEGRAM = "TelegramBot (like TwitterBot)"
PYREQ = "python-requests/2.32.3"
CURL = "curl/8.6.0"
EXAMPLE = ('203.0.113.5 - - [2026-11-02T06:40:12+00:00] "GET /errata/index.json HTTP/1.1" 200 1234 '
           '"https://www.google.com/" "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
           'Chrome/128.0 Safari/537.36" formachines.ai 0.003')

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def line(ip: str, ua: str, path: str, *, day: str = "2026-11-03", status: int = 200, referer: str = "-",
         host: str = HOST, method: str = "GET", t: str = "10:00:00") -> str:
    return (f'{ip} - - [{day}T{t}+00:00] "{method} {path} HTTP/1.1" {status} 512 "{referer}" "{ua}" {host} 0.001\n')


def write_log(path: Path, lines: list[str], gz: bool = False) -> Path:
    data = "".join(lines)
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------------------------- parsing

def test_parse_line_example_format():
    rec = sm.parse_line(EXAMPLE)
    assert rec is not None
    assert rec.ip == "203.0.113.5"
    assert rec.day == "2026-11-02"
    assert rec.method == "GET"
    assert rec.path == "/errata/index.json"
    assert rec.status == 200
    assert rec.referer == "https://www.google.com/"
    assert rec.ua.startswith("Mozilla/5.0 (Macintosh")
    assert rec.host == "formachines.ai"


def test_parse_line_strips_query_and_handles_garbage():
    rec = sm.parse_line(line("1.2.3.4", CURL, "/api/x?token=abc&y=1"))
    assert rec is not None and rec.path == "/api/x" and rec.src == ""
    raw = '1.2.3.4 - - [2026-11-03T10:00:00+00:00] "\\x16\\x03\\x01" 400 0 "-" "-" x.formachines.ai 0.000\n'
    rec = sm.parse_line(raw)
    assert rec is not None and rec.method == "" and rec.ua == "" and rec.status == 400 and rec.src == ""
    assert sm.parse_line("not a log line\n") is None
    assert sm.parse_line("") is None


@pytest.mark.parametrize("path,src", [
    ("/errata/?src=bluesky", "bluesky"),
    ("/errata/?utm=1&src=telegram&x=2", "telegram"),
    ("/errata/?src=dev-to_2", "dev-to_2"),
    ("/errata/?src=bluesky#frag", "bluesky"),
    ("https://x.formachines.ai/errata/?src=devto", "devto"),      # absolute-form request target
    ("/errata/?src=", ""),                                        # empty
    ("/errata/?src=Bluesky", ""),                                 # upper-case not allowed
    ("/errata/?src=blue%20sky", ""),                              # space after unquoting
    ("/errata/?src=" + "a" * 33, ""),                             # too long
    ("/errata/?src=../etc", ""),
    ("/errata/?source=bluesky", ""),                              # other parameter name
    ("/errata/", ""),
])
def test_parse_line_extracts_src_and_strips_query(path, src):
    rec = sm.parse_line(line("1.2.3.4", CHROME, path))
    assert rec is not None
    assert rec.src == src
    assert rec.path == "/errata/"
    assert "?" not in rec.path and "src" not in rec.path


def test_iter_records_reads_plain_and_gz(tmp_path: Path):
    plain = write_log(tmp_path / "access.log", [line("1.2.3.4", CHROME, "/a"), line("1.2.3.4", CHROME, "/b")])
    gz = write_log(tmp_path / "access.log.2.gz", [line("5.6.7.8", CURL, "/c", day="2026-11-01"), "garbage\n"], gz=True)
    stats = sm.ParseStats()
    recs = list(sm.iter_records([plain, gz, tmp_path / "missing.log"], stats))
    assert [r.path for r in recs] == ["/a", "/b", "/c"]
    assert recs[2].day == "2026-11-01"
    assert stats.files == 2 and stats.missing == 1 and stats.lines == 4 and stats.parsed == 3 and stats.skipped == 1


# ---------------------------------------------------------------------------------------------- classification

@pytest.mark.parametrize("ua,ip,path,cls", [
    ("agent-team/0.1 (+https://formachines.ai)", "9.9.9.9", "/", "own"),
    (CHROME, "127.0.0.1", "/", "own"),
    (GOOGLEBOT, "66.249.66.1", "/", "crawler"),
    (GPTBOT, "20.1.1.1", "/errata/", "crawler"),
    (CHROME, "1.1.1.1", "/.env", "scanner"),
    ("sqlmap/1.8", "1.1.1.1", "/index.html", "scanner"),
    (PYREQ, "1.1.1.1", "/feed.xml", "agent"),
    (CURL, "1.1.1.1", "/", "agent"),
    (CHROME, "1.1.1.1", "/", "human"),
    (IPHONE, "1.1.1.1", "/errata/", "human"),
    ("", "1.1.1.1", "/", "unknown"),
    ("foo/1.0", "1.1.1.1", "/", "unknown"),
])
def test_classify_each_class(ua, ip, path, cls):
    assert sm.classify(ua, ip, path)[0] == cls


def test_classify_names_and_markers():
    assert sm.classify(GOOGLEBOT, "1.1.1.1", "/") == ("crawler", "Googlebot")
    assert sm.classify(TELEGRAM, "1.1.1.1", "/") == ("crawler", "TelegramBot")
    assert sm.classify(PYREQ, "1.1.1.1", "/") == ("agent", "python-requests")
    assert sm.classify("Go-http-client/2.0", "1.1.1.1", "/") == ("agent", "go-http-client")
    assert sm.classify("claude-code/1.0", "1.1.1.1", "/") == ("agent", "claude-code")
    # a browser UA with a bot marker is not human; a bot-like UA without a known name is crawler "other"
    assert sm.classify("Mozilla/5.0 (compatible; HeadlessChrome/120)", "1.1.1.1", "/")[0] != "human"
    assert sm.classify("Mozilla/5.0 (compatible; SomeNewBot/1.0; +https://example.org)", "1.1.1.1", "/") == ("crawler", "other")
    # own prefix list given explicitly replaces the default
    assert sm.classify("agent-team/0.1", "9.9.9.9", "/", own_ua_prefixes=("mybot/",))[0] == "unknown"
    assert sm.classify("mybot/1", "9.9.9.9", "/", own_ua_prefixes=("mybot/",))[0] == "own"
    sm.OWN_IPS.add("95.179.139.151")
    try:
        assert sm.classify("curl/8.5.0", "95.179.139.151", "/", own_ua_prefixes=("mybot/",))[0] == "own"
        assert sm.classify("curl/8.5.0", "9.9.9.9", "/", own_ua_prefixes=("mybot/",))[0] != "own"
    finally:
        sm.OWN_IPS.discard("95.179.139.151")


def test_external_referer_and_targets():
    assert sm.external_referer_host("https://www.google.com/", HOST) == "www.google.com"
    assert sm.external_referer_host("https://x.formachines.ai/page", HOST) is None
    assert sm.external_referer_host("https://www.x.formachines.ai/page", HOST) is None
    assert sm.external_referer_host("-", HOST) is None
    assert sm.external_referer_host("", HOST) is None
    assert sm.external_referer_host("http://203.0.113.9/", HOST) == "x.x.x.x"
    assert sm.target_matches("/feed.xml", "/feed.xml")
    assert not sm.target_matches("/feed.xml2", "/feed.xml")
    assert sm.target_matches("/api/v1/x", "/api/") and sm.target_matches("/api", "/api/")
    assert sm.target_matches("/go/vote/2026-11-03/up/", "/go/vote/*/up/") and not sm.target_matches("/go/vote/2026-11-03/down/", "/go/vote/*/up/")
    assert not sm.target_matches("/go/vote/a/b/up/", "/go/vote/*/up/") and sm.target_matches("/go/vote/a/b/up/", "/go/vote/**/up/")
    assert sm.target_matches("/d/2026-11-03/index.json", "/d/*/index.json")
    assert not sm.target_matches("/apix", "/api/")
    assert sm.target_matches("/errata/index.json", "index.json")


# ---------------------------------------------------------------------------------------------------- engaged

def test_engaged_by_asset_or_external_referer(tmp_path: Path):
    lines = [
        # A: page + css → engaged
        line("10.0.0.1", CHROME, "/"), line("10.0.0.1", CHROME, "/static/site.css"),
        # B: page with external referer → engaged
        line("10.0.0.2", CHROME, "/errata/", referer="https://news.ycombinator.com/item?id=1"),
        # C: page only → not engaged
        line("10.0.0.3", CHROME, "/errata/"),
        # D: asset 404 only → not engaged
        line("10.0.0.4", IPHONE, "/missing.js", status=404),
        # E: internal referer only → not engaged
        line("10.0.0.5", IPHONE, "/errata/x", referer=f"https://{HOST}/"),
        # agent loading css does not count as engaged human
        line("10.0.0.6", PYREQ, "/static/site.css"),
    ]
    res = sm.run([write_log(tmp_path / "access.log", lines)], HOST)
    d = res["days"]["2026-11-03"]
    assert d["humans"]["sources"] == 5
    assert d["humans"]["engaged"] == 2
    assert d["humans"]["returns"] == 0
    assert d["classes"]["agent"]["sources"] == 1
    assert d["referers"] == [{"host": "news.ycombinator.com", "requests": 1}]
    assert d["top_pages"][0] == {"path": "/errata/", "requests": 2}
    assert res["totals"]["humans"]["engaged"] == 2


# ---------------------------------------------------------------------------------------------------- returns

def test_returns_across_days_and_history_before_since(tmp_path: Path):
    lines = [
        # day 1: A and B seen as humans (B not engaged)
        line("10.0.0.1", CHROME, "/", day="2026-11-01"), line("10.0.0.1", CHROME, "/s.css", day="2026-11-01"),
        line("10.0.0.2", CHROME, "/", day="2026-11-01"),
        # day 2: A engaged again (return), B engaged (return: seen day 1), C new engaged (not return), D not engaged
        line("10.0.0.1", CHROME, "/s.css", day="2026-11-02"),
        line("10.0.0.2", CHROME, "/", day="2026-11-02", referer="https://t.co/x"),
        line("10.0.0.3", CHROME, "/s.css", day="2026-11-02"),
        line("10.0.0.2", IPHONE, "/", day="2026-11-02"),       # same ip, other UA = other source
        # day 3: A again with a different UA = new source; C returns
        line("10.0.0.1", IPHONE, "/s.css", day="2026-11-03"),
        line("10.0.0.3", CHROME, "/", day="2026-11-03", referer="https://t.co/y"),
    ]
    log_path = write_log(tmp_path / "access.log", lines)
    res = sm.run([log_path], HOST)
    days = res["days"]
    assert days["2026-11-01"]["humans"] == {"requests": 3, "sources": 2, "engaged": 1, "returns": 0}
    assert days["2026-11-02"]["humans"]["engaged"] == 3 and days["2026-11-02"]["humans"]["returns"] == 2
    assert days["2026-11-03"]["humans"]["engaged"] == 2 and days["2026-11-03"]["humans"]["returns"] == 1
    assert res["totals"]["humans"]["returns"] == 3
    # with --since, day 1 is history only: not reported, but still feeds returns of day 2
    res2 = sm.run([log_path], HOST, since="2026-11-02", until="2026-11-02")
    assert list(res2["days"]) == ["2026-11-02"]
    assert res2["days"]["2026-11-02"]["humans"]["returns"] == 2
    assert res2["window"] == {"since": "2026-11-02", "until": "2026-11-02"}
    assert res2["totals"]["days"] == 1


# ---------------------------------------------------------------------------------------------------- targets

def test_targets_count_requests_and_sources_by_class(tmp_path: Path):
    lines = [
        line("10.0.0.1", CHROME, "/feed.xml"), line("10.0.0.1", CHROME, "/feed.xml"),
        line("10.0.0.2", PYREQ, "/feed.xml"), line("10.0.0.3", CURL, "/feed.xml"), line("10.0.0.3", CURL, "/feed.xml?x=1"),
        line("10.0.0.4", GOOGLEBOT, "/feed.xml"),
        line("10.0.0.5", CHROME, "/api/v1/errata"), line("10.0.0.6", PYREQ, "/api/v1/errata"),
        line("10.0.0.7", CURL, "/errata/index.json"), line("10.0.0.8", CHROME, "/other"),
    ]
    res = sm.run([write_log(tmp_path / "access.log", lines)], HOST, targets=["/feed.xml", "/api/", "index.json"])
    t = res["days"]["2026-11-03"]["targets"]
    assert set(t) == {"/feed.xml", "/api/", "index.json"}
    assert t["/feed.xml"]["requests"] == 6
    assert t["/feed.xml"]["human"] == {"requests": 2, "sources": 1}
    assert t["/feed.xml"]["agent"] == {"requests": 3, "sources": 2}
    assert t["/feed.xml"]["crawler"] == {"requests": 1, "sources": 1}
    assert t["/api/"]["human"] == {"requests": 1, "sources": 1} and t["/api/"]["agent"] == {"requests": 1, "sources": 1}
    assert t["index.json"]["agent"] == {"requests": 1, "sources": 1} and t["index.json"]["human"]["requests"] == 0
    assert res["totals"]["targets"]["/feed.xml"]["agent"]["sources"] == 2
    assert res["totals"]["agents"]["by_family"] == {"curl": 3, "python-requests": 2}
    assert res["totals"]["crawlers"]["by_bot"] == {"Googlebot": 1}


# ------------------------------------------------------------------------------------------------------- by_src

def test_by_src_counts_classes_human_and_engaged(tmp_path: Path):
    lines = [
        # bluesky, day 1: A human + css (engaged, css without src) · B human page only · C human with ext referer
        line("10.0.0.1", CHROME, "/errata/?src=bluesky", day="2026-11-03"),
        line("10.0.0.1", CHROME, "/static/site.css", day="2026-11-03"),
        line("10.0.0.2", IPHONE, "/errata/?src=bluesky", day="2026-11-03"),
        line("10.0.0.3", CHROME, "/errata/?src=bluesky", day="2026-11-03", referer="https://bsky.app/profile/x"),
        # bluesky: a link-preview bot and an agent also follow the tagged link — counted in requests/sources only
        line("10.0.0.4", TELEGRAM, "/errata/?src=bluesky", day="2026-11-03"),
        line("10.0.0.5", PYREQ, "/errata/?src=bluesky", day="2026-11-03"),
        line("10.0.0.5", PYREQ, "/errata/?src=bluesky", day="2026-11-03"),
        # telegram, day 1: one human, not engaged
        line("10.0.0.6", CHROME, "/errata/?src=telegram", day="2026-11-03"),
        # invalid src values are ignored: not a key in by_src, request still counted as human
        line("10.0.0.7", CHROME, "/errata/?src=Bluesky", day="2026-11-03"),
        line("10.0.0.8", CHROME, "/errata/?src=blue%20sky", day="2026-11-03"),
        line("10.0.0.9", CHROME, "/errata/?src=" + "z" * 40, day="2026-11-03"),
        # day 2: A again via telegram (engaged via css), B via bluesky again (totals must not double-count B)
        line("10.0.0.1", CHROME, "/errata/?src=telegram", day="2026-11-04"),
        line("10.0.0.1", CHROME, "/static/site.css", day="2026-11-04"),
        line("10.0.0.2", IPHONE, "/errata/?src=bluesky", day="2026-11-04"),
    ]
    res = sm.run([write_log(tmp_path / "access.log", lines)], HOST)
    d1, d2 = res["days"]["2026-11-03"], res["days"]["2026-11-04"]
    # classification unchanged by the query string
    assert d1["humans"]["sources"] == 7 and d1["humans"]["engaged"] == 2
    assert d1["classes"]["crawler"]["requests"] == 1 and d1["classes"]["agent"]["requests"] == 2
    assert d1["top_pages"][0] == {"path": "/errata/", "requests": 7}
    assert set(d1["by_src"]) == {"bluesky", "telegram"}
    assert d1["by_src"]["bluesky"] == {"requests": 6, "sources": 5, "human_sources": 3, "engaged_sources": 2}
    assert d1["by_src"]["telegram"] == {"requests": 1, "sources": 1, "human_sources": 1, "engaged_sources": 0}
    assert list(d1["by_src"]) == ["bluesky", "telegram"]       # ordered by requests
    assert d2["by_src"]["telegram"] == {"requests": 1, "sources": 1, "human_sources": 1, "engaged_sources": 1}
    assert d2["by_src"]["bluesky"] == {"requests": 1, "sources": 1, "human_sources": 1, "engaged_sources": 0}
    tot = res["totals"]["by_src"]
    assert tot["bluesky"] == {"requests": 7, "sources": 5, "human_sources": 3, "engaged_sources": 2}
    assert tot["telegram"] == {"requests": 2, "sources": 2, "human_sources": 2, "engaged_sources": 1}
    md = sm.to_markdown(res)
    assert "## Sources by src" in md
    assert "| src | human sources | engaged | requests |" in md
    assert "| `bluesky` | 3 | 2 | 7 |" in md
    assert "| `telegram` | 2 | 1 | 2 |" in md
    assert "Bluesky" not in json.dumps(res["totals"]["by_src"]) and "blue sky" not in md


def test_by_src_window_and_empty(tmp_path: Path):
    lines = [
        line("10.0.0.1", CHROME, "/?src=devto", day="2026-11-01"),
        line("10.0.0.2", CHROME, "/?src=devto", day="2026-11-02"),
        line("10.0.0.3", CHROME, "/", day="2026-11-03"),
    ]
    log_path = write_log(tmp_path / "access.log", lines)
    res = sm.run([log_path], HOST, since="2026-11-02")
    assert res["days"]["2026-11-02"]["by_src"]["devto"]["human_sources"] == 1
    assert res["days"]["2026-11-03"]["by_src"] == {}
    assert res["totals"]["by_src"] == {"devto": {"requests": 1, "sources": 1, "human_sources": 1, "engaged_sources": 0}}
    res = sm.run([log_path], HOST, since="2026-11-03")
    assert res["totals"]["by_src"] == {}
    assert "Sources by src" not in sm.to_markdown(res)


# ----------------------------------------------------------------------------------------------------- privacy

def test_output_contains_no_ip_addresses(tmp_path: Path):
    lines = [
        line("203.0.113.5", CHROME, "/", referer="https://www.google.com/"),
        line("203.0.113.5", CHROME, "/s.css"),
        line("198.51.100.7", CHROME, "/page", referer="http://198.51.100.9/ref"),   # referer host is an IP
        line("198.51.100.8", CHROME, "/go/http://192.0.2.1/x"),                      # IP inside a human path
        line("192.0.2.44", PYREQ, "/feed.xml"), line("192.0.2.45", GOOGLEBOT, "/"), line("192.0.2.46", "", "/"),
        line("192.0.2.47", "zgrab/0.x", "/.env"),
    ]
    res = sm.run([write_log(tmp_path / "access.log", lines)], HOST, targets=["/feed.xml"])
    text = json.dumps(res, ensure_ascii=False)
    assert not IPV4.search(text), IPV4.findall(text)
    assert not IPV4.search(sm.to_markdown(res))
    # user-agents are not in the output either
    assert "Chrome/128.0" not in text and "python-requests" in text   # family name only


# -------------------------------------------------------------------------------------------- other hosts / shape

def test_other_hosts_are_ignored_and_status_buckets(tmp_path: Path):
    lines = [
        line("10.0.0.1", CHROME, "/", status=200), line("10.0.0.1", CHROME, "/r", status=301),
        line("10.0.0.2", CHROME, "/nope", status=404), line("10.0.0.2", CHROME, "/err", status=502),
        line("10.0.0.9", CHROME, "/", host="other.example"), line("10.0.0.9", CHROME, "/", host="X.FORMACHINES.AI"),
    ]
    res = sm.run([write_log(tmp_path / "access.log", lines)], "X.formachines.ai")
    d = res["days"]["2026-11-03"]
    assert res["host"] == HOST
    assert d["requests_raw"] == 5
    assert d["status"] == {"2xx": 2, "3xx": 1, "4xx": 1, "5xx": 1}
    assert res["parse"]["parsed"] == 6
    assert set(res) == {"host", "generated", "window", "targets", "parse", "days", "totals"}
    assert set(d) == {"requests_raw", "status", "classes", "humans", "agents", "crawlers", "scanners", "targets",
                      "top_pages", "referers", "by_src"}
    assert d["by_src"] == {} and res["totals"]["by_src"] == {}
    assert "## Sources by src" not in sm.to_markdown(res)


def test_empty_result_is_well_formed(tmp_path: Path):
    res = sm.run([write_log(tmp_path / "access.log", [line("10.0.0.1", CHROME, "/", host="other")])], HOST,
                 targets=["/api/"])
    assert res["days"] == {} and res["window"] == {"since": None, "until": None}
    assert res["totals"]["requests_raw"] == 0 and res["totals"]["targets"]["/api/"]["requests"] == 0
    assert "| **total** |" in sm.to_markdown(res)


# --------------------------------------------------------------------------------------------------------- CLI

def test_cli_writes_json_and_md(tmp_path: Path, capsys):
    log_path = write_log(tmp_path / "access.log", [
        line("10.0.0.1", CHROME, "/", referer="https://news.ycombinator.com/"), line("10.0.0.2", PYREQ, "/feed.xml"),
    ])
    out = tmp_path / "out"
    rc = sm.main(["--host", HOST, "--log", str(log_path), "--log", str(tmp_path / "nope.gz"),
                  "--target", "/feed.xml", "--out", str(out)])
    assert rc == 0
    data = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert data["host"] == HOST and data["days"]["2026-11-03"]["targets"]["/feed.xml"]["agent"]["sources"] == 1
    md = (out / "metrics.md").read_text(encoding="utf-8")
    assert "| 2026-11-03 | 2 | 1/1/0 | 1 (1) |" in md
    assert "news.ycombinator.com" in md
    # stdout mode
    rc = sm.main(["--host", HOST, "--log", str(log_path), "--out", "-"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["totals"]["requests_raw"] == 2


def test_cli_all_hosts_writes_per_host_and_skips_unsafe(tmp_path: Path):
    log_path = write_log(tmp_path / "access.log", [
        line("10.0.0.1", CHROME, "/", host="a.example"), line("10.0.0.1", CHROME, "/", host="b.example"),
        line("10.0.0.1", CHROME, "/", host="../evil"), line("10.0.0.1", CHROME, "/", host="203.0.113.1"),
    ])
    out = tmp_path / "all"
    assert sm.main(["--all-hosts", "--log", str(log_path), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["a.example", "b.example"]
    assert json.loads((out / "a.example" / "metrics.json").read_text())["totals"]["requests_raw"] == 1
    assert not (tmp_path / "evil").exists()


def test_cli_all_hosts_filters_with_hosts_flag(tmp_path: Path):
    log_path = write_log(tmp_path / "access.log", [
        line("10.0.0.1", CHROME, "/", host="a.example"), line("10.0.0.1", CHROME, "/", host="b.example"),
        line("10.0.0.1", CHROME, "/", host="b.example"), line("10.0.0.1", CHROME, "/", host="c.example"),
        line("10.0.0.1", CHROME, "/", host="junk-host.example"),
    ])
    # API: only_hosts restricts the result; names are case-insensitive; a listed host absent from the log is just absent
    res = sm.run_all_hosts([log_path], only_hosts={"A.example", "b.example", "zzz.example"})
    assert sorted(res) == ["a.example", "b.example"]
    assert res["b.example"]["totals"]["requests_raw"] == 2
    assert sorted(sm.run_all_hosts([log_path], only_hosts=None)) == ["a.example", "b.example", "c.example",
                                                                      "junk-host.example"]
    # --min-requests still applies on top of --hosts
    assert sorted(sm.run_all_hosts([log_path], only_hosts={"a.example", "b.example"}, min_requests=2)) == ["b.example"]
    # CLI: only the listed directories are created
    out = tmp_path / "all"
    assert sm.main(["--all-hosts", "--log", str(log_path), "--out", str(out), "--hosts", "a.example, b.example"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["a.example", "b.example"]
    assert not (out / "c.example").exists() and not (out / "junk-host.example").exists()
    # empty --hosts is an error, not "no filter"
    assert sm.main(["--all-hosts", "--log", str(log_path), "--out", str(tmp_path / "x"), "--hosts", " , "]) == 2
    assert not (tmp_path / "x").exists()


def test_cli_rejects_bad_date(tmp_path: Path):
    log_path = write_log(tmp_path / "access.log", [line("10.0.0.1", CHROME, "/")])
    assert sm.main(["--host", HOST, "--log", str(log_path), "--since", "03.11.2026", "--out", "-"]) == 2


def test_scanner_probes_at_any_depth():
    for path in ("/app/.env", "/api/.env", "/config/.env", "/old/wp-login.php", "/x/.git/config", "/foo/phpmyadmin/", "/a/.aws/credentials"):
        assert sm.classify("Mozilla/5.0 (X11; Linux) Chrome/120", "9.9.9.9", path)[0] == "scanner", path
    assert sm.classify("Mozilla/5.0 (X11; Linux) Chrome/120", "9.9.9.9", "/pkg/npm/dotenv/")[0] == "human"     # 'env' inside a name is not a probe
    assert sm.classify("Mozilla/5.0 (X11; Linux) Chrome/120", "9.9.9.9", "/environment/")[0] == "human"
