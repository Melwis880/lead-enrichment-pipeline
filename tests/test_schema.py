import copy
import json

import pytest

from lead_enrich.schema import (
    EXTRACTION_JSON_SCHEMA,
    FIELD_NAMES,
    Evidence,
    ExtractionError,
    validate_extraction,
)

GOOD = {
    "industry": {"value": "food_and_drink", "detail": "Artisan bakery", "confidence": 0.95},
    "description": {"value": "Neighbourhood sourdough bakery.", "confidence": 0.9},
    "size_signal": {"value": "2-10", "evidence": "a team of six bakers", "confidence": 0.8},
    "location": {"city": "Bristol", "region": None, "country_code": "gb", "confidence": 0.9},
    "products_services": {"value": ["Sourdough", "Pastries", "Sourdough"], "confidence": 0.9},
    "contact_email": {"value": "hello@bakery.example", "confidence": 0.95},
    "contact_form_url": {"value": "https://bakery.example/contact", "confidence": 0.9},
}


def evidence() -> Evidence:
    return Evidence(
        domain="https://www.bakery.example",
        generic_emails={"hello@bakery.example"},
        form_urls={"https://bakery.example/contact"},
    )


def run(data, min_conf=0.6):
    return validate_extraction(data, evidence(), min_conf)


def mutate(path, value):
    data = copy.deepcopy(GOOD)
    field, key = path.split(".")
    data[field][key] = value
    return data


def test_schema_is_strict_for_structured_outputs():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)

    walk(EXTRACTION_JSON_SCHEMA)
    assert tuple(EXTRACTION_JSON_SCHEMA["properties"]) == FIELD_NAMES


def test_good_output_passes_without_flags():
    result = run(json.dumps(GOOD))
    assert result.flags() == []
    assert result.get("industry") == "food_and_drink"
    assert result.fields["industry"].note == "Artisan bakery"
    assert result.get("location")["country_code"] == "GB"
    assert result.get("products_services") == ["Sourdough", "Pastries"]
    assert result.get("contact_email") == "hello@bakery.example"


def test_low_confidence_is_flagged_and_blanked():
    result = run(mutate("industry.confidence", 0.3))
    assert result.get("industry") is None
    assert "industry:low_confidence(0.30)" in result.flags()
    assert result.get("description") is not None  # other fields survive


def test_threshold_is_configurable():
    assert run(mutate("industry.confidence", 0.3), min_conf=0.2).get("industry")


def test_unknown_values_are_blank_not_flagged():
    data = mutate("industry.value", "unknown")
    data["size_signal"]["value"] = "unknown"
    result = run(data)
    assert result.get("industry") is None and result.get("size_signal") is None
    assert result.flags() == []


@pytest.mark.parametrize(
    "path,value,flag",
    [
        ("industry.value", "space_tourism", "invalid_value"),
        ("size_signal.value", "huge", "invalid_value"),
        ("industry.confidence", 1.7, "invalid_confidence"),
        ("industry.confidence", "high", "invalid_confidence"),
        ("location.country_code", "United Kingdom", "invalid_country_code"),
        ("products_services.value", "bread", "invalid_value"),
        ("description.value", "x" * 500, "too_long"),
    ],
)
def test_invalid_values_are_flagged(path, value, flag):
    field = path.split(".")[0]
    result = run(mutate(path, value))
    assert f"{field}:{flag}" in result.flags()


def test_personal_email_is_rejected_even_if_in_output():
    result = run(mutate("contact_email.value", "jane.doe@bakery.example"))
    assert result.get("contact_email") is None
    assert "contact_email:personal_email_rejected" in result.flags()


def test_hallucinated_generic_email_is_rejected():
    result = run(mutate("contact_email.value", "info@bakery.example"))
    assert "contact_email:not_found_in_source" in result.flags()


def test_contact_form_must_be_on_site_and_verified():
    off_site = run(mutate("contact_form_url.value", "https://forms.other.example/x"))
    assert "contact_form_url:off_site_url" in off_site.flags()
    no_form = run(mutate("contact_form_url.value", "https://bakery.example/about"))
    assert "contact_form_url:no_form_found_at_url" in no_form.flags()


def test_malformed_field_does_not_sink_the_record():
    data = copy.deepcopy(GOOD)
    data["location"] = "Bristol"
    del data["description"]
    result = run(data)
    assert "location:invalid_shape" in result.flags()
    assert "description:missing_from_output" in result.flags()
    assert result.get("industry") == "food_and_drink"


@pytest.mark.parametrize("raw", ["not json {", "[1, 2]", json.dumps({"foo": 1})])
def test_unusable_output_raises(raw):
    with pytest.raises(ExtractionError):
        run(raw)


@pytest.mark.parametrize(
    "email,ok",
    [
        ("hello@bakery.example", True),
        ("info@mail.bakery.example", True),  # subdomain of the company domain
        ("info@otherbakery.example", False),  # look-alike, not a subdomain
        ("info@bakery.example.evil.example", False),
        ("info@gmail.example", False),
    ],
)
def test_contact_email_must_be_on_company_domain(email, ok):
    ev = evidence()
    ev.generic_emails.add(email)  # present on the page, so only the domain rule decides
    result = validate_extraction(mutate("contact_email.value", email), ev, 0.6)
    if ok:
        assert result.get("contact_email") == email
    else:
        assert result.get("contact_email") is None
        assert "contact_email:off_domain_email" in result.flags()


def test_internationalised_company_domain_matches_punycode_email():
    ev = Evidence(domain="müller.example", generic_emails={"info@xn--mller-kva.example"})
    data = mutate("contact_email.value", "info@xn--mller-kva.example")
    data["contact_form_url"]["value"] = None
    assert validate_extraction(data, ev, 0.6).get("contact_email") == "info@xn--mller-kva.example"


def test_personal_emails_are_removed_from_free_text_fields():
    text = "Write to jane.smith@shop.example or info@shop.example"
    raw = {
        "industry": {"value": "retail", "detail": "ask jane.smith@shop.example", "confidence": 0.9},
        "description": {"value": text, "confidence": 0.9},
        "products_services": {"value": ["Bread", "sam@shop.example"], "confidence": 0.9},
    }
    e = validate_extraction(raw, Evidence(domain="shop.example"), min_confidence=0.6)
    blob = json.dumps(e.model_dump())
    assert "jane.smith" not in blob and "sam@" not in blob
    assert "info@shop.example" in e.get("description")  # role mailboxes stay
    assert "[personal email removed]" in e.get("description")
