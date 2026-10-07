# Built from the repository root: docker build -f packages/api/Dockerfile .
FROM python:3.11-slim

# Unbuffered so `docker compose logs` shows progress while a job is running.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ripgrep for the literal search over the mounted trees.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY packages/core packages/core
COPY packages/indexer packages/indexer
COPY packages/api packages/api
RUN pip install --no-cache-dir ./packages/core ./packages/indexer ./packages/api \
    && rm -rf packages

# The built-in skills, imported into the database when the API starts.
COPY skills ./skills

# graphify-out is created here rather than left to the bind mount: the
# extractor reads a graph from nowhere else, and a directory docker creates
# on demand belongs to root, which this user could not write into.
RUN useradd --create-home --uid 10001 enggraph \
    && mkdir -p /app/graphify-out /app/cache \
    && chown 10001:10001 /app/graphify-out /app/cache
USER 10001

CMD ["python", "-m", "enggraph.api"]
