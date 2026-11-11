# Built from the repository root: docker build -f packages/embed/Dockerfile .
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# Unbuffered so `docker compose logs` shows progress while a job is running.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Every dependency, the build backend included, comes from this hashed lock.
COPY packages/embed/requirements.txt requirements.txt
COPY packages/core packages/core
COPY packages/embed packages/embed
# Wheels kept in a build cache, outside the image, so a source edit does
# not download every dependency again.
# hadolint ignore=DL3042
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements.txt \
    && pip install --no-deps --no-build-isolation \
        ./packages/core ./packages/embed \
    && pip check \
    && rm -rf packages requirements.txt

# No service here writes to a tree, so none needs root. The uid is given
# numerically so it resolves the same way on any host.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

CMD ["python", "-m", "enggraph.embed"]
