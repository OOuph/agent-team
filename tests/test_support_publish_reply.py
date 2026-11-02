"""Support role plumbing, GitHub comments and Bluesky replies in the publish tool (dry-run), protected-path rule."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import publish, support
from team.store import Store


def _product(tmp_path: Path, extra: dict | None = None) -> C.Product:
    raw = {**C.DEFAULTS, "support": {"ignore_authors": ["formachines.bsky.social"]}, **(extra or {})}
    st = C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    d = st.products_dir / "p1"
    d.mkdir(parents=True)
    (d / "VISION.md").write_text("# p1\n", encoding="utf-8")
    return C.Product("p1", d, {**C.PRODUCT_DEFAULTS, "slug": "p1", "host": "p1.example", "repo": {"url": "https://github.com/o/r.git"}}, st)


def test_pending_skips_answered_and_own(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
    monkeypatch.delenv("GITHUB_LOGIN", raising=False)
    p = _product(tmp_path)
    s = Store(p.dir)
    s.add_observation({"title": "Issue #3: broken feed", "classification": "feedback", "source": "github-issue", "author": "alice",
                       "url": "https://github.com/o/r/issues/3", "actual": "feed 500", "fingerprint": "gh-issue-o-r-3"})
    s.add_observation({"title": "reply from @formachines.bsky.social", "classification": "feedback", "source": "bluesky-reply",
                       "author": "formachines.bsky.social", "url": "https://bsky.app/x", "actual": "hi", "fingerprint": "bsky-at://did/app.bsky.feed.post/1"})
    s.add_observation({"title": "reply from @bob", "classification": "feedback", "source": "bluesky-mention", "author": "bob.bsky.social",
                       "url": "https://bsky.app/y", "actual": "nice", "fingerprint": "bsky-at://did/app.bsky.feed.post/2"})
    s.add_observation({"title": "404 on /x", "classification": "reproducible_defect", "source": "persona", "url": "https://p1.example/x",
                       "actual": "404", "fingerprint": "persona-1"})
    todo = support.pending(p, s)
    assert [o["fingerprint"] for o in todo] == ["gh-issue-o-r-3", "bsky-at://did/app.bsky.feed.post/2"]
    assert support.channel_for(todo[0]) == ("github", {"target": "https://github.com/o/r/issues/3"})
    assert support.channel_for(todo[1]) == ("bluesky", {"reply_to": "at://did/app.bsky.feed.post/2"})
    s.event("support.reply", "gh-issue-o-r-3", "support", {"url": "u"})
    assert [o["fingerprint"] for o in support.pending(p, s)] == ["bsky-at://did/app.bsky.feed.post/2"]
    assert support.replies_today(s) == 1
    prompt = support.support_prompt(p, todo[0], "# vision")
    assert "feed 500" in prompt and "escalate" in prompt
    s.close()


def test_publish_github_and_bluesky_reply_dry_run(capsys) -> None:
    r = publish.post("github", "Thanks, checked the feed: it builds. Goes to the backlog.", target="https://github.com/o/r/issues/3",
                     disclosure="AI agent", dry_run=True, env={})
    assert r.ok and r.dry_run and r.channel == "github"
    assert "o/r#3" in capsys.readouterr().err
    r = publish.post("github", "x", target="https://github.com/o/r/pull/3", disclosure="AI agent", dry_run=True, env={})
    assert not r.ok
    r = publish.post("bluesky", "Thanks!", disclosure="AI agent", dry_run=True, env={}, reply_to="at://did/app.bsky.feed.post/2")
    assert r.ok and r.dry_run
    assert '"reply"' in capsys.readouterr().err


def test_protected_path_rule() -> None:
    protected = ["team/store.py", "deploy/", "watchdog.sh"]
    touched = ["team/roles/po.md", "deploy/droplet/publish.sh", "SCHEME.md"]
    hit = [f for f in touched if any(f == pp or f.startswith(pp.rstrip("/") + "/") for pp in protected)]
    assert hit == ["deploy/droplet/publish.sh"]
