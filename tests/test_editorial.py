"""Editorial loop plumbing: draft mode of the publish CLI, lessons, scores, editor prompt, degradation flag."""
from __future__ import annotations

import json
from pathlib import Path

from team import config as C
from team import editorial, publish


def _settings(tmp_path: Path) -> C.Settings:
    return C.Settings(dict(C.DEFAULTS), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings) -> C.Product:
    d = st.products_dir / "p1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "VISION.md").write_text("# p1\n\n## Тон\nживой рассказ\n", encoding="utf-8")
    return C.Product("p1", d, {**C.PRODUCT_DEFAULTS, "slug": "p1", "host": "p1.example"}, st)


def test_publish_cli_draft_mode(tmp_path: Path, monkeypatch, capsys) -> None:
    drafts = tmp_path / "drafts"
    monkeypatch.setenv("TEAM_PUBLISH_DRAFT_DIR", str(drafts))
    monkeypatch.delenv("TEAM_PUBLISH_MAX_PER_DAY", raising=False)
    f = tmp_path / "post.txt"
    f.write_text("One minute. That's how long the first daily post ran.", encoding="utf-8")
    rc = publish.main(["--channel", "telegram", "--target", "@x", "--text-file", str(f), "--url", "https://p1.example/day/1/"])
    assert rc == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out.strip().splitlines()[-1])
    assert out["ok"] and out["dry_run"] and str(out["post_id"]).startswith("draft:")
    assert "not dry-run" in cap.err          # 07.11 T-0057: the marketer read the draft flag as dry-run mode
    files = sorted(drafts.glob("*.json"))
    assert len(files) == 1 and files[0].name == "01-telegram.json"
    d = publish.load_draft(files[0])
    assert d["channel"] == "telegram" and d["target"] == "@x" and "src=telegram" in d["url"] and d["text"].startswith("One minute")
    # publishing the draft later goes through post(); dry-run here
    monkeypatch.delenv("TEAM_PUBLISH_DRAFT_DIR")
    res = publish.publish_draft(files[0], text="Edited text.", dry_run=True)
    assert res.ok and res.dry_run and res.channel == "telegram"


def test_lessons_scores_rolling_and_degradation(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    assert editorial.add_lessons(p, ["Open with the measurement, not the project name", "open with the measurement, not the project name"], "editor") == 1
    assert editorial.read_lessons(p) == ["Open with the measurement, not the project name _(editor, " + editorial.datetime.now(editorial.timezone.utc).strftime("%Y-%m-%d") + ")_"] or editorial.read_lessons(p)[0].startswith("Open with")
    assert editorial.rolling(st, "p1") == (None, 0, [])
    for i in range(6):
        editorial.record_score(st, {"product": "p1", "task": f"T-{i}", "channel": "telegram", "round": 1, "verdict": "approve",
                                    "scores": {a: 3 for a in editorial.AXES}, "mean": 3.0, "issues": ["no hook"], "final": True})
    m, n, top = editorial.rolling(st, "p1")
    assert m == 3.0 and n == 6 and top == ["no hook"]
    assert editorial.degrading(st, "p1")
    editorial.record_score(st, {"product": "p1", "task": "T-x", "channel": "bluesky", "round": 1, "verdict": "revise",
                                "scores": {a: 2 for a in editorial.AXES}, "mean": 2.0, "issues": [], "final": False})
    assert editorial.rolling(st, "p1")[1] == 6                     # non-final rounds are not counted
    assert editorial.mean_score({"hook": 5, "clarity": 4, "specificity": 4, "tone": 5, "honesty": 5}) == 4.6


def test_editor_prompt_contents(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    drafts = [("01-telegram.json", {"channel": "telegram", "target": "@x", "text": "Hello", "url": "u"})]
    txt = editorial.editor_prompt(p, {"id": "T-1", "title": "post"}, drafts, ["Lesson A"], ["no hook"], 2, ["fix the opening"])
    assert "DRAFT `01-telegram.json`" in txt and "Lesson A" in txt and "no hook" in txt and "ISSUES YOU RAISED IN ROUND 1" in txt
    assert "review round 2" in txt and "живой рассказ" in txt


def test_label_lessons_not_appended(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    n = editorial.add_lessons(p, ["Keep the AI label in the draft text.", "Always add a disclosure line.",
                                  "Open with a concrete event."], "editor")
    assert n == 1
    assert editorial.read_lessons(p)[0].startswith("Open with a concrete event.")
    assert len(editorial.read_lessons(p)) == 1
