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
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        self.calls.append({"method": method, "url": url, "headers": headers, "json": parsed, "raw": body, "timeout": timeout})
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


def test_bluesky_reply_to_web_link_becomes_at_uri():
    # 07.11 T-0057: the bsky.app link went to getPosts as is and got HTTP 400
    parent = {"uri": "at://did:plc:me/app.bsky.feed.post/3mzn", "cid": "pc", "record": {}}
    http = FakeHttp({"resolveHandle": (200, {"did": "did:plc:me"}), "getPosts": (200, {"posts": [parent]}), **BSKY_ROUTES})
    r = post("bluesky", "Correction", disclosure=DISC, env=BSKY_ENV, http=http,
             reply_to="https://bsky.app/profile/formachines.bsky.social/post/3mzn")
    assert r.ok
    res, get = http.calls[0], http.calls[1]
    assert "handle=formachines.bsky.social" in res["url"]
    assert publish.urllib.parse.quote("at://did:plc:me/app.bsky.feed.post/3mzn", safe="") in get["url"]
    reply = http.calls[-1]["json"]["record"]["reply"]
    assert reply == {"root": {"uri": parent["uri"], "cid": "pc"}, "parent": {"uri": parent["uri"], "cid": "pc"}}


def test_bluesky_at_uri_passes_through_and_did_link_skips_resolve():
    http = FakeHttp()
    assert publish.bluesky_at_uri(http, "at://did:plc:x/app.bsky.feed.post/1") == "at://did:plc:x/app.bsky.feed.post/1"
    assert publish.bluesky_at_uri(http, "https://bsky.app/profile/did:plc:x/post/2") == "at://did:plc:x/app.bsky.feed.post/2"
    assert http.calls == []


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


class SeqHttp:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(json.loads(body))
        status, data = self.answers.pop(0)
        return status, json.dumps(data).encode()


def test_devto_canonical_taken_retries_with_suffix():
    http = SeqHttp([(422, {"error": "Canonical url has already been taken"}) , (201, {"id": 5, "url": "https://dev.to/x"})])
    r = post("devto", "b", title="T", url="https://site/p", disclosure=DISC, env={"DEVTO_API_KEY": "K"}, http=http)
    assert r.ok and len(http.calls) == 2
    import re
    assert re.search(r"\?src=devto&d=\d{4}-\d{2}-\d{2}$", http.calls[1]["article"]["canonical_url"])
    http2 = SeqHttp([(422, {"error": "canonical_url taken"}), (201, {"id": 6})])
    r = post("devto", "b", title="T", url="https://site/p?a=1", disclosure=DISC, env={"DEVTO_API_KEY": "K"}, http=http2)
    assert r.ok and "?a=1&src=devto&d=" in http2.calls[1]["article"]["canonical_url"]


def test_devto_other_422_not_retried():
    http = SeqHttp([(422, {"error": "Title is too long"}), (201, {"id": 5})])
    r = post("devto", "b", title="T", url="https://site/p", disclosure=DISC, env={"DEVTO_API_KEY": "K"}, http=http)
    assert not r.ok and len(http.calls) == 1


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


# ---- threads ----------------------------------------------------------------------------------
TH_ENV = {"THREADS_USER_ID": "1789", "THREADS_ACCESS_TOKEN": "THTOK/+="}


