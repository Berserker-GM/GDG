"""Run one pipeline refresh against a MOCKED Google Skills site, then print /api/leaderboard.

Every imported profile gets a synthetic page with a deterministic set of badges, so the whole
import -> refresh -> serve flow can be exercised offline.

    python scripts/mock_refresh.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db  # noqa: E402
from app.api import create_app  # noqa: E402
from app.collectors import build_collectors  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Member  # noqa: E402
from app.pipeline import run_pipeline  # noqa: E402
from tests.helpers import COURSE, GAME, SKILL, badge_page, mock_transport  # noqa: E402


def fake_page(profile_id: str, name: str) -> str:
    seed = int(hashlib.sha256(profile_id.encode()).hexdigest(), 16)
    kinds = [SKILL, COURSE, GAME]
    badges = []
    for j in range(seed % 7):
        source, desc = kinds[(seed >> j) % 3]
        badges.append((f"{seed % 100000}{j}", f"Badge {j}", f"Oct {1 + j * 3}, 2026", source, desc))
    return badge_page(name, badges)


async def main() -> None:
    db.init_db()
    with db.session_scope() as s:
        pages = {
            m.profile_id: fake_page(m.profile_id, m.full_name)
            for m in s.scalars(select(Member).where(Member.profile_id.is_not(None)))
        }
    collectors = build_collectors(get_settings(), transport=mock_transport(pages))
    summary = await run_pipeline("mock", collectors)
    for c in collectors.values():
        await c.aclose()
    print("Run summary:", json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
    with TestClient(create_app()) as client:
        print(json.dumps(client.get("/api/leaderboard?limit=10").json(), indent=2))
