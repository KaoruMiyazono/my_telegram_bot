#!/usr/bin/env python3
"""Docker healthcheck for Telegram polling reachability."""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        print("TG_BOT_TOKEN is not set", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"Telegram healthcheck failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if payload.get("ok") is True:
        return 0

    print(f"Telegram healthcheck returned not-ok: {payload!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
