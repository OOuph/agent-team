"""team-fetch <url>: the only network tool of the reviewer and the personas. GET only, allowed hosts only, body cut,
every request traced to TEAM_TRACE_FILE and sent with TEAM_USER_AGENT so that sitemetrics counts it as `own`."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit


def _json_out(body: str, path: str, keys: bool, count: bool) -> int:
    """--json-path a.b[0].c: print the value (or its length with --count); --json-keys: keys of the value at the path."""
    try:
        val = json.loads(body)
    except ValueError as e:
        print(f"team-fetch: body is not JSON ({e})", file=sys.stderr)
        return 4
    for name, idx in re.findall(r"([^.\[\]]+)|\[(-?\d+)\]", path):
        try:
            val = val[int(idx)] if idx else val[name]
        except (KeyError, IndexError, TypeError):
            print(f"team-fetch: json path {path!r} not found at {(name or idx)!r}", file=sys.stderr)
            return 4
    if keys:
        if not isinstance(val, dict):
            print(f"team-fetch: value at {path!r} is not an object", file=sys.stderr)
            return 4
        print(json.dumps(list(val), ensure_ascii=False))
    elif count:
        print(len(val) if isinstance(val, (list, dict, str)) else val)
    else:
        print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
    return 0


# real shell metacharacters, double quote, "=" (attribute text) and "&" (HTML entities); plain words such as "not" are fine
DENIED_GREP_CHARS = '|;&$`"='


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    grep: str | None = None
    tail: int | None = None
    around: int | None = None
    offset = 0
    count = False
    tag = False
    json_path: str | None = None
    json_keys = False
    rest: list[str] = []
    i = 0
    while i < len(argv):                       # team-fetch <url> [--grep TEXT] [--tail N]: a big page without the whole body
        if argv[i] == "--grep" and i + 1 < len(argv):
            bad = next((c for c in argv[i + 1] if c in DENIED_GREP_CHARS), None)
            if bad is not None:
                print(f"team-fetch: --grep pattern contains the denied character {bad!r}; use one plain word, or --tag NAME / --count", file=sys.stderr)
                return 2
            grep = argv[i + 1]; i += 2; continue
        if argv[i] == "--tag" and i + 1 < len(argv):   # literal "<NAME": the role never types < or >
            grep = "<" + argv[i + 1].strip("<>/ "); tag = True; i += 2; continue
        if argv[i] == "--tail" and i + 1 < len(argv):
            tail = int(argv[i + 1]); i += 2; continue
        if argv[i] == "--around" and i + 1 < len(argv):
            around = int(argv[i + 1]); i += 2; continue
        if argv[i] == "--offset" and i + 1 < len(argv):
            offset = int(argv[i + 1]); i += 2; continue
        if argv[i] == "--json-path" and i + 1 < len(argv):
            json_path = argv[i + 1]; i += 2; continue
        if argv[i] == "--json-keys":
            json_keys = True; i += 1; continue
        if argv[i] == "--count":
            count = True; i += 1; continue
        rest.append(argv[i]); i += 1
    if len(rest) != 1 or not rest[0].startswith("http"):
        print("usage: team-fetch <url> [--grep TEXT | --tag NAME [--around N] [--count]] [--tail N] [--offset K] [--json-path a.b[0].c [--count]] [--json-keys]", file=sys.stderr)
        return 2
    url = rest[0]
    allowed = [h.strip().lower() for h in os.environ.get("TEAM_ALLOWED_HOSTS", "").split(",") if h.strip()]
    host = (urlsplit(url).hostname or "").lower()
    if allowed and host not in allowed and not any(host.endswith("." + h) for h in allowed):
        print(f"team-fetch: host {host} is not allowed (allowed: {', '.join(allowed)})", file=sys.stderr)
        return 3
    ua = os.environ.get("TEAM_USER_AGENT", "agent-team/fetch")
    max_body = int(os.environ.get("TEAM_MAX_BODY", "30000"))
    timeout = float(os.environ.get("TEAM_TIMEOUT", "20"))
    pause = float(os.environ.get("TEAM_PAUSE", "0.5"))
    time.sleep(pause)
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html, application/json, text/plain, */*;q=0.5"})
    status, headers, body = 0, {}, ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, headers, body = r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, headers, body = e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        body = f"{type(e).__name__}: {e}"
    trace = os.environ.get("TEAM_TRACE_FILE")
    if trace:
        with open(trace, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "url": url, "status": status,
                                "bytes": len(body)}) + "\n")
    print(f"HTTP {status}")
    for k in ("content-type", "content-length", "last-modified", "location", "cache-control"):
        if k in headers:
            print(f"{k}: {headers[k]}")
    print()
    if json_path is not None or json_keys:
        return _json_out(body, json_path or "", json_keys, count)
    if tag and around is None and not count:
        around = 80
    if grep and (around is not None or count):
        low, needle = body.lower(), grep.lower()
        pos, start = [], low.find(needle)
        while start != -1:
            pos.append(start); start = low.find(needle, start + max(1, len(needle)))
        lines = sum(1 for ln in body.splitlines() if needle in ln.lower())
        print(f"[grep {grep!r}: {len(pos)} matches in {lines} lines of {len(body)} chars]")
        if count:
            return 0
        for n, p in enumerate(pos[:40], 1):
            a = max(0, p - around)
            print(f"--- match {n} at char {p} ---")
            print(body[a:p + len(grep) + around])
        return 0
    if grep:
        hits = [ln for ln in body.splitlines() if grep.lower() in ln.lower()]
        print(f"[grep {grep!r}: {len(hits)} matching lines of {len(body)} chars]")
        print("\n".join(h.strip()[:500] for h in hits[:40]))
        return 0
    if tail:
        print(f"[tail: last {tail} of {len(body)} chars]")
        print(body[-tail:])
        return 0
    chunk = body[offset:offset + max_body]
    print(chunk)
    rest_chars = len(body) - offset - len(chunk)
    if rest_chars > 0:
        print(f"\n… [cut: {rest_chars} more chars after char {offset + len(chunk)}; use --offset K, --grep TEXT [--around N] or --tail N to see the rest, or fetch a JSON address]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
