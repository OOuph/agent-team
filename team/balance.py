"""Automatic balance reading for a game product (owner 21.11: «автоматическая балансировка — быстро находить имбу по
внутренней симуляции и по реальным матчам и быстро лечить патчем данных»; SCHEME §11, docs/game-metrics.md).

The game server may add an OPTIONAL `balance` block to every day of `/_team/metrics.json`:

    "balance": {"version": "<balance tables version>",
                "groups": {"<group>": {"<option>": {"picks": 120, "wins": 76, "kills": 310, "deaths": 190,
                                                    "damage_dealt": 81200, "damage_taken": 60100}}},
                "humans_only": {"<group>": {"<option>": {...}}}}        # optional, the same shape as `groups`

This module sums the block over the last days that share the newest tables version, renders one compact table for
the PO, the analyst and the game designer, and names the options whose win rate left the product's band with enough
picks. `Cycle.balance_check` (measure phase) turns each such option into ONE observation of class `balance`; it is
raised again only when the tables version changes or after `reraise_h` hours. A product that sends no `balance`
block gets no table and no observation: nothing changes for it.

Thresholds come from `product.yaml: balance: {win_rate_band: [lo, hi], min_picks: N, window_days: D, reraise_h: H,
sim_report: <path>}`; every default is a constant below. Pure functions, no model, no network.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("team.balance")

# --- parameters (product.yaml `balance:` overrides the first five) -------------------------------------------------
DEFAULT_WIN_RATE_BAND = (0.45, 0.55)    # an option is balanced while wins / picks stays inside (both ends included)
DEFAULT_MIN_PICKS = 30                  # below this many picks a win rate is noise: shown, never raised
DEFAULT_WINDOW_DAYS = 7                 # days summed (only the days that carry the newest tables version)
DEFAULT_RERAISE_H = 24                  # the same option of the same tables version is raised again after this many hours
DEFAULT_SIM_REPORT = "design/reports/simulation.md"   # repository-relative: the report of the product's own balance simulation
SIM_REPORT_CHARS = 12000                # how much of the simulation report travels in the game designer's prompt
MAX_GROUPS = 8                          # groups rendered (the rest is counted in a note)
MAX_OPTIONS = 24                        # options rendered per group and scope
MAX_OBSERVATIONS_PER_CHECK = 6          # at most this many new cards per measure phase (the strongest deviations first)
OBS_CLASS = "balance"                   # observation class the PO turns into a game-designer task (team/roles/po.md)
OBS_SOURCE = "dispatcher"
BLOCK_KEY = "balance"                   # the key inside a day of the metrics file
SCOPE_ALL, SCOPE_HUMANS = "groups", "humans_only"
SCOPE_LABEL = {SCOPE_ALL: "all matches", SCOPE_HUMANS: "humans only"}
STAT_KEYS = ("picks", "wins", "kills", "deaths", "damage_dealt", "damage_taken")
UNKNOWN_VERSION = "?"


@dataclass(frozen=True)
class Config:
    lo: float = DEFAULT_WIN_RATE_BAND[0]
    hi: float = DEFAULT_WIN_RATE_BAND[1]
    min_picks: int = DEFAULT_MIN_PICKS
    window_days: int = DEFAULT_WINDOW_DAYS
    reraise_h: float = DEFAULT_RERAISE_H
    sim_report: str = DEFAULT_SIM_REPORT

    @property
    def centre(self) -> float:
        return (self.lo + self.hi) / 2

    @property
    def band(self) -> str:
        return f"{self.lo:.2f}–{self.hi:.2f}"


def settings(raw: dict[str, Any] | None) -> Config:
    """`balance:` of a product.yaml → Config. A missing key or a bad value falls back to the default with a warning:
    a typo in a threshold must not stop the cycle, and the table header prints the band actually used."""
    block = (raw or {}).get(BLOCK_KEY)
    if not isinstance(block, dict):
        if block is not None:
            log.warning("product.yaml: `balance` must be a mapping, got %r — defaults in use", block)
        return Config()
    lo, hi = DEFAULT_WIN_RATE_BAND
    band = block.get("win_rate_band")
    if band is not None:
        try:
            b_lo, b_hi = float(band[0]), float(band[1])
            if not (0.0 <= b_lo < b_hi <= 1.0) or len(band) != 2:
                raise ValueError("need 0 ≤ lo < hi ≤ 1")
            lo, hi = b_lo, b_hi
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            log.warning("product.yaml: balance.win_rate_band %r is not [lo, hi] (%s) — default %s in use", band, exc, DEFAULT_WIN_RATE_BAND)

    def num(key: str, default: float, minimum: float) -> float:
        v = block.get(key)
        if v is None:
            return default
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < minimum:
            log.warning("product.yaml: balance.%s %r is not a number ≥ %s — default %s in use", key, v, minimum, default)
            return default
        return v

    sim = block.get("sim_report")
    return Config(lo=lo, hi=hi, min_picks=int(num("min_picks", DEFAULT_MIN_PICKS, 1)),
                  window_days=int(num("window_days", DEFAULT_WINDOW_DAYS, 1)), reraise_h=float(num("reraise_h", DEFAULT_RERAISE_H, 0)),
                  sim_report=str(sim).strip() if isinstance(sim, str) and sim.strip() else DEFAULT_SIM_REPORT)


# ------------------------------------------------------------------ reading the block

def _block(day: Any) -> dict[str, Any] | None:
    b = day.get(BLOCK_KEY) if isinstance(day, dict) else None
    return b if isinstance(b, dict) and isinstance(b.get(SCOPE_ALL), dict) else None


def has_block(days: dict[str, Any] | None) -> bool:
    """True when at least one day of the metrics carries a readable `balance` block."""
    return any(_block(d) for d in (days or {}).values())


def _groups(block: dict[str, Any], scope: str) -> dict[str, Any]:
    g = block.get(scope)
    if scope == SCOPE_HUMANS and isinstance(g, dict) and isinstance(g.get(SCOPE_ALL), dict):
        g = g[SCOPE_ALL]                # tolerated: humans_only written as a whole block {"groups": {...}}
    return g if isinstance(g, dict) else {}


def _n(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


@dataclass
class Aggregate:
    version: str
    days: list[str]                                             # the days summed, oldest first
    skipped_days: list[str] = field(default_factory=list)       # days inside the window with another tables version
    stats: dict[str, dict[str, dict[str, dict[str, float]]]] = field(default_factory=dict)   # scope → group → option → sums


def aggregate(days: dict[str, Any] | None, cfg: Config) -> Aggregate | None:
    """Sums of the balance block over the window, or None when no day has one. Only days with the NEWEST tables
    version are summed: numbers measured before a balance patch say nothing about the tables that are live now."""
    with_block = sorted(d for d, v in (days or {}).items() if _block(v))
    if not with_block:
        return None
    newest = with_block[-1]
    version = str(_block(days[newest]).get("version") or UNKNOWN_VERSION)
    try:
        since = (datetime.strptime(newest[:10], "%Y-%m-%d") - timedelta(days=cfg.window_days - 1)).strftime("%Y-%m-%d")
    except ValueError:
        since = ""                                              # day keys that are not dates: no window, every day counts
    agg = Aggregate(version=version, days=[])
    for day in with_block:
        if day < since:
            continue
        b = _block(days[day])
        if str(b.get("version") or UNKNOWN_VERSION) != version:
            agg.skipped_days.append(day)
            continue
        agg.days.append(day)
        for scope in (SCOPE_ALL, SCOPE_HUMANS):
            for group, options in _groups(b, scope).items():
                if not isinstance(options, dict):
                    continue
                for key, st in options.items():
                    if not isinstance(st, dict):
                        continue
                    acc = agg.stats.setdefault(scope, {}).setdefault(str(group), {}).setdefault(str(key), dict.fromkeys(STAT_KEYS, 0.0))
                    for k in STAT_KEYS:
                        acc[k] += _n(st.get(k))
    log.debug("balance aggregate: version %s, days %s, skipped (other version) %s, groups %s", version, agg.days, agg.skipped_days,
              sorted(agg.stats.get(SCOPE_ALL, {})))
    return agg


@dataclass
class Row:
    scope: str
    group: str
    key: str
    picks: int
    wins: int
    win_rate: float | None          # None when there are no picks
    deviation: float | None         # win rate minus the centre of the band
    kd: float | None                # kills / deaths
    damage_ratio: float | None      # damage dealt / damage taken
    enough: bool                    # picks ≥ min_picks
    verdict: str                    # "over" | "under" | "ok" | "few picks"

    @property
    def out_of_band(self) -> bool:
        return self.verdict in ("over", "under")


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 2) if b > 0 else None


def rows(agg: Aggregate | None, cfg: Config) -> list[Row]:
    """One row per scope / group / option; out-of-band rows first inside a group, then by picks."""
    out: list[Row] = []
    if agg is None:
        return out
    for scope in (SCOPE_ALL, SCOPE_HUMANS):
        for group in sorted(agg.stats.get(scope, {})):
            grp: list[Row] = []
            for key, st in agg.stats[scope][group].items():
                picks, wins = int(st["picks"]), int(st["wins"])
                wr = round(wins / picks, 4) if picks > 0 else None
                enough = picks >= cfg.min_picks
                if wr is None or not enough:
                    verdict = "few picks"
                elif wr > cfg.hi:
                    verdict = "over"
                elif wr < cfg.lo:
                    verdict = "under"
                else:
                    verdict = "ok"
                grp.append(Row(scope, group, key, picks, wins, wr, round(wr - cfg.centre, 4) if wr is not None else None,
                               _ratio(st["kills"], st["deaths"]), _ratio(st["damage_dealt"], st["damage_taken"]), enough, verdict))
            grp.sort(key=lambda r: (not r.out_of_band, -(abs(r.deviation) if r.deviation is not None and r.enough else -1), -r.picks, r.key))
            out += grp
    return out


# ------------------------------------------------------------------ rendering (PO, analyst, game designer)

def _f(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}" if signed else f"{v:.2f}"


def render_md(days: dict[str, Any] | None, cfg: Config | None = None) -> str:
    """The compact balance table, or "" when the metrics carry no balance block (the caller appends nothing)."""
    cfg = cfg or Config()
    agg = aggregate(days, cfg)
    if agg is None:
        return ""
    rs = rows(agg, cfg)
    span = agg.days[0] if len(agg.days) == 1 else f"{agg.days[0]}…{agg.days[-1]}"
    out = [f"## Balance — tables version {agg.version}, days {span} ({len(agg.days)}); win-rate band {cfg.band}, min picks {cfg.min_picks}",
           "win rate = wins / picks; deviation = win rate − centre of the band; OVER / UNDER = outside the band with enough picks "
           "(the dispatcher raises one `balance` observation per such option); few picks = not judged yet."]
    if agg.skipped_days:
        out.append(f"Not summed (another tables version): {', '.join(agg.skipped_days)}.")
    groups = sorted({(r.scope, r.group) for r in rs}, key=lambda sg: (sg[1], sg[0] != SCOPE_ALL))
    shown = groups[:MAX_GROUPS * 2]
    for scope, group in shown:
        grp = [r for r in rs if r.scope == scope and r.group == group]
        out += ["", f"### {group} — {SCOPE_LABEL[scope]}", "| option | picks | win rate | deviation | K/D | dmg dealt/taken | verdict |", "|---|---|---|---|---|---|---|"]
        for r in grp[:MAX_OPTIONS]:
            out.append(f"| {r.key} | {r.picks} | {_f(r.win_rate)} | {_f(r.deviation, signed=True)} | {_f(r.kd)} | {_f(r.damage_ratio)} | "
                       f"{r.verdict.upper() if r.out_of_band else r.verdict} |")
        if len(grp) > MAX_OPTIONS:
            out.append(f"… and {len(grp) - MAX_OPTIONS} more option(s) with fewer picks.")
    if len(groups) > len(shown):
        out.append(f"\n… and {len(groups) - len(shown)} more group table(s) not shown.")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ findings → observations

@dataclass
class Finding:
    group: str
    key: str
    version: str
    lead: Row                        # the row the title quotes: all matches when it is out of band, else humans only
    rows: list[Row]                  # every scope's row for this option (context for the PO and the game designer)
    days: list[str]


def findings(days: dict[str, Any] | None, cfg: Config | None = None) -> list[Finding]:
    """Options whose win rate is outside the band with at least `min_picks` picks — ONE finding per group/option even
    when both the all-matches and the humans-only numbers are out. The strongest deviation first."""
    cfg = cfg or Config()
    agg = aggregate(days, cfg)
    if agg is None:
        return []
    by_opt: dict[tuple[str, str], list[Row]] = {}
    for r in rows(agg, cfg):
        by_opt.setdefault((r.group, r.key), []).append(r)
    out = []
    for (group, key), rs in by_opt.items():
        bad = [r for r in rs if r.out_of_band]
        if not bad:
            continue
        lead = next((r for r in bad if r.scope == SCOPE_ALL), bad[0])
        out.append(Finding(group, key, agg.version, lead, rs, list(agg.days)))
    out.sort(key=lambda f: -abs(f.lead.deviation or 0))
    return out


def finding_title(f: Finding, cfg: Config) -> str:
    scope = "" if f.lead.scope == SCOPE_ALL else f", {SCOPE_LABEL[f.lead.scope]}"
    return f"Balance: {f.group}/{f.key} win rate {f.lead.win_rate:.2f} over {f.lead.picks} picks{scope} (band {cfg.band})"[:140]


def _same(o: dict[str, Any], f: Finding) -> bool:
    b = o.get(BLOCK_KEY)
    return isinstance(b, dict) and b.get("group") == f.group and b.get("key") == f.key and str(b.get("version")) == f.version


def _ts(s: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def observation_cards(found: list[Finding], existing: list[dict[str, Any]], cfg: Config, *, now: datetime, url: str = "",
                      run_id: str = "") -> list[dict[str, Any]]:
    """Cards for the findings that are due: an option already raised for the SAME tables version less than
    `reraise_h` hours ago is skipped (so a 5-minute cycle does not repeat it); a new tables version or an older card
    lets it through. At most MAX_OBSERVATIONS_PER_CHECK cards, the strongest deviations first."""
    cards: list[dict[str, Any]] = []
    for f in found:
        prev = [t for t in (_ts(o.get("observed_at")) for o in existing if _same(o, f)) if t]
        last = max(prev) if prev else None
        if last and (now - last) < timedelta(hours=cfg.reraise_h):
            log.debug("balance %s/%s (version %s): raised %.1f h ago < %s h — not repeated", f.group, f.key, f.version,
                      (now - last).total_seconds() / 3600, cfg.reraise_h)
            continue
        if len(cards) >= MAX_OBSERVATIONS_PER_CHECK:
            log.info("balance: %d more out-of-band option(s) wait for the next check (cap %d per check)", len(found) - found.index(f),
                     MAX_OBSERVATIONS_PER_CHECK)
            break
        numbers = "; ".join(f"{SCOPE_LABEL[r.scope]}: {r.picks} picks, {r.wins} wins, win rate {_f(r.win_rate)} ({r.verdict}), "
                            f"K/D {_f(r.kd)}, dmg dealt/taken {_f(r.damage_ratio)}" for r in f.rows)
        stamp = now.strftime("%Y%m%dT%H%M")
        cards.append({
            "title": finding_title(f, cfg), "classification": OBS_CLASS, "source": OBS_SOURCE, "url": url, "run_id": run_id,
            "expected": f"win rate inside {cfg.band} once an option has ≥ {cfg.min_picks} picks (product.yaml `balance`)",
            "actual": f"{numbers}. Balance tables version {f.version}; days {', '.join(f.days)}."
                      + (" Raised before for this version; still out of band." if last else ""),
            "detail": "PO: a game-designer task at once (type design; `standing: balance` when you create it) — one lever in "
                      "design/balance/*.json, before/after expectation, the simulation check to run.",
            BLOCK_KEY: {"group": f.group, "key": f.key, "version": f.version, "scope": f.lead.scope, "direction": f.lead.verdict,
                        "win_rate": f.lead.win_rate, "picks": f.lead.picks, "band": [cfg.lo, cfg.hi], "min_picks": cfg.min_picks},
            # unique per raise: Store.add_observation folds equal fingerprints into the old card, and a re-raise after
            # `reraise_h` (or for a new tables version) must reach the PO as a new one
            "fingerprint": "balance-" + hashlib.sha1(f"{f.group}|{f.key}|{f.version}|{len(prev)}|{stamp}".encode()).hexdigest()[:12]})
    return cards
