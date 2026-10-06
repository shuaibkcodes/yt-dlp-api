"""Classify yt-dlp failures into stable reasons the caller can act on.

yt-dlp signals most problems as one ExtractorError with prose, so the text is
what there is to go on. The reason codes are stable even when the wording moves.
"""

from __future__ import annotations

# status code, matched against the lowercased message
REASON_STATUS = {
    "authentication_required": 403,
    "video_password_required": 403,
    "geo_restricted": 451,
    "unavailable": 404,
    "unsupported_url": 422,
    "rate_limited": 429,
    "too_large": 413,
    "extraction_failed": 422,
    "blocked_url": 422,
    "unresolvable_host": 422,
}

_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "authentication_required",
        (
            "only works when logged-in",
            "account credentials",
            "sign in to confirm",
            "log in",
            "login required",
            "requires authentication",
            "private video",
            "privacy settings",
            "cannot be played here",
            "members-only",
            "premieres in",
            "--cookies",
        ),
    ),
    ("video_password_required", ("protected by a password", "--video-password", "video password")),
    (
        "geo_restricted",
        (
            "not available from your location",
            "available in your country",
            "geo restricted",
            "geo-restricted",
            "blocked it in your country",
        ),
    ),
    ("rate_limited", ("http error 429", "too many requests", "rate-limit", "rate limit")),
    ("too_large", ("file is larger than max-filesize", "exceeds the maximum")),
    (
        "unavailable",
        (
            "video unavailable",
            "has been removed",
            "does not exist",
            "http error 404",
            "no longer available",
            "is not available",
            "deleted",
        ),
    ),
    ("unsupported_url", ("unsupported url", "no video formats found", "is not a valid url")),
)


def classify(message: str, exception_name: str = "") -> str:
    if exception_name == "GeoRestrictedError":
        return "geo_restricted"
    if exception_name == "UnsupportedError":
        return "unsupported_url"
    text = (message or "").lower()
    for reason, markers in _MARKERS:
        if any(marker in text for marker in markers):
            return reason
    return "extraction_failed"


def status_for(reason: str) -> int:
    return REASON_STATUS.get(reason, 422)
