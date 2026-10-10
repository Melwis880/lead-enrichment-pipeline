"""One canonical form for every URL the tool checks, compares or fetches."""

from __future__ import annotations

import requests

MAX_PASSES = 4


class InvalidURLError(ValueError):
    """The URL cannot be turned into a request."""


def canonical_url(url: str) -> str:
    """The exact URL requests will send for ``url``.

    The SSRF guard, the robots.txt origin, the same-site check and the request itself must all
    read the same host. Python's urlsplit and urllib3 disagree on inputs such as
    ``http://127.0.0.1\\@shop.example/`` (urlsplit: shop.example, urllib3: 127.0.0.1) or
    ``straße.example`` (IDNA 2003 vs UTS 46), so every URL is first converted to the form
    requests prepares, and only that form is used afterwards.

    One pass is not always stable: urllib3 removes dot segments before requests decodes
    percent-escapes, so ``/x/%2e%2e/private`` prepares to ``/x/../private`` and, prepared again
    when the request is sent, to ``/private``. robots.txt would then be checked for one path and
    another one requested. So the URL is prepared until it stops changing; a URL that is still
    changing after a few passes is rejected. The result is a fixed point: preparing it again
    (as requests does when sending) gives the same URL.
    """
    current = url
    for _ in range(MAX_PASSES):
        try:
            prepared = requests.Request("GET", current).prepare().url
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            raise InvalidURLError("invalid URL") from exc
        if not prepared:
            raise InvalidURLError("invalid URL")
        if prepared == current:
            return prepared
        current = prepared
    raise InvalidURLError("unstable URL")
