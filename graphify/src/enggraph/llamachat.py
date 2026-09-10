"""Ask a llama.cpp server for one sentence, over HTTP.

The same protocol `worker/enggraph_worker/server.py` speaks. That file is not
imported here and this one is not imported there: the worker runs on a machine
that has no copy of this package - often Windows - and its whole point is to
need nothing but the standard library. Two small clients are the price of
that, and the shape they share is the API's, not ours.

Unlike the embedder there is no container beside this stack to fall back on:
either an address is configured and answers, or the summarizing queue is
drained the two ways it always was - `make summarize` in the graphify
container, and a worker claiming leases from the API.
"""

from __future__ import annotations

import logging
import time
import urllib.error
from typing import Any

from enggraph.config import (
    CHAT_TIMEOUTS,
    PROBE_TIMEOUTS,
    SUMMARIZE_PROBE_SECONDS,
    SUMMARIZE_SERVER_KEY,
    SUMMARIZE_SERVER_URL,
    Timeouts,
)
from enggraph.dial import request_json

LOG = logging.getLogger(__name__)

CHAT_PATH = "/v1/chat/completions"
PROPS_PATH = "/props"


class ChatError(RuntimeError):
    """A server was reached and did not answer with a completion."""


class ChatRejected(ChatError):
    """A working server refused this prompt: the file's problem, not its own."""


# Statuses that are about the prompt rather than the server.
INPUT_REFUSALS = frozenset({400, 413, 422, 500})


def describe_refusal(urls: list[str]) -> str:
    """Turn a list of addresses that did not answer into the remedy for it."""
    where = ", ".join(urls) if urls else "nowhere: no server URL is set"
    return (
        f"no llama.cpp server answered at {where}.\n"
        "Start one on the machine with the GPU:\n"
        "    llama-server -m <model.gguf> -c 8192 -ngl 99 "
        "--host 0.0.0.0 --port 8080\n"
        "and set the address on the settings page, where Test dials it."
    )


def candidates(stored: str = "") -> list[str]:
    """Return the addresses to try, in order, without the empty ones.

    As with the embedder: an address stored in the settings is the only one
    tried, because naming a server is an instruction rather than a preference.
    """
    named = (stored or "").strip().rstrip("/")
    if named:
        return [named]
    trimmed = SUMMARIZE_SERVER_URL.strip().rstrip("/")
    return [trimmed] if trimmed else []


def refused_key(url: str) -> str:
    """Turn a 401 into the remedy for it, which is not the one for silence."""
    return (
        f"{url} refused the token (401). It was started with --api-key, so "
        "the key on the settings page has to be the same one."
    )


def call(
    url: str, body: dict | None, timeouts: Timeouts, key: str = ""
) -> dict[str, Any]:
    """Make one request and return what it answered. Raises on anything else."""
    return request_json(url, body, timeouts, key)


class Chat:
    """The chat servers this process may use, and which one it is using."""

    def __init__(
        self,
        stored_url: str = "",
        stored_key: str = "",
        timeouts: Timeouts = CHAT_TIMEOUTS,
        probe_seconds: int = SUMMARIZE_PROBE_SECONDS,
    ) -> None:
        """Take the addresses to try. Nothing is contacted until it is used."""
        self.urls = candidates(stored_url)
        self.key = stored_key or SUMMARIZE_SERVER_KEY
        self.timeouts = timeouts
        self.probe_seconds = probe_seconds
        self.chosen: str | None = None
        self.model = ""
        self.n_ctx = 0
        self._silent_until = 0.0
        self._down = False

    def props(self) -> dict[str, Any]:
        """Read what the first answering server is running.

        This is the probe as well as the introduction: a server that answers
        `/props` names its model and its context window, which is exactly what
        the settings page shows beside a URL before storing it.
        """
        tried: list[str] = []
        for url in self._order():
            try:
                answer = call(f"{url}{PROPS_PATH}", None, PROBE_TIMEOUTS, self.key)
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    raise ChatError(refused_key(url)) from None
                LOG.debug("chat server %s answered %s", url, error.code)
                tried.append(url)
                continue
            except OSError as error:
                LOG.debug("chat server %s did not answer (%s)", url, error)
                tried.append(url)
                continue
            settings = answer.get("default_generation_settings") or {}
            self.n_ctx = int(settings.get("n_ctx") or answer.get("n_ctx") or 0)
            self.model = str(answer.get("model_path") or "").split("/")[-1]
            self.chosen = url
            return answer
        self.chosen = None
        raise ChatError(describe_refusal(tried or self.urls))

    def ask(self, system: str, prompt: str, max_tokens: int) -> str:
        """Ask the first server that answers, and return its raw reply."""
        tried: list[str] = []
        for url in self._order():
            body = {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "stream": False,
            }
            try:
                answer = call(f"{url}{CHAT_PATH}", body, self.timeouts, self.key)
            except urllib.error.HTTPError as error:
                # A refused token is not an address that did not answer: the
                # server is there, and trying the next one hides the remedy.
                if error.code == 401:
                    raise ChatError(refused_key(url)) from None
                if error.code in INPUT_REFUSALS:
                    raise ChatRejected(
                        f"{url} refused this prompt ({error.code})"
                    ) from None
                LOG.debug("chat server %s answered %s", url, error.code)
                tried.append(url)
                continue
            except OSError as error:
                LOG.debug("chat server %s did not answer (%s)", url, error)
                tried.append(url)
                continue
            choices = answer.get("choices") or []
            if not choices:
                raise ChatError(f"no choices in the answer: {str(answer)[:200]}")
            if self.chosen != url:
                LOG.info("Summarizing through %s", url)
            self.chosen = url
            self._down = False
            return choices[0].get("message", {}).get("content") or ""
        self.chosen = None
        self._silent_until = time.monotonic() + self.probe_seconds
        error = ChatError(describe_refusal(tried or self.urls))
        if not self._down:
            LOG.info("%s", error)
        self._down = True
        raise error

    def available(self) -> bool:
        """Whether a server answers, asked at most once per probe window."""
        now = time.monotonic()
        if self.chosen is not None:
            return True
        if now < self._silent_until:
            return False
        try:
            self.props()
        except ChatError as error:
            self._silent_until = now + self.probe_seconds
            if not self._down:
                LOG.info("%s", error)
            self._down = True
            return False
        if self._down:
            LOG.info("chat server %s answers again", self.chosen)
        self._down = False
        return True

    def _order(self) -> list[str]:
        """Return the addresses to try, the one that last answered first."""
        if self.chosen is None:
            return self.urls
        return [self.chosen] + [url for url in self.urls if url != self.chosen]
