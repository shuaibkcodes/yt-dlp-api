"""Cache-key canonicalization.

Distinct URL strings very often name the same media: a share link, the same
link with a timestamp, the same video reached from a playlist. Keying the
cache on a canonical form turns those into one extraction. The original URL is
always what gets handed to yt-dlp; this only decides what counts as the same
request.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Parameters that never change which media is returned.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "igshid", "si", "feature", "ab_channel", "pp",
        "source", "spm", "ref", "ref_src", "_branch_match_id",
    }
)

# YouTube-only: playback position and playlist context. `--no-playlist` is always
# set, so playlist context cannot change what is downloaded.
YOUTUBE_PARAMS = frozenset({"t", "start", "end", "time_continue", "list", "index", "start_radio", "rv", "pbjreload"})

YOUTUBE_HOSTS = frozenset({"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"})
YOUTUBE_PATH_PREFIXES = ("/shorts/", "/embed/", "/v/", "/live/")


def cache_key(url: str) -> str:
    """A stable key for URLs that name the same media, or the URL itself if unsure."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.hostname:
        return url

    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path
    query = parse_qsl(parsed.query, keep_blank_values=True)
    drop = set(TRACKING_PARAMS)

    if host == "youtu.be" and len(path) > 1:
        # https://youtu.be/ID -> the canonical watch URL
        host, video_id, path = "youtube.com", path.lstrip("/").split("/")[0], "/watch"
        query.append(("v", video_id))
        drop |= YOUTUBE_PARAMS
    elif host in YOUTUBE_HOSTS:
        # m./music./nocookie all serve the same video for a given id.
        host = "youtube.com"
        for prefix in YOUTUBE_PATH_PREFIXES:
            if path.startswith(prefix):
                video_id = path[len(prefix) :].split("/")[0]
                path = "/watch"
                query.append(("v", video_id))
                break
        drop |= YOUTUBE_PARAMS

    kept = sorted((k, v) for k, v in query if k.lower() not in drop)
    # A trailing slash never distinguishes media.
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(kept), ""))
