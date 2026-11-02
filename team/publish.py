"""Publish posts to social channels on behalf of the marketer role (CLI: ``team-publish``).

The marketer role has no network libraries of its own: every outgoing post goes through this module,
which appends the mandatory "written and posted by an AI agent" disclosure (EU AI Act, art. 50).
The role cannot switch the disclosure off: an empty disclosure falls back to ``DEFAULT_DISCLOSURE``.

Channels: telegram (Bot API), bluesky (AT Protocol), devto (Forem API), mastodon, x (stub).
stdlib only. Network calls go through an injectable ``http(method, url, headers, body, timeout)``
callable returning ``(status, body_bytes)``; tests pass a fake, production uses urllib.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("team.publish")

# ---- settings ---------------------------------------------------------------------------------
DEFAULT_DISCLOSURE = "Written and posted by an AI agent without human editing."
TIMEOUT_S = 20
ERROR_BODY_CHARS = 200
USER_AGENT = "agent-team-publish/0.1"
DRY_RUN_ENV = "TEAM_PUBLISH_DRY_RUN"

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096
BLUESKY_PDS_DEFAULT = "https://bsky.social"
BLUESKY_MAX_GRAPHEMES = 300
BLUESKY_POST_COLLECTION = "app.bsky.feed.post"
DEVTO_API = "https://dev.to/api/articles"
DEVTO_MAX_TAGS = 4
MASTODON_MAX_CHARS = 500
ELLIPSIS = "…"

CHANNELS = ("telegram", "bluesky", "devto", "mastodon", "x")
SECRET_KEYS = {"telegram": ("TELEGRAM_BOT_TOKEN",), "bluesky": ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"),
               "devto": ("DEVTO_API_KEY",), "mastodon": ("MASTODON_INSTANCE", "MASTODON_TOKEN"), "x": ()}

HttpFn = Callable[[str, str, dict[str, str], bytes | None, int], tuple[int, bytes]]


@dataclass
class PostResult:
    ok: bool
    channel: str
    post_id: str | None
    url: str | None
    error: str | None
    dry_run: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class PublishError(Exception):
    """Raised internally for a failed step; converted to PostResult(ok=False) in post()."""


# ---- helpers ----------------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
_URL_TRAIL = ".,;:!?'\""
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)
_ZWJ = "‍"
_KEYCAP = 0x20E3


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def graphemes(text: str) -> list[str]:
    """Approximate extended grapheme clusters: combining marks, ZWJ sequences, variation selectors,
    skin tones, keycaps and regional-indicator pairs stay attached to their base character."""
    out: list[str] = []
    prev_zwj = False
    ri_run = 0
    for ch in text:
        cp = ord(ch)
        is_ri = cp in _REGIONAL
        ri_run = ri_run + 1 if is_ri else 0
        join = bool(out) and (
            prev_zwj or ch == _ZWJ or unicodedata.combining(ch) != 0 or unicodedata.category(ch) in ("Mn", "Mc", "Me")
            or cp in _VARIATION_SELECTORS or cp in _SKIN_TONES or cp == _KEYCAP or (is_ri and ri_run % 2 == 0)
            or (ch == "\n" and out[-1] == "\r")
        )
        if join:
            out[-1] += ch
        else:
            out.append(ch)
        prev_zwj = ch == _ZWJ
    return out


def truncate_graphemes(text: str, limit: int) -> str:
    """Cut text to ``limit`` graphemes, ending with an ellipsis when something was removed."""
    g = graphemes(text)
    if len(g) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS if limit == 1 else ""
    return "".join(g[: limit - 1]).rstrip() + ELLIPSIS


def truncate_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS if limit == 1 else ""
    return text[: limit - 1].rstrip() + ELLIPSIS


def find_urls(text: str) -> list[tuple[int, int, str]]:
    """(start, end, url) character spans of http(s) links; trailing punctuation is not part of the link."""
    out = []
    for m in _URL_RE.finditer(text):
        s, e, u = m.start(), m.end(), m.group(0)
        stripped = u.rstrip(_URL_TRAIL)
        e -= len(u) - len(stripped)
        if stripped:
            out.append((s, e, stripped))
    return out


def bluesky_facets(text: str) -> list[dict[str, Any]]:
    """AT Protocol link facets with UTF-8 byte offsets for every URL in ``text``."""
    facets = []
    for s, e, u in find_urls(text):
        b_start = len(text[:s].encode("utf-8"))
        b_end = b_start + len(u.encode("utf-8"))
        facets.append({"index": {"byteStart": b_start, "byteEnd": b_end},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": u}]})
    return facets


def compose(text: str, disclosure: str, url: str | None, *, limit: int | None, counter: str = "graphemes") -> str:
    """body + optional url line + disclosure, truncating only the body (with an ellipsis) to fit ``limit``."""
    body = text.strip()
    fixed = ""
    if url and url not in body:
        fixed += "\n" + url
    fixed += "\n\n" + disclosure
    if limit is not None:
        budget = limit - (len(graphemes(fixed)) if counter == "graphemes" else len(fixed))
        if budget < 1:
            raise PublishError(f"disclosure and url do not fit the {limit} limit")
        body = truncate_graphemes(body, budget) if counter == "graphemes" else truncate_chars(body, budget)
    return body + fixed


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact(s: str, secrets: list[str]) -> str:
    for sec in secrets:
        if sec:
            s = s.replace(sec, "<redacted>")
    return s


def _require(env: dict[str, str], keys: tuple[str, ...]) -> dict[str, str]:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise PublishError("missing secret(s): " + ", ".join(missing))
    return {k: env[k] for k in keys}


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int) -> tuple[int, bytes]:
    """Default transport. HTTP error statuses are returned, not raised; network failures raise."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status), r.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()


