"""Drive a running server end to end: import the form if the DB is empty, refresh, wait, print the board.

Talks to the HTTP API only, so it works against a local or a deployed instance.

    pip install -r requirements-dev.txt
    python scripts/run_pipeline.py --base-url http://localhost:8000 --admin-token "$ADMIN_TOKEN" --xlsx path/to/form.xlsx

Every option falls back to an environment variable: BASE_URL, ADMIN_TOKEN, FORM_XLSX.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"),
                        help="server URL (env BASE_URL, default http://localhost:8000)")
    parser.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN"),
                        help="value for X-Admin-Token (env ADMIN_TOKEN)")
    parser.add_argument("--xlsx", default=os.environ.get("FORM_XLSX"),
                        help="form export to import when the DB is empty (env FORM_XLSX)")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="delay between status polls")
    parser.add_argument("--max-polls", type=int, default=120, help="give up waiting after this many polls")
    args = parser.parse_args()
    if not args.admin_token:
        parser.error("an admin token is required: pass --admin-token or set ADMIN_TOKEN")
    args.base_url = args.base_url.rstrip("/")
    return args


def import_form(base: str, headers: dict[str, str], xlsx: Path) -> None:
    print("\nImporting form...")
    with xlsx.open("rb") as f:
        resp = requests.post(f"{base}/api/admin/import", headers=headers,
                             files={"form": (xlsx.name, f, XLSX_MIME)}, timeout=120)
    resp.raise_for_status()
    rpt = resp.json()
    print(f"  Created: {rpt['created']}  Updated: {rpt['updated']}  Unique: {rpt['unique_students']}")
    if rpt.get("unsupported_links"):
        print(f"  Unsupported platform: {len(rpt['unsupported_links'])}")
    if rpt.get("usn_conflicts"):
        print(f"  USN conflicts resolved: {len(rpt['usn_conflicts'])}")


def main() -> int:
    args = parse_args()
    base = args.base_url
    headers = {"X-Admin-Token": args.admin_token}

    # 1. Check current status
    status = requests.get(f"{base}/api/leaderboard/status", timeout=30).json()
    print(f"Active members in DB: {status['active_members']}")

    # 2. Import if DB is empty
    if status["active_members"] == 0:
        if not args.xlsx:
            print("DB is empty and no form was given: pass --xlsx or set FORM_XLSX.", file=sys.stderr)
            return 2
        xlsx = Path(args.xlsx)
        if not xlsx.is_file():
            print(f"Form not found: {xlsx}", file=sys.stderr)
            return 2
        import_form(base, headers, xlsx)
    else:
        print("DB already populated — skipping import.")

    # 3. Trigger refresh (scrape badges)
    print("\nTriggering badge scrape...")
    resp = requests.post(f"{base}/api/admin/refresh", headers=headers, timeout=30)
    if resp.status_code == 409:
        print("  A run is already in progress.")
    else:
        resp.raise_for_status()
        print(f"  Run #{resp.json()['run_id']} started.")

    # 4. Poll until done
    print("\nWaiting for scrape to complete (this may take a few minutes)...")
    for i in range(args.max_polls):
        time.sleep(args.poll_seconds)
        st = requests.get(f"{base}/api/leaderboard/status", timeout=30).json()
        run = st.get("last_run") or {}
        run_status = run.get("status", "?")
        print(f"  [{(i + 1) * args.poll_seconds:.0f}s] {run.get('attempted', 0)}/{st['active_members']} fetched"
              f"  ok={run.get('succeeded', 0)}  fail={run.get('failed', 0)}  status={run_status}")
        if not st["running"]:
            print(f"\nScrape finished with status: {run_status}")
            break
    else:
        print("\nStopped waiting; the run is still going on the server.")

    # 5. Print leaderboard
    print("\n" + "=" * 70)
    print("LEADERBOARD")
    print("=" * 70)
    lb = requests.get(f"{base}/api/leaderboard", params={"limit": 100}, timeout=30).json()
    entries = lb.get("entries", [])
    if not entries:
        print("No published results yet.")
    else:
        print(f"{'Rank':<5} {'Name':<30} {'Score':>7}  {'Badges':>6}  Dept")
        print("-" * 70)
        for e in entries:
            print(f"{e['rank']:<5} {e['name']:<30} {e['score']:>7.1f}  {e['counted_badges']:>6}  {e.get('department') or ''}")

    print(f"\nTotal entries: {len(entries)}")
    print(f"Published at:  {lb.get('published_at')}")
    print(f"Mode:          {lb.get('mode')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
