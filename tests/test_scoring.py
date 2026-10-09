from pathlib import Path

import pytest
from pydantic import ValidationError

from lead_enrich.schema import FIELD_NAMES, Enrichment, FieldResult
from lead_enrich.scoring import ICP, load_icp, score_lead

ROOT = Path(__file__).resolve().parents[1]


def enrichment(**values) -> Enrichment:
    return Enrichment(
        fields={n: FieldResult(value=values.get(n), confidence=0.9) for n in FIELD_NAMES}
    )


@pytest.fixture
def icp() -> ICP:
    return load_icp(ROOT / "config" / "icp.yaml")


def test_ideal_lead_scores_tier_a_with_reasons(icp):
    e = enrichment(
        industry="construction_and_trades",
        size_signal="2-10",
        location={"city": "Bristol", "region": None, "country_code": "GB"},
        contact_email="info@x.example",
        description="Plumbers who send lots of quotes and invoices.",
    )
    result = score_lead(e, icp)
    assert result.tier == "A"
    assert result.score == 25 + 30 + 25 + 10 + 10
    assert "+30 size 2-10" in result.reasons
    assert "+25 in target area (Bristol)" in result.reasons
    assert any(r.startswith("+10 keywords") for r in result.reasons)


def test_negative_rules_apply(icp):
    e = enrichment(
        industry="accounting_and_finance",
        size_signal="201+",
        location={"city": "Leeds", "region": None, "country_code": "GB"},
    )
    result = score_lead(e, icp)
    assert result.score == 0  # clamped at 0
    assert "-20 industry accounting_and_finance" in result.reasons
    assert result.tier == "C"


def test_country_match_when_city_not_targeted(icp):
    e = enrichment(location={"city": "Leeds", "region": None, "country_code": "GB"})
    assert "+10 in target country (GB)" in score_lead(e, icp).reasons


def test_missing_core_fields_route_to_review(icp):
    e = enrichment(industry="retail")
    result = score_lead(e, icp)
    assert result.tier == "review"
    assert "+0 size unknown or flagged" in result.reasons


def test_keyword_points_are_capped(icp):
    e = enrichment(products_services=["invoicing", "wholesale", "catering", "deliveries"])
    assert any(r.startswith("+10 keywords") for r in score_lead(e, icp).reasons)


def test_unknown_industry_in_yaml_is_rejected():
    with pytest.raises(ValidationError):
        ICP.model_validate({"industry": {"points": {"crypto": 10}}})


def test_tiers_are_configurable():
    icp = ICP.model_validate(
        {"tiers": {"hot": 10, "cold": 0}, "min_known_core_fields": 0, "contact": {"points": 15}}
    )
    assert score_lead(enrichment(contact_email="info@a.example"), icp).tier == "hot"
