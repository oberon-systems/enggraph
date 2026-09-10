"""Turn text into vectors, through whichever server answers.

There is one client and a list of addresses, not two backends: the machine
with the GPU and the small model beside this stack both run llama-server, and
both answer the OpenAI embeddings route. The queue only ever uses the primary
server; a search query may fall back to the small local one when the primary
cannot be reached, because a query must answer now and a queue can wait.

A server that did not answer is remembered for a while. The queue behind this
runs every tick whether or not anything is listening, and dialling a dead
address once per file would make an unconfigured stack noisy and slow.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error

from enggraph.config import (
    EMBED_DIM,
    EMBED_LOCAL_URL,
    EMBED_MODEL,
    EMBED_PATH,
    EMBED_PROBE_SECONDS,
    EMBED_QUERY_TIMEOUTS,
    EMBED_QUEUE_TIMEOUTS,
    EMBED_SERVER_KEY,
    EMBED_SERVER_URL,
    Timeouts,
)
from enggraph.dial import Unreachable, request_json

LOG = logging.getLogger(__name__)

# Addresses a query could not connect to, skipped until the time stored: with
# the primary down and no local server, a search must not wait on either.
_UNREACHABLE: dict[str, float] = {}


class EmbedError(RuntimeError):
    """A server was reached and did not answer with vectors."""


# Statuses that are about the input rather than the server: one file's problem,
# never a reason to stop dialling a server every other project also uses.
INPUT_REFUSALS = frozenset({400, 413, 422, 500})


class EmbedRejected(EmbedError):
    """A working server refused this input, for example a chunk too large."""


class EmbedTimeout(EmbedError):
    """A server took the request and was still thinking when time ran out.

    Told apart from silence because the remedy is different: the server is
    there and working, and what is wrong is that it was handed more than it
    could finish in the time allowed.
    """


def describe_refusal(urls: list[str]) -> str:
    """Turn a list of addresses that did not answer into the remedy for it."""
    where = ", ".join(urls) if urls else "nowhere: no server URL is set"
    return (
        f"no embedding server answered at {where}.\n"
        "Point the settings URL (or EMBED_SERVER_URL) at a llama-server "
        "started with --embeddings:\n"
        "    llama-server -m nomic-embed-text-v1.5.f16.gguf --embeddings "
        "--host 0.0.0.0 --port 8080\n"
        "To embed the queue on this machine's CPU, name the bundled server "
        "(`make up PROFILE=embed`, http://embedder:8080) there explicitly."
    )


def primary(stored: str = "") -> str:
    """Return the server the queue works against: the stored one, else the env."""
    return (stored or EMBED_SERVER_URL or "").strip().rstrip("/")


def candidates(stored: str = "", for_query: bool = False) -> list[str]:
    """Return the addresses to try, in order, without the empty ones.

    The queue gets the primary alone: a queue drained on a CPU because the GPU
    was away is work nobody asked for. A query may fall back to the local one.
    """
    urls = [primary(stored)]
    if for_query:
        urls.append((EMBED_LOCAL_URL or "").strip().rstrip("/"))
    seen: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.append(url)
    return seen


def refused_key(url: str) -> str:
    """Turn a 401 into the remedy for it, which is not the one for silence."""
    return (
        f"{url} refused the token (401). It was started with --api-key, so "
        "the key on the settings page has to be the same one."
    )


def detail_of(error: urllib.error.HTTPError) -> str:
    """Return the message a llama-server error body carries, if any."""
    try:
        body = json.loads(error.read().decode("utf-8", "ignore"))
        return str(body.get("error", {}).get("message", ""))[:200]
    except (ValueError, OSError, AttributeError):
        return ""


def post(url: str, body: dict, timeouts: Timeouts, key: str = "") -> dict:
    """Make one request and return what it answered. Raises on anything else."""
    return request_json(url, body, timeouts, key)


class Embedder:
    """The embedding servers this process may use, and which one it is using."""

    def __init__(
        self,
        stored_url: str = "",
        stored_key: str = "",
        model: str = EMBED_MODEL,
        timeouts: Timeouts | None = None,
        probe_seconds: int = EMBED_PROBE_SECONDS,
        for_query: bool = False,
    ) -> None:
        """Take the addresses to try. Nothing is contacted until it is used."""
        self.urls = candidates(stored_url, for_query)
        self.primary = primary(stored_url)
        self.for_query = for_query
        self.key = stored_key or EMBED_SERVER_KEY
        self.model = model
        self.timeouts = timeouts or (
            EMBED_QUERY_TIMEOUTS if for_query else EMBED_QUEUE_TIMEOUTS
        )
        self.probe_seconds = probe_seconds
        self.chosen: str | None = None
        # Why an address is not being used, kept so the answer can be shown
        # rather than only logged.
        self.refusals: dict[str, str] = {}
        self._reported: set[str] = set()
        self._silent_until = 0.0
        self._down = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch through the first server that answers.

        A batch that runs out of time is halved and tried again rather than
        failed: how many chunks a server can finish inside the timeout depends
        on its hardware and on what else it is doing, and neither is knowable
        from here. Halving finds the size that works in a few attempts, and
        one chunk that still cannot be done in the time allowed is the only
        thing worth reporting.

        Raises EmbedError when nothing answers. The caller is a background
        loop, and that is not a failure of the files it was holding: nothing
        is counted against them and the batch is tried again next tick.
        """
        if not texts:
            return []
        try:
            return self._one_batch(texts)
        except EmbedTimeout:
            if len(texts) == 1:
                raise
            middle = len(texts) // 2
            LOG.info(
                "A batch of %d timed out; halving it to %d",
                len(texts),
                middle,
            )
            return self.embed(texts[:middle]) + self.embed(texts[middle:])

    def _one_batch(self, texts: list[str]) -> list[list[float]]:
        """Send one request, to the first address that answers it."""
        tried: list[str] = []
        for url in self._order():
            if self.for_query and _UNREACHABLE.get(url, 0.0) > time.monotonic():
                tried.append(url)
                continue
            try:
                answer = post(
                    f"{url}{EMBED_PATH}",
                    {"model": self.model, "input": texts},
                    self.timeouts,
                    # The token is the primary's; the local fallback never sees it.
                    self.key if url == self.primary else "",
                )
            except urllib.error.HTTPError as error:
                # A refused token is not an address that did not answer: the
                # server is there, and trying the next one hides the remedy.
                if error.code == 401:
                    raise EmbedError(refused_key(url)) from None
                if error.code in INPUT_REFUSALS:
                    raise EmbedRejected(
                        f"{url} refused this input ({error.code}): {detail_of(error)}"
                    ) from None
                # Nor is a refusal. A chat server answers 501 here - it was
                # started without --embeddings - and falling through to the
                # next address without saying so is how a queue ends up on a
                # CPU while somebody watches an idle GPU.
                self._refused(url, error)
                tried.append(url)
                continue
            except TimeoutError as error:
                # The server has the request and is working on it. Saying
                # "nobody answered" here would send the file back to the queue
                # to be handed out at the same size and time out again.
                raise EmbedTimeout(
                    f"{url} did not finish {len(texts)} chunk(s): {error}"
                ) from None
            except Unreachable as error:
                if self.for_query:
                    _UNREACHABLE[url] = time.monotonic() + self.probe_seconds
                LOG.debug("embedding server %s is unreachable (%s)", url, error)
                tried.append(url)
                continue
            except OSError as error:
                LOG.debug("embedding server %s did not answer (%s)", url, error)
                tried.append(url)
                continue
            vectors = read_vectors(answer, len(texts))
            self._check_width(url, vectors[0])
            if self.chosen != url or self._down:
                LOG.info("Embedding through %s as %s", url, self.model)
            self.chosen = url
            self._down = False
            _UNREACHABLE.pop(url, None)
            return vectors
        self.chosen = None
        self._silent_until = time.monotonic() + self.probe_seconds
        raise EmbedError(self.why(tried or self.urls))

    def _refused(self, url: str, error: urllib.error.HTTPError) -> None:
        """Say once why an address will not be used, and keep the reason."""
        detail = detail_of(error)
        note = f"{url} refused the embeddings route ({error.code})"
        if detail:
            note = f"{note}: {detail}"
        self.refusals[url] = note
        if url not in self._reported:
            self._reported.add(url)
            LOG.info("%s", note)

    def embed_one(self, text: str) -> list[float]:
        """Embed a single string, which is what a search query is."""
        vectors = self.embed([text])
        return vectors[0]

    def _check_width(self, url: str, vector: list[float]) -> None:
        """Refuse a vector of the wrong width, naming what is wrong.

        The column is declared at one width, so a server running another model
        cannot be written to it at all. Caught here rather than at the insert:
        the database would refuse it one file at a time, for ever, and the
        reason would be a type error rather than "that is the wrong model".
        """
        if len(vector) == EMBED_DIM:
            return
        note = (
            f"{url} answered with {len(vector)}-dimensional vectors, and this "
            f"database stores {EMBED_DIM}. It is running a different model - "
            f"point this at a server started with {self.model} "
            "(--embeddings --pooling mean), or migrate the column."
        )
        self.refusals[url] = note
        raise EmbedError(note)

    def why(self, tried: list[str]) -> str:
        """Say what happened at each address, or how to start one."""
        refused = [self.refusals[url] for url in tried if url in self.refusals]
        if refused:
            return "; ".join(refused)
        return describe_refusal(tried)

    def available(self) -> bool:
        """Whether a server answers, asked at most once per probe window.

        The answer is a side effect of embedding, so this only dials when
        nothing has recently. A dead address stays dead for the window rather
        than being retried per file.
        """
        now = time.monotonic()
        if self.chosen is not None:
            return True
        if now < self._silent_until:
            return False
        try:
            self.embed_one("ping")
        except EmbedError as error:
            if not self._down:
                LOG.info("%s", error)
            self._down = True
            return False
        return True

    def _order(self) -> list[str]:
        """Return the addresses to try, the one that last answered first."""
        if self.chosen is None:
            return self.urls
        return [self.chosen] + [url for url in self.urls if url != self.chosen]


def read_vectors(answer: dict, expected: int) -> list[list[float]]:
    """Take the vectors out of an answer, or say what arrived instead.

    llama-server and the OpenAI route agree on `data[].embedding`, and
    llama.cpp's own `/embedding` route does not. Checking the count as well as
    the shape is what keeps a batch from being written misaligned - a vector
    landing on the wrong chunk is not an error any query would report.
    """
    data = answer.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise EmbedError(
            f"embedding server answered with {type(data).__name__} of "
            f"{len(data) if isinstance(data, list) else 0}, wanted {expected}"
        )
    vectors: list[list[float]] = []
    for row in data:
        vector = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(vector, list) or not vector:
            raise EmbedError("embedding server answered a row without a vector")
        vectors.append([float(value) for value in vector])
    return vectors
