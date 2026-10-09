import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from lead_enrich.cache import JsonDiskCache
from lead_enrich.config import LLMSettings
from lead_enrich.extractors import (
    SYSTEM_PROMPT,
    ClaudeExtractor,
    FatalExtractorError,
    MockExtractor,
)
from lead_enrich.schema import EXTRACTION_JSON_SCHEMA

PAYLOAD = {"industry": {"value": "retail", "detail": None, "confidence": 0.9}}
PAYLOAD_JSON = json.dumps(PAYLOAD)


def fake_response(text=PAYLOAD_JSON, stop_reason="end_turn", tokens=(1200, 300)):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def api_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


def test_request_uses_structured_output_and_model(tmp_path):
    client = FakeClient(fake_response())
    ex = ClaudeExtractor(LLMSettings(model="claude-haiku-4-5-20251001"), client=client)
    result = ex.extract("Shop", "shop.example", "<page>...</page>")
    call = client.calls[0]
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_config"]["format"]["schema"] == EXTRACTION_JSON_SCHEMA
    assert "shop.example" in call["messages"][0]["content"]
    assert result.data == PAYLOAD and result.error is None


def test_usage_and_cost_are_reported():
    ex = ClaudeExtractor(
        LLMSettings(input_price_per_mtok=1.0, output_price_per_mtok=5.0),
        client=FakeClient(fake_response(tokens=(2000, 400))),
    )
    result = ex.extract("Shop", "shop.example", "doc")
    assert (result.input_tokens, result.output_tokens) == (2000, 400)
    assert result.cost_usd == pytest.approx(0.002 + 0.002)


def test_cache_hit_skips_api_and_costs_nothing(tmp_path):
    cache = JsonDiskCache(tmp_path)
    first = FakeClient(fake_response())
    ClaudeExtractor(LLMSettings(), client=first, cache=cache).extract("S", "s.example", "doc")
    second = FakeClient(fake_response())
    result = ClaudeExtractor(LLMSettings(), client=second, cache=cache).extract(
        "S", "s.example", "doc"
    )
    assert result.from_cache and result.cost_usd == 0 and second.calls == []


def test_cache_key_changes_with_model(tmp_path):
    cache = JsonDiskCache(tmp_path)
    ClaudeExtractor(
        LLMSettings(model="a"), client=FakeClient(fake_response()), cache=cache
    ).extract("S", "s.example", "doc")
    other = FakeClient(fake_response())
    ClaudeExtractor(LLMSettings(model="b"), client=other, cache=cache).extract(
        "S", "s.example", "doc"
    )
    assert len(other.calls) == 1


@pytest.mark.parametrize(
    "stop_reason,expected", [("refusal", "refused"), ("max_tokens", "truncated")]
)
def test_bad_stop_reasons_are_errors(stop_reason, expected):
    ex = ClaudeExtractor(LLMSettings(), client=FakeClient(fake_response(stop_reason=stop_reason)))
    result = ex.extract("S", "s.example", "doc")
    assert result.data is None and expected in result.error
    assert result.input_tokens == 1200  # still billed, still counted


def test_invalid_json_is_an_error():
    ex = ClaudeExtractor(LLMSettings(), client=FakeClient(fake_response(text="{oops")))
    assert "not valid JSON" in ex.extract("S", "s.example", "doc").error


@pytest.mark.parametrize(
    "exc",
    [
        api_error(anthropic.AuthenticationError, 401),
        api_error(anthropic.PermissionDeniedError, 403),
        api_error(anthropic.NotFoundError, 404),
    ],
)
def test_misconfiguration_aborts_the_run(exc):
    ex = ClaudeExtractor(LLMSettings(), client=FakeClient(exc))
    with pytest.raises(FatalExtractorError):
        ex.extract("S", "s.example", "doc")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (api_error(anthropic.RateLimitError, 429), "rate limit"),
        (api_error(anthropic.InternalServerError, 500), "HTTP 500"),
        (api_error(anthropic.BadRequestError, 400), "bad request"),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            "connection",
        ),
    ],
)
def test_transient_errors_fail_only_this_company(exc, expected):
    ex = ClaudeExtractor(LLMSettings(), client=FakeClient(exc))
    assert expected in ex.extract("S", "s.example", "doc").error


def test_mock_extractor_reads_fixture(tmp_path):
    (tmp_path / "shop.example.json").write_text(json.dumps(PAYLOAD))
    ex = MockExtractor(tmp_path)
    assert ex.extract("Shop", "shop.example", "doc").data == PAYLOAD
    assert "no mock fixture" in ex.extract("X", "x.example", "doc").error


def test_missing_credentials_abort_with_clear_message():
    exc = TypeError("Could not resolve authentication method. Expected one of api_key ...")
    ex = ClaudeExtractor(LLMSettings(), client=FakeClient(exc))
    with pytest.raises(FatalExtractorError, match="ANTHROPIC_API_KEY"):
        ex.extract("S", "s.example", "doc")
