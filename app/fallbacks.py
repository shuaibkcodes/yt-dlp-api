"""Alternative URL forms to retry when a site refuses anonymous extraction.

Some sites expose the same public media through more than one endpoint, where
one needs an account and the other does not. Vimeo is the case in hand: yt-dlp
reaches `vimeo.com/<id>` through its API, whose web client is marked as
requiring auth, while `player.vimeo.com/video/<id>` is read from the embed
player config and needs none.

This is not a way past access control. The player config enforces the same
privacy settings, so a private or non-embeddable video still fails — only with
its own, clearer message.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_VIMEO_PATH = re.compile(r"^/(?:video/)?(?P<id>\d+)(?:/(?P<hash>[\da-f]{6,32}))?/?$")


def alternative_urls(url: str) -> list[str]:
    """Other URLs naming the same media, best first. Empty when there is none."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return []
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "vimeo.com":
        match = _VIMEO_PATH.match(parsed.path)
        if not match:
            return []
        candidate = f"https://player.vimeo.com/video/{match['id']}"
        if match["hash"]:
            candidate = f"{candidate}?h={match['hash']}"
        return [candidate]

    return []
