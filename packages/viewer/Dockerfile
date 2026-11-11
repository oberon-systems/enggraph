# Built from the repository root: docker build -f packages/viewer/Dockerfile .
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# Unbuffered so `docker compose logs` shows progress while a job is running.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Every dependency, the build backend included, comes from this hashed lock.
COPY packages/viewer/requirements.txt requirements.txt
COPY packages/core packages/core
COPY packages/viewer packages/viewer
# Wheels kept in a build cache, outside the image, so a source edit does
# not download every dependency again.
# Nothing installs after this, so the installer does not stay in the image.
# hadolint ignore=DL3042
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements.txt \
    && pip install --no-deps --no-build-isolation \
        ./packages/core ./packages/viewer \
    && pip check \
    && rm -rf packages requirements.txt \
    && pip uninstall --yes pip setuptools wheel

# The graph page draws itself with vis-network, vendored into the image so the
# page renders on a host with no way out to the internet, and so a silent
# release cannot change how the graph looks. The viewer serves it from here.
ARG VIS_NETWORK_VERSION=9.1.9
ARG VIS_NETWORK_URL=https://unpkg.com/vis-network@${VIS_NETWORK_VERSION}/standalone/umd/vis-network.min.js
# The build fails unless the file is the one this digest names.
ARG VIS_NETWORK_SHA256=f53f833ddb9bf97efe856bb0637d4fe88f39e39999c7e94a4b8afc8de8a1a2e5
RUN mkdir -p /app/vendor \
    && python -c "import hashlib, sys, urllib.request; \
data = urllib.request.urlopen(sys.argv[1], timeout=60).read(); \
sys.exit('the download is not the pinned file') \
if hashlib.sha256(data).hexdigest() != sys.argv[3] else None; \
open(sys.argv[2], 'wb').write(data)" \
        "${VIS_NETWORK_URL}" /app/vendor/vis-network.min.js "${VIS_NETWORK_SHA256}"
ENV VIS_NETWORK_PATH=/app/vendor/vis-network.min.js

# No service here writes to a tree, so none needs root. The uid is given
# numerically so it resolves the same way on any host.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

CMD ["python", "-m", "enggraph.viewer"]