def test_threads_container_publish_permalink(monkeypatch):
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    http = FakeHttp({"/1789/threads_publish": (200, {"id": "M55"}), "/1789/threads": (200, {"id": "C1"}),
                     "/M55?fields=permalink": (200, {"permalink": "https://www.threads.com/@formachines/post/AbC"})})
    r = post("threads", "Hello threads", url="https://s.io/p", disclosure=DISC, env=TH_ENV, http=http)
    assert r.ok and r.post_id == "M55" and r.url == "https://www.threads.com/@formachines/post/AbC"
    create, pub, meta = http.calls
    assert create["url"] == "https://graph.threads.net/v1.0/1789/threads" and create["method"] == "POST"
    assert create["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    form = dict(publish.urllib.parse.parse_qsl(create["raw"].decode()))
    assert form == {"media_type": "TEXT", "text": f"Hello threads\nhttps://s.io/p\n\n{DISC}", "access_token": "THTOK/+="}
    assert dict(publish.urllib.parse.parse_qsl(pub["raw"].decode())) == {"creation_id": "C1", "access_token": "THTOK/+="}
    assert "THTOK" not in create["url"] and "THTOK" not in pub["url"]


def test_threads_publish_retries_then_fails_without_token_in_error(monkeypatch):
    slept = []
    monkeypatch.setattr(publish.time, "sleep", lambda s: slept.append(s))
    http = FakeHttp({"/threads_publish": (400, {"error": {"message": "Media not ready THTOK/+="}}), "/threads": (200, {"id": "C1"})})
    r = post("threads", "x", disclosure=DISC, env=TH_ENV, http=http)
    assert not r.ok and "threads.publish" in r.error and "HTTP 400" in r.error and "THTOK" not in r.error
    assert len(slept) == publish.THREADS_PUBLISH_RETRIES and len(http.calls) == 1 + publish.THREADS_PUBLISH_RETRIES


def test_threads_reply_to_link_becomes_media_id(monkeypatch):
    # 07.11 T-0057: a threads.com link went out as reply_to_id and Threads answered 500
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    http = FakeHttp({"/1789/threads?fields": (200, {"data": [{"id": "M1", "permalink": "https://www.threads.com/@formachines/post/Zz"},
                                                             {"id": "M2", "permalink": "https://www.threads.com/@formachines/post/DfaG_0FDBtN"}]}),
                     "/1789/threads_publish": (200, {"id": "M56"}), "/1789/threads": (200, {"id": "C2"}),
                     "/M56?fields=permalink": (200, {"permalink": "https://www.threads.com/@formachines/post/New"})})
    r = post("threads", "Correction", disclosure=DISC, env=TH_ENV, http=http,
             reply_to="https://www.threads.com/@formachines/post/DfaG_0FDBtN")
    assert r.ok and r.post_id == "M56"
    lookup, create = http.calls[0], http.calls[1]
    assert lookup["method"] == "GET" and "/1789/threads?" in lookup["url"]
    assert dict(publish.urllib.parse.parse_qsl(create["raw"].decode()))["reply_to_id"] == "M2"


def test_threads_reply_to_unknown_link_fails_without_token():
    http = FakeHttp({"/1789/threads?fields": (200, {"data": [{"id": "M1", "permalink": "https://www.threads.com/@formachines/post/Zz"}]})})
    r = post("threads", "x", disclosure=DISC, env=TH_ENV, http=http, reply_to="https://www.threads.net/@formachines/post/Missing")
    assert not r.ok and "Missing" in r.error and "THTOK" not in r.error and len(http.calls) == 1


def test_threads_reply_to_media_id_passes_through(monkeypatch):
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    http = FakeHttp({"/1789/threads_publish": (200, {"id": "M57"}), "/1789/threads": (200, {"id": "C3"}),
                     "/M57?fields=permalink": (200, {"permalink": "p"})})
    r = post("threads", "x", disclosure=DISC, env=TH_ENV, http=http, reply_to="17900000000000001")
    assert r.ok and dict(publish.urllib.parse.parse_qsl(http.calls[0]["raw"].decode()))["reply_to_id"] == "17900000000000001"


def test_threads_limit_500_chars():
    http = FakeHttp({"/threads_publish": (200, {"id": "M"}), "/threads": (200, {"id": "C"}), "fields=permalink": (500, b"x")})
    r = post("threads", "b" * 900, disclosure=DISC, env=TH_ENV, http=http)
    assert r.ok and r.url is None                      # permalink failure does not fail a published post
    text = dict(publish.urllib.parse.parse_qsl(http.calls[0]["raw"].decode()))["text"]
    assert len(text) == 500 and text.endswith(DISC) and "…" in text


def test_threads_refresh_token():
    http = FakeHttp({"refresh_access_token": (200, {"access_token": "NEW", "token_type": "bearer", "expires_in": 5183944})})
    assert publish.threads_refresh_token("OLD", http=http) == ("NEW", 5183944)
    assert "grant_type=th_refresh_token" in http.calls[0]["url"]


# ---- x ----------------------------------------------------------------------------------------
X_ENV = {"X_API_KEY": "ck", "X_API_SECRET": "cs", "X_ACCESS_TOKEN": "at", "X_ACCESS_SECRET": "as", "X_HANDLE": "@formachines"}


def test_oauth1_signature_matches_documented_example():
    h = publish.oauth1_header("POST", "https://api.twitter.com/1.1/statuses/update.json", "xvz1evFS4wEEPTGEFPHBog",
                              "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw", "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
                              "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE", nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
                              timestamp="1318622958",
                              params={"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"})
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in h and h.startswith("OAuth ")


def test_x_post_payload_header_and_link():
    http = FakeHttp({"api.x.com/2/tweets": (201, {"data": {"id": "1850", "text": "..."}})})
    r = post("x", "Release radar day", url="https://releases.formachines.ai/today/?src=x", disclosure="🤖 AI agent · formachines.ai",
             env=X_ENV, http=http, reply_to="https://x.com/someone/status/77")
    assert r.ok and r.post_id == "1850" and r.url == "https://x.com/formachines/status/1850"
    call = http.calls[0]
    assert call["url"] == "https://api.x.com/2/tweets" and call["headers"]["Content-Type"] == "application/json"
    auth = call["headers"]["Authorization"]
    assert auth.startswith("OAuth ") and 'oauth_consumer_key="ck"' in auth and 'oauth_token="at"' in auth
    assert call["json"]["text"].endswith("\n\n🤖 AI agent · formachines.ai")
    assert call["json"]["reply"] == {"in_reply_to_tweet_id": "77"}


def test_x_weight_counts_links_as_23_and_emoji_as_2():
    assert publish.x_weight("abc") == 3
    assert publish.x_weight("https://example.com/" + "a" * 100) == 23
    assert publish.x_weight("🤖") == 2 and publish.x_weight("👨‍👩‍👧") == 2 and publish.x_weight("日本") == 4
    assert publish.x_weight("see formachines.ai") == 4 + 23
    assert publish.x_weight("see agentteam.formachines.ai") == 4 + len("agentteam.formachines.ai")


def test_x_truncates_to_280_weighted_keeping_url_and_disclosure():
    http = FakeHttp({"/2/tweets": (201, {"data": {"id": "9"}})})
    url = "https://agentteam.formachines.ai/day/2026-11-03/?src=x"
    r = post("x", "日本 " + "word " * 200, url=url, disclosure="🤖 AI agent · formachines.ai", env=X_ENV, http=http)
    assert r.ok
    text = http.calls[0]["json"]["text"]
    assert publish.x_weight(text) <= 280 and publish.x_weight(text) >= 270
    assert text.endswith(f"…\n{url}\n\n🤖 AI agent · formachines.ai")


def test_x_error_redacts_secrets():
    http = FakeHttp({"/2/tweets": (403, {"title": "Forbidden", "detail": "token at not allowed"})})
    r = post("x", "x", disclosure=DISC, env={**X_ENV, "X_ACCESS_TOKEN": "SECRETTOKEN"}, http=http)
    assert not r.ok and "HTTP 403" in r.error and "SECRETTOKEN" not in r.error


def test_x_oauth2_refreshes_rotates_and_reuses(tmp_path):
    state = tmp_path / "x_oauth2.json"
    env = {"X_CLIENT_ID": "CID", "X_CLIENT_SECRET": "CSEC", "X_REFRESH_TOKEN": "RT0", "X_OAUTH2_STATE": str(state), "X_HANDLE": "formachines_ai"}
    http = FakeHttp({"/2/oauth2/token": (200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 7200, "scope": "tweet.write"}),
                     "/2/tweets": (201, {"data": {"id": "5"}})})
    r = post("x", "hello", disclosure=DISC, env=env, http=http)
    assert r.ok and r.url == "https://x.com/formachines_ai/status/5"
    tok, tw = http.calls
    assert tok["headers"]["Authorization"].startswith("Basic ")
    assert dict(publish.urllib.parse.parse_qsl(tok["raw"].decode())) == {"grant_type": "refresh_token", "refresh_token": "RT0", "client_id": "CID"}
    assert tw["headers"]["Authorization"] == "Bearer AT1"
    saved = json.loads(state.read_text())
    assert saved["access_token"] == "AT1" and saved["refresh_token"] == "RT1"
    post("x", "again", disclosure=DISC, env=env, http=http)          # fresh token reused, no second refresh
    assert [c["url"].rsplit("/", 1)[-1] for c in http.calls] == ["token", "tweets", "tweets"]


