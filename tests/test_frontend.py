import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app

INDEX = Path(__file__).parent.parent / "frontend" / "index.html"
RENDER_FUNCTIONS = ["renderPodium", "renderStudentRows", "renderTeamRows", "renderBadges"]


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


def function_source(html: str, name: str) -> str:
    """Source of ``function name(...) { ... }`` by brace matching."""
    start = html.find(f"function {name}(")
    assert start != -1, f"{name} not found in index.html"
    depth, i = 0, html.index("{", html.index(")", start))
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start : j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_root_serves_index(settings):
    with TestClient(create_app()) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.text == INDEX.read_text(encoding="utf-8")


def test_no_hardcoded_student_entries(html):
    assert "initialSnapshot" not in html
    # Object literals carrying a literal name or numeric id, e.g. {"id": 3, "name": "..."}.
    assert not re.search(r"""["']?name["']?\s*:\s*["'][^"']""", html)
    assert not re.search(r""""id"\s*:\s*\d""", html)
    # Static markup must not ship sample values either.
    for element_id in ["modalStudentName", "modalSubtitle", "modalAvatar"]:
        assert re.search(rf'id="{element_id}"></div>', html), f"#{element_id} should start empty"


def test_escape_html_used_in_render_functions(html):
    assert "function escapeHtml(" in html
    for name in RENDER_FUNCTIONS:
        assert "escapeHtml(" in function_source(html, name), f"{name} must escape API values"


def test_no_raw_api_interpolation(html):
    # API fields interpolated straight into a template string would bypass escapeHtml.
    raw = re.findall(r"\$\{\s*(?:s|t|m|b|e|item)\.(?:name|title|dept|department|avatar|image_url|badge_url)\s*\}", html)
    assert raw == []


def test_ranks_come_from_api(html):
    assert "b.score - a.score" not in html
    assert "i + 1" not in html
    assert "rankLabel(s.rank)" in html and "rankLabel(t.rank)" in html


def test_team_mode_and_no_invented_defaults(html):
    assert 'data.mode === \'team\'' in html
    assert "/api/members/" in html
    assert "'Engineering'" not in html
    assert "|| 2" not in html and "|| 1;" not in html


def test_unavailable_messages(html):
    assert "Leaderboard not published yet" in html
    assert "Couldn't load leaderboard, try again later" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_escape_and_url_guards_behave(html):
    script = "\n".join(
        function_source(html, n) for n in ["escapeHtml", "safeImageUrl", "safeBadgeUrl"]
    ) + """
const FALLBACK_BADGE_URL = 'https://www.skills.google';
console.log(JSON.stringify({
  esc: escapeHtml(`<img src=x onerror="a('b')">&\\u0060`),
  nul: escapeHtml(null),
  img: [safeImageUrl('https://cdn.example/x.png'), safeImageUrl('javascript:alert(1)'), safeImageUrl('http://x')],
  badge: [safeBadgeUrl('https://www.skills.google/public_profiles/x'), safeBadgeUrl('https://evil.example/'),
          safeBadgeUrl('https://www.skills.google.evil.example/')],
}));
"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out["esc"] == "&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;&#96;"
    assert out["nul"] == ""
    assert out["img"] == ["https://cdn.example/x.png", None, None]
    assert out["badge"] == [
        "https://www.skills.google/public_profiles/x",
        "https://www.skills.google",
        "https://www.skills.google",
    ]
