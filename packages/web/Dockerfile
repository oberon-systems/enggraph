# Built from the repository root: docker build -f packages/web/Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY packages/core packages/core
COPY packages/web packages/web
RUN pip install --no-cache-dir ./packages/core ./packages/web \
    && rm -rf packages

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
