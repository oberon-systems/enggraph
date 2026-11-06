# Built from the repository root: docker build -f packages/api/Dockerfile .
FROM python:3.11-slim

# Unbuffered so `docker compose logs` shows progress while a job is running.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY packages/core packages/core
COPY packages/indexer packages/indexer
COPY packages/api packages/api
# Wheels kept in a build cache, outside the image, so a source edit does
# not download every dependency again.
# hadolint ignore=DL3042
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ./packages/core ./packages/indexer ./packages/api \
    && rm -rf packages

# The built-in skills, imported into the database when the API starts.
COPY skills ./skills

RUN useradd --create-home --uid 10001 enggraph
USER 10001

CMD ["python", "-m", "enggraph.api"]
