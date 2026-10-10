"""Extraction schema (sent to the model) and validation of what comes back.

The model's answer is never trusted as-is: each field is validated on its own, so one bad field
is flagged and blanked without throwing away the rest. Contact channels must also be
verifiable in the fetched pages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .parser import (
    EMAIL_RE,
    is_generic_email,
    normalize_domain,
    redact_personal_emails,
    same_site,
)

# Bump when validation rules change, so cached validated results are not reused.
VALIDATION_VERSION = "1"

INDUSTRIES = (
    "food_and_drink",
    "hospitality",
    "retail",
    "construction_and_trades",
    "health_and_care",
    "beauty_and_wellness",
    "fitness_and_sport",
    "professional_services",
    "accounting_and_finance",
    "creative_and_media",
    "software_and_it",
    "manufacturing",
    "logistics_and_transport",
    "education",
    "nonprofit",
    "other",
    "unknown",
)
SIZE_SIGNALS = ("solo", "2-10", "11-50", "51-200", "201+", "unknown")
FIELD_NAMES = (
    "industry",
    "description",
    "size_signal",
    "location",
    "products_services",
    "contact_email",
    "contact_form_url",
)

_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_CONF = {"type": "number", "description": "0.0-1.0: how directly the pages state this."}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


EXTRACTION_JSON_SCHEMA: dict[str, Any] = _obj(
    {
        "industry": _obj(
            {
                "value": {"type": "string", "enum": list(INDUSTRIES)},
                "detail": _NULLABLE_STR,
                "confidence": _CONF,
            }
        ),
        "description": _obj({"value": _NULLABLE_STR, "confidence": _CONF}),
        "size_signal": _obj(
            {
                "value": {"type": "string", "enum": list(SIZE_SIGNALS)},
                "evidence": _NULLABLE_STR,
                "confidence": _CONF,
            }
        ),
        "location": _obj(
            {
                "city": _NULLABLE_STR,
                "region": _NULLABLE_STR,
                "country_code": _NULLABLE_STR,
                "confidence": _CONF,
            }
        ),
        "products_services": _obj(
            {"value": {"type": "array", "items": {"type": "string"}}, "confidence": _CONF}
        ),
        "contact_email": _obj({"value": _NULLABLE_STR, "confidence": _CONF}),
        "contact_form_url": _obj({"value": _NULLABLE_STR, "confidence": _CONF}),
    }
)


class FieldResult(BaseModel):
    value: Any = None
    confidence: float = 0.0
    note: str | None = None  # industry detail / size evidence
    flag: str | None = None  # why the value was rejected, if it was


class Enrichment(BaseModel):
    fields: dict[str, FieldResult]

    def get(self, name: str) -> Any:
        return self.fields[name].value

    def flags(self) -> list[str]:
        return [f"{n}:{f.flag}" for n, f in self.fields.items() if f.flag]


@dataclass
class Evidence:
    """What we actually saw on the company's pages, used to verify contact channels."""

    domain: str
    generic_emails: set[str] = field(default_factory=set)  # seen on HTTPS pages
    form_urls: set[str] = field(default_factory=set)  # HTTPS pages with a contact form
    # Seen only on plain-HTTP pages: could have been injected in transit, so never accepted.
    insecure_emails: set[str] = field(default_factory=set)
    insecure_form_urls: set[str] = field(default_factory=set)

    def fingerprint(self) -> list:
        """Everything validation reads from the evidence, in a stable JSON-able form."""
        return [
            normalize_domain(self.domain),
            sorted(self.generic_emails),
            sorted(self.form_urls),
            sorted(self.insecure_emails),
            sorted(self.insecure_form_urls),
        ]


class ExtractionError(ValueError):
    """The model's output could not be used at all."""


COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
MAX_DESCRIPTION = 400
MAX_SHORT = 80
MAX_PRODUCTS = 8


def _confidence(raw: Any) -> tuple[float, str | None]:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 0.0, "invalid_confidence"
    if not 0.0 <= float(raw) <= 1.0:
        return 0.0, "invalid_confidence"
    return float(raw), None


def _text(raw: Any) -> str:
    """Whitespace-normalised model text with personal email addresses removed: an address that
    slipped past redaction and was repeated by the model must not reach the output or cache."""
    return redact_personal_emails(" ".join(str(raw).split()))[0]


