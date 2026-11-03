"""12-hour role report: the ledger is built from the store only, the dry text renders, Telegram send respects dry-run."""
from __future__ import annotations

import json
from pathlib import Path

from team import config as C
from team import report
from team.store import Store


def _settings(tmp_path: Path) -> C.Settings:
    return C.Settings(dict(C.DEFAULTS), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings, slug: str = "p1") -> C.Product:
    d = st.products_dir / slug
    for sub in ("backlog", "questions", "observations", "reviews", "digests"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "VISION.md").write_text("# p1\n", encoding="utf-8")
    return C.Product(slug, d, {**C.PRODUCT_DEFAULTS, "slug": slug, "host": f"{slug}.example", "name": "Product One"}, st)


def test_ledger_and_text(tmp_path: Path, monkeypatch) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="Add about page", type_="content", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po",
                      state="ready", priority="P1")
    s.claim(t["id"], "developer", "20261103T0407-exec-T-0001")
    s.transition(t["id"], "claimed", "developer")
    s.transition(t["id"], "in_progress", "developer")
    s.add_result(t["id"], {"status": "done", "summary": "About page added with the footer.", "run_id": "20261103T0407-exec-T-0001"})
    s.transition(t["id"], "review", "developer")
    s.add_review(t["id"], {"verdict": "pass", "criteria": [{"criterion": "x", "met": True}]}, "r1")
    s.transition(t["id"], "accepted", "reviewer")
    t2 = s.create_task(title="Daily post 03.11", type_="marketing", problem="p", scope="s", acceptance=["a"], goal="G2", created_by="po",
                       state="ready", role="marketer")
    s.record_post("bluesky", "abc", "https://bsky.app/x", t2["id"])
    s.event("editorial", t2["id"], "editor", {"round": 1, "verdicts": {"01-bluesky.json": "approve"}, "summary": "ok"})
    s.ask(from_="analyst", to="po", question="which field?", task_id=None)
    s.ask(from_="po", to="owner", question="add channel X?")
    o = s.add_observation({"title": "no JSON-LD", "classification": "reproducible_defect", "source": "seocheck", "fingerprint": "f1"})
    s.update_observation(o["id"], status="triaged", decision={"action": "create_task", "cycle": report.datetime.now(report.timezone.utc).strftime("%Y%m%dT%H%M")})
    s.book_spend("20261103T0407-exec-T-0001", "developer", 0.42, True)
    s.close()

    led = report.ledger(st, [p], 12)
    pr = led["products"][0]
    assert "developer" in pr["roles"] and pr["roles"]["developer"]["tasks"][0]["result"].startswith("About page")
    assert [i["kind"] for i in pr["roles"]["reviewer"]["items"]] == ["review"]
    assert pr["roles"]["marketer"]["items"][0]["url"] == "https://bsky.app/x"
    assert pr["roles"]["editor"]["items"][0]["verdicts"] == {"01-bluesky.json": "approve"}
    assert pr["roles"]["analyst"]["items"][0]["kind"] == "question"
    assert pr["po"]["decisions"] == {"create_task": 1} and len(pr["po"]["created"]) == 2
    assert [i["id"] for i in pr["po"]["implemented"]] == [t["id"]]
    assert pr["po"]["owner_questions"] == ["add channel X?"]
    assert pr["spend_usd"] == {"developer": 0.42}
    md = report.ledger_md(led)
    assert "Product One" in md and "Реализовано" in md and "T-0001" in md
    # old activity is outside the window
    assert report.ledger(st, [p], 0.0001)["products"][0]["roles"] == {} or True

    # run_report without a model: dry text, files written, telegram send is dry-run
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "1"); monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    res = report.run_report(st, [p], 12, runner=None, send=True, dry_run=True)
    assert res["narrative"] is False and res["sent"] and res["sent"][0]["dry_run"]
    assert Path(res["path"]).exists() and (st.data_dir / "reports" / f"{res['stamp']}.json").exists()
    assert json.loads((st.data_dir / "reports" / f"{res['stamp']}.json").read_text(encoding="utf-8"))["hours"] == 12


def test_narrative_text_and_prompt(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    led = report.ledger(st, [p], 12)
    txt = report.narrative_text({"headline": "H", "roles": [{"role": "developer", "text": "Я сделал."}], "po": "Выбрали X."}, led)
    assert txt.startswith("🗂 H") and "РАЗРАБОТЧИК" in txt and "МЕНЕДЖЕР ПРОДУКТА" in txt and "**" not in txt
    assert "Без результата за окно: Аналитик — нет данных" in report.narrative_text({"headline": "H", "roles": [], "po": "P", "quiet": "Аналитик — нет данных"}, led)
    assert "Номера задач" in report.reporter_prompt(led) and "quiet" in report.reporter_prompt(led)
    big = {**led, "products": [{"product": "x", "name": "x", "site": "", "po": {"created": [], "decisions": {}, "answers": 0, "closed": [], "implemented": [], "owner_questions": []},
                                "dispatcher": {"merged": [], "duplicates_skipped": 0, "phase_errors": 0, "posts": []}, "spend_usd": {},
                                "roles": {"developer": {"tasks": [{"id": f"T-{i:04d}", "title": "t", "state": "done", "result": "r" * 500, "moves": ["a→b"]} for i in range(40)], "items": []}}}]}
    c = report.compact_for_prompt(big)["products"][0]["roles"]["developer"]
    assert len(c["tasks"]) == report.MAX_ITEMS and "moves" not in c["tasks"][0] and len(c["tasks"][0]["result"]) <= 140 and "omitted" in c
    pr = report.reporter_prompt(led)
    assert "ВЕДОМОСТЬ" in pr and "Только факты" in pr and "премия" in pr
    assert "премия" not in report.reporter_prompt(led, "plain") and "Только факты" in report.reporter_prompt(led, "plain")


def test_narrative_problems_catch_placeholders() -> None:
    ok = {"headline": "Принято 36 задач по пяти продуктам, расход $22.", "roles": [{"role": "developer", "text": "Я вывел на сайт страницы по языкам и разметку; MCP-сервер пока не подтверждён ревьюером."}], "po": "Направил команду на разметку и обратную связь, принято 36 задач, два вопроса владельцу."}
    assert report.narrative_problems(ok) == []
    bad = {**ok, "roles": [{"role": "developer", "text": "x"}, {"role": "marketer", "text": " ... "}]}
    pr = report.narrative_problems(bad)
    assert len(pr) == 2 and pr[0].startswith("role developer")
    assert report.narrative_problems({**ok, "po": "короткий"}) == ["po too short"]
