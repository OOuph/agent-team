"""`team onboard` (owner 20.11): brief → one batch of questions and accounts → owner's answers → VISION, GDD, backlog.
Everything runs on the fixture or a FakeRunner: no model, no token, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from team import config as C
from team import deputy, hypothesis, onboard
from team.cli import main
from team.cycle import Cycle, schema
from team.runner import RunResult, load_roles
from team.store import Store

ROLES_DIR = Path(__file__).resolve().parent.parent / "team" / "roles"


def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"deputy": {"enabled": True, "after_h": 0}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _brief(tmp_path: Path) -> Path:
    b = tmp_path / "brief.md"
    b.write_text(onboard.SAMPLE_BRIEF.read_text(encoding="utf-8"), encoding="utf-8")
    return b


class FakeRunner:
    """Returns the fixture as a structured result, so the real Cycle.role_run wiring (budget, run.json, event) is exercised."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        self.calls.append((role, prompt[:40]))
        fx = onboard.fixture()
        out = fx["questions"] if "step 1" in prompt else fx["plan"]
        return RunResult(True, out, {"total_cost_usd": 0.2}, 0.2, 2, None, session_id="s", duration_s=1.0)


# ------------------------------------------------------------------ step 1

def test_start_writes_questions_and_blocks_the_cycle(tmp_path: Path):
    st = _settings(tmp_path)
    res = onboard.start(st, "fleets", _brief(tmp_path), host="fleets.example.test", dry_run=True)
    d = st.products_dir / "fleets"
    assert res["n_questions"] == 5 and res["n_accounts"] == 3 and res["source"].startswith("fixture")
    md = (d / "onboarding" / "questions.md").read_text(encoding="utf-8")
    assert md.count("\nОтвет:") == 5 and md.count("\nРешение:") == 3
    assert "A1 · хост" in md and "Что передать:" in md and "Без него:" in md
    assert (d / "onboarding" / "brief.md").exists() and (d / "onboarding" / "questions.yaml").exists()
    p = C.load_product(st, d)
    assert p.raw["onboarding"]["state"] == "questions" and p.raw["profile"] == "game" and p.host == "fleets.example.test"
    # one open owner question, and the deputy leaves it alone
    s = Store(d)
    qs = s.questions(["open"])
    assert len(qs) == 1 and qs[0]["from"] == "onboarding" and qs[0]["to"] == "owner" and "questions.md" in qs[0]["question"]
    assert deputy.due_questions(st, s) == []
    s.close()
    # the hourly cycle refuses the product until onboarding is done
    cyc = Cycle(st, p, None, "c1")
    assert "onboarding not finished" in (cyc.stopped() or "")
    assert cyc.run() == {"stopped": cyc.stopped()}
    cyc.store.close()


def test_start_twice_keeps_one_question_and_refuses_after_done(tmp_path: Path):
    st = _settings(tmp_path)
    onboard.start(st, "g", _brief(tmp_path), dry_run=True)
    onboard.start(st, "g", _brief(tmp_path), dry_run=True)          # re-run before answering: same product, one open question
    s = Store(st.products_dir / "g")
    assert len(s.questions(["open"])) == 1
    s.close()
    onboard.apply_answers(st, "g", dry_run=True)
    with pytest.raises(onboard.OnboardingError):
        onboard.start(st, "g", _brief(tmp_path), dry_run=True)
    with pytest.raises(onboard.OnboardingError):
        onboard.apply_answers(st, "g", dry_run=True)                 # state is done, not questions


def test_empty_brief_is_refused(tmp_path: Path):
    st = _settings(tmp_path)
    b = tmp_path / "empty.md"
    b.write_text("  \n")
    with pytest.raises(onboard.OnboardingError):
        onboard.start(st, "x", b, dry_run=True)
    assert not (st.products_dir / "x").exists()


# ------------------------------------------------------------------ the owner's file

def test_parse_answers_filled_empty_and_multiline():
    md = ("## Вопросы\n\n### Q1 · session · что при разрыве?\nЗачем: x\nОтвет: ждём 5 минут\n\n"
          "### Q2 · loop · конец партии?\nПо умолчанию: все корабли\nОтвет: \n\n"
          "### Q3 · tone · тон?\nОтвет: военно-морской,\nс названиями кораблей\n\n"
          "## Доступы и аккаунты\n\n### A1 · хост\nРешение: дал: host game.example.test, ключ добавлен\n\n### A2 · репозиторий\nРешение:\n")
    p = onboard.parse_answers(md)
    assert p["answers"] == {"Q1": "ждём 5 минут", "Q2": "", "Q3": "военно-морской,\nс названиями кораблей"}
    assert p["decisions"] == {"A1": "дал: host game.example.test, ключ добавлен", "A2": ""}


