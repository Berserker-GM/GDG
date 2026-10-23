from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def docker_env_file(path: Path) -> dict[str, str]:
    """Parse like ``docker run --env-file``: no quote handling and no inline-comment stripping."""
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep and key == key.strip() and " " not in key, f"not a KEY=value line: {line!r}"
        env[key] = value
    return env


def test_env_example_works_as_docker_env_file(monkeypatch):
    env = docker_env_file(ENV_EXAMPLE)
    for key, value in env.items():
        assert " #" not in value, f"{key}: inline comments end up in the value under docker --env-file"
        monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    assert settings.leaderboard_mode == "auto"
    assert settings.min_success_ratio == 0.6
