"""A minimal HTTP wrapper for yt-dlp suitable for container deployment."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import socket
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app import config
from app.cache import TTLCache
from app.errors import classify, status_for
from app.fallbacks import alternative_urls
from app.pool import PoolUnavailable, WorkerError, WorkerPool
from app.queue import QueueFull, QueueTimeout, TaskQueue
from app.urls import cache_key

APP_VERSION = config.APP_VERSION
MAX_FILE_BYTES = config.MAX_FILE_BYTES
DOWNLOAD_TIMEOUT_SECONDS = config.DOWNLOAD_TIMEOUT_SECONDS

download_queue = TaskQueue(
    "download",
    concurrency=config.DOWNLOAD_CONCURRENCY,
    capacity=config.DOWNLOAD_QUEUE_SIZE,
    wait_timeout=config.DOWNLOAD_QUEUE_WAIT_SECONDS,
)
info_queue = TaskQueue(
    "info",
    concurrency=config.INFO_CONCURRENCY,
    capacity=config.INFO_QUEUE_SIZE,
    wait_timeout=config.INFO_QUEUE_WAIT_SECONDS,
)
info_cache = TTLCache(config.INFO_CACHE_TTL_SECONDS, config.INFO_CACHE_SIZE)
worker_pool = WorkerPool(
    size=config.WORKER_COUNT,
    threads=config.WORKER_THREADS,
    startup_timeout=config.WORKER_STARTUP_TIMEOUT_SECONDS,
    env={"WORKER_MEMORY_LIMIT_MB": str(config.WORKER_MEMORY_LIMIT_MB)},
)


_cookies_path: str = ""


def _prepare_cookies() -> str:
    """Copy the mounted cookie jar somewhere writable.

    yt-dlp saves the jar back when it closes, which fails on a read-only mount
    and would turn every authenticated request into an error.
    """
    source = config.YTDLP_COOKIES_FILE
    if not source:
        return ""
    origin = Path(source)
    if not origin.is_file():
        print(f"YTDLP_COOKIES_FILE={source} does not exist; continuing without cookies", flush=True)
        return ""
    target = Path(config.COOKIES_WORKING_COPY)
    try:
        shutil.copyfile(origin, target)
        target.chmod(0o600)
    except OSError as error:
        print(f"could not stage cookies at {target}: {error}; continuing without", flush=True)
        return ""
    return str(target)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _cookies_path
    # Only this process makes these directories, so anything here outlived a previous
    # process and would otherwise sit on a small disk forever.
    for stale in Path(tempfile.gettempdir()).glob("yt-dlp-*"):
        if stale.is_dir():
            _delete_directory(stale)
    _cookies_path = _prepare_cookies()
    if config.USE_WORKER_POOL:
        # Import yt-dlp in every worker now, so no request ever pays for it.
        await worker_pool.start()
    try:
        yield
    finally:
        if config.USE_WORKER_POOL:
            await worker_pool.stop()


app = FastAPI(title="yt-dlp download API", version=APP_VERSION, docs_url="/docs", lifespan=lifespan)


class DownloadRequest(BaseModel):
    url: str = Field(description="Public http(s) URL of the media page or file")
    format: Literal["best", "mp4", "mp3"] = "best"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must be an absolute http(s) URL")
        return value


class MediaInfoRequest(BaseModel):
    url: str = Field(description="Public http(s) URL of the media page")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return DownloadRequest.validate_url(value)


_hostname_cache = TTLCache(config.HOSTNAME_CACHE_TTL_SECONDS, config.HOSTNAME_CACHE_SIZE)


async def is_public_hostname(hostname: str) -> bool:
    """Reject loopback/private destinations so the endpoint cannot fetch local services."""
    loop = asyncio.get_running_loop()
    try:
        # The resolver runs off the event loop; a blocking lookup would stall every request.
        addresses = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return False
    return bool(addresses)


async def require_public_url(url: str) -> None:
    hostname = urlparse(url).hostname
    if hostname is None:
        raise media_error("url must resolve only to public IP addresses", "blocked_url")
    # Repeated hits on one host skip the resolver entirely.
    verdict = await _hostname_cache.get_or_create(hostname, lambda: _verdict(hostname))
    if not verdict["public"]:
        raise media_error("url must resolve only to public IP addresses", "blocked_url")


async def _verdict(hostname: str) -> dict[str, bool]:
    return {"public": await is_public_hostname(hostname)}


CREDENTIAL_HINT = (
    "this source requires an account; mount a cookie jar and set YTDLP_COOKIES_FILE"
)


def media_error(message: str, reason: str | None = None) -> HTTPException:
    """Turn a yt-dlp failure into a status the caller can branch on."""
    reason = reason or classify(message)
    detail: dict[str, object] = {"reason": reason, "message": message}
    if reason in {"authentication_required", "video_password_required"}:
        detail["hint"] = CREDENTIAL_HINT
    return HTTPException(status_code=status_for(reason), detail=detail)


def queue_error(queue: TaskQueue, error: Exception) -> HTTPException:
    """Every error body has the same shape: a stable reason plus readable text."""
    if isinstance(error, QueueFull):
        if error.reason == "slow":
            reason = "queue_saturated"
            message = (
                f"{queue.name} is saturated: {error.queued} ahead of you, about "
                f"{error.retry_after}s to start, over the {queue.wait_timeout}s limit"
            )
        else:
            reason = "queue_full"
            message = f"{queue.name} queue is full ({error.queued} waiting); retry shortly"
        retry_after = error.retry_after
    else:
        assert isinstance(error, QueueTimeout)
        reason = "queue_timeout"
        message = f"waited {error.waited}s for a {queue.name} slot without starting; retry shortly"
        retry_after = max(1, queue.wait_timeout // 2)
    return HTTPException(
        status_code=503,
        detail={"reason": reason, "message": message, "queue": queue.name},
        headers={"Retry-After": str(retry_after)},
    )


def parse_extractor_args(raw: str) -> dict[str, dict[str, list[str]]]:
    """Turn `--extractor-args`-style text into the mapping the library expects."""
    parsed: dict[str, dict[str, list[str]]] = {}
    for chunk in filter(None, (part.strip() for part in raw.split(";"))):
        extractor, _, arguments = chunk.partition(":")
        if not arguments:
            continue
        options = parsed.setdefault(extractor.strip(), {})
        for pair in filter(None, (item.strip() for item in arguments.split(","))):
            key, _, value = pair.partition("=")
            options.setdefault(key.strip(), []).extend(v for v in value.split("+") if v)
    return parsed


def base_options() -> dict[str, object]:
    options: dict[str, object] = {
        "socket_timeout": config.SOCKET_TIMEOUT_SECONDS,
        "retries": config.YTDLP_RETRIES,
        "cachedir": config.YTDLP_CACHE_DIR,
    }
    if config.YTDLP_EXTRACTOR_ARGS:
        options["extractor_args"] = parse_extractor_args(config.YTDLP_EXTRACTOR_ARGS)
    if _cookies_path:
        options["cookiefile"] = _cookies_path
    if config.YTDLP_NETRC_FILE:
        options["usenetrc"] = True
        options["netrc_location"] = config.YTDLP_NETRC_FILE
    return options


def format_selector(wanted: str) -> str:
    """Prefer already-muxed streams: merging costs an ffmpeg pass this CPU cannot spare."""
    if wanted == "mp4":
        selector = "best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none][vcodec!=none]"
        if config.ALLOW_REMUX:
            selector = f"{selector}/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
        return selector
    if wanted == "mp3":
        return "bestaudio[ext=m4a]/bestaudio/best"
    return "best[acodec!=none][vcodec!=none]/best"


def download_options(request: DownloadRequest, output_template: str) -> dict[str, object]:
    options: dict[str, object] = {
        **base_options(),
        "outtmpl": output_template,
        "restrictfilenames": True,
        "max_filesize": MAX_FILE_BYTES,
        "fragment_retries": config.YTDLP_RETRIES,
        "concurrent_fragment_downloads": config.CONCURRENT_FRAGMENTS,
        "format": format_selector(request.format),
    }
    if request.format == "mp4":
        options["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    elif request.format == "mp3":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": config.MP3_AUDIO_QUALITY,
            }
        ]
    return options


def build_command(request: DownloadRequest, output_template: str) -> list[str]:
    """The CLI equivalent, used when USE_WORKER_POOL is off."""
    command = [
        "yt-dlp",
        *_common_flags(),
        "--restrict-filenames",
        "--max-filesize",
        str(MAX_FILE_BYTES),
        "--fragment-retries",
        str(config.YTDLP_RETRIES),
        "--concurrent-fragments",
        str(config.CONCURRENT_FRAGMENTS),
        "--output",
        output_template,
        "--format",
        format_selector(request.format),
    ]
    if request.format == "mp4":
        command += ["--remux-video", "mp4"]
    elif request.format == "mp3":
        command += [
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            config.MP3_AUDIO_QUALITY,
        ]
    return [*command, "--", request.url]


def _common_flags() -> list[str]:
    flags = [
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--ignore-config",
        "--socket-timeout",
        str(config.SOCKET_TIMEOUT_SECONDS),
        "--retries",
        str(config.YTDLP_RETRIES),
        "--cache-dir",
        config.YTDLP_CACHE_DIR,
    ]
    if config.YTDLP_EXTRACTOR_ARGS:
        flags += ["--extractor-args", config.YTDLP_EXTRACTOR_ARGS]
    return flags


async def run_yt_dlp(command: list[str], timeout: int, capture_stdout: bool) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.communicate()
        raise
    return process.returncode or 0, stdout or b"", stderr or b""


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "queues": {"download": download_queue.stats(), "info": info_queue.stats()},
        "workers": worker_pool.stats() if config.USE_WORKER_POOL else {"enabled": False},
        "infoCache": info_cache.stats(),
    }


async def _attempt(op: str, url: str, options: dict[str, object], timeout: int) -> object:
    """Run an extraction, retrying an equivalent URL if this one demands an account.

    The retry happens inside the caller's queue slot, so it never queues twice.
    """
    candidates = [url]
    if config.URL_FALLBACKS:
        candidates += alternative_urls(url)
    failure: WorkerError | None = None
    for candidate in candidates:
        try:
            return await worker_pool.submit(op, {"url": candidate, "options": options}, timeout)
        except WorkerError as error:
            if error.reason != "authentication_required":
                raise
            failure = error
    assert failure is not None
    raise failure


async def fetch_info(url: str) -> dict[str, object]:
    try:
        async with info_queue.slot():
            if config.USE_WORKER_POOL:
                result = await _attempt(
                    "info", url, base_options(), config.INFO_TIMEOUT_SECONDS
                )
                return result  # type: ignore[return-value]
            return await _fetch_info_via_cli(url)
    except (QueueFull, QueueTimeout) as error:
        raise queue_error(info_queue, error) from None
    except WorkerError as error:
        raise media_error(str(error), error.reason) from None
    except PoolUnavailable:
        raise HTTPException(
            status_code=503,
            detail={"reason": "workers_restarting", "message": "yt-dlp workers are restarting"},
            headers={"Retry-After": "5"},
        ) from None
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504, detail={"reason": "timeout", "message": "media lookup timed out"}
        ) from None


async def _fetch_info_via_cli(url: str) -> dict[str, object]:
    command = ["yt-dlp", *_common_flags(), "--skip-download", "--dump-single-json", "--", url]
    returncode, stdout, stderr = await run_yt_dlp(command, config.INFO_TIMEOUT_SECONDS, capture_stdout=True)
    if returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise media_error(message or "yt-dlp could not inspect this URL")
    try:
        metadata = json.loads(stdout)
    except json.JSONDecodeError:
        raise media_error("yt-dlp returned invalid media metadata", "extraction_failed") from None

    formats = [
        {
            "url": item["url"],
            "videoQuality": f"{item['height']}p" if item.get("height") else item.get("format_note", "unknown"),
        }
        for item in metadata.get("formats", [])
        if item.get("url") and item.get("vcodec") not in {None, "none"}
    ]
    return {
        "title": metadata.get("title") or "untitled",
        "thumbnail": metadata.get("thumbnail"),
        "videoFormats": formats,
    }


# Outcomes that describe the media itself, and so are worth remembering briefly.
CACHEABLE_FAILURES = {403, 404, 413, 422, 451}


async def _lookup(url: str) -> dict[str, object]:
    """Wrap a lookup so a rejection can be cached too, briefly."""
    try:
        return {"ok": True, "value": await fetch_info(url)}
    except HTTPException as error:
        if error.status_code in CACHEABLE_FAILURES and config.NEGATIVE_CACHE_TTL_SECONDS > 0:
            return {"ok": False, "status": error.status_code, "detail": error.detail}
        raise


def _entry_ttl(entry: dict[str, object]) -> float | None:
    return None if entry.get("ok") else float(config.NEGATIVE_CACHE_TTL_SECONDS)


@app.post("/info")
async def info(request: MediaInfoRequest) -> dict[str, object]:
    """Return media metadata and downloadable video variants without downloading them."""
    await require_public_url(request.url)
    # Keyed on the canonical form, so the same video reached by different links —
    # a share link, a timestamp, a playlist position — is one extraction, and
    # simultaneous callers share it.
    key = cache_key(request.url)
    entry = await info_cache.get_or_create(key, lambda: _lookup(request.url), ttl_for=_entry_ttl)
    if not entry["ok"]:
        raise HTTPException(status_code=int(entry["status"]), detail=entry["detail"])
    return entry["value"]  # type: ignore[return-value]


@app.post("/download")
async def download(request: DownloadRequest) -> FileResponse:
    await require_public_url(request.url)
    try:
        async with download_queue.slot():
            return await run_download(request)
    except (QueueFull, QueueTimeout) as error:
        raise queue_error(download_queue, error) from None


async def run_download(request: DownloadRequest) -> FileResponse:
    temp_root = Path(tempfile.gettempdir())
    free_bytes = shutil.disk_usage(temp_root).free
    if free_bytes < MAX_FILE_BYTES + config.MIN_FREE_DISK_BYTES:
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "disk_pressure",
                "message": "not enough temporary disk space for a download right now",
            },
            headers={"Retry-After": "30"},
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="yt-dlp-"))
    output_template = str(temp_dir / "download.%(ext)s")
    try:
        if config.USE_WORKER_POOL:
            await _attempt(
                "download",
                request.url,
                download_options(request, output_template),
                DOWNLOAD_TIMEOUT_SECONDS,
            )
            stderr = b""
            returncode = 0
        else:
            returncode, _, stderr = await run_yt_dlp(
                build_command(request, output_template), DOWNLOAD_TIMEOUT_SECONDS, capture_stdout=False
            )
    except asyncio.TimeoutError:
        _delete_directory(temp_dir)
        raise HTTPException(
            status_code=504, detail={"reason": "timeout", "message": "download timed out"}
        ) from None
    except WorkerError as error:
        _delete_directory(temp_dir)
        raise media_error(str(error), error.reason) from None
    except PoolUnavailable:
        _delete_directory(temp_dir)
        raise HTTPException(
            status_code=503,
            detail={"reason": "workers_restarting", "message": "yt-dlp workers are restarting"},
            headers={"Retry-After": "5"},
        ) from None
    except Exception:
        _delete_directory(temp_dir)
        raise

    files = [item for item in temp_dir.glob("download.*") if not item.name.endswith(".part")]
    if returncode != 0 or len(files) != 1:
        _delete_directory(temp_dir)
        message = stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise media_error(message or "yt-dlp could not download this URL")

    file_path = files[0]
    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
        background=_CleanupDirectory(temp_dir),
    )


def _delete_directory(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


class _CleanupDirectory:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def __call__(self) -> None:
        _delete_directory(self.directory)
