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
# The build fails unless the file is the one this digest names.
ARG HTMX_SHA256=e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447
RUN mkdir -p /app/vendor \
    && python -c "import hashlib, sys, urllib.request; \
data = urllib.request.urlopen(sys.argv[1], timeout=60).read(); \
sys.exit('the download is not the pinned file') \
if hashlib.sha256(data).hexdigest() != sys.argv[3] else None; \
open(sys.argv[2], 'wb').write(data)" \
        "${HTMX_URL}" /app/vendor/htmx.min.js "${HTMX_SHA256}"
ENV HTMX_PATH=/app/vendor/htmx.min.js

# The dashboard reads the database and asks the other services; it holds no
# tree and writes no file, so it needs no root.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

EXPOSE 3002

CMD ["python", "-m", "enggraph.web"]
