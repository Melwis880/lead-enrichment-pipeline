"""One canonical form for every URL the tool checks, compares or fetches."""

from __future__ import annotations

import requests


class InvalidURLError(ValueError):
    """The URL cannot be turned into a request."""


def canonical_url(url: str) -> str:
    """The exact URL requests will send for ``url``.

    The SSRF guard, the robots.txt origin, the same-site check and the request itself must all
    read the same host. Python's urlsplit and urllib3 disagree on inputs such as
    ``http://127.0.0.1\\@shop.example/`` (urlsplit: shop.example, urllib3: 127.0.0.1) or
    ``straße.example`` (IDNA 2003 vs UTS 46), so every URL is first converted to the form
    requests prepares, and only that form is used afterwards. Idempotent.
    """
    try:
        prepared = requests.Request("GET", url).prepare().url
    except (requests.RequestException, ValueError, UnicodeError) as exc:
        raise InvalidURLError("invalid URL") from exc
    if not prepared:
        raise InvalidURLError("invalid URL")
    return prepared
