"""Generate a SYNTHETIC form export for local testing (no real student data).

    python scripts/generate_sample_form.py data/sample_form.xlsx [--teams data/sample_teams.csv]

Profiles use made-up UUIDs; pair this with mocked fetches (see scripts/mock_refresh.py).
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import UUIDS, form_row, profile_url, write_form  # noqa: E402

NAMES = [
    "Aarav Shetty", "Bhavana Rao", "Chirag Gowda", "Divya Nair", "Eshan Kulkarni", "Fathima Khan",
    "Gagan Reddy", "Harini Iyer", "Ishaan Patil", "Jyothi Hegde", "Karthik Bhat", "Lavanya Murthy",
]
DEPTS = ["CSE", "AI&ML", "CSBS", "ECE"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--teams")
    args = parser.parse_args()

    start = datetime(2026, 9, 1, 9, 0)
    rows = []
    for i, name in enumerate(NAMES):
        rows.append(form_row(f"1BY23CS{i + 1:03d}", f" {name} ", profile_url(i), ts=start + timedelta(hours=i),
                             dept=DEPTS[i % 4], year=float(2 + i % 3)))
    # Data issues seen in the real export
    rows.append(form_row("1by23cs002 ", "Bhavana Rao", profile_url(50), ts=start + timedelta(days=3)))  # dup USN
    rows.append(form_row("1BY23CS101", "Mohan Das", "https://me.developers.google.com/u/104857600123"))
    rows.append(form_row("1BY23CS102", "Nisha Jain", "https://www.google.com/search?q=my+profile"))
    rows.append(form_row("1BY23CS103", "Omkar Joshi", profile_url(53), registered="No", email="omkar.gmail.com"))
    rows.append(form_row("1BY23CS104", "Pooja Shah",
                         f"https://www.cloudskillsboost.google/public_profiles/{UUIDS[54]}/"))
    out = write_form(Path(args.output), rows)
    print(f"Wrote {out} ({len(rows)} rows)")

    if args.teams:
        with open(args.teams, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["USN", "Team"])
            for i in range(len(NAMES)):
                writer.writerow([f"1BY23CS{i + 1:03d}", f"Team {chr(65 + i // 3)}"])
        print(f"Wrote {args.teams}")


if __name__ == "__main__":
    main()
