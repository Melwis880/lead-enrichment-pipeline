"""Settings loaded from YAML with environment overrides."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_USER_AGENT = (
    "LeadEnrichBot/0.1 (+https://github.com/melwis880/lead-enrichment-pipeline; "
    "public business info only)"
)


class FetchSettings(BaseModel):
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = 10.0  # per read
    connect_timeout_seconds: float = 5.0  # capped at timeout_seconds
    deadline_seconds: float = 20.0  # whole response, not per read
    max_retries: int = Field(default=2, ge=0, le=5)
    backoff_base_seconds: float = 1.0
    min_delay_seconds: float = Field(default=2.0, ge=0.5)
    max_pages_per_company: int = Field(default=3, ge=1, le=5)
    max_page_bytes: int = 2_000_000
    cache_dir: str = "cache/http"
    cache_ttl_hours: float = 72.0  # parsed pages; keep it below the schedule interval
    robots_cache_ttl_hours: float = 24.0  # RFC 9309 2.4: at most 24 h


class LLMSettings(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    max_input_chars: int = 12_000
    timeout_seconds: float = 60.0
    max_retries: int = 2
    cache_dir: str = "cache/llm"
    mock_fixtures_dir: str = "fixtures/llm"
    input_price_per_mtok: float = 1.0
    output_price_per_mtok: float = 5.0


class ValidationSettings(BaseModel):
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class Settings(BaseModel):
    fetch: FetchSettings = FetchSettings()
    llm: LLMSettings = LLMSettings()
    validation: ValidationSettings = ValidationSettings()
    mock_llm: bool = False


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from YAML (if given and present), then apply env overrides."""
    data: dict = {}
    if path is not None and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    settings = Settings.model_validate(data)
    if model := os.environ.get("ENRICH_MODEL", "").strip():
        settings.llm.model = model
    settings.mock_llm = env_flag("MOCK_LLM")
    return settings


DOTENV_KEYS = frozenset({"ANTHROPIC_API_KEY", "ENRICH_MODEL", "MOCK_LLM"})


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines for DOTENV_KEYS only, never overriding variables
    already set. Anything else (proxies, base URLs, PYTHONPATH...) is ignored, so a stray or
    planted .env cannot redirect API traffic or code loading."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in DOTENV_KEYS and value and key not in os.environ:
            os.environ[key] = value
