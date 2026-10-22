"""Command line: ``python -m app.cli <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import db
from app.config import get_settings


def _setup_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_import(args: argparse.Namespace) -> int:
    from app.importer import FormImportError, format_report, import_form

    db.init_db()
    try:
        with db.session_scope() as session:
            report = import_form(session, args.form, args.teams)
    except FormImportError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    from app.pipeline import RunInProgress, run_pipeline

    db.init_db()
    try:
        summary = asyncio.run(run_pipeline(trigger="cli"))
    except RunInProgress as exc:
        print(f"Refresh refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["status"] == "published" else 1


def cmd_scrape_one(args: argparse.Namespace) -> int:
    from app.collectors import ProfileRef, SkillsGoogleCollector
    from app.urls import check_profile_url

    link = check_profile_url(args.url)
    if link.profile_id is None:
        print(f"Not a supported Google Skills profile URL ({link.status})", file=sys.stderr)
        return 2

    async def go():
        async with SkillsGoogleCollector() as collector:
            return await collector.fetch(ProfileRef(member_id=None, url=link.url, profile_id=link.profile_id))

    result = asyncio.run(go())
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 1


def cmd_leaderboard(args: argparse.Namespace) -> int:
    from app.models import RUN_PUBLISHED, Run

    db.init_db()
    with db.session_scope() as session:
        run = session.scalars(
            select(Run)
            .where(Run.status == RUN_PUBLISHED)
            .order_by(Run.id.desc())
            .options(selectinload(Run.standings))
            .limit(1)
        ).first()
        if run is None:
            print("No published leaderboard yet. Run `python -m app.cli refresh`.")
            return 1
        print(f"Run {run.id} ({run.mode}) published {run.published_at:%Y-%m-%d %H:%M} UTC")
        print(f"{'#':>3}  {'Name':<40} {'Score':>8} {'Badges':>7}  Skill/Course/Game")
        for s in run.standings[: args.limit]:
            counts = (s.breakdown or {}).get("counts_by_category", {})
            mix = f"{counts.get('skill_badge', 0)}/{counts.get('course_badge', 0)}/{counts.get('game_badge', 0)}"
            score = f"{s.score:g}"
            print(f"{s.rank:>3}  {s.name[:40]:<40} {score:>8} {s.counted_badges:>7}  {mix}")
    return 0


def cmd_link_report(args: argparse.Namespace) -> int:
    from app.api import link_report_csv

    db.init_db()
    with db.session_scope() as session:
        content = link_report_csv(session)
    if args.output:
        Path(args.output).write_text(content, encoding="utf-8", newline="")
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(content)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Skill Lab leaderboard")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="Import the Google Form export (.xlsx)")
    p.add_argument("form", help="Form responses .xlsx")
    p.add_argument("--teams", help="Optional USN,Team mapping (.csv or .xlsx)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("refresh", help="Run the scrape + score pipeline once")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("scrape-one", help="Fetch and parse one profile (debugging)")
    p.add_argument("url")
    p.set_defaults(func=cmd_scrape_one)

    p = sub.add_parser("leaderboard", help="Print the current published leaderboard")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_leaderboard)

    p = sub.add_parser("link-report", help="CSV of every member's link status")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_link_report)

    p = sub.add_parser("serve", help="Run the API with uvicorn")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
