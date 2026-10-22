import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import shutil

import pytest

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"


def healthcheck_command() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"HEALTHCHECK[^\n]*\\\n\s*CMD (\[.*\])", text)
    assert match, "HEALTHCHECK must use exec-form CMD"
    return json.loads(match.group(1))


def test_healthcheck_uses_port_env():
    cmd = healthcheck_command()
    assert cmd[:2] == ["sh", "-c"]
    assert "${PORT:-8000}" in cmd[2]
    assert "/health" in cmd[2]


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_healthcheck_command_honours_port(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Health)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cmd = healthcheck_command()
        script = cmd[2].replace("python -c", f'"{Path(__import__("sys").executable).as_posix()}" -c', 1)
        ok = subprocess.run(["sh", "-c", script], env={"PORT": str(port)}, timeout=20)
        assert ok.returncode == 0
        # Nothing listens on the default port's replacement -> unhealthy
        bad = subprocess.run(["sh", "-c", script], env={"PORT": "1"}, timeout=20, capture_output=True)
        assert bad.returncode != 0
    finally:
        server.shutdown()
