# Built from the repository root: docker build -f packages/mcp/Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY packages/core packages/core
COPY packages/mcp packages/mcp
# Wheels kept in a build cache, outside the image, so a source edit does
# not download every dependency again.
# hadolint ignore=DL3042
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ./packages/core ./packages/mcp \
    && rm -rf packages

# The server reads the database and asks the worker API; it holds no tree and
# writes no file, so it needs no root.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

EXPOSE 3000

CMD ["python", "-m", "enggraph.mcp"]
