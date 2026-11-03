"""Refresh the long-lived Threads token stored in .env (``bin/threads-refresh-token``).

A long-lived Threads token lives 60 days and can be refreshed once it is older than 24 h and not yet expired. This
rewrites the ``THREADS_ACCESS_TOKEN=`` line in place and prints only the new expiry, never the token.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team.publish import PublishError, HttpFn, threads_refresh_token

log = logging.getLogger("team.threads_token")
KEY = "THREADS_ACCESS_TOKEN"


def refresh_env(path: Path, http: HttpFn | None = None) -> datetime:
    """Refresh the token in ``path``; return the new expiry (UTC). Raises PublishError / ValueError on failure."""
    lines = path.read_text(encoding="utf-8").splitlines()
    cur = next((ln.split("=", 1)[1].strip() for ln in lines if ln.startswith(KEY + "=")), "")
    if not cur:
        raise ValueError(f"{KEY} not set in {path}")
    new, expires_in = threads_refresh_token(cur, http=http)
    path.write_text("\n".join(f"{KEY}={new}" if ln.startswith(KEY + "=") else ln for ln in lines) + "\n", encoding="utf-8")
    until = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    log.info("threads token refreshed in %s; valid until %s", path, until.isoformat(timespec="minutes"))
    return until


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0] if args else ".env")
    try:
        until = refresh_env(path)
    except (PublishError, ValueError, OSError) as e:
        log.error("refresh failed: %s", e)
        return 1
    print(f"threads token refreshed; valid until ≈ {until:%Y-%m-%d %H:%M} UTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())
