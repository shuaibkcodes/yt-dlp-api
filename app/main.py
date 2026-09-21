"""A minimal HTTP wrapper for yt-dlp suitable for container deployment."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
import json
from contextlib import suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "1.0.0"
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(2 * 1024 * 1024 * 1024)))
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "900"))

app = FastAPI(title="yt-dlp download API", version=APP_VERSION, docs_url="/docs")


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


def is_public_hostname(hostname: str) -> bool:
    """Reject loopback/private destinations so the endpoint cannot fetch local services."""
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return False
    return bool(addresses)


def build_command(request: DownloadRequest, output_template: str) -> list[str]:
    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-progress",
        "--restrict-filenames",
        "--max-filesize",
        str(MAX_FILE_BYTES),
        "--output",
        output_template,
    ]
    if request.format == "mp4":
        command += ["--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", "--merge-output-format", "mp4"]
    elif request.format == "mp3":
        command += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        command += ["--format", "best"]
    return [*command, "--", request.url]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


@app.post("/info")
async def info(request: MediaInfoRequest) -> dict[str, object]:
    """Return media metadata and downloadable video variants without downloading them."""
    hostname = urlparse(request.url).hostname
    if hostname is None or not is_public_hostname(hostname):
        raise HTTPException(status_code=422, detail="url must resolve only to public IP addresses")

    process = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--skip-download",
        "--dump-single-json",
        "--",
        request.url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.communicate()
        raise HTTPException(status_code=504, detail="media lookup timed out")

    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(status_code=422, detail=message[-500:] or "yt-dlp could not inspect this URL")

    try:
        metadata = json.loads(stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="yt-dlp returned invalid media metadata")

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


@app.post("/download")
async def download(request: DownloadRequest) -> FileResponse:
    hostname = urlparse(request.url).hostname
    if hostname is None or not is_public_hostname(hostname):
        raise HTTPException(status_code=422, detail="url must resolve only to public IP addresses")

    temp_dir = Path(tempfile.mkdtemp(prefix="yt-dlp-"))
    output_template = str(temp_dir / "download.%(ext)s")
    try:
        process = await asyncio.create_subprocess_exec(
            *build_command(request, output_template),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.communicate()
        _delete_directory(temp_dir)
        raise HTTPException(status_code=504, detail="download timed out")
    except Exception:
        _delete_directory(temp_dir)
        raise

    files = list(temp_dir.glob("download.*"))
    if process.returncode != 0 or len(files) != 1:
        _delete_directory(temp_dir)
        message = stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(status_code=422, detail=message[-500:] or "yt-dlp could not download this URL")

    file_path = files[0]
    return FileResponse(
        file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
        background=_CleanupDirectory(temp_dir),
    )


def _delete_directory(directory: Path) -> None:
    for item in directory.glob("*"):
        with suppress(FileNotFoundError):
            item.unlink()
    with suppress(FileNotFoundError):
        directory.rmdir()


class _CleanupDirectory:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def __call__(self) -> None:
        _delete_directory(self.directory)
