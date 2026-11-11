# Built from the repository root: docker build -f packages/api/Dockerfile .
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# Unbuffered so `docker compose logs` shows progress while a job is running.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Every dependency, the build backend included, comes from this hashed lock.
COPY packages/api/requirements.txt requirements.txt
COPY packages/core packages/core
COPY packages/indexer packages/indexer
COPY packages/api packages/api
# Wheels kept in a build cache, outside the image, so a source edit does
# not download every dependency again.
# Nothing installs after this, so the installer does not stay in the image.
# hadolint ignore=DL3042
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements.txt \
    && pip install --no-deps --no-build-isolation \
        ./packages/core ./packages/indexer ./packages/api \
    && pip check \
    && rm -rf packages requirements.txt \
    && pip uninstall --yes pip setuptools wheel

# The built-in skills, imported into the database when the API starts.
COPY skills ./skills

RUN useradd --create-home --uid 10001 enggraph
USER 10001

CMD ["python", "-m", "enggraph.api"]
