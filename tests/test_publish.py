"""Tests for team.publish: payloads per channel, bluesky truncation/facets, secrets, dry-run, ledger. No network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from team import publish
from team.publish import DEFAULT_DISCLOSURE, PostResult, bluesky_facets, compose, graphemes, post

DISC = "Written and posted by an AI agent."


class FakeHttp:
    """Records calls; answers by URL substring with (status, json)."""

    def __init__(self, routes: dict[str, tuple[int, object]] | None = None):
        self.routes = routes or {}
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "json": json.loads(body) if body else None, "timeout": timeout})
        for key, (status, data) in self.routes.items():
            if key in url:
                return status, (data if isinstance(data, bytes) else json.dumps(data).encode())
        return 404, b'{"error":"no route"}'


# ---- telegram ---------------------------------------------------------------------------------
def test_telegram_payload_and_link():
    http = FakeHttp({"sendMessage": (200, {"ok": True, "result": {"message_id": 42, "chat": {"username": "chan"}}})})
    env = {"TELEGRAM_BOT_TOKEN": "123:SECRET"}
    r = post("telegram", "Hello <b> & world", url="https://x.io/p", disclosure=DISC, target="@chan", env=env, http=http)
    assert r.ok and r.post_id == "42" and r.url == "https://t.me/chan/42" and not r.dry_run
    call = http.calls[0]
    assert call["url"] == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert call["timeout"] == 20 and call["headers"]["Content-Type"] == "application/json"
    p = call["json"]
    assert p["chat_id"] == "@chan" and p["parse_mode"] == "HTML" and p["disable_web_page_preview"] is False
    assert p["text"] == f"Hello &lt;b&gt; &amp; world\nhttps://x.io/p\n\n<i>{DISC}</i>"


def test_telegram_requires_target():
    r = post("telegram", "x", disclosure=DISC, env={"TELEGRAM_BOT_TOKEN": "t"}, http=FakeHttp())
    assert not r.ok and "target" in r.error


def test_telegram_http_error_has_status_and_body_no_token():
    http = FakeHttp({"sendMessage": (400, {"ok": False, "description": "Bad Request: chat not found"})})
    r = post("telegram", "x", disclosure=DISC, target="@c", env={"TELEGRAM_BOT_TOKEN": "TOK"}, http=http)
    assert not r.ok and "HTTP 400" in r.error and "chat not found" in r.error and "TOK" not in r.error


def test_network_exception_becomes_result():
    def boom(*a):
        raise OSError("timed out")
    r = post("mastodon", "x", disclosure=DISC, env={"MASTODON_INSTANCE": "m.io", "MASTODON_TOKEN": "T"}, http=boom)
    assert not r.ok and "network error" in r.error and "timed out" in r.error


# ---- bluesky ----------------------------------------------------------------------------------
BSKY_ENV = {"BLUESKY_HANDLE": "me.bsky.social", "BLUESKY_APP_PASSWORD": "app-pass"}
BSKY_ROUTES = {"createSession": (200, {"accessJwt": "JWT", "did": "did:plc:abc", "handle": "me.bsky.social"}),
               "createRecord": (200, {"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "bafy"})}


def test_bluesky_session_record_and_url():
    http = FakeHttp(BSKY_ROUTES)
    r = post("bluesky", "Hi there", disclosure=DISC, env=BSKY_ENV, http=http)
    assert r.ok and r.post_id == "at://did:plc:abc/app.bsky.feed.post/3kxyz"
    assert r.url == "https://bsky.app/profile/me.bsky.social/post/3kxyz"
    sess, rec = http.calls
    assert sess["url"].endswith("/xrpc/com.atproto.server.createSession")
    assert sess["json"] == {"identifier": "me.bsky.social", "password": "app-pass"}
    assert rec["headers"]["Authorization"] == "Bearer JWT"
    assert rec["json"]["repo"] == "did:plc:abc" and rec["json"]["collection"] == "app.bsky.feed.post"
    record = rec["json"]["record"]
    assert record["$type"] == "app.bsky.feed.post" and record["text"] == f"Hi there\n\n{DISC}" and "createdAt" in record
    assert "facets" not in record


def test_bluesky_truncates_to_300_graphemes_keeping_disclosure():
    http = FakeHttp(BSKY_ROUTES)
    long = "👨‍👩‍👧 " + "é" * 400  # ZWJ family + combining-mark letters
    r = post("bluesky", long, disclosure=DISC, env=BSKY_ENV, http=http)
    assert r.ok
    text = http.calls[1]["json"]["record"]["text"]
    assert len(graphemes(text)) == 300
    assert text.endswith("\n\n" + DISC)
    body = text[: -len("\n\n" + DISC)]
    assert body.endswith("…") and body.startswith("👨‍👩‍👧 ")


def test_bluesky_facets_byte_offsets_utf8():
    text = "Ünïcode before https://example.com/a?b=1, then more"
    facets = bluesky_facets(text)
    assert len(facets) == 1
    f = facets[0]
    assert f["features"] == [{"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com/a?b=1"}]
    start = text.index("https://")
    assert f["index"]["byteStart"] == len(text[:start].encode()) == start + 2  # Ü and ï are 2 bytes each
    assert text.encode()[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode() == "https://example.com/a?b=1"


def test_bluesky_url_argument_becomes_facet():
    http = FakeHttp(BSKY_ROUTES)
    r = post("bluesky", "Read this", url="https://formachines.ai/errata/", disclosure=DISC, env=BSKY_ENV, http=http)
    assert r.ok
    record = http.calls[1]["json"]["record"]
    assert record["text"] == f"Read this\nhttps://formachines.ai/errata/\n\n{DISC}"
    assert record["facets"][0]["features"][0]["uri"] == "https://formachines.ai/errata/"
    assert record["facets"][0]["index"] == {"byteStart": 10, "byteEnd": 10 + len("https://formachines.ai/errata/")}


def test_bluesky_session_failure():
    http = FakeHttp({"createSession": (401, {"error": "AuthenticationRequired", "message": "Invalid identifier or password"})})
    r = post("bluesky", "x", disclosure=DISC, env=BSKY_ENV, http=http)
    assert not r.ok and "createSession" in r.error and "HTTP 401" in r.error and "app-pass" not in r.error
    assert len(http.calls) == 1


# ---- devto ------------------------------------------------------------------------------------
def test_devto_payload():
    http = FakeHttp({"dev.to/api/articles": (201, {"id": 77, "url": "https://dev.to/me/post-1"})})
    r = post("devto", "# Body\n\ntext", title="Post 1", url="https://site/p", tags=["a", "b", "c", "d", "e"],
             disclosure=DISC, env={"DEVTO_API_KEY": "KEY"}, http=http)
    assert r.ok and r.post_id == "77" and r.url == "https://dev.to/me/post-1"
    call = http.calls[0]
    assert call["url"] == "https://dev.to/api/articles" and call["headers"]["api-key"] == "KEY"
    art = call["json"]["article"]
    assert art["title"] == "Post 1" and art["published"] is True and art["canonical_url"] == "https://site/p"
    assert art["tags"] == ["a", "b", "c", "d"]
    assert art["body_markdown"].startswith(f"*{DISC}*\n\n# Body")


def test_devto_requires_title():
    r = post("devto", "x", disclosure=DISC, env={"DEVTO_API_KEY": "K"}, http=FakeHttp())
    assert not r.ok and "title" in r.error


# ---- mastodon ---------------------------------------------------------------------------------
def test_mastodon_payload_and_limit():
    http = FakeHttp({"/api/v1/statuses": (200, {"id": "111", "url": "https://m.io/@me/111"})})
    env = {"MASTODON_INSTANCE": "m.io", "MASTODON_TOKEN": "TOK"}
    r = post("mastodon", "a" * 600, disclosure=DISC, env=env, http=http)
    assert r.ok and r.post_id == "111" and r.url == "https://m.io/@me/111"
    call = http.calls[0]
    assert call["url"] == "https://m.io/api/v1/statuses" and call["headers"]["Authorization"] == "Bearer TOK"
    status = call["json"]["status"]
    assert len(status) == 500 and status.endswith("\n\n" + DISC) and "…" in status


# ---- x stub, secrets, disclosure -----------------------------------------------------------------
def test_x_not_configured():
    r = post("x", "x", disclosure=DISC, env={}, http=FakeHttp())
    assert r == PostResult(False, "x", None, None, "x: not configured", False)


def test_unknown_channel():
    r = post("nope", "x", disclosure=DISC, env={}, http=FakeHttp())
    assert not r.ok and "unknown channel" in r.error


@pytest.mark.parametrize("channel,target", [("telegram", "@c"), ("bluesky", None), ("devto", None), ("mastodon", None)])
def test_missing_secret_is_result_not_exception(channel, target):
    http = FakeHttp()
    r = post(channel, "x", title="t", disclosure=DISC, target=target, env={}, http=http)
    assert not r.ok and "missing secret" in r.error and http.calls == []


def test_empty_disclosure_falls_back_to_default():
    http = FakeHttp(BSKY_ROUTES)
    post("bluesky", "x", disclosure="", env=BSKY_ENV, http=http)
    assert http.calls[1]["json"]["record"]["text"].endswith(DEFAULT_DISCLOSURE)


def test_compose_rejects_limit_smaller_than_disclosure():
    with pytest.raises(publish.PublishError):
        compose("body", "d" * 300, None, limit=300)


# ---- dry run ----------------------------------------------------------------------------------
@pytest.mark.parametrize("channel,target", [("telegram", "123"), ("bluesky", None), ("devto", None), ("mastodon", None)])
def test_dry_run_no_http_no_secrets_needed(channel, target, capsys):
    http = FakeHttp()
    r = post(channel, "text", title="t", disclosure=DISC, target=target, env={}, http=http, dry_run=True)
    assert r.ok and r.dry_run and http.calls == []
    err = capsys.readouterr().err
    assert '"dry_run": true' in err and DISC in err


def test_env_guard_forces_dry_run():
    http = FakeHttp(BSKY_ROUTES)
    r = post("bluesky", "x", disclosure=DISC, env={**BSKY_ENV, "TEAM_PUBLISH_DRY_RUN": "1"}, http=http)
    assert r.ok and r.dry_run and http.calls == []


def test_default_http_is_urllib_and_mockable(monkeypatch):
    class Resp:
        status = 200

        def read(self):
            return b'{"ok": true, "result": {"message_id": 5}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["timeout"], seen["data"] = req.full_url, timeout, req.data
        return Resp()

    monkeypatch.setattr(publish.urllib.request, "urlopen", fake_urlopen)
    r = post("telegram", "hi", disclosure=DISC, target="42", env={"TELEGRAM_BOT_TOKEN": "T"})
    assert r.ok and r.post_id == "5" and r.url is None
    assert seen["timeout"] == 20 and "/botT/sendMessage" in seen["url"] and json.loads(seen["data"])["chat_id"] == "42"


# ---- CLI + ledger -----------------------------------------------------------------------------
def test_cli_dry_run_writes_ledger(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("TEAM_PUBLISH_DRY_RUN", raising=False)
    src = tmp_path / "post.md"
    src.write_text("Post body\n", encoding="utf-8")
    ledger = tmp_path / "sub" / "ledger.jsonl"
    code = publish.main(["--channel", "telegram", "--target", "@chan", "--text-file", str(src), "--dry-run",
                         "--ledger", str(ledger), "--disclosure", DISC, "--tags", "a, b,"])
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    res = json.loads(out[0])
    assert res["ok"] is True and res["dry_run"] is True and res["channel"] == "telegram"
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "telegram" and row["target"] == "@chan" and row["ok"] is True and row["dry_run"] is True
    assert row["sha256"] == publish.sha256_text("Post body\n") and row["ts"] and row["post_id"] is None
    assert set(row) == {"ts", "channel", "target", "ok", "post_id", "url", "error", "dry_run", "sha256"}


def test_cli_failure_exit_code_and_ledger_error(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("TEAM_PUBLISH_DRY_RUN", raising=False)
    monkeypatch.delenv("DEVTO_API_KEY", raising=False)
    ledger = tmp_path / "ledger.jsonl"
    code = publish.main(["--channel", "devto", "--text", "body", "--title", "T", "--ledger", str(ledger)])
    assert code == 1
    res = json.loads(capsys.readouterr().out.strip())
    assert res["ok"] is False and "missing secret" in res["error"]
    row = json.loads(ledger.read_text().strip())
    assert row["ok"] is False and "missing secret" in row["error"]


def test_cli_env_guard(monkeypatch, capsys):
    monkeypatch.setenv("TEAM_PUBLISH_DRY_RUN", "1")
    monkeypatch.setenv("MASTODON_INSTANCE", "https://m.io")
    monkeypatch.setenv("MASTODON_TOKEN", "T")
    monkeypatch.setattr(publish.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network call in dry run"))
    assert publish.main(["--channel", "mastodon", "--text", "x"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["dry_run"] is True


# ---- graphemes --------------------------------------------------------------------------------
def test_graphemes_clusters():
    assert len(graphemes("abc")) == 3
    assert len(graphemes("é")) == 1
    assert len(graphemes("👨‍👩‍👧‍👦")) == 1
    assert len(graphemes("🇵🇹🇷🇺")) == 2
    assert len(graphemes("👍🏽")) == 1
    assert len(graphemes("1️⃣")) == 1
