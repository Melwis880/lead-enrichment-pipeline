import os

from lead_enrich.config import load_dotenv


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
