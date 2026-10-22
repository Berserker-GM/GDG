"""Profile collectors, one per platform.

To add a platform (e.g. Google Developer Program / g.dev), implement the ``Collector`` protocol in a
new module and register it in ``build_collectors``. Members whose ``platform`` has no collector are
skipped by the pipeline.
"""

from __future__ import annotations

import httpx

from app.collectors.base import Collector, ParsedBadge, ProfileRef, ProfileResult
from app.collectors.skills_google import SkillsGoogleCollector
from app.config import Settings


def build_collectors(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Collector]:
    collector = SkillsGoogleCollector(settings=settings, transport=transport)
    return {collector.platform: collector}


__all__ = [
    "Collector",
    "ParsedBadge",
    "ProfileRef",
    "ProfileResult",
    "SkillsGoogleCollector",
    "build_collectors",
]
