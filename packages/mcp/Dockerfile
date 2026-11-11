# Built from the repository root: docker build -f packages/mcp/Dockerfile .
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Every dependency, the build backend included, comes from this hashed lock.
COPY packages/mcp/requirements.txt requirements.txt
COPY packages/core packages/core
COPY packages/mcp packages/mcp
# Wheels kept in a build cache, outside the image, so a source edit does
# not download every dependency again.
# hadolint ignore=DL3042
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements.txt \
    && pip install --no-deps --no-build-isolation \
        ./packages/core ./packages/mcp \
    && pip check \
    && rm -rf packages requirements.txt

# The server reads the database and asks the worker API; it holds no tree and
# writes no file, so it needs no root.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

EXPOSE 3000

CMD ["python", "-m", "enggraph.mcp"]
