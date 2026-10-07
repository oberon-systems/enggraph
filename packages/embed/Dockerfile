# Built from the repository root: docker build -f packages/embed/Dockerfile .
FROM python:3.11-slim

# Unbuffered so `docker compose logs` shows progress while a job is running.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY packages/core packages/core
COPY packages/embed packages/embed
RUN pip install --no-cache-dir ./packages/core ./packages/embed \
    && rm -rf packages

# No service here writes to a tree, so none needs root. The uid is given
# numerically so it resolves the same way on any host.
RUN useradd --create-home --uid 10001 enggraph
USER 10001

CMD ["python", "-m", "enggraph.embed"]
