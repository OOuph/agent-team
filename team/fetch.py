"""team-fetch <url>: the only network tool of the reviewer and the personas. GET only, allowed hosts only, body cut,
every request traced to TEAM_TRACE_FILE and sent with TEAM_USER_AGENT so that sitemetrics counts it as `own`."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    grep: str | None = None
    tail: int | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):                       # team-fetch <url> [--grep TEXT] [--tail N]: a big page without the whole body
        if argv[i] == "--grep" and i + 1 < len(argv):
            grep = argv[i + 1]; i += 2; continue
        if argv[i] == "--tail" and i + 1 < len(argv):
            tail = int(argv[i + 1]); i += 2; continue
        rest.append(argv[i]); i += 1
    if len(rest) != 1 or not rest[0].startswith("http"):
        print("usage: team-fetch <url> [--grep TEXT] [--tail N]", file=sys.stderr)
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
    if grep:
        hits = [ln for ln in body.splitlines() if grep.lower() in ln.lower()]
        print(f"[grep {grep!r}: {len(hits)} matching lines of {len(body)} chars]")
        print("\n".join(h.strip()[:500] for h in hits[:40]))
        return 0
    if tail:
        print(f"[tail: last {tail} of {len(body)} chars]")
        print(body[-tail:])
        return 0
    print(body[:max_body])
    if len(body) > max_body:
        print(f"\n… [cut: {len(body) - max_body} more chars; use --grep TEXT or --tail N to see the rest, or fetch a JSON address]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
