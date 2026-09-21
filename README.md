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

To extract metadata and direct video variants without downloading them through the API:

```sh
curl -X POST http://localhost:8080/info \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/media-page"}'
```

The response contains `title`, `thumbnail`, and `videoFormats`. Direct format URLs are temporary provider URLs; request `/info` again when a returned URL has expired.

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

For a low-risk test, set `MAX_FILE_BYTES` to `104857600` (100 MiB). Render free instances spin down after inactivity and have monthly bandwidth/instance-hour limits, so they are suitable for testing rather than a public downloader service.

## Public deployment notes

The container listens on port 8080. Put it behind a TLS reverse proxy (for example Caddy, Nginx, or a managed container service) and add rate limiting before exposing it publicly. This API returns the downloaded file in the response and has a two-GiB default file-size ceiling and a 15-minute timeout. Tune `MAX_FILE_BYTES`, `DOWNLOAD_TIMEOUT_SECONDS`, and the `tmpfs` size together for your host capacity.

The IP check prevents initial hostname resolution to local/private IP addresses. A production public service should additionally use egress filtering at the network layer, rate limits, request logging, and an authentication gate if it is not intended for unrestricted use.

Interactive API documentation is at `/docs`; liveness is at `/healthz`.
