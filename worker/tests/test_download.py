"""The downloader, which must refuse anything that is not weights."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from enggraph_worker import download, runserver


def test_an_error_page_is_not_installed_as_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 body saved under the model's name would fail a run later."""

    class Response:
        """An error page, handed over once and then exhausted."""

        def __init__(self) -> None:
            self.body: bytes | None = b"<html>404 Not Found</html>"

        def read(self, size: int = -1) -> bytes:
            body, self.body = self.body, None
            return body or b""

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(download.urllib.request, "urlopen", lambda *_, **__: Response())
    with pytest.raises(SystemExit, match="not a GGUF file"):
        download.fetch("qwen-1.5b", tmp_path)
    assert not list(tmp_path.glob("*.gguf"))
    assert list(tmp_path.glob("*.part")), "the partial download is kept to look at"


def test_existing_weights_are_not_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is a gigabyte; a re-run must not fetch it twice."""
    target = tmp_path / download.file_name("qwen-1.5b")
    target.write_bytes(b"GGUF and then some")
    pin = hashlib.sha256(b"GGUF and then some").hexdigest()

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("downloaded an existing model")

    monkeypatch.setattr(download.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(download, "sha256", lambda model: pin)
    assert download.fetch("qwen-1.5b", tmp_path) == target


def test_weights_on_disk_that_are_not_the_pinned_file_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whoever can write the models directory does not choose the model."""
    target = tmp_path / download.file_name("qwen-1.5b")
    target.write_bytes(b"GGUF of somebody else's making")

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("downloaded over a file nobody asked to replace")

    monkeypatch.setattr(download.urllib.request, "urlopen", refuse)
    with pytest.raises(SystemExit, match="not the file the catalogue pins"):
        download.fetch("qwen-1.5b", tmp_path)
    with pytest.raises(SystemExit, match="not the file the catalogue pins"):
        runserver.ensure_model("qwen-1.5b", tmp_path, install=False)
    assert target.read_bytes().startswith(b"GGUF of somebody")


def test_a_body_without_end_stops_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hash is known only at the end, so the size is what stops it."""

    class Endless:
        """A server that never stops sending."""

        def read(self, size: int = -1) -> bytes:
            return b"GGUF" * 64

        def __enter__(self) -> Endless:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(download.urllib.request, "urlopen", lambda *_, **__: Endless())
    monkeypatch.setattr(download, "MAX_MODEL_BYTES", 4096)
    with pytest.raises(SystemExit, match="more than any model weighs"):
        download.fetch("qwen-1.5b", tmp_path)
    assert not list(tmp_path.glob("*.gguf"))
    partial = next(tmp_path.glob("*.part"))
    assert partial.stat().st_size <= 4096


def test_weights_that_are_not_the_pinned_file_are_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The magic number says it is a model, not that it is this one."""

    class Response:
        """A GGUF file of somebody else's making."""

        def __init__(self) -> None:
            self.body: bytes | None = b"GGUF and then other weights"

        def read(self, size: int = -1) -> bytes:
            body, self.body = self.body, None
            return body or b""

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    asked: dict[str, object] = {}

    def opened(source: str, **kwargs: object) -> Response:
        asked.update(kwargs, source=source)
        return Response()

    monkeypatch.setattr(download.urllib.request, "urlopen", opened)
    with pytest.raises(SystemExit, match="not the file the catalogue pins"):
        download.fetch("qwen-1.5b", tmp_path)
    assert not list(tmp_path.glob("*.gguf"))
    assert asked["timeout"] == download.TIMEOUT_SECONDS
    assert "/resolve/main/" not in str(asked["source"])
