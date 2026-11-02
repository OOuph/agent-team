import json
from pathlib import Path

from team import config as C
from team import feedback


def _product(tmp_path: Path) -> C.Product:
    st = C.Settings(C._merge(C.DEFAULTS, {}), tmp_path, tmp_path / "p", tmp_path / "d", tmp_path / "d" / "r")
    d = st.products_dir / "x"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: x\nrepo:\n  url: https://github.com/org/site.git\nchannels:\n  bluesky: h.bsky.social\n")
    return C.load_product(st, d)


def test_github_issues_become_cards(tmp_path):
    p = _product(tmp_path)

    def http(url, headers):
        if "/issues?" in url:
            return 200, json.dumps([
                {"number": 7, "title": "Wrong quote for axios", "body": "The quote is from 1.20", "html_url": "https://github.com/org/site/issues/7", "user": {"login": "dev1"}},
                {"number": 8, "title": "pr", "pull_request": {}, "html_url": "x", "user": {"login": "d"}}]).encode()
        return 200, json.dumps([
            {"id": 55, "body": "me too", "html_url": "https://github.com/org/site/issues/7#c55", "issue_url": "https://api.github.com/repos/org/site/issues/7", "user": {"login": "dev2"}},
            {"id": 56, "body": "bot", "html_url": "y", "issue_url": "z/7", "user": {"login": "x[bot]"}}]).encode()

    cards = feedback.github_issues(p, http=http, env={"GITHUB_TOKEN": "t"})
    assert [c["fingerprint"] for c in cards] == ["gh-issue-org-site-7", "gh-comment-55"]
    assert cards[0]["classification"] == "feedback" and cards[0]["author"] == "dev1"


def test_bluesky_replies_filtered_by_reason_and_time(tmp_path):
    p = _product(tmp_path)

    def post(url, body, headers):
        return 200, json.dumps({"accessJwt": "j"}).encode()

    def http(url, headers):
        assert headers["Authorization"] == "Bearer j"
        return 200, json.dumps({"notifications": [
            {"reason": "reply", "indexedAt": "2099-01-01T00:00:00Z", "author": {"handle": "a.bsky.social"}, "record": {"text": "nice"}, "uri": "at://did/app.bsky.feed.post/abc"},
            {"reason": "like", "indexedAt": "2099-01-01T00:00:00Z", "author": {"handle": "b"}, "record": {}, "uri": "at://x/y/z"},
            {"reason": "mention", "indexedAt": "2000-01-01T00:00:00Z", "author": {"handle": "old"}, "record": {"text": "old"}, "uri": "at://o/l/d"}]}).encode()

    cards = feedback.bluesky_replies(p, http=http, post=post, env={"BLUESKY_HANDLE": "h", "BLUESKY_APP_PASSWORD": "p"})
    assert len(cards) == 1 and cards[0]["url"] == "https://bsky.app/profile/a.bsky.social/post/abc"


def test_no_config_no_cards(tmp_path):
    p = _product(tmp_path)
    assert feedback.bluesky_replies(p, env={}) == []
