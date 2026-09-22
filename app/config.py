"""Environment-driven settings, tuned for a 0.1 vCPU / 512 MiB container."""

from __future__ import annotations

import os


TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def _raw(name: str) -> str:
    """The value of `name`, or "" when unset or blank.

    A deployment dashboard makes it easy to add a key and leave the value
    empty. That means "I did not set this", so it has to fall back to the
    default rather than being parsed — an empty string is not "off".
    """
    return (os.environ.get(name) or "").strip()


def env_int(name: str, default: int) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"{name}={raw!r} is not a number; using {default}", flush=True)
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    print(f"{name}={raw!r} is not a boolean; using {default}", flush=True)
    return default


def env_str(name: str, default: str) -> str:
    return _raw(name) or default


APP_VERSION = "1.5.1"

MAX_FILE_BYTES = env_int("MAX_FILE_BYTES", 256 * 1024 * 1024)
DOWNLOAD_TIMEOUT_SECONDS = env_int("DOWNLOAD_TIMEOUT_SECONDS", 900)
INFO_TIMEOUT_SECONDS = env_int("INFO_TIMEOUT_SECONDS", 60)

# Warm yt-dlp worker processes. Sized so 20 requests can be in flight at once:
# WORKER_COUNT * WORKER_THREADS is the ceiling on simultaneous yt-dlp work.
WORKER_COUNT = env_int("WORKER_COUNT", 2)
WORKER_THREADS = env_int("WORKER_THREADS", 12)
WORKER_STARTUP_TIMEOUT_SECONDS = env_int("WORKER_STARTUP_TIMEOUT_SECONDS", 120)
WORKER_MEMORY_LIMIT_MB = env_int("WORKER_MEMORY_LIMIT_MB", 0)
USE_WORKER_POOL = env_bool("USE_WORKER_POOL", True)

# Downloads write to a small disk, so they stay far below the request ceiling;
# the queue holds the rest rather than rejecting them.
DOWNLOAD_CONCURRENCY = env_int("DOWNLOAD_CONCURRENCY", 3)
DOWNLOAD_QUEUE_SIZE = env_int("DOWNLOAD_QUEUE_SIZE", 32)
DOWNLOAD_QUEUE_WAIT_SECONDS = env_int("DOWNLOAD_QUEUE_WAIT_SECONDS", 180)

# Metadata lookups are network-bound and hold no disk, so they carry the concurrency.
INFO_CONCURRENCY = env_int("INFO_CONCURRENCY", 20)
INFO_QUEUE_SIZE = env_int("INFO_QUEUE_SIZE", 80)
INFO_QUEUE_WAIT_SECONDS = env_int("INFO_QUEUE_WAIT_SECONDS", 45)

INFO_CACHE_TTL_SECONDS = env_int("INFO_CACHE_TTL_SECONDS", 300)
INFO_CACHE_SIZE = env_int("INFO_CACHE_SIZE", 512)

# A failed lookup is cached briefly so a client retrying a dead link in a loop
# cannot spend the whole CPU budget re-extracting it.
NEGATIVE_CACHE_TTL_SECONDS = env_int("NEGATIVE_CACHE_TTL_SECONDS", 30)

# DNS answers for the public-IP check, so a burst against one host resolves once.
HOSTNAME_CACHE_TTL_SECONDS = env_int("HOSTNAME_CACHE_TTL_SECONDS", 60)
HOSTNAME_CACHE_SIZE = env_int("HOSTNAME_CACHE_SIZE", 512)

# Refuse a download unless the temp filesystem can still hold it plus headroom.
MIN_FREE_DISK_BYTES = env_int("MIN_FREE_DISK_BYTES", 64 * 1024 * 1024)

# Parallel fragment fetches cost bandwidth, not CPU, so they are the cheap speed win.
CONCURRENT_FRAGMENTS = env_int("CONCURRENT_FRAGMENTS", 4)
SOCKET_TIMEOUT_SECONDS = env_int("SOCKET_TIMEOUT_SECONDS", 15)
YTDLP_RETRIES = env_int("YTDLP_RETRIES", 3)

# Merging separate video+audio streams means an ffmpeg pass; off by default here.
ALLOW_REMUX = env_bool("ALLOW_REMUX", False)
MP3_AUDIO_QUALITY = env_str("MP3_AUDIO_QUALITY", "5")
YTDLP_EXTRACTOR_ARGS = env_str("YTDLP_EXTRACTOR_ARGS", "")
YTDLP_CACHE_DIR = env_str("YTDLP_CACHE_DIR", "/tmp/yt-dlp-cache")

# Credentials for sites that refuse anonymous extraction (Vimeo's web client, for
# one). Both are paths mounted into the container; neither is set by default.
# Retry an equivalent URL when a site refuses anonymous access (see app/fallbacks.py).
URL_FALLBACKS = env_bool("URL_FALLBACKS", True)

YTDLP_COOKIES_FILE = env_str("YTDLP_COOKIES_FILE", "")
YTDLP_NETRC_FILE = env_str("YTDLP_NETRC_FILE", "")
# yt-dlp rewrites the cookie jar on close, so it is copied somewhere writable.
COOKIES_WORKING_COPY = env_str("COOKIES_WORKING_COPY", "/tmp/yt-dlp-cookies.txt")