def test_merge_answers_applies_defaults_and_marks_them():
    qs = onboard.fixture()["questions"]
    qa = onboard.merge_answers(qs, {"answers": {"Q1": "партия пропадает"}, "decisions": {"A1": "дал"}})
    by_id = {q["id"]: q for q in qa["questions"]}
    assert by_id["Q1"]["answer"] == "партия пропадает" and by_id["Q1"]["by_default"] is False
    assert by_id["Q2"]["by_default"] is True and by_id["Q2"]["answer"].endswith(onboard.DEFAULT_MARK)
    acc = {a["id"]: a for a in qa["accounts"]}
    assert acc["A1"]["decision"] == "дал" and acc["A1"]["answered"] and not acc["A2"]["answered"]


def test_rendered_questions_md_round_trips_through_the_parser(tmp_path: Path):
    st = _settings(tmp_path)
    res = onboard.start(st, "rt", _brief(tmp_path), dry_run=True)
    md_path = Path(res["questions_md"])
    md = md_path.read_text(encoding="utf-8").replace("### Q1 ", "### Q1 ", 1)
    md = md.replace("Ответ: \n", "Ответ: ждём 2 минуты\n", 1)          # the owner fills the first field only
    md_path.write_text(md, encoding="utf-8")
    parsed = onboard.parse_answers(md)
    assert parsed["answers"]["Q1"] == "ждём 2 минуты" and parsed["answers"]["Q5"] == ""
    assert set(parsed["decisions"]) == {"A1", "A2", "A3"}


def test_text_outside_the_answer_fields_goes_to_the_plan_with_a_warning(tmp_path: Path, caplog):
    """28.11, pokerbr К6: the owner corrected «Как мы поняли продукт» and added a line after the last section; the parser
    read only `Ответ:` / `Решение:` and the plan never saw either."""
    st = _settings(tmp_path)
    res = onboard.start(st, "notes", _brief(tmp_path), dry_run=True)
    md_path = Path(res["questions_md"])
    clean = md_path.read_text(encoding="utf-8")
    assert onboard.stray_notes(clean, clean) == []                     # the file as it was rendered holds no notes
    filled = clean.replace("Ответ: \n", "Ответ: ждём 2 минуты\nи ещё строка ответа\n", 1)
    assert onboard.stray_notes(filled, clean) == []                    # an answer, however long, is not a note
    md = filled.replace("## Вопросы", "Поправка: продукт только для Бразилии, не для всей Латинской Америки.\n\n## Вопросы", 1)
    md += "\n## От владельца\nДомен уже куплен, новый не предлагать.\n"
    md_path.write_text(md, encoding="utf-8")
    notes = onboard.stray_notes(md, clean)
    assert [n["under"] for n in notes] == ["Как мы поняли продукт", "От владельца"]
    assert "только для Бразилии" in notes[0]["text"] and "Домен уже куплен" in notes[1]["text"]
    with caplog.at_level("WARNING"):
        out = onboard.apply_answers(st, "notes", dry_run=True)
    assert out["notes_outside_fields"] == 2 and "outside the" in caplog.text
    od = st.products_dir / "notes" / "onboarding"
    prompt = (od / "plan-prompt.md").read_text(encoding="utf-8")
    assert "OWNER'S NOTES OUTSIDE THE ANSWER FIELDS" in prompt and "только для Бразилии" in prompt and "Домен уже куплен" in prompt
    assert len(onboard._read_yaml(od / onboard.ANSWERS_YAML)["notes"]) == 2


def test_a_file_filled_only_in_its_fields_gives_the_plan_prompt_as_before(tmp_path: Path):
    st = _settings(tmp_path)
    onboard.start(st, "plain", _brief(tmp_path), dry_run=True)
    out = onboard.apply_answers(st, "plain", dry_run=True)
    od = st.products_dir / "plain" / "onboarding"
    assert out["notes_outside_fields"] == 0 and "OWNER'S NOTES" not in (od / "plan-prompt.md").read_text(encoding="utf-8")
    assert "notes" not in onboard._read_yaml(od / onboard.ANSWERS_YAML)


# ------------------------------------------------------------------ step 2

