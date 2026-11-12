# Docker usage

The wheel installs cleanly on a slim base image. Playwright's Chromium needs
its system libraries, so use the upstream Playwright image or install the
dependencies explicitly.

## Build

```dockerfile
# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.64.1-jammy

WORKDIR /app

# Install the library only; the base image already has Chromium + OS deps.
COPY dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl

# Never bake an API key into the image.
ENV JEV_MODEL=jev-latest

ENTRYPOINT ["jev-browser"]
```

```bash
python -m build
docker build -t jev-browser-py .
```

## Run

```bash
docker run --rm \
  -e TYPESAFE_API_KEY \
  -v "$PWD/.artifacts:/app/.artifacts" \
  jev-browser-py \
  "confirm the page title mentions Example Domain" \
  https://example.com/ \
  --allow-origin https://example.com \
  --max-steps 6 --max-seconds 60 \
  --out /app/.artifacts --json
```

## Isolation notes

The container is the isolation boundary this library assumes. Inside one
container, a run can reach anything the container can reach, because the
Python network guard is not an egress firewall. For untrusted goals:

- run with `--network none` and a proxy that enforces your allow-list, or
- use a dedicated Docker network with a filtering gateway, or
- keep `--allow-origin` narrow and treat the container as the real boundary.

Do not publish a long-running service that accepts arbitrary goals and URLs
without an external authorization layer; that is the aiagent-md Gateway /
Sentinel's job, not this package's.

## Health check for CI

```bash
docker run --rm jev-browser-py --help
```

`--help` exits without touching the network, so it is a safe build-time smoke.
