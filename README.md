# yt-dlp Docker download API

A Docker image that exposes a small HTTP API around `yt-dlp`. Use it only for media you are authorized to download and in accordance with the source site's terms.

## Run locally

```sh
docker compose up --build
curl http://localhost:8080/healthz
curl -X POST http://localhost:8080/download \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/media-page","format":"mp4"}' \
  --output download.mp4
```

`format` may be `best` (default), `mp4`, or `mp3`. The service does not permit caller-provided yt-dlp flags, local files, private-network URLs, or playlists.

Errors return a stable `reason` code — see [Error responses](#error-responses).

To extract metadata and direct video variants without downloading them through the API:

```sh
curl -X POST http://localhost:8080/info \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/media-page"}'
```

The response contains `title`, `thumbnail`, and `videoFormats`. Direct format URLs are temporary provider URLs; request `/info` again when a returned URL has expired.

## Handling 20 requests at a time

The service is sized to keep 20 requests in flight on a 0.1 vCPU / 512 MiB instance.

**Warm worker pool.** Running `yt-dlp` as a CLI starts a fresh interpreter and imports roughly a thousand modules per request — seconds of CPU at a 0.1 vCPU share, paid before any network work begins. Instead, `WORKER_COUNT` (2) long-lived worker processes import yt-dlp once at startup and then serve requests over a pipe, `WORKER_THREADS` (12) at a time each. That is 24 concurrent slots with no per-request startup cost, from two resident interpreters rather than twenty.

Workers are supervised: an extractor that wedges past its timeout gets the whole process killed and respawned, because a stuck Python thread cannot be cancelled. In-flight requests on that worker return `503`, and the pool keeps serving from the others.

**Admission queues.** Requests are admitted through two FIFO queues so the pool is never oversubscribed:

| Queue | Runs at once | Waiting room | Wait limit |
| --- | --- | --- | --- |
| `/info` | `INFO_CONCURRENCY` (20) | `INFO_QUEUE_SIZE` (80) | `INFO_QUEUE_WAIT_SECONDS` (45) |
| `/download` | `DOWNLOAD_CONCURRENCY` (3) | `DOWNLOAD_QUEUE_SIZE` (32) | `DOWNLOAD_QUEUE_WAIT_SECONDS` (180) |

Twenty simultaneous `/info` requests all run; twenty simultaneous `/download` requests all complete, three at a time, with the rest waiting rather than being rejected. Past the waiting room, or past the wait limit, callers get `503` with `Retry-After` instead of the instance falling over. A download is also refused with `503` when `/tmp` cannot hold `MAX_FILE_BYTES` plus `MIN_FREE_DISK_BYTES`.

Downloads stay at 3 because each one holds a file on a small disk, not because of CPU. Raise it only together with the `tmpfs` size.

**What does not scale.** Throughput is still bounded by 0.1 vCPU. Twenty concurrent requests are accepted and served without the instance breaking, but they share one tenth of a core, so each is slower than it would be alone. If sustained throughput matters more than burst tolerance, the instance needs more CPU — no amount of queueing creates it.

### Memory budget

Roughly 70 MiB for the API process plus about 80 MiB per worker, so the default two workers leave most of the 512 MiB free for in-flight requests. `WORKER_COUNT` is the main memory dial. `WORKER_MEMORY_LIMIT_MB` (off by default) caps each worker's address space so a runaway extractor fails its own request instead of getting the container OOM-killed.

## When every URL is different

Caching and request coalescing do nothing for twenty distinct videos, so it is worth being clear about what carries the load in that case and what does not.

**Still works, because it is per-process rather than per-URL:**

- The warm worker pool. No request pays the interpreter start and yt-dlp import, whatever the URL. This is the largest saving and it is URL-independent.
- The DNS verdict cache, whenever the distinct URLs share a host — twenty different YouTube videos are one hostname and one resolution.
- yt-dlp's own on-disk cache (`XDG_CACHE_HOME=/tmp`), which holds player signature functions shared across all videos on a site. The first extraction after a restart warms it for the rest.

**Does nothing:** the `/info` result cache and coalescing. Expect zero hits.

**What actually binds, then.** Each distinct video needs a real extraction: several network round trips plus CPU to parse the response. At a 0.1 vCPU share, that CPU is the ceiling — if an extraction costs one second of CPU, the instance can finish roughly one every ten seconds no matter how the queue is arranged. Twenty distinct URLs is twenty extractions; they are served, but not quickly.

Three things make that behave well rather than badly:

1. **Canonical cache keys.** "Different URLs" in real traffic often means the same video reached different ways. `https://youtu.be/ID?si=x`, `…/watch?v=ID&t=90` and `…/shorts/ID` are one cache entry and one extraction. Only links that genuinely name different media pay full price.
2. **Measured, predictive backpressure.** Each queue tracks the mean service time it actually observes and estimates how long a new caller would wait. If that estimate exceeds the wait limit, the caller is refused *immediately* with a `Retry-After` built from the measurement, instead of holding a connection open for `INFO_QUEUE_WAIT_SECONDS` and being refused anyway. Under a 12-caller burst with 0.5s of work each and a 2s limit, nobody times out: the ones that cannot be served are told so in milliseconds.
3. **Negative caching.** A failing link is remembered for `NEGATIVE_CACHE_TTL_SECONDS` (30), so a client retrying a dead URL in a loop cannot spend the CPU budget re-extracting it.

**The remaining lever is CPU per extraction**, and it is site-specific. `YTDLP_EXTRACTOR_ARGS` is passed straight through to yt-dlp, and for YouTube choosing a player client that avoids fetching and parsing the watch page — and avoids running the JS interpreter for signature deciphering — is worth more than everything above combined for distinct URLs. The right value changes as sites change, so nothing is set by default; check the yt-dlp extractor documentation for the current one and measure `meanServiceMs` on `/healthz` before and after.

## Observing it

`GET /healthz` reports live depth:

```json
{
  "status": "ok",
  "version": "1.3.0",
  "queues": {
    "info": {
      "concurrency": 20, "capacity": 80, "running": 6, "waiting": 0,
      "meanServiceMs": 820, "estimatedWaitSeconds": 0.0,
      "completed": 431, "rejectedFull": 0, "rejectedSlow": 12, "timedOut": 0
    },
    "download": {"concurrency": 3, "capacity": 32, "running": 3, "waiting": 9, "meanServiceMs": 21400}
  },
  "workers": {"size": 2, "threadsPerWorker": 12, "alive": 2, "inflight": 9, "restarts": 0},
  "infoCache": {"entries": 12, "hits": 40, "misses": 14, "coalesced": 6}
}
```

`meanServiceMs` is the measured cost of one extraction and is the number to watch: multiply it by your request rate to see whether the CPU share can keep up. `rejectedSlow` counts callers turned away up front because the measurement said they could not be served in time — a healthy signal, unlike `timedOut`, which should stay at zero. Rising `waiting` means CPU is the limit; rising `restarts` means extractions are wedging. Low `hits` with high `misses` simply means callers are asking for different videos.

## Speed on a small instance

Most of the wall-clock cost on 0.1 vCPU is CPU, not bandwidth, so the defaults avoid spending it:

- **No per-request interpreter start.** The worker pool above, which is the largest single saving.
- **No ffmpeg merge by default.** `format=mp4` picks a stream that already carries video and audio, so no merge pass runs. Set `ALLOW_REMUX=1` to allow the `bestvideo+bestaudio` fallback when no pre-muxed stream exists; expect it to be slow here.
- **Small payloads across the pipe.** A full yt-dlp info dict can run to megabytes; workers project it down to `title`, `thumbnail` and `videoFormats` before sending, so the API process never parses the rest.
- **Cached metadata, keyed on the video rather than the link.** `/info` results are cached for `INFO_CACHE_TTL_SECONDS` (300), and simultaneous callers share one lookup. The key is a canonical form of the URL, so a share link, a link with a timestamp and a link from inside a playlist all count as the same video. Caching only helps when URLs repeat, though — see below.
- **Cached DNS.** The public-IP check caches its verdict per hostname for `HOSTNAME_CACHE_TTL_SECONDS` (60) and resolves off the event loop.
- **Parallel fragments.** `CONCURRENT_FRAGMENTS` (4) fetches fragmented streams in parallel, which costs bandwidth rather than CPU.
- **Cheaper mp3.** `MP3_AUDIO_QUALITY` defaults to `5` (VBR ~130 kbps) rather than `0`; mp3 requests always transcode and are the slowest path on this hardware.
- **Bounded stalls.** `SOCKET_TIMEOUT_SECONDS` (15) and `YTDLP_RETRIES` (3) stop a dead source from holding a slot for the full timeout.

Returned files stream from disk, so a client on a slow connection does not hold a download slot while it reads the response.

Set `USE_WORKER_POOL=0` to fall back to spawning the `yt-dlp` CLI per request. It is slower, but useful for isolating a problem to the pool.

### Settings for 0.1 vCPU / 512 MiB

Image defaults already target this size: `MAX_FILE_BYTES=268435456` (256 MiB), `WORKER_COUNT=2`, `WORKER_THREADS=12`, `DOWNLOAD_CONCURRENCY=3`, and a 1 GiB `/tmp`. Raise `WORKER_COUNT` only along with memory, `DOWNLOAD_CONCURRENCY` only along with the `tmpfs` size, and `INFO_CONCURRENCY` only up to `WORKER_COUNT * WORKER_THREADS`.

## Sites that require an account

Some extractors refuse anonymous access. Vimeo is the common one: yt-dlp's `web` client is marked as requiring auth, so an anonymous request returns

```json
{"detail": {"reason": "authentication_required",
            "message": "ERROR: [vimeo] ...: The web client only works when logged-in. ...",
            "hint": "this source requires an account; mount a cookie jar and set YTDLP_COOKIES_FILE"}}
```

No extractor argument fixes this. Vimeo's other client, `android`, does not require auth but is marked cache-only: it cannot obtain a token of its own and only works if one is already in yt-dlp's cache, which a fresh container does not have.

Two things address it before credentials are needed, and both are on by default:

**Equivalent-URL retry — this is what fixes Vimeo.** When a request comes back demanding an account, the service retries the same media through an endpoint that does not use the API: for Vimeo, `player.vimeo.com/video/<id>`, read from the embed player config. Measured against live Vimeo, `vimeo.com/68054365` fails with the login error and the retry then succeeds. The retry runs inside the queue slot already held, so it never queues twice, and only triggers on `authentication_required`. Set `URL_FALLBACKS=0` to disable it — doing so brings the login error back, which is how the effect above was isolated.

**Browser impersonation.** yt-dlp asks to impersonate a browser when fetching a Vimeo page but does not require it: without a backend it only warns and sends a plain request. The dependency is *not* in the `[default]` extra, so the image installs `yt-dlp[default,curl-cffi]`, giving ~38 impersonation targets instead of none. Measured on its own this did **not** fix Vimeo — with `URL_FALLBACKS=0` the login error persists — so it is kept for data-centre-IP and TLS-fingerprint blocking on other sites, not as the Vimeo fix. yt-dlp warnings now reach the container log, where they were previously swallowed.

This is not a way around access control: the player config enforces the same privacy settings, so a private or non-embeddable video still fails, with its own clearer message. Only genuinely public media starts working.

For media that really is behind an account, export a cookie jar in Netscape format from a browser logged into the account, then point the service at it:

```sh
COOKIES_FILE=/path/to/cookies.txt docker compose up -d
```

Or directly, mounting it read-only:

```sh
docker run -v /path/to/cookies.txt:/run/secrets/cookies.txt:ro \
  -e YTDLP_COOKIES_FILE=/run/secrets/cookies.txt your-image
```

The file is copied to a writable location at `0600` on startup, because yt-dlp saves the cookie jar back when it closes and would fail against a read-only mount. A missing or unreadable file is logged and the service starts without it. `YTDLP_NETRC_FILE` works the same way for `.netrc` credentials.

Two things to weigh before enabling it: every caller of the API then acts as that account, and session cookies expire, so a `403` with `reason: authentication_required` returning after a period of working means the jar needs refreshing.

## Error responses

Failures carry a stable `reason` alongside the yt-dlp text, so a caller can branch without parsing prose:

| `reason` | Status | Meaning |
| --- | --- | --- |
| `authentication_required` | 403 | The site needs an account; see above |
| `video_password_required` | 403 | The item is password-protected |
| `geo_restricted` | 451 | Blocked in the server's region |
| `unavailable` | 404 | Removed, private or never existed |
| `too_large` | 413 | Above `MAX_FILE_BYTES` |
| `rate_limited` | 429 | The source is throttling this server |
| `unsupported_url` | 422 | No extractor handles it |
| `extraction_failed` | 422 | Anything else |
| `blocked_url` | 422 | Not an absolute http(s) URL, or it resolves to a private address |
| `unresolvable_host` | 422 | The URL's host name does not resolve (typo, dead domain, or a DNS failure) |
| `queue_saturated` | 503 | Measured wait exceeds the limit; refused up front |
| `queue_full` | 503 | No waiting room left |
| `queue_timeout` | 503 | Waited for a slot and never started |
| `workers_restarting` | 503 | A worker was recycled mid-request |
| `disk_pressure` | 503 | `/tmp` cannot hold another download |
| `timeout` | 504 | Exceeded the lookup or download timeout |

Every error body has the same shape: `{"detail": {"reason": ..., "message": ...}}`, where `message` holds the original yt-dlp text. This replaces the previous plain-string `detail`, so a client that read `detail` as text needs `detail.message` now. Classification is by message matching, since yt-dlp reports most failures as one error type — an unrecognized message falls back to `extraction_failed` and is never misreported. These outcomes are negatively cached for `NEGATIVE_CACHE_TTL_SECONDS` (30).

## Publish to Docker Hub

Replace `YOUR_DOCKERHUB_USER` with your Docker Hub namespace:

```sh
docker build -t YOUR_DOCKERHUB_USER/yt-dlp-api:latest .
docker login
docker push YOUR_DOCKERHUB_USER/yt-dlp-api:latest
```

On a server, set `DOCKER_IMAGE=YOUR_DOCKERHUB_USER/yt-dlp-api:latest` in the deployment environment, then run:

```sh
docker compose pull
docker compose up -d
```

## Production: public HTTPS URL

Use `compose.production.yaml` with Caddy rather than exposing port 8080. Before starting it:

1. Point an `A` record such as `downloads.example.com` to the server's public IPv4 address.
2. Copy `Caddyfile.example` to `Caddyfile`, then replace `downloads.example.com` with that exact DNS name.
3. Set `DOCKER_IMAGE` to your Docker Hub image name in the shell or deployment environment.
4. Allow inbound TCP ports 80 and 443 in the provider firewall. Restrict SSH (22) to your own IP address.

```sh
docker compose -f compose.production.yaml up -d
```

Caddy obtains and renews the TLS certificate automatically once DNS is pointing to the server. The public endpoint is then `https://downloads.example.com/download`.

## Free Render test deployment

Render can run this as a free Web Service and assign an `https://<service>.onrender.com` URL. Push this project to a GitHub repository, then in Render choose **New > Web Service**, connect that repository, select the **Docker** runtime, and choose the **Free** plan. Set the health-check path to `/healthz`.

For a low-risk test, set `MAX_FILE_BYTES` to `104857600` (100 MiB). Render's free instance has a small CPU share, so keep `WORKER_COUNT=2` and `DOWNLOAD_CONCURRENCY` low. Render free instances spin down after inactivity and have monthly bandwidth/instance-hour limits, so they are suitable for testing rather than a public downloader service.

## Public deployment notes

The container listens on port 8080. Put it behind a TLS reverse proxy (for example Caddy, Nginx, or a managed container service) and add rate limiting before exposing it publicly. The built-in queues cap concurrent work but do not limit how often one caller may submit. This API returns the downloaded file in the response and has a 256-MiB default file-size ceiling and a 15-minute timeout. Tune `MAX_FILE_BYTES`, `DOWNLOAD_TIMEOUT_SECONDS`, and the `tmpfs` size together for your host capacity.

The IP check prevents initial hostname resolution to local/private IP addresses. A production public service should additionally use egress filtering at the network layer, rate limits, request logging, and an authentication gate if it is not intended for unrestricted use.

Interactive API documentation is at `/docs`; liveness is at `/healthz`.
