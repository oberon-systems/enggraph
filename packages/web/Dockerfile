# Built from the repository root: docker build -f packages/web/Dockerfile .
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Every dependency, the build backend included, comes from this hashed lock.
COPY packages/web/requirements.txt requirements.txt
COPY packages/core packages/core
COPY packages/web packages/web
# Wheels kept in a build cache, outside the image, so a source edit does
# not download every dependency again.
# Nothing installs after this, so the installer does not stay in the image.
# hadolint ignore=DL3042
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements.txt \
    && pip install --no-deps --no-build-isolation \
        ./packages/core ./packages/web \
    && pip check \
    && rm -rf packages requirements.txt \
    && pip uninstall --yes pip setuptools wheel

# The pages refresh themselves with htmx. Vendored into the image, so they
# work on a host with no way out and a silent release cannot change them.
ARG HTMX_VERSION=2.0.4
ARG HTMX_URL=https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js
RUN mkdir -p /app/vendor \
    && python -c "import sys, urllib.request; \
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
        "${HTMX_URL}" /app/vendor/htmx.min.js \
    && test -s /app/vendor/htmx.min.js
ENV HTMX_PATH=/app/vendor/htmx.min.js

# The dashboard reads the database and asks the other services; it holds no
# tree and writes no file, so it needs no root.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

EXPOSE 3002

CMD ["python", "-m", "enggraph.web"]