def _call_json(http: HttpFn, method: str, url: str, payload: dict[str, Any] | None, headers: dict[str, str],
               secrets: list[str], label: str) -> dict[str, Any]:
    """POST/GET JSON, raising PublishError with status and the first 200 chars of the body on any failure."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    body = None
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log.debug("%s %s %s", label, method, _redact(url, secrets))
    try:
        status, raw = http(method, url, hdrs, body, TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - any transport failure becomes a result, never an exception
        raise PublishError(f"{label}: network error: {_redact(f'{type(e).__name__}: {e}', secrets)}") from e
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if not 200 <= status < 300:
        raise PublishError(f"{label}: HTTP {status}: {_redact(text[:ERROR_BODY_CHARS], secrets)}")
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise PublishError(f"{label}: HTTP {status}: non-JSON body: {_redact(text[:ERROR_BODY_CHARS], secrets)}") from e
    return data if isinstance(data, dict) else {"data": data}


def _print_dry_run(channel: str, payload: dict[str, Any]) -> None:
    print(json.dumps({"dry_run": True, "channel": channel, "payload": payload}, ensure_ascii=False, indent=2),
          file=sys.stderr)


# ---- channels ---------------------------------------------------------------------------------
def _telegram(text: str, target: str | None, url: str | None, disclosure: str, env: dict[str, str],
              dry_run: bool, http: HttpFn) -> PostResult:
    if not target:
        raise PublishError("telegram: target (chat id or @channel) is required")
    body = compose(html.escape(text), f"<i>{html.escape(disclosure)}</i>", html.escape(url) if url else None,
                   limit=TELEGRAM_MAX_CHARS, counter="chars")
    payload = {"chat_id": target, "text": body, "parse_mode": "HTML", "disable_web_page_preview": False}
    if dry_run:
        _print_dry_run("telegram", payload)
        return PostResult(True, "telegram", None, None, None, True)
    token = _require(env, SECRET_KEYS["telegram"])["TELEGRAM_BOT_TOKEN"]
    data = _call_json(http, "POST", f"{TELEGRAM_API}/bot{token}/sendMessage", payload, {}, [token], "telegram")
    if not data.get("ok"):
        raise PublishError(f"telegram: API error: {_redact(json.dumps(data)[:ERROR_BODY_CHARS], [token])}")
    msg = data.get("result") or {}
    mid = msg.get("message_id")
    username = (msg.get("chat") or {}).get("username") or (target[1:] if target.startswith("@") else None)
    link = f"https://t.me/{username}/{mid}" if username and mid is not None else None
    return PostResult(True, "telegram", str(mid) if mid is not None else None, link, None, False)


def _bluesky(text: str, url: str | None, disclosure: str, env: dict[str, str], dry_run: bool, http: HttpFn) -> PostResult:
    body = compose(text, disclosure, url, limit=BLUESKY_MAX_GRAPHEMES, counter="graphemes")
    record: dict[str, Any] = {"$type": BLUESKY_POST_COLLECTION, "text": body, "createdAt": utcnow_iso()}
    facets = bluesky_facets(body)
    if facets:
        record["facets"] = facets
    if dry_run:
        _print_dry_run("bluesky", {"collection": BLUESKY_POST_COLLECTION, "record": record})
        return PostResult(True, "bluesky", None, None, None, True)
    sec = _require(env, SECRET_KEYS["bluesky"])
    pds = (env.get("BLUESKY_PDS") or BLUESKY_PDS_DEFAULT).rstrip("/")
    handle, password = sec["BLUESKY_HANDLE"].lstrip("@"), sec["BLUESKY_APP_PASSWORD"]
    session = _call_json(http, "POST", f"{pds}/xrpc/com.atproto.server.createSession",
                         {"identifier": handle, "password": password}, {}, [password], "bluesky.createSession")
    jwt, did = session.get("accessJwt"), session.get("did")
    if not jwt or not did:
        raise PublishError("bluesky.createSession: response without accessJwt/did")
    out = _call_json(http, "POST", f"{pds}/xrpc/com.atproto.repo.createRecord",
                     {"repo": did, "collection": BLUESKY_POST_COLLECTION, "record": record},
                     {"Authorization": f"Bearer {jwt}"}, [password, jwt], "bluesky.createRecord")
    uri = str(out.get("uri") or "")
    rkey = uri.rsplit("/", 1)[-1] if uri else None
    link = f"https://bsky.app/profile/{session.get('handle') or handle}/post/{rkey}" if rkey else None
    return PostResult(True, "bluesky", uri or None, link, None, False)


def _devto(text: str, title: str | None, url: str | None, tags: list[str] | None, disclosure: str,
           env: dict[str, str], dry_run: bool, http: HttpFn) -> PostResult:
    if not title:
        raise PublishError("devto: title is required")
    article: dict[str, Any] = {"title": title, "body_markdown": f"*{disclosure}*\n\n{text.strip()}\n", "published": True,
                               "tags": [t for t in (tags or []) if t][:DEVTO_MAX_TAGS]}
    if url:
        article["canonical_url"] = url
    payload = {"article": article}
    if dry_run:
        _print_dry_run("devto", payload)
        return PostResult(True, "devto", None, None, None, True)
    key = _require(env, SECRET_KEYS["devto"])["DEVTO_API_KEY"]
    data = _call_json(http, "POST", DEVTO_API, payload, {"api-key": key}, [key], "devto")
    pid = data.get("id")
    return PostResult(True, "devto", str(pid) if pid is not None else None, data.get("url"), None, False)


def _mastodon(text: str, url: str | None, disclosure: str, env: dict[str, str], dry_run: bool, http: HttpFn) -> PostResult:
    body = compose(text, disclosure, url, limit=MASTODON_MAX_CHARS, counter="chars")
    payload = {"status": body, "visibility": "public"}
    if dry_run:
        _print_dry_run("mastodon", payload)
        return PostResult(True, "mastodon", None, None, None, True)
    sec = _require(env, SECRET_KEYS["mastodon"])
    instance = sec["MASTODON_INSTANCE"].rstrip("/")
    if not instance.startswith("http"):
        instance = "https://" + instance
    token = sec["MASTODON_TOKEN"]
    data = _call_json(http, "POST", f"{instance}/api/v1/statuses", payload,
                      {"Authorization": f"Bearer {token}", "Idempotency-Key": sha256_text(body)[:32]}, [token], "mastodon")
    pid = data.get("id")
    return PostResult(True, "mastodon", str(pid) if pid is not None else None, data.get("url"), None, False)


# ---- public API -------------------------------------------------------------------------------
def post(channel: str, text: str, *, url: str | None = None, title: str | None = None, tags: list[str] | None = None,
         disclosure: str, dry_run: bool = False, env: dict[str, str] | None = None, http: HttpFn | None = None,
         target: str | None = None) -> PostResult:
    """Publish ``text`` to ``channel`` with the disclosure appended. Never raises: failures are PostResult(ok=False).

    ``target`` is channel-specific (telegram: chat id or @channel). ``dry_run`` builds and prints the payload
    (stderr, no secrets) without any network call; ``TEAM_PUBLISH_DRY_RUN=1`` in the environment forces it.
    """
    env = dict(os.environ if env is None else env)
    http = http or urllib_http
    disclosure = (disclosure or "").strip() or DEFAULT_DISCLOSURE
    dry_run = dry_run or env.get(DRY_RUN_ENV, "") == "1"
    log.info("post channel=%s target=%s dry_run=%s chars=%d", channel, target, dry_run, len(text or ""))
    try:
        if channel not in CHANNELS:
            raise PublishError(f"unknown channel {channel!r}; known: {', '.join(CHANNELS)}")
        if not (text or "").strip():
            raise PublishError("text is empty")
        if channel == "telegram":
            res = _telegram(text, target, url, disclosure, env, dry_run, http)
        elif channel == "bluesky":
            res = _bluesky(text, url, disclosure, env, dry_run, http)
        elif channel == "devto":
            res = _devto(text, title, url, tags, disclosure, env, dry_run, http)
        elif channel == "mastodon":
            res = _mastodon(text, url, disclosure, env, dry_run, http)
        else:
            raise PublishError(f"{channel}: not configured")
    except PublishError as e:
        log.warning("post failed channel=%s: %s", channel, e)
        return PostResult(False, channel, None, None, str(e), dry_run)
    except Exception as e:  # noqa: BLE001 - keep the contract: no exception escapes post()
        log.exception("post crashed channel=%s", channel)
        return PostResult(False, channel, None, None, f"{channel}: {type(e).__name__}: {e}", dry_run)
    log.info("post done channel=%s ok=%s id=%s url=%s", channel, res.ok, res.post_id, res.url)
    return res


def append_ledger(path: Path, res: PostResult, target: str | None, text: str) -> None:
    """Append one JSON line (the dispatcher's journal) to ``path``, creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": utcnow_iso(), "channel": res.channel, "target": target, "ok": res.ok, "post_id": res.post_id,
           "url": res.url, "error": res.error, "dry_run": res.dry_run, "sha256": sha256_text(text)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.debug("ledger appended %s", path)



def _disclosure_for(channel: str) -> str:
    """Per-channel disclosure from the dispatcher (TEAM_PUBLISH_DISCLOSURES: JSON channel -> text, with `default`),
    else TEAM_PUBLISH_DISCLOSURE, else the module default. Never empty: the tool always discloses."""
    raw = os.environ.get("TEAM_PUBLISH_DISCLOSURES")
    if raw:
        try:
            d = json.loads(raw) or {}
            txt = str(d.get(channel) or d.get("default") or "").strip()
            if txt:
                return txt
        except (ValueError, TypeError):
            pass
    return os.environ.get("TEAM_PUBLISH_DISCLOSURE") or DEFAULT_DISCLOSURE


def _with_src(url: str | None, channel: str) -> str | None:
    """Attribution: ?src=<channel> on the linked page so that sitemetrics can tell which channel brought a visit.
    Only for http(s) links without a src= already; TEAM_PUBLISH_NO_SRC=1 switches it off."""
    if not url or not url.startswith("http") or os.environ.get("TEAM_PUBLISH_NO_SRC") == "1" or "src=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}src={channel}"


def _daily_limit_error(channel: str, ledger: Path | None) -> str | None:
    """The dispatcher's daily cap per channel: TEAM_PUBLISH_MAX_PER_DAY, plus posts already booked today
    (TEAM_PUBLISH_COUNTS, a JSON object channel → count) and the ok lines of this run's ledger. Final for today."""
    raw = os.environ.get("TEAM_PUBLISH_MAX_PER_DAY")
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError:
        return None
    n = 0
    try:
        n += int((json.loads(os.environ.get("TEAM_PUBLISH_COUNTS") or "{}") or {}).get(channel, 0))
    except (ValueError, TypeError):
        pass
    if ledger and ledger.exists():
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for ln in ledger.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("channel") == channel and e.get("ok") and not e.get("dry_run") and str(e.get("ts", "")).startswith(today):
                n += 1
    if n >= cap:
        return f"daily limit reached for {channel}: {n} of {cap} posts today; no more posts until tomorrow"
    return None

# ---- CLI --------------------------------------------------------------------------------------
def _parse_tags(s: str | None) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="team-publish", description=__doc__.splitlines()[0])
    p.add_argument("--channel", required=True, choices=CHANNELS)
    p.add_argument("--target", help="telegram: chat id or @channel")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text-file", type=Path)
    src.add_argument("--text")
    p.add_argument("--title", help="devto: article title")
    p.add_argument("--url", help="link to attach / canonical_url for devto")
    p.add_argument("--tags", help="comma-separated")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--disclosure", default=DEFAULT_DISCLOSURE)
    p.add_argument("--ledger", type=Path, default=(Path(os.environ["TEAM_PUBLISH_LEDGER"]) if os.environ.get("TEAM_PUBLISH_LEDGER") else None),
                   help="JSONL journal to append the result to (default: $TEAM_PUBLISH_LEDGER, set by the dispatcher)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        text = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
    except OSError as e:
        res = PostResult(False, args.channel, None, None, f"cannot read text file: {e}", args.dry_run)
        print(res.to_json())
        return 1
    disclosure = args.disclosure if args.disclosure != DEFAULT_DISCLOSURE else _disclosure_for(args.channel)
    url = _with_src(args.url, args.channel)
    limit_err = _daily_limit_error(args.channel, args.ledger)
    if limit_err:
        res = PostResult(False, args.channel, None, None, limit_err, args.dry_run)
    else:
        res = post(args.channel, text, url=url, title=args.title, tags=_parse_tags(args.tags),
                   disclosure=disclosure, dry_run=args.dry_run, target=args.target)
    if args.ledger:
        try:
            append_ledger(args.ledger, res, args.target, text)
        except OSError as e:
            log.error("ledger write failed: %s", e)
    print(res.to_json())
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
