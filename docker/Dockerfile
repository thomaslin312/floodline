# GDAL, PROJ and GEOS come from wheels, so a slim base is enough and there is no
# apt-get of a geospatial stack to go stale.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# The GDAL wheels bundle most of their own stack but link against a few system
# libraries that slim does not ship. libexpat in particular: without it rasterio
# fails at import with a message that names the .so rather than the wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first and separately, so a source change does not invalidate the layer
# that takes minutes to build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra serve --extra db --extra report

# README is copied because pyproject declares it; the build backend reads it
# when it installs the project itself.
COPY src ./src
COPY alembic.ini README.md ./
RUN uv sync --frozen --extra serve --extra db --extra report

# Writable by the unprivileged user below, and a volume mount point in compose.
RUN mkdir -p /var/lib/floodline \
    && useradd --create-home --uid 10001 floodline \
    && chown -R floodline:floodline /var/lib/floodline /app
USER floodline

EXPOSE 8000

# `asgi:app` is the merged application: the map at `/`, the service under `/api`, and
# the probes at the root. It used to be the service alone, which is how the image came
# to serve a 404 on the one page anyone opens.
#
# One worker. Each request holds a whole watershed grid resident, so parallelism is
# bounded by memory rather than by cores, and the in-process concurrency limiter is
# what actually caps it. More workers would multiply the memory ceiling without
# multiplying the throughput.
CMD ["uv", "run", "--no-sync", "uvicorn", "floodline.api.asgi:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
