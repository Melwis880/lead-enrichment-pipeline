"""Transparent rule-based fit score. Every point comes with a reason."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .schema import INDUSTRIES, SIZE_SIGNALS, Enrichment


class IndustryRule(BaseModel):
    points: dict[str, int] = {}

    @field_validator("points")
    @classmethod
    def _known(cls, v: dict[str, int]) -> dict[str, int]:
        unknown = set(v) - set(INDUSTRIES)
        if unknown:
            raise ValueError(f"unknown industries in icp.yaml: {sorted(unknown)}")
        return v


class SizeRule(BaseModel):
    points: dict[str, int] = {}

    @field_validator("points")
    @classmethod
    def _known(cls, v: dict[str, int]) -> dict[str, int]:
        unknown = set(v) - set(SIZE_SIGNALS)
        if unknown:
            raise ValueError(f"unknown size buckets in icp.yaml: {sorted(unknown)}")
        return v


class LocationRule(BaseModel):
    target_cities: list[str] = []
    city_points: int = 0
    target_countries: list[str] = []
    country_points: int = 0


class ContactRule(BaseModel):
    points: int = 0


class KeywordRule(BaseModel):
    terms: list[str] = []
    points_each: int = 0
    max_points: int = 0


class ICP(BaseModel):
    name: str = "ICP"
    tiers: dict[str, int] = {"A": 60, "B": 35, "C": 0}
    min_known_core_fields: int = Field(default=2, ge=0, le=3)
    industry: IndustryRule = IndustryRule()
    size: SizeRule = SizeRule()
    location: LocationRule = LocationRule()
    contact: ContactRule = ContactRule()
    keywords: KeywordRule = KeywordRule()


class ScoreResult(BaseModel):
    score: int
    tier: str
    reasons: list[str]


def load_icp(path: str | Path) -> ICP:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ICP.model_validate(data)


def _fmt(points: int, text: str) -> str:
    return f"{points:+d} {text}"


def score_lead(e: Enrichment, icp: ICP) -> ScoreResult:
    total = 0
    reasons: list[str] = []
    known_core = 0

    industry = e.get("industry")
    if industry:
        known_core += 1
        pts = icp.industry.points.get(industry, 0)
        total += pts
        reasons.append(_fmt(pts, f"industry {industry}"))
    else:
        reasons.append("+0 industry unknown or flagged")

    size = e.get("size_signal")
    if size:
        known_core += 1
        pts = icp.size.points.get(size, 0)
        total += pts
        reasons.append(_fmt(pts, f"size {size}"))
    else:
        reasons.append("+0 size unknown or flagged")

    loc = e.get("location") or {}
    city, country = (loc.get("city") or ""), (loc.get("country_code") or "")
    if city or country:
        known_core += 1
    cities = {c.lower() for c in icp.location.target_cities}
    countries = {c.upper() for c in icp.location.target_countries}
    if city and city.lower() in cities:
        total += icp.location.city_points
        reasons.append(_fmt(icp.location.city_points, f"in target area ({city})"))
    elif country and country.upper() in countries:
        total += icp.location.country_points
        reasons.append(_fmt(icp.location.country_points, f"in target country ({country})"))
    elif city or country:
        reasons.append(f"+0 outside target area ({', '.join(x for x in (city, country) if x)})")
    else:
        reasons.append("+0 location unknown or flagged")

    if e.get("contact_email") or e.get("contact_form_url"):
        total += icp.contact.points
        reasons.append(_fmt(icp.contact.points, "verified business contact channel"))
    else:
        reasons.append("+0 no verified contact channel")

    haystack = " ".join(
        [e.get("description") or "", " ".join(e.get("products_services") or [])]
    ).lower()
    hits = [t for t in icp.keywords.terms if t.lower() in haystack]
    if hits and icp.keywords.points_each:
        pts = min(len(hits) * icp.keywords.points_each, icp.keywords.max_points)
        total += pts
        reasons.append(_fmt(pts, f"keywords: {', '.join(hits)}"))

    score = max(0, min(100, total))
    if known_core < icp.min_known_core_fields:
        tier = "review"
        reasons.append(f"tier=review: only {known_core} of 3 core fields known")
    else:
        tier = next(
            (
                name
                for name, floor in sorted(icp.tiers.items(), key=lambda kv: -kv[1])
                if score >= floor
            ),
            "C",
        )
    return ScoreResult(score=score, tier=tier, reasons=reasons)
