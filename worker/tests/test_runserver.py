"""Starting the model server, with nothing downloaded and no server to run."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from enggraph_worker import runserver


def settings(**changed: object) -> argparse.Namespace:
    """Build the namespace the flags would have produced."""
    values = {
        "ctx": runserver.DEFAULT_CTX,
        "gpu_layers": runserver.DEFAULT_GPU_LAYERS,
        "host": runserver.DEFAULT_HOST,
        "port": runserver.DEFAULT_PORT,
        "embeddings": False,
        "pooling": "mean",
        "ubatch": runserver.DEFAULT_EMBED_UBATCH,
    }
    values.update(changed)
    return argparse.Namespace(**values)


def test_the_executable_is_named_for_the_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One name on Windows, another everywhere else."""
    monkeypatch.setattr(runserver.platform, "system", lambda: "Windows")
    assert runserver.server_path(Path("/x")).name == "llama-server.exe"

    monkeypatch.setattr(runserver.platform, "system", lambda: "Linux")
    assert runserver.server_path(Path("/x")).name == "llama-server"


def test_a_missing_server_names_the_script_that_fetches_it(tmp_path: Path) -> None:
    """Without --install this must say where to get one, not download it."""
    with pytest.raises(SystemExit) as exit_info:
        runserver.ensure_server(tmp_path, install=False)

    assert "get-llama-server.bat" in str(exit_info.value)


def test_linux_is_sent_to_the_image_not_to_a_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The llama.cpp releases carry no Linux CUDA archive to install."""
    monkeypatch.setattr(runserver.platform, "system", lambda: "Linux")

    with pytest.raises(SystemExit) as exit_info:
        runserver.ensure_server(tmp_path, install=True)

    message = str(exit_info.value)
    assert "docker run" in message
    assert "server-cuda" in message


def test_a_present_server_is_used_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is downloaded when the binary is already there."""
    monkeypatch.setattr(runserver.platform, "system", lambda: "Linux")
    (tmp_path / "llama-server").write_bytes(b"")

    assert runserver.ensure_server(tmp_path, install=False) == tmp_path / "llama-server"


def test_missing_weights_name_the_downloader(tmp_path: Path) -> None:
    """The model is a separate download from the server, and says so."""
    with pytest.raises(SystemExit) as exit_info:
        runserver.ensure_model("qwen-1.5b", tmp_path, install=False)

    assert "enggraph_worker.download" in str(exit_info.value)


def test_the_command_carries_this_stack_s_defaults() -> None:
    """A job's text has to fit one slot, so --parallel is not left to chance."""
    line = runserver.command(
        Path("/x/llama-server"), Path("/m/qwen.gguf"), settings(port=8090)
    )

    assert line[0] == "/x/llama-server"
    assert line[line.index("-m") + 1] == "/m/qwen.gguf"
    assert line[line.index("-c") + 1] == str(runserver.DEFAULT_CTX)
    assert line[line.index("-ngl") + 1] == str(runserver.DEFAULT_GPU_LAYERS)
    assert line[line.index("--port") + 1] == "8090"
    assert line[line.index("--parallel") + 1] == "1"


def test_a_chat_server_is_not_given_the_embedding_flags() -> None:
    """They would change what it serves, and it serves sentences."""
    line = runserver.command(Path("/x/llama-server"), Path("/m/qwen.gguf"), settings())

    assert "--embeddings" not in line
    assert "--pooling" not in line


def test_the_embedding_server_carries_all_three_flags() -> None:
    """Each one is a separate refusal when it is missing.

    `--embeddings` opens the route at all; without `--pooling` llama.cpp
    answers "Pooling type 'none' is not OAI compatible"; and a physical batch
    left at its default of 512 refuses any chunk longer than that as too
    large to process. All three were learned the hard way.
    """
    line = runserver.command(
        Path("/x/llama-server"),
        Path("/m/nomic.gguf"),
        settings(embeddings=True, ctx=runserver.DEFAULT_EMBED_CTX),
    )

    assert "--embeddings" in line
    assert line[line.index("--pooling") + 1] == "mean"
    assert line[line.index("--ubatch-size") + 1] == str(runserver.DEFAULT_EMBED_UBATCH)
    assert line[line.index("--batch-size") + 1] == str(runserver.DEFAULT_EMBED_UBATCH)
    assert line[line.index("-c") + 1] == str(runserver.DEFAULT_EMBED_CTX)