def _short(raw: Any, limit: int = MAX_SHORT) -> str | None:
    if not isinstance(raw, str):
        return None
    return _text(raw)[:limit] or None


def _ascii_domain(domain: str) -> str:
    domain = domain.strip().lower().rstrip(".")
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return domain


def on_company_domain(email: str, company_domain: str) -> bool:
    """True if the address is at the company's domain or one of its subdomains."""
    domain = _ascii_domain(email.rsplit("@", 1)[-1])
    company = _ascii_domain(normalize_domain(company_domain))
    return bool(company) and (domain == company or domain.endswith("." + company))


def _check_field(name: str, raw: Any, evidence: Evidence) -> FieldResult:
    """Validate one field's value. Returns a FieldResult with value or flag set."""
    if not isinstance(raw, dict):
        return FieldResult(flag="invalid_shape")
    conf, conf_flag = _confidence(raw.get("confidence"))
    result = FieldResult(confidence=conf)
    value = raw.get("value")

    if name == "industry":
        result.note = _short(raw.get("detail"))
        if value not in INDUSTRIES:
            result.flag = "invalid_value"
        elif value != "unknown":
            result.value = value
    elif name == "description":
        if value is not None:
            text = _text(value)
            if len(text) > MAX_DESCRIPTION:
                result.flag = "too_long"
            elif text:
                result.value = text
    elif name == "size_signal":
        result.note = _short(raw.get("evidence"), 160)
        if value not in SIZE_SIGNALS:
            result.flag = "invalid_value"
        elif value != "unknown":
            result.value = value
    elif name == "location":
        city, region = _short(raw.get("city"), 60), _short(raw.get("region"), 60)
        country = raw.get("country_code")
        if isinstance(country, str) and country.strip():
            country = country.strip().upper()
            if not COUNTRY_RE.match(country):
                result.flag = "invalid_country_code"
                country = None
        else:
            country = None
        if any((city, region, country)):
            result.value = {"city": city, "region": region, "country_code": country}
    elif name == "products_services":
        if not isinstance(value, list):
            result.flag = "invalid_value"
        else:
            items = [s for s in (_short(v, 60) for v in value) if s]
            result.value = list(dict.fromkeys(items))[:MAX_PRODUCTS] or None
    elif name == "contact_email":
        if value:
            email = str(value).strip().lower().removeprefix("mailto:")
            if not EMAIL_RE.fullmatch(email):
                result.flag = "invalid_email"
            elif not is_generic_email(email):
                result.flag = "personal_email_rejected"
            elif not on_company_domain(email, evidence.domain):
                result.flag = "off_domain_email"
            elif email not in evidence.generic_emails:
                insecure = email in evidence.insecure_emails
                result.flag = "insecure_source" if insecure else "not_found_in_source"
            else:
                result.value = email
    elif name == "contact_form_url":
        if value:
            url = str(value).strip()
            if not same_site(url, evidence.domain):
                result.flag = "off_site_url"
            elif url.rstrip("/") not in {u.rstrip("/") for u in evidence.form_urls}:
                insecure = url.rstrip("/") in {u.rstrip("/") for u in evidence.insecure_form_urls}
                result.flag = "insecure_source" if insecure else "no_form_found_at_url"
            else:
                result.value = url

    if conf_flag and result.flag is None:
        result.flag = conf_flag
        result.value = None
    return result


def validate_extraction(raw: str | dict, evidence: Evidence, min_confidence: float) -> Enrichment:
    """Validate the model's JSON. Raises ExtractionError only if nothing is usable."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"model output is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ExtractionError("model output is not a JSON object")
    if not any(name in raw for name in FIELD_NAMES):
        raise ExtractionError("model output has none of the expected fields")

    evidence.domain = normalize_domain(evidence.domain)
    fields: dict[str, FieldResult] = {}
    for name in FIELD_NAMES:
        if name not in raw:
            fields[name] = FieldResult(flag="missing_from_output")
            continue
        result = _check_field(name, raw[name], evidence)
        if result.value is not None and result.flag is None and result.confidence < min_confidence:
            result.flag = f"low_confidence({result.confidence:.2f})"
            result.value = None  # flagged, not guessed
        fields[name] = result
    return Enrichment(fields=fields)
