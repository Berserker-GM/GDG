"""Poll /api/leaderboard/status until the current run finishes.

    pip install -r requirements-dev.txt
    python scripts/monitor_run.py --base-url http://localhost:8000

The base URL falls back to the BASE_URL environment variable.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"),
                        help="server URL (env BASE_URL, default http://localhost:8000)")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between polls")
    parser.add_argument("--max-polls", type=int, default=40, help="give up after this many polls")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    for i in range(args.max_polls):
        d = requests.get(f"{base}/api/leaderboard/status", timeout=30).json()
        run = d.get("last_run") or {}
        print(f"[{i * args.interval:.0f}s] running={d['running']} | attempted={run.get('attempted', 0)}"
              f" succeeded={run.get('succeeded', 0)} failed={run.get('failed', 0)} status={run.get('status', '?')}")
        if not d["running"]:
            print("Done!")
            return 0
        time.sleep(args.interval)
    print("Still running; stopped polling.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