def test_x_oauth2_expired_uses_rotated_refresh_token_from_state(tmp_path):
    state = tmp_path / "x.json"
    state.write_text(json.dumps({"access_token": "OLD", "refresh_token": "RT5", "expires_at": 0}))
    env = {"X_CLIENT_ID": "CID", "X_CLIENT_SECRET": "CSEC", "X_REFRESH_TOKEN": "RT0-stale", "X_OAUTH2_STATE": str(state)}
    http = FakeHttp({"/2/oauth2/token": (200, {"access_token": "AT6", "refresh_token": "RT6", "expires_in": 7200}),
                     "/2/tweets": (201, {"data": {"id": "6"}})})
    assert post("x", "x", disclosure=DISC, env=env, http=http).ok
    assert dict(publish.urllib.parse.parse_qsl(http.calls[0]["raw"].decode()))["refresh_token"] == "RT5"
    assert json.loads(state.read_text())["refresh_token"] == "RT6"


def test_x_oauth2_refresh_failure_redacts(tmp_path):
    env = {"X_CLIENT_ID": "CID", "X_CLIENT_SECRET": "CSECRET", "X_REFRESH_TOKEN": "RTSECRET", "X_OAUTH2_STATE": str(tmp_path / "s.json")}
    http = FakeHttp({"/2/oauth2/token": (400, {"error": "invalid_request", "error_description": "bad RTSECRET CSECRET"})})
    r = post("x", "x", disclosure=DISC, env=env, http=http)
    assert not r.ok and "x.oauth2.refresh" in r.error and "RTSECRET" not in r.error and "CSECRET" not in r.error


