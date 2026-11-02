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
    if len(argv) != 1 or not argv[0].startswith("http"):
        print("usage: team-fetch <url>", file=sys.stderr)
        return 2
    url = argv[0]
    allowed = [h.strip().lower() for h in os.environ.get("TEAM_ALLOWED_HOSTS", "").split(",") if h.strip()]
    host = (urlsplit(url).hostname or "").lower()
    if allowed and host not in allowed and not any(host.endswith("." + h) for h in allowed):
        print(f"team-fetch: host {host} is not allowed (allowed: {', '.join(allowed)})", file=sys.stderr)
        return 3
    ua = os.environ.get("TEAM_USER_AGENT", "agent-team/fetch")
    max_body = int(os.environ.get("TEAM_MAX_BODY", "12000"))
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
    print(body[:max_body])
    if len(body) > max_body:
        print(f"\n… [cut: {len(body) - max_body} more chars; fetch a JSON address for structured data]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
