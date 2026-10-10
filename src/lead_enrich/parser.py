"""HTML parsing: visible text, page discovery, business-contact detection, redaction."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .urls import InvalidURLError, canonical_url

# Unicode-aware: internationalised local parts and domains (jane@müller.de) are emails too.
_LABEL = r"[^\W_](?:[\w-]*[^\W_])?"
_TLD = r"(?:[^\W\d_]{2,}|xn--[\w-]+)"
EMAIL_RE = re.compile(rf"(?<![\w.%+-])[\w.%+-]+@(?:{_LABEL}\.)+{_TLD}(?![\w-])")

# Obfuscated forms: jane [at] shop [dot] example, jane(at)shop(dot)de, jane at shop dot com.
_BR_AT = r"\s*[\[({<]\s*(?:at|@)\s*[\])}>]\s*"
_BR_DOT = r"\s*(?:[\[({<]\s*(?:dot|\.)\s*[\])}>]|\.)\s*"
OBFUSCATED_EMAIL_RE = re.compile(
    rf"(?<![\w.+-])(?P<local>[\w.+-]+)"
    rf"(?:{_BR_AT}{_LABEL}(?:{_BR_DOT}{_LABEL})*{_BR_DOT}{_TLD}"  # bracketed "at"
    rf"|\s+at\s+{_LABEL}(?:\s+dot\s+{_LABEL})*\s+dot\s+{_TLD})"  # plain words: "dot" required
    rf"(?![\w-])",
    re.IGNORECASE,
)

# Role-based mailbox names. Anything else (jane@, j.smith@, ceo@...) is treated as personal
# data and never leaves this module unredacted.
GENERIC_LOCAL_PARTS = frozenset(
    """
    info hello contact contactus enquiries enquiry inquiries inquiry office sales support help
    admin mail post bookings booking reservations reception orders order shop store studio
    accounts billing service customerservice customercare general team kontakt hallo
    """.split()
)

ABOUT_HINTS = ("about", "about-us", "who-we-are", "our-story", "company", "ueber-uns")
CONTACT_HINTS = ("contact", "get-in-touch", "find-us", "visit", "impressum", "imprint")
# Pages that list individuals. We do not need them and do not fetch them.
EXCLUDED_HINTS = ("team", "staff", "people", "careers", "jobs", "login", "cart", "privacy")

REDACTED_EMAIL = "[personal email removed]"
# Bump when parsing or redaction changes, so cached parsed pages are not reused.
PARSER_VERSION = "4"  # 4: canonical (punycode, fixed-point) link URLs
STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "canvas")


@dataclass
class Link:
    url: str
    text: str


@dataclass
class ParsedPage:
    url: str
    title: str = ""
    meta_description: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    generic_emails: list[str] = field(default_factory=list)
    personal_emails_removed: int = 0
    has_contact_form: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ParsedPage:
        links = [Link(**lk) for lk in data.get("links", [])]
        return cls(**{**data, "links": links})


def normalize_domain(raw: str) -> str:
    """'https://www.Foo.example/about' -> 'foo.example' (lowercase host, no www)."""
    raw = raw.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlsplit(raw).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def site_key(raw: str) -> str:
    """normalize_domain in the ASCII (punycode) form requests connects to, for comparisons:
    the input CSV holds 'bäckerei-müller.example' while canonical links hold
    'xn--bckerei-mller-...'. Uses the same UTS 46 mapping as requests (not Python's IDNA 2003
    codec, which turns 'ß' into 'ss')."""
    host = normalize_domain(raw)
    if not host or host.isascii():
        return host
    try:
        return normalize_domain(canonical_url(f"http://{host}/"))
    except InvalidURLError:
        return host


def same_site(url: str, domain: str) -> bool:
    return site_key(url) == site_key(domain)


def is_generic_email(email: str) -> bool:
    """Role mailbox from the allowlist. Plus-addressing (sales+jane.smith@) can name a person,
    so any local part with "+" counts as personal."""
    local = email.split("@", 1)[0].lower()
    return "+" not in local and local in GENERIC_LOCAL_PARTS


def redact_personal_emails(text: str) -> tuple[str, int]:
    count = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal count
        local = match.groupdict().get("local") or match.group(0)
        if is_generic_email(local + "@"):
            return match.group(0)
        count += 1
        return REDACTED_EMAIL

    text = OBFUSCATED_EMAIL_RE.sub(_sub, text)
    return EMAIL_RE.sub(_sub, text), count


def _clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def parse_html(html: str, url: str) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")
    page = ParsedPage(url=url)

    if soup.title and soup.title.string:
        page.title = " ".join(soup.title.string.split())
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        page.meta_description = " ".join(str(meta["content"]).split())

    emails: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.lower().startswith("mailto:"):
            emails.append(href[7:].split("?", 1)[0].strip())
            continue
        if href.startswith(("#", "javascript:", "tel:")):
            continue
        try:
            # Canonical form, so the same-site check sees the host that would really be
            # contacted (http://10.0.0.1\@shop.example/ is 10.0.0.1, not shop.example).
            absolute = canonical_url(urljoin(url, href))
        except (InvalidURLError, ValueError):
            continue
        if urlsplit(absolute).scheme in ("http", "https"):
            page.links.append(Link(_clean_url(absolute), " ".join(a.get_text(" ").split())))

    page.has_contact_form = any(
        form.find("textarea") or form.find("input", attrs={"type": "email"})
        for form in soup.find_all("form")
    )

    for tag in soup(STRIP_TAGS):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    emails += EMAIL_RE.findall(text)

    page.text, page.personal_emails_removed = redact_personal_emails(text)
    page.title, _ = redact_personal_emails(page.title)
    page.meta_description, _ = redact_personal_emails(page.meta_description)
    seen: set[str] = set()
    for email in emails:
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        if is_generic_email(email):
            page.generic_emails.append(key)
    return page


def _link_score(link: Link, hints: tuple[str, ...]) -> int:
    path = urlsplit(link.url).path.lower().strip("/")
    label = link.text.lower()
    if any(h in path or h in label for h in EXCLUDED_HINTS):
        return 0
    score = 0
    for hint in hints:
        if path == hint or path.endswith("/" + hint):
            score = max(score, 3)
        elif hint in path:
            score = max(score, 2)
        elif hint.replace("-", " ") in label:
            score = max(score, 1)
    return score


def discover_pages(home: ParsedPage, domain: str, max_pages: int) -> list[str]:
    """Pick the best about page and the best contact page linked from the homepage."""
    picked: list[str] = []
    home_url = _clean_url(home.url)
    candidates = [lk for lk in home.links if same_site(lk.url, domain) and lk.url != home_url]
    for hints in (ABOUT_HINTS, CONTACT_HINTS):
        if len(picked) >= max_pages - 1:
            break
        scored = [(_link_score(lk, hints), lk.url) for lk in candidates if lk.url not in picked]
        scored = [s for s in scored if s[0] > 0]
        if scored:
            best = max(scored, key=lambda s: (s[0], -len(s[1])))
            picked.append(best[1])
    return picked


def no_tags(text: str) -> str:
    """Untrusted text must not open or close any block of the LLM message (<page>, the outer
    <pages>, or any wrapper added later), so every "<" is escaped, whatever follows it."""
    return text.replace("<", "&lt;")


def build_llm_document(pages: list[ParsedPage], max_chars: int) -> tuple[str, bool]:
    """Concatenate pages for the LLM, splitting the character budget evenly between pages."""
    if not pages:
        return "", False
    budget = max(max_chars // len(pages), 500)
    truncated = False
    parts = []
    for page in pages:
        body = page.text
        if len(body) > budget:
            body, truncated = body[:budget] + " [...]", True
        url = no_tags(page.url).replace('"', "%22")
        parts.append(
            f'<page url="{url}">\nTitle: {no_tags(page.title)}\n'
            f"Meta description: {no_tags(page.meta_description)}\n"
            f"Contact form on page: {'yes' if page.has_contact_form else 'no'}\n"
            f"Text: {no_tags(body)}\n</page>"
        )
    return "\n\n".join(parts), truncated