# ---- secrets, disclosure -------------------------------------------------------------------------


def test_unknown_channel():
    r = post("nope", "x", disclosure=DISC, env={}, http=FakeHttp())
    assert not r.ok and "unknown channel" in r.error


@pytest.mark.parametrize("channel,target", [("telegram", "@c"), ("bluesky", None), ("devto", None), ("mastodon", None), ("threads", None), ("x", None)])
def test_missing_secret_is_result_not_exception(channel, target):
    http = FakeHttp()
    r = post(channel, "x", title="t", disclosure=DISC, target=target, env={}, http=http)
    assert not r.ok and "missing secret" in r.error and http.calls == []


def test_threads_refresh_env_rewrites_only_token_line(tmp_path):
    from team import threads_token
    env = tmp_path / ".env"
    env.write_text("A=1\nTHREADS_ACCESS_TOKEN=OLD\nTHREADS_USER_ID=9\n", encoding="utf-8")
    http = FakeHttp({"refresh_access_token": (200, {"access_token": "NEW", "expires_in": 86400})})
    threads_token.refresh_env(env, http=http)
    assert env.read_text(encoding="utf-8") == "A=1\nTHREADS_ACCESS_TOKEN=NEW\nTHREADS_USER_ID=9\n"


def test_empty_disclosure_falls_back_to_default():
    http = FakeHttp(BSKY_ROUTES)
    post("bluesky", "x", disclosure="", env=BSKY_ENV, http=http)
    assert http.calls[1]["json"]["record"]["text"].endswith(DEFAULT_DISCLOSURE)