def test_apply_answers_writes_vision_tasks_and_finishes(tmp_path: Path):
    st = _settings(tmp_path)
    onboard.start(st, "fleets", _brief(tmp_path), repo="https://github.com/example/fleets.git", dry_run=True)
    res = onboard.apply_answers(st, "fleets", dry_run=True)
    d = st.products_dir / "fleets"
    vision = (d / "VISION.md").read_text(encoding="utf-8")
    assert vision.startswith("# Два флота — VISION") and "| M0 |" in vision
    assert res["game_design"] == "" and not (d / "GAME-DESIGN.md").exists()      # fixture leaves the GDD to the game-designer task
    p = C.load_product(st, d)
    assert p.raw["onboarding"]["state"] == "done" and p.raw["profile"] == "game" and p.raw["stage"] == "M0"
    assert p.raw["repo"]["kind"] == "service" and p.raw["repo"]["url"] == "https://github.com/example/fleets.git"   # the CLI URL wins over the plan's empty one
    assert p.raw["repo"]["health_path"] == "/healthz" and p.raw["deploy"] == {"mode": "pending"}
    assert p.raw["phase_gates"]["marketing"] == "M2"
    s = Store(d)
    tasks = {t["id"]: t for t in s.tasks()}
    assert len(tasks) == 6 and res["tasks"] == sorted(tasks)
    by_title = {t["title"]: t for t in tasks.values()}
    gdd = by_title["GAME-DESIGN.md v1: правила, пять кораблей, условие победы, таблица баланса"]
    assert gdd["type"] == "design" and gdd["role"] == "game-designer" and gdd["state"] == "ready" and gdd["created_by"] == "onboarding"
    core = by_title["Ход и выстрел по GAME-DESIGN.md на сервере; клиент показывает состояние"]
    assert core["type"] == "milestone" and set(core["depends_on"]) == {gdd["id"], by_title["Комнаты по коду: создать, войти вторым, старт партии"]["id"]}
    assert not hypothesis.applies(core)                                       # milestone tasks are outside the hypothesis rule
    scaffold = by_title["Каркас репозитория: сервер ws + клиент, npm test, /healthz, PROTOCOL.md"]
    assert scaffold["repo_only"] is True and scaffold["type"] == "ops"
    deploy = by_title["Деплой сервера на хост владельца и поддомен"]
    assert deploy["state"] == "deferred" and "A1" in deploy["return_condition"] and res["deferred"] == [deploy["id"]]
    assert s.questions(["open"]) == [] and s.questions(["answered"])[0]["answered_by"] == "owner"
    s.close()
    decisions = (d / "decisions.md").read_text(encoding="utf-8")
    assert "Q1 по умолчанию" in decisions and "доступ A1" in decisions
    assert (d / "digests" / "onboarding.md").read_text(encoding="utf-8").count("deferred") == 1
    # the cycle now runs (no runner → phases pass with nothing to do, not stopped)
    cyc = Cycle(st, p, None, "c2")
    assert cyc.stopped() is None
    cyc.store.close()
    log_lines = [json.loads(x) for x in (d / "onboarding" / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["kind"] for x in log_lines] == ["questions", "plan"] and log_lines[1]["tasks"] == 6


def test_apply_answers_without_start_is_refused(tmp_path: Path):
    st = _settings(tmp_path)
    with pytest.raises(onboard.OnboardingError):
        onboard.apply_answers(st, "nobody", dry_run=True)


def test_fake_runner_path_books_the_runs(tmp_path: Path):
    st = _settings(tmp_path)
    r = FakeRunner()
    res = onboard.start(st, "fleets", _brief(tmp_path), runner=r)
    assert res["source"] == "model" and r.calls[0][0] == "onboarding"
    res2 = onboard.apply_answers(st, "fleets", runner=r)
    assert res2["source"] == "model" and [c[0] for c in r.calls] == ["onboarding", "onboarding"]
    s = Store(st.products_dir / "fleets")
    assert s.spent_today() == pytest.approx(0.4)                                # both runs went through Cycle.role_run and were booked
    kinds = [e["kind"] for e in s.events()]
    assert kinds.count("run") == 2
    s.close()
    runs = sorted((st.data_dir / "runs" / "fleets").glob("onboard-*/onboarding-*/run.json"))
    assert len(runs) == 2


class FailingRunner:
    def run(self, role, prompt, schema, **kw):
        return RunResult(False, None, None, 0.1, 1, "boom")


def test_failed_model_run_raises_and_keeps_state(tmp_path: Path):
    st = _settings(tmp_path)
    with pytest.raises(onboard.OnboardingError):
        onboard.start(st, "f", _brief(tmp_path), runner=FailingRunner())
    assert C.load_product(st, st.products_dir / "f").raw["onboarding"]["state"] == "questions"    # re-run with --dry-run or a fixed token is possible
    assert not (st.products_dir / "f" / "onboarding" / "questions.md").exists()


# ------------------------------------------------------------------ definitions, fixture, CLI

def test_role_card_schemas_and_fixture_agree():
    roles = load_roles(ROLES_DIR)
    assert "onboarding" in roles and roles["onboarding"]["model"] == "opus" and roles["onboarding"]["_tools"] == []
    import jsonschema
    fx = onboard.fixture()
    jsonschema.validate(fx["questions"], schema("onboarding-questions"))
    jsonschema.validate(fx["plan"], schema("onboarding-plan"))
    assert fx["plan"]["backlog"][0]["role"] == "game-designer"
    assert C.DEFAULTS["claude"]["models"]["onboarding"] == "opus"


def test_cli_dry_run_end_to_end(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "team.yaml"
    cfg.write_text("paths:\n  products_dir: products\n  data_dir: data\n", encoding="utf-8")
    monkeypatch.setenv("TEAM_CONFIG", str(cfg))
    monkeypatch.setattr(C, "ROOT", tmp_path)
    brief = _brief(tmp_path)
    assert main(["onboard", "demo-game", "--brief", str(brief), "--host", "demo.example.test", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "5 questions and 3 accounts" in out and "owner question Q-0001" in out
    assert main(["onboard", "demo-game", "--status"]) == 0
    assert "state=questions" in capsys.readouterr().out
    assert main(["onboard", "demo-game", "--answers", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "onboarding done (fixture" in out and "deferred: T-0006" in out and "team cycle --product demo-game --dry-run" in out
    assert main(["onboard", "demo-game", "--status"]) == 0
    assert "state=done" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["onboard", "demo-game"])                                           # no mode given


# ------------------------------------------------------------------ profile site (owner 26.11: a static site, not a game)

def test_site_profile_start_uses_site_template_and_fixture(tmp_path: Path):
    st = _settings(tmp_path)
    res = onboard.start(st, "stats", _brief(tmp_path), host="stats.example.test", dry_run=True, profile="site")
    d = st.products_dir / "stats"
    p = C.load_product(st, d)
    assert p.raw["profile"] == "site" and p.profile == "site" and res["n_questions"] == 3
    prompt = (d / "onboarding" / "questions-prompt.md").read_text(encoding="utf-8")
    assert "profile `site`" in prompt
    assert onboard.SITE_TEMPLATE.read_text(encoding="utf-8")[:60] in prompt
    assert onboard.GAME_TEMPLATE.read_text(encoding="utf-8")[:60] not in prompt
    cyc = Cycle(st, p, None, "c1")
    assert "onboarding not finished" in (cyc.stopped() or "")          # the barrier holds for a site too
    cyc.store.close()


def test_site_profile_plan_has_no_standing_and_no_gdd(tmp_path: Path):
    st = _settings(tmp_path)
    onboard.start(st, "stats", _brief(tmp_path), dry_run=True, profile="site")
    res = onboard.apply_answers(st, "stats", dry_run=True)
    d = st.products_dir / "stats"
    prompt = (d / "onboarding" / "plan-prompt.md").read_text(encoding="utf-8")
    assert "STANDING DIRECTIONS" not in prompt and "profile `site`" in prompt
    vision = (d / "VISION.md").read_text(encoding="utf-8")
    assert onboard.VISION_STANDING_HEADING not in vision                 # no game section appended to a site
    assert res["game_design"] == "" and not (d / "GAME-DESIGN.md").exists()
    p = C.load_product(st, d)
    assert p.profile == "site" and p.raw["onboarding"]["state"] == "done" and p.deploy_mode == "git_pull"
    assert p.raw["repo"]["kind"] == "static" and "phase_gates" not in p.raw
    assert len(res["deferred"]) == 1


def test_site_fixture_matches_schemas_and_bad_profile_refused(tmp_path: Path):
    import jsonschema
    fx = onboard.fixture("site")
    jsonschema.validate(fx["questions"], schema("onboarding-questions"))
    jsonschema.validate(fx["plan"], schema("onboarding-plan"))
    assert fx["plan"]["product"]["profile"] == "site"
    with pytest.raises(onboard.OnboardingError):
        onboard.start(_settings(tmp_path), "x", _brief(tmp_path), dry_run=True, profile="app")


def test_cli_profile_site(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "team.yaml"
    cfg.write_text("paths:\n  products_dir: products\n  data_dir: data\n", encoding="utf-8")
    monkeypatch.setenv("TEAM_CONFIG", str(cfg))
    monkeypatch.setattr(C, "ROOT", tmp_path)
    assert main(["onboard", "demo-site", "--brief", str(_brief(tmp_path)), "--profile", "site", "--dry-run"]) == 0
    assert "3 questions and 3 accounts" in capsys.readouterr().out
    assert main(["onboard", "demo-site", "--answers", "--dry-run"]) == 0
    assert "onboarding done (fixture" in capsys.readouterr().out
