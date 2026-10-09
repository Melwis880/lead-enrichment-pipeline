from lead_enrich.parser import (
    REDACTED_EMAIL,
    build_llm_document,
    discover_pages,
    is_generic_email,
    normalize_domain,
    parse_html,
)

HOME = """
<html><head><title>Harbour  Bakery | Bristol</title>
<meta name="description" content="Sourdough and pastries since 2011.">
<style>.x{color:red}</style><script>var secret = "do not index";</script></head>
<body>
<nav><a href="/">Home</a> <a href="/about-us">About us</a> <a href="/our-team">Our team</a>
<a href="/contact">Contact</a> <a href="https://instagram.example/harbour">Instagram</a>
<a href="mailto:hello@harbourbakery.example?subject=Hi">Email us</a>
<a href="tel:+44117000000">Call</a></nav>
<main><h1>Fresh bread daily</h1><p>Questions? info@harbourbakery.example or ask our
founder at jane.doe@harbourbakery.example.</p></main>
</body></html>
"""


def test_visible_text_strips_scripts_and_styles():
    page = parse_html(HOME, "https://harbourbakery.example/")
    assert page.title == "Harbour Bakery | Bristol"
    assert page.meta_description == "Sourdough and pastries since 2011."
    assert "Fresh bread daily" in page.text
    assert "do not index" not in page.text and "color:red" not in page.text


def test_personal_emails_are_redacted_generic_kept():
    page = parse_html(HOME, "https://harbourbakery.example/")
    assert "jane.doe" not in page.text
    assert REDACTED_EMAIL in page.text
    assert page.personal_emails_removed == 1
    assert page.generic_emails == ["hello@harbourbakery.example", "info@harbourbakery.example"]


def test_generic_email_rules():
    assert is_generic_email("Info@Shop.example")
    assert is_generic_email("hello+web@shop.example")
    assert not is_generic_email("jane@shop.example")
    assert not is_generic_email("j.smith@shop.example")
    assert not is_generic_email("ceo@shop.example")


def test_discovers_about_and_contact_but_not_team_or_external():
    page = parse_html(HOME, "https://harbourbakery.example/")
    pages = discover_pages(page, "harbourbakery.example", max_pages=3)
    assert pages == [
        "https://harbourbakery.example/about-us",
        "https://harbourbakery.example/contact",
    ]


def test_discovery_respects_page_budget():
    page = parse_html(HOME, "https://harbourbakery.example/")
    assert discover_pages(page, "harbourbakery.example", max_pages=2) == [
        "https://harbourbakery.example/about-us"
    ]
    assert discover_pages(page, "harbourbakery.example", max_pages=1) == []


def test_impressum_counts_as_contact_page():
    html = '<a href="/impressum">Impressum</a><a href="/blog">Blog</a>'
    page = parse_html(html, "https://www.firma.example/")
    assert discover_pages(page, "firma.example", 3) == ["https://www.firma.example/impressum"]


def test_contact_form_detection():
    with_form = '<form action="/send"><input type="email" name="e"><textarea></textarea></form>'
    newsletter = '<form><input type="text" name="q"></form>'
    assert parse_html(with_form, "https://a.example/contact").has_contact_form
    assert not parse_html(newsletter, "https://a.example/").has_contact_form


def test_normalize_domain():
    assert normalize_domain("https://www.Foo.example/about") == "foo.example"
    assert normalize_domain("foo.example") == "foo.example"
    assert normalize_domain("  ") == ""


def test_llm_document_respects_budget():
    page = parse_html("<p>" + "word " * 5000 + "</p>", "https://a.example/")
    doc, truncated = build_llm_document([page, page], max_chars=2000)
    assert truncated
    assert len(doc) < 2600
    assert doc.count("<page ") == 2


def test_broken_html_does_not_crash():
    page = parse_html(
        "<html><body><p>Unclosed <b>tags <a href='/about'>About", "https://a.example/"
    )
    assert "Unclosed" in page.text
    assert page.links[0].url == "https://a.example/about"