def test_compose_rejects_limit_smaller_than_disclosure():
    with pytest.raises(publish.PublishError):
        compose("body", "d" * 300, None, limit=300)


# ---- dry run ----------------------------------------------------------------------------------
@pytest.mark.parametrize("channel,target", [("telegram", "123"), ("bluesky", None), ("devto", None), ("mastodon", None), ("threads", None), ("x", None)])
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


# ---- channel caps (owner 10.11) ---------------------------------------------------------------
def _clear_caps(monkeypatch):
    for k in ("TEAM_PUBLISH_MAX_PER_DAY", "TEAM_PUBLISH_CAPS", "TEAM_PUBLISH_COUNTS", "TEAM_PUBLISH_COUNTS_WEEK"):
        monkeypatch.delenv(k, raising=False)


def test_caps_default_daily_still_applies(monkeypatch):
    _clear_caps(monkeypatch)
    monkeypatch.setenv("TEAM_PUBLISH_MAX_PER_DAY", "2")
    monkeypatch.setenv("TEAM_PUBLISH_COUNTS", json.dumps({"x": 2}))
    assert "daily limit" in publish._daily_limit_error("x", None)
    assert publish._daily_limit_error("bluesky", None) is None


def test_caps_weekly_blocks_and_counts_this_run(monkeypatch, tmp_path):
    _clear_caps(monkeypatch)
    monkeypatch.setenv("TEAM_PUBLISH_MAX_PER_DAY", "2")
    monkeypatch.setenv("TEAM_PUBLISH_CAPS", json.dumps({"bluesky": {"per_week": 2}, "x": {"per_day": 3}}))
    monkeypatch.setenv("TEAM_PUBLISH_COUNTS_WEEK", json.dumps({"bluesky": 1}))
    assert publish._daily_limit_error("bluesky", None) is None
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"channel": "bluesky", "ok": True, "ts": "2020-01-01T00:00:00Z"}) + "\n")
    assert "weekly limit" in publish._daily_limit_error("bluesky", ledger)
    monkeypatch.setenv("TEAM_PUBLISH_COUNTS", json.dumps({"x": 2}))
    assert publish._daily_limit_error("x", None) is None          # per-channel per_day overrides the default 2
    monkeypatch.setenv("TEAM_PUBLISH_COUNTS", json.dumps({"x": 3}))
    assert "daily limit" in publish._daily_limit_error("x", None)


def test_caps_block_in_prompt():
    from team import prompts
    txt = prompts.caps_block({"x": {"per_day": 2}, "mastodon": {"per_week": 1}})
    assert "- x: 2/day" in txt and "- mastodon: 1/7 days" in txt
    assert prompts.caps_block({}) == ""


def test_week_cap_same_full_in_planning_and_publish(monkeypatch, tmp_path):
    from team import prompts
    from team.store import Store
    st = Store(tmp_path / "s.db")
    for i in range(5):
        st.record_post("bluesky", f"p{i}", f"u{i}", None, "t")
    monkeypatch.setenv("TEAM_PUBLISH_CAPS", json.dumps({"bluesky": {"per_week": 2}}))
    monkeypatch.setenv("TEAM_PUBLISH_COUNTS_WEEK", json.dumps({"bluesky": publish.week_count(st, "bluesky")}))
    monkeypatch.delenv("TEAM_PUBLISH_MAX_PER_DAY", raising=False)
    monkeypatch.delenv("TEAM_PUBLISH_COUNTS", raising=False)
    assert "weekly limit reached" in publish._daily_limit_error("bluesky", None)
    u = prompts.week_usage_map(st, {"bluesky": {"per_week": 2}}, ["bluesky"])["bluesky"]
    assert (u["used"], u["cap"], u["full"], u["over"]) == (5, 2, True, True)
    assert "5/2" in prompts.usage_lines({"bluesky": u}) and "OVER" in prompts.usage_lines({"bluesky": u})
