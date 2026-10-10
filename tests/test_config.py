import os

import pytest
from pydantic import ValidationError

from lead_enrich.config import FetchSettings, LLMSettings, load_dotenv


def test_dotenv_loads_only_allowlisted_keys(tmp_path, monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY",
        "ENRICH_MODEL",
        "MOCK_LLM",
        "HTTPS_PROXY",
        "ANTHROPIC_BASE_URL",
        "PYTHONPATH",
        "LD_PRELOAD",
    ):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "ANTHROPIC_API_KEY=sk-test\nENRICH_MODEL=claude-test\nMOCK_LLM=1\n"
        "HTTPS_PROXY=http://attacker.example:8080\n"
        "ANTHROPIC_BASE_URL=https://attacker.example\n"
        "PYTHONPATH=/tmp/evil\nLD_PRELOAD=/tmp/evil.so\n"
    )
    load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert os.environ["ENRICH_MODEL"] == "claude-test"
    assert os.environ["MOCK_LLM"] == "1"
    for key in ("HTTPS_PROXY", "ANTHROPIC_BASE_URL", "PYTHONPATH", "LD_PRELOAD"):
        assert key not in os.environ, key


def test_dotenv_never_overrides_existing_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ENRICH_MODEL", "from-shell")
    env = tmp_path / ".env"
    env.write_text("ENRICH_MODEL=from-file\n")
    load_dotenv(env)
    assert os.environ["ENRICH_MODEL"] == "from-shell"


@pytest.mark.parametrize(
    "field", ["timeout_seconds", "connect_timeout_seconds", "deadline_seconds", "cache_ttl_hours"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_fetch_timeouts_must_be_positive(field, value):
    with pytest.raises(ValidationError):
        FetchSettings(**{field: value})


def test_robots_txt_is_never_cached_longer_than_a_day():
    with pytest.raises(ValidationError):
        FetchSettings(robots_cache_ttl_hours=48)


@pytest.mark.parametrize("field", ["timeout_seconds", "cache_ttl_hours"])
def test_llm_timeout_and_cache_ttl_must_be_positive(field):
    with pytest.raises(ValidationError):
        LLMSettings(**{field: 0})
