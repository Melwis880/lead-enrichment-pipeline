import pytest

from lead_enrich.config import FetchSettings


class FakeClock:
    """Deterministic clock: sleep() advances time instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fetch_settings() -> FetchSettings:
    return FetchSettings(min_delay_seconds=2.0, max_retries=2, backoff_base_seconds=1.0)


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    # Tests must never reach the real API, whatever the developer's shell has set.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ENRICH_MODEL", raising=False)
    monkeypatch.delenv("MOCK_LLM", raising=False)
