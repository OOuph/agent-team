"""Subscription limits and usage credits (owner 04.11: "if the subscription runs out, the purchased credits must be used").

Every `claude -p` stream carries `rate_limit_info`: the weekly window's utilization, whether extra usage (credits) is
allowed for the account, and whether this request is already billed to credits. The CLI switches to credits by itself
when the plan limit is reached and credits are allowed; the dispatcher only has to (1) see it, (2) tell the owner once
per window that real money is now being spent, and (3) stop starting runs when both the plan and the credits are
exhausted, until the window resets, instead of failing every cycle.

State lives in data/billing.json; events go to the product store via the caller.
"""
from __future__ import annotations

import fcntl
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Settings

log = logging.getLogger("team.billing")
STATE_FILE = "billing.json"


def limit_info(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last rate_limit_info of a run's stream-json events, or None."""
    for e in reversed(events):
        info = e.get("rate_limit_info")
        if isinstance(info, dict):
            return info
    return None


def _path(st: Settings) -> Path:
    return st.data_dir / STATE_FILE


def load(st: Settings) -> dict[str, Any]:
    p = _path(st)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(st: Settings, state: dict[str, Any]) -> None:
    p = _path(st)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


DEFAULT_DAILY_CAP_USD = 100.0      # owner 04.11: "лимит расхода 100 в день, общий на всех; подписку не учитывай" — credits only


def daily_cap(st: Settings) -> float:
    return float((st.raw.get("billing") or {}).get("daily_cap_usd_all", DEFAULT_DAILY_CAP_USD))


@contextmanager
def _locked(st: Settings):
    lp = st.data_dir / (STATE_FILE + ".lock")
    lp.parent.mkdir(parents=True, exist_ok=True)
    with lp.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def spent_today(st: Settings, now: float | None = None) -> float:
    """Credits (real money) spent today (UTC) by the whole team: products, framework, retro, reports. Runs covered by
    the subscription are not counted (owner 04.11)."""
    now = now if now is not None else _now()
    return float((load(st).get("credit_spend") or {}).get(_day(now), 0.0))


def remaining_today(st: Settings, now: float | None = None) -> float | None:
    """What a run may still spend today; None while the team runs inside the subscription (no money cap applies)."""
    if not load(st).get("using_credits"):
        return None
    return max(0.0, daily_cap(st) - spent_today(st, now))


def _ts(x: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(x), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return None


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def paused(st: Settings, now: float | None = None) -> str | None:
    """Reason string while runs must not start: plan and credits exhausted until the window resets, or the shared daily cap is reached."""
    now = now if now is not None else _now()
    s = load(st)
    until = s.get("paused_until")
    if until and float(until) > now:
        return f"plan limit and credits exhausted until {_ts(until)}"
    spent = float((s.get("credit_spend") or {}).get(_day(now), 0.0))
    if spent >= daily_cap(st):
        return f"shared daily credit cap ${daily_cap(st):.0f} reached (${spent:.2f} of credits today); runs resume after 00:00 UTC"
    return None


def observe(st: Settings, info: dict[str, Any] | None, run_id: str, notify: Callable[[str], Any] | None = None,
            now: float | None = None, cost: float | None = None) -> list[str]:
    """Book one run's cost into the shared daily ledger and record its limit info; return the transitions it caused
    ("overage_started", "credits_unavailable", "exhausted", "daily_cap"). The owner is notified once per window/day."""
    with _locked(st):
        return _observe(st, info, run_id, notify, now if now is not None else _now(), cost)


def _observe(st: Settings, info: dict[str, Any] | None, run_id: str, notify: Callable[[str], Any] | None, now: float,
             cost: float | None) -> list[str]:
    s = load(st)
    day = _day(now)
    spend = s.setdefault("spend", {})
    if cost:
        spend[day] = round(float(spend.get(day, 0.0)) + float(cost), 4)
        if info and info.get("isUsingOverage"):
            cs = s.setdefault("credit_spend", {})
            cs[day] = round(float(cs.get(day, 0.0)) + float(cost), 4)
    cspend = s.setdefault("credit_spend", {})
    for led in (spend, cspend):
        for d in sorted(led)[:-14]:                        # keep two weeks
            led.pop(d, None)
    sent = s.setdefault("notified", {})
    out: list[str] = []
    if cspend.get(day, 0.0) >= daily_cap(st) and not sent.get(f"daily_cap:{day}"):
        sent[f"daily_cap:{day}"] = day
        out.append("daily_cap")
        if notify:
            try:
                notify(f"Расход кредитов командой за сутки дошёл до общего лимита ${daily_cap(st):.0f} (${cspend[day]:.2f}). "
                       f"Новые прогоны ролей остановлены до 00:00 UTC; сайты работают.")
            except Exception as exc:
                log.warning("billing notify failed: %s", exc)
    if not info:
        _save(st, s)
        return out
    win = str(info.get("resetsAt") or "")
    util = ((info.get("unifiedWindows") or {}).get(str(info.get("rateLimitType") or "")) or {}).get("utilization")
    using = bool(info.get("isUsingOverage"))
    ovs = str(info.get("overageStatus") or "")
    status = str(info.get("status") or "")
    s.update({"updated": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"), "run_id": run_id,
              "window": info.get("rateLimitType"), "window_resets": _ts(info.get("resetsAt")), "utilization": util,
              "overage_status": ovs, "overage_reason": info.get("overageDisabledReason"), "using_credits": using, "status": status})

    def once(kind: str, text: str) -> None:
        key = f"{kind}:{win}"
        if sent.get(key):
            return
        sent[key] = s["updated"]
        out.append(kind)
        if notify:
            try:
                notify(text)
            except Exception as exc:                       # a failed message must not break the run bookkeeping
                log.warning("billing notify failed: %s", exc)

    if using:
        once("overage_started", f"Подписка Claude: недельный лимит исчерпан, команда работает на купленных кредитах "
                                f"(сброс лимита {_ts(info.get('resetsAt'))}). Кредиты — реальные деньги; на них действует общий лимит "
                                f"команды ${daily_cap(st):.0f} в сутки.")
    if ovs and ovs != "allowed" and isinstance(util, (int, float)) and util >= 0.9:
        once("credits_unavailable", f"Подписка Claude: использовано {round(util * 100)} % недельного лимита, а кредиты "
                                    f"недоступны ({info.get('overageDisabledReason') or ovs}). После 100 % команда встанет "
                                    f"до {_ts(info.get('resetsAt'))}.")
    if status == "rejected" and not using and ovs != "allowed":
        until = info.get("overageResetsAt") if ovs == "rejected" and info.get("overageResetsAt") else info.get("resetsAt")
        if until:
            s["paused_until"] = float(until)
        once("exhausted", f"Подписка Claude и кредиты исчерпаны: новые прогоны ролей остановлены до {_ts(until)}. "
                          f"Сайты продолжают работать.")
    _save(st, s)
    return out


def status_line(st: Settings) -> str:
    s = load(st)
    if not s:
        return "Подписка Claude: данных о лимитах ещё нет"
    util = s.get("utilization")
    pct = f"{round(util * 100)} %" if isinstance(util, (int, float)) else "?"
    credits = "идут кредиты" if s.get("using_credits") else ("кредиты доступны" if s.get("overage_status") == "allowed"
                                                            else f"кредиты недоступны ({s.get('overage_reason') or s.get('overage_status')})")
    now = _now()
    cred = float((s.get("credit_spend") or {}).get(_day(now), 0.0))
    return (f"Подписка Claude: неделя {pct}, сброс {s.get('window_resets')}; {credits}. Кредитами сегодня ${cred:.2f} "
            f"из лимита ${daily_cap(st):.0f} в сутки")


def owner_notify(dry_run: bool = False) -> Callable[[str], Any]:
    """Send a billing notice to the owner's ops chat (same bot as the 12-hour report); no-op without its env."""
    def _send(text: str) -> Any:
        from .report import send_telegram
        return send_telegram(text, dry_run)
    return _send
