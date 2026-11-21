"""Balance in the game metrics contract (owner 21.11, team/balance.py; docs/game-metrics.md): the optional `balance`
block rendered for the PO, the analyst and the game designer; thresholds from product.yaml; one observation per
out-of-band option, raised once per tables version and day; nothing for a product that sends no block. No model."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from team import balance, config as C, prompts
from team.cycle import Cycle, metrics_md
from team.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _opt(picks: int, wins: int, **kw) -> dict:
    return {"picks": picks, "wins": wins, **kw}


BASE_DAY = {"rounds_started": 14, "rounds_finished": 9, "players": 11, "move_latency_ms": {"p50": 38, "p95": 140, "samples": 620}}
METRICS = {
    "kind": "game", "host": "arena.example.test", "version": "a1b2c3d4e5f6", "generated": "2026-11-21T06:00:12Z",
    "days": {
        "2026-11-19": {**BASE_DAY, "balance": {"version": "v6", "groups": {"weapons": {"railgun": _opt(200, 100)}}}},      # before the patch: not summed
        "2026-11-20": {**BASE_DAY, "balance": {"version": "v7", "groups": {
            "weapons": {"railgun": _opt(70, 44, kills=180, deaths=100, damage_dealt=40000, damage_taken=30000), "shotgun": _opt(60, 29),
                        "mine": _opt(10, 9), "pistol": _opt(50, 17), "laser": _opt(45, 22)},
            "levels": {"canyon": _opt(40, 20)}}}},
        "2026-11-21": {**BASE_DAY, "balance": {"version": "v7", "groups": {
            "weapons": {"railgun": _opt(50, 32), "shotgun": _opt(40, 19), "mine": _opt(5, 5), "pistol": _opt(40, 13), "laser": _opt(35, 18)},
            "levels": {"canyon": _opt(30, 16)}},
            "humans_only": {"weapons": {"railgun": _opt(40, 27), "shotgun": _opt(12, 2), "laser": _opt(35, 25)}}}}}}
RAILGUN = "Balance: weapons/railgun win rate 0.63 over 120 picks (band 0.45–0.55)"
PISTOL = "Balance: weapons/pistol win rate 0.33 over 90 picks (band 0.45–0.55)"
LASER = "Balance: weapons/laser win rate 0.71 over 35 picks, humans only (band 0.45–0.55)"


def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_n_cycles": 0, "digest_hours_utc": [0]}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _game(st: C.Settings, slug: str = "arena", metrics: dict | None = METRICS, extra: dict | None = None) -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    (d / "product.yaml").write_text(yaml.safe_dump({"slug": slug, "name": slug, "host": "", "profile": "game", "stage": "M1", "standing": [],
                                                    "repo": {"url": "", "kind": "service"}, **(extra or {})}, sort_keys=False), encoding="utf-8")
    (d / "VISION.md").write_text("# Arena\n", encoding="utf-8")
    p = C.load_product(st, d)
    if metrics is not None:
        _put_metrics(p, metrics)
    return p


def _put_metrics(p: C.Product, data: dict, day: str = "2026-11-21") -> None:
    (p.dir / "metrics").mkdir(exist_ok=True)
    (p.dir / "metrics" / f"{day}.json").write_text(json.dumps(data), encoding="utf-8")


def _measure(p: C.Product, cycle_id: str = "c1", dry_run: bool = False) -> list[dict]:
    cyc = Cycle(p.st, p, None, cycle_id, dry_run=dry_run)
    cyc.run(["measure"])
    obs = cyc.store.observations()
    cyc.store.close()
    return obs


# ------------------------------------------------------------------ thresholds

def test_thresholds_default_in_code_and_come_from_product_yaml(tmp_path: Path):
    cfg = balance.settings({})
    assert (cfg.lo, cfg.hi, cfg.min_picks, cfg.window_days, cfg.reraise_h) == (0.45, 0.55, 30, 7, 24) and cfg.band == "0.45–0.55"
    assert cfg.sim_report == "design/reports/simulation.md" and cfg.centre == 0.5
    p = _game(_settings(tmp_path), metrics=None, extra={"balance": {"win_rate_band": [0.2, 0.3], "min_picks": 10, "reraise_h": 6, "sim_report": "sim/r.md"}})
    cfg = balance.settings(p.raw)
    assert (cfg.lo, cfg.hi, cfg.min_picks, cfg.reraise_h, cfg.sim_report) == (0.2, 0.3, 10, 6, "sim/r.md") and cfg.centre == 0.25
    for bad in ({"balance": "tight"}, {"balance": {"win_rate_band": [0.6, 0.4], "min_picks": 0}}, {"balance": {"win_rate_band": "x", "min_picks": "many"}},
                {"balance": {"win_rate_band": [0.4], "min_picks": True}}):
        cfg = balance.settings(bad)                                    # a typo falls back to the default, it does not stop the cycle
        assert (cfg.lo, cfg.hi, cfg.min_picks) == (0.45, 0.55, 30), bad


# ------------------------------------------------------------------ rendering

def test_balance_block_is_rendered_as_a_compact_table(tmp_path: Path):
    md = metrics_md(METRICS)
    assert md.startswith("# game metrics arena.example.test") and "| 2026-11-21 | 14 | 9 |" in md       # the day table is still there
    assert "## Balance — tables version v7, days 2026-11-20…2026-11-21 (2); win-rate band 0.45–0.55, min picks 30" in md
    assert "Not summed (another tables version): 2026-11-19." in md
    assert "### weapons — all matches\n| option | picks | win rate | deviation | K/D | dmg dealt/taken | verdict |" in md
    assert "| railgun | 120 | 0.63 | +0.13 | 1.80 | 1.33 | OVER |" in md
    assert "| pistol | 90 | 0.33 | -0.17 | — | — | UNDER |" in md
    assert "| shotgun | 100 | 0.48 | -0.02 | — | — | ok |" in md and "| laser | 80 | 0.50 | +0.00 | — | — | ok |" in md
    assert "| mine | 15 | 0.93 | +0.43 | — | — | few picks |" in md
    assert "### weapons — humans only" in md and "| laser | 35 | 0.71 | +0.21 | — | — | OVER |" in md and "| shotgun | 12 | 0.17 | -0.33 | — | — | few picks |" in md
    assert "### levels — all matches" in md and "| canyon | 70 | 0.51 | +0.01 | — | — | ok |" in md
    weapons = md.split("### weapons — all matches")[1].split("###")[0]
    assert weapons.index("| railgun |") < weapons.index("| shotgun |") and weapons.index("| pistol |") < weapons.index("| shotgun |")   # out of band first

    tight = metrics_md(METRICS, balance.Config(lo=0.2, hi=0.7, min_picks=10))                            # the product's own thresholds
    assert "win-rate band 0.20–0.70, min picks 10" in tight and "| railgun | 120 | 0.63 | +0.18 | 1.80 | 1.33 | ok |" in tight
    assert "| mine | 15 | 0.93 | +0.48 | — | — | OVER |" in tight

    f = tmp_path / "2026-11-21.json"; f.write_text(json.dumps(METRICS))
    dig = prompts.metrics_digest([f], balance_cfg=balance.settings({}))                                 # the analyst's view
    assert dig.startswith("Game metrics served by the game server") and "## Balance — tables version v7" in dig
    assert "| railgun | 120 | 0.63 | +0.13 | 1.80 | 1.33 | OVER |" in dig and dig.index("## 2026-11-21") < dig.index("## Balance")


def test_metrics_without_a_balance_block_render_exactly_as_before(tmp_path: Path):
    plain = copy.deepcopy(METRICS)
    for d in plain["days"].values():
        d.pop("balance")
    md = metrics_md(plain, balance.Config())
    assert "Balance" not in md and md.endswith("| — | — | — | — |\n") and md.count("\n") == 9      # title, blank, Now, blank, header, rule, 3 days — and nothing after them
    f = tmp_path / "m.json"; f.write_text(json.dumps(plain))
    assert "Balance" not in prompts.metrics_digest([f], balance_cfg=balance.Config())
    assert balance.render_md(plain["days"]) == "" and balance.findings(plain["days"]) == [] and not balance.has_block(plain["days"])
    assert balance.render_md(None) == "" and balance.aggregate({}, balance.Config()) is None


def test_malformed_blocks_are_ignored_not_fatal():
    days = {"2026-11-21": {"balance": {"version": 7, "groups": {"weapons": {"railgun": {"picks": "many", "wins": None}, "bad": 5, "ok": _opt(40, 30)}, "junk": "x"},
                                       "humans_only": {"groups": {"weapons": {"ok": _opt(31, 31)}}}}},      # humans_only as a whole block: tolerated
            "2026-11-20": {"balance": "soon"}, "2026-11-19": {"balance": {"version": "7"}}, "x": None}
    cfg = balance.Config()
    agg = balance.aggregate(days, cfg)
    assert agg.version == "7" and agg.days == ["2026-11-21"]
    rs = {(r.scope, r.key): r for r in balance.rows(agg, cfg)}
    assert rs[("groups", "railgun")].verdict == "few picks" and rs[("groups", "ok")].verdict == "over" and rs[("humans_only", "ok")].win_rate == 1.0
    assert [f.key for f in balance.findings(days, cfg)] == ["ok"] and "| ok | 40 | 0.75 |" in balance.render_md(days, cfg)


def test_window_sums_only_recent_days_of_the_newest_version():
    days = {f"2026-11-{d:02d}": {"balance": {"version": "v1", "groups": {"g": {"a": _opt(10, 9)}}}} for d in range(10, 22)}
    agg = balance.aggregate(days, balance.Config(window_days=7))
    assert agg.days == [f"2026-11-{d:02d}" for d in range(15, 22)] and agg.stats["groups"]["g"]["a"]["picks"] == 70
    assert balance.aggregate(days, balance.Config(window_days=1)).stats["groups"]["g"]["a"]["picks"] == 10


# ------------------------------------------------------------------ observations in the measure phase

def test_out_of_band_option_is_raised_once_and_not_repeated(tmp_path: Path):
    p = _game(_settings(tmp_path))
    obs = _measure(p)
    assert sorted(o["title"] for o in obs) == sorted([RAILGUN, PISTOL, LASER])        # one per option: railgun is out in both scopes, one card
    rail = next(o for o in obs if o["title"] == RAILGUN)
    assert rail["classification"] == "balance" and rail["source"] == "dispatcher" and rail["status"] == "new" and rail["run_id"] == "c1-balance"
    assert rail["balance"] == {"group": "weapons", "key": "railgun", "version": "v7", "scope": "groups", "direction": "over", "win_rate": 0.6333,
                               "picks": 120, "band": [0.45, 0.55], "min_picks": 30}
    assert "all matches: 120 picks, 76 wins, win rate 0.63 (over), K/D 1.80" in rail["actual"] and "humans only: 40 picks, 27 wins, win rate 0.68 (over)" in rail["actual"]
    assert "Balance tables version v7; days 2026-11-20, 2026-11-21." in rail["actual"] and "game-designer task at once" in rail["detail"]
    assert next(o for o in obs if o["title"] == PISTOL)["balance"]["direction"] == "under"
    assert next(o for o in obs if o["title"] == LASER)["balance"]["scope"] == "humans_only"
    assert not any("mine" in o["title"] or "shotgun" in o["title"] for o in obs)       # min_picks: 15 picks (and 12 human ones) are not judged

    for cid in ("c2", "c3", "c4"):                                                     # the next cycles: the same finding is not re-raised
        assert len(_measure(p, cid)) == 3
    s = Store(p.dir)
    assert not any(o.get("repeats") for o in s.observations()) and len([e for e in s.events() if e["kind"] == "observation.new"]) == 3
    for o in s.observations():                                                         # the PO triaged them; still quiet inside the 24 h
        s.update_observation(o["id"], status="triaged")
    s.close()
    assert len(_measure(p, "c5")) == 3


def test_raised_again_after_24_hours_or_for_a_new_tables_version(tmp_path: Path):
    p = _game(_settings(tmp_path))
    assert len(_measure(p)) == 3
    s = Store(p.dir)
    rail = next(o for o in s.observations() if o["title"] == RAILGUN)
    s.update_observation(rail["id"], status="triaged", observed_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"))
    s.close()
    obs = _measure(p, "c2")
    again = [o for o in obs if o["title"] == RAILGUN]
    assert len(obs) == 4 and len(again) == 2 and again[1]["status"] == "new" and again[1]["actual"].endswith("Raised before for this version; still out of band.")
    assert len(_measure(p, "c3")) == 4

    patched = copy.deepcopy(METRICS)                                                   # the game designer's patch went live: tables v8, still too strong
    patched["days"]["2026-11-22"] = {**BASE_DAY, "balance": {"version": "v8", "groups": {"weapons": {"railgun": _opt(60, 40), "pistol": _opt(60, 30)}}}}
    _put_metrics(p, patched, day="2026-11-22")
    obs = _measure(p, "c4")
    new = [o for o in obs if (o.get("balance") or {}).get("version") == "v8"]
    assert len(obs) == 5 and [o["title"] for o in new] == ["Balance: weapons/railgun win rate 0.67 over 60 picks (band 0.45–0.55)"]
    assert len(_measure(p, "c5")) == 5


def test_min_picks_and_band_come_from_the_product(tmp_path: Path):
    st = _settings(tmp_path)
    loose = _game(st, "loose", extra={"balance": {"win_rate_band": [0.3, 0.7], "min_picks": 10}})
    assert [o["title"] for o in _measure(loose)] == ["Balance: weapons/mine win rate 0.93 over 15 picks (band 0.30–0.70)",
                                                    "Balance: weapons/shotgun win rate 0.17 over 12 picks, humans only (band 0.30–0.70)",
                                                    "Balance: weapons/laser win rate 0.71 over 35 picks, humans only (band 0.30–0.70)"]
    strict = _game(st, "strict", extra={"balance": {"min_picks": 500}})
    assert _measure(strict) == []


def test_no_balance_block_no_observation_and_dry_run_writes_nothing(tmp_path: Path):
    st = _settings(tmp_path)
    plain = copy.deepcopy(METRICS)
    for d in plain["days"].values():
        d.pop("balance")
    quiet = _game(st, "quiet", metrics=plain)
    assert _measure(quiet) == []
    nometrics = _game(st, "nometrics", metrics=None)
    assert _measure(nometrics) == []
    dry = _game(st, "dry")
    assert _measure(dry, dry_run=True) == [] and len(_measure(dry, "c2")) == 3
    site = st.products_dir / "site"; site.mkdir()
    (site / "product.yaml").write_text("slug: site\nname: site\nhost: ''\n", encoding="utf-8"); (site / "VISION.md").write_text("# site\n")
    sp = C.load_product(st, site)
    _put_metrics(sp, {"host": "x", "generated": "g", "days": {"2026-11-21": {"humans": {"sources": 3}}}})
    assert _measure(sp) == []


def test_at_most_a_few_cards_per_check_the_strongest_first():
    days = {"2026-11-21": {"balance": {"version": "v1", "groups": {"g": {f"o{i:02d}": _opt(100, 60 + i) for i in range(10)}}}}}
    cfg = balance.Config()
    found = balance.findings(days, cfg)
    assert len(found) == 10 and found[0].key == "o09"
    cards = balance.observation_cards(found, [], cfg, now=NOW)
    assert len(cards) == balance.MAX_OBSERVATIONS_PER_CHECK and cards[0]["title"].startswith("Balance: g/o09 win rate 0.69 over 100 picks")
    assert len({c["fingerprint"] for c in cards}) == len(cards)
    seen = [{**c, "observed_at": NOW.isoformat()} for c in cards]
    rest = balance.observation_cards(found, seen, cfg, now=NOW + timedelta(minutes=5))          # the next check takes the rest
    assert [c["balance"]["key"] for c in rest] == ["o03", "o02", "o01", "o00"]


# ------------------------------------------------------------------ who reads it

def test_po_and_game_designer_read_the_table_and_the_observation(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _game(st, metrics=None, extra={"site_base": "https://arena.example.test", "balance": {"min_picks": 100}})
    import urllib.request

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(METRICS).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=20: R())
    cyc = Cycle(st, p, None, "c1")
    assert cyc.fetch_metrics()
    latest = (p.dir / "metrics" / "latest.md").read_text(encoding="utf-8")
    assert "win-rate band 0.45–0.55, min picks 100" in latest and "| railgun | 120 | 0.63 | +0.13 | 1.80 | 1.33 | OVER |" in latest
    assert "| pistol | 90 | 0.33 | -0.17 | — | — | few picks |" in latest                   # the product's min_picks, not the default
    assert cyc.balance_check() and [o["title"] for o in cyc.store.observations("new")] == [RAILGUN]
    po = prompts.po_prompt(p, cyc.store, cycle_id="c1", observations=cyc.store.observations("new"), questions=[], metrics_md=latest,
                           spent_today=0, cap=20, max_ready=3, idle=False)
    assert "## Balance — tables version v7" in po and "classification: balance" in po and RAILGUN in po
    t = cyc.store.create_task(title="balance", type_="design", problem="p", scope="s", acceptance=["a"], goal="g", created_by="po", role="game-designer")
    gd = prompts.game_designer_prompt(p, t, p.dir, cyc.store)
    assert "# GAME METRICS\n# game metrics arena.example.test" in gd and "### weapons — humans only" in gd
    assert RAILGUN in gd.split("# WHAT TESTERS, PLAYERS AND THE PO OBSERVED")[1]
    cyc.store.close()


def test_contract_and_scheme_document_the_block():
    doc = (ROOT / "docs" / "game-metrics.md").read_text(encoding="utf-8")
    assert '"balance": {' in doc and '"humans_only"' in doc and "win_rate_band" in doc and "min_picks" in doc
    for key in balance.STAT_KEYS:
        assert f'"{key}"' in doc, key
    scheme = (ROOT / "SCHEME.md").read_text(encoding="utf-8")
    assert "team/balance.py" in scheme and "humans_only" in scheme
    assert "2026-11-21" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
