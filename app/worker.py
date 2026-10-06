"""A long-lived yt-dlp worker process.

yt-dlp is imported once here and then reused, so a request never pays the
interpreter start plus the ~1000-module import that `yt-dlp` as a CLI costs.
The parent speaks newline-delimited JSON over stdin/stdout.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from app.config import env_int
from app.errors import classify


def _private_stdout():
    """Move the protocol onto a private fd and point fd 1 at stderr.

    yt-dlp and its extractors print to stdout; without this, one stray line
    would corrupt the protocol stream.
    """
    protocol = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return protocol


def _apply_memory_limit() -> None:
    megabytes = env_int("WORKER_MEMORY_LIMIT_MB", 0)
    if megabytes <= 0:
        return
    import resource

    limit = megabytes * 1024 * 1024
    with_soft = min(limit, resource.getrlimit(resource.RLIMIT_AS)[1] or limit)
    resource.setrlimit(resource.RLIMIT_AS, (with_soft, resource.getrlimit(resource.RLIMIT_AS)[1]))


class Worker:
    def __init__(self, threads: int) -> None:
        self._out = _private_stdout()
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="ytdlp")
        import yt_dlp  # imported once, at startup, never per request

        self._yt_dlp = yt_dlp

    def send(self, message: dict) -> None:
        line = json.dumps(message, separators=(",", ":"))
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _build(self, options: dict):
        base = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "ignoreconfig": True,
            "logger": _NullLogger(),
        }
        return self._yt_dlp.YoutubeDL({**base, **options})

    def handle(self, request: dict) -> None:
        request_id = request.get("id")
        try:
            if request.get("op") == "info":
                result = self.info(request["url"], request.get("options") or {})
            elif request.get("op") == "download":
                result = self.download(request["url"], request.get("options") or {})
            elif request.get("op") == "ping":
                result = {"pong": True}
            else:
                raise ValueError(f"unknown op {request.get('op')!r}")
            self.send({"id": request_id, "ok": True, "result": result})
        except BaseException as error:  # noqa: BLE001 - every failure must reach the parent
            message = _describe(error)
            self.send({
                "id": request_id,
                "ok": False,
                "error": message,
                "reason": classify(message, type(error).__name__),
            })

    def info(self, url: str, options: dict) -> dict:
        # A fresh YoutubeDL per request: the object is not meant to be shared
        # across threads, but building one is cheap once the import is warm.
        with self._build({**options, "skip_download": True}) as ydl:
            metadata = ydl.sanitize_info(ydl.extract_info(url, download=False))
        # Projected here, not in the parent: a full info dict can be megabytes,
        # and shipping that through a pipe would cost more CPU than the lookup.
        formats = [
            {
                "url": item["url"],
                "videoQuality": f"{item['height']}p"
                if item.get("height")
                else item.get("format_note", "unknown"),
            }
            for item in metadata.get("formats", [])
            # A video-only stream (acodec "none", e.g. Instagram/YouTube DASH) plays
            # silently. An unknown codec counts as present, as yt-dlp treats it: Instagram's
            # progressive files, the ones with sound, carry no codec fields at all.
            if item.get("url") and item.get("vcodec") != "none" and item.get("acodec") != "none"
        ]
        return {
            "title": metadata.get("title") or "untitled",
            "thumbnail": metadata.get("thumbnail"),
            "videoFormats": formats,
        }

    def download(self, url: str, options: dict) -> dict:
        with self._build(options) as ydl:
            metadata = ydl.extract_info(url, download=True)
            info = ydl.sanitize_info(metadata)
        requested = (info.get("requested_downloads") or [{}])[0]
        return {"filepath": requested.get("filepath"), "title": info.get("title")}

    def run(self) -> None:
        self.send({"id": None, "ok": True, "result": {"ready": True, "pid": os.getpid()}})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._pool.submit(self.handle, request)


class _NullLogger:
    """Quiet, except for the things worth seeing in container logs.

    yt-dlp reports a missing impersonation target as a warning and then carries
    on with a plain request; swallowing that hid why extraction degraded.
    """

    def debug(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...

    def warning(self, message: str) -> None:
        print(message, file=sys.stderr)

    def error(self, message: str) -> None:
        print(message, file=sys.stderr)


def _describe(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[-500:]


def main() -> None:
    _apply_memory_limit()
    threads = env_int("WORKER_THREADS", 8)
    Worker(threads=threads).run()


if __name__ == "__main__":
    main()
