# Built from the repository root: docker build -f packages/viewer/Dockerfile .
FROM python:3.11-slim

# Unbuffered so `docker compose logs` shows progress while a job is running.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY packages/core packages/core
COPY packages/viewer packages/viewer
RUN pip install --no-cache-dir ./packages/core ./packages/viewer \
    && rm -rf packages

# The graph page draws itself with vis-network, which upstream loads from a
# CDN. Vendored into the image instead, so the page renders on a host with no
# way out to the internet, and so a silent upstream release cannot change how
# the graph looks. The viewer rewrites the script tag to point here.
ARG VIS_NETWORK_VERSION=9.1.9
ARG VIS_NETWORK_URL=https://unpkg.com/vis-network@${VIS_NETWORK_VERSION}/standalone/umd/vis-network.min.js
RUN mkdir -p /app/vendor \
    && python -c "import sys, urllib.request; \
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
        "${VIS_NETWORK_URL}" /app/vendor/vis-network.min.js \
    && test -s /app/vendor/vis-network.min.js
ENV VIS_NETWORK_PATH=/app/vendor/vis-network.min.js

# No service here writes to a tree, so none needs root. The uid is given
# numerically so it resolves the same way on any host.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

CMD ["python", "-m", "enggraph.viewer"]
