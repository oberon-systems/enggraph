"""Start llama-server on this machine, fetching whatever is missing first.

One command from nothing to a model answering on a port: the binaries, the
weights and the flags that suit this stack's jobs. It is what
start-llama-server.bat runs.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

from enggraph_worker import getserver
from enggraph_worker.catalogue import DEFAULT_MODEL, default_dir, file_name
from enggraph_worker.download import fetch, is_gguf

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_CTX = 8192
DEFAULT_GPU_LAYERS = 99
# The embedding server is a second process on a second port: one llama-server
# runs one model, and the model that writes a sentence is not the model that
# writes a vector.
DEFAULT_EMBED_PORT = 8081
DEFAULT_EMBED_MODEL = "nomic-embed"
# nomic-embed-text-v1.5 was trained with a 2048 token window, and llama.cpp
# caps a slot to it anyway.
DEFAULT_EMBED_CTX = 2048
# The physical batch is the one that refuses: its default is 512 tokens, and
# a chunk of 1500 characters is more than that. Left alone, every long chunk
# comes back as "input is too large to process".
DEFAULT_EMBED_UBATCH = 8192


def server_path(dest: Path) -> Path:
    """Where the executable sits, under whichever name this OS uses."""
    name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    return dest / name


def ensure_server(dest: Path, install: bool) -> Path:
    """Return the server, downloading a release first if it is not there."""
    server = server_path(dest)
    if server.is_file():
        return server
    if not install:
        raise SystemExit(
            f"no llama-server in {dest}. Run get-llama-server.bat, or pass "
            "--install to take one now."
        )
    if platform.system() != "Windows":
        raise SystemExit(
            f"no llama-server in {dest}, and the llama.cpp releases carry no "
            "Linux CUDA archive to install. Run the server from the project's "
            "own image instead:\n"
            "    docker run --rm --gpus all -p 8080:8080 "
            "-v ~/.local/share/enggraph/models:/models \\\n"
            "        ghcr.io/ggml-org/llama.cpp:server-cuda -m /models/<file> "
            "-c 8192 -ngl 99 --host 0.0.0.0 --port 8080 --parallel 1"
        )
    return getserver.install(dest)


def ensure_model(model: str, directory: Path, install: bool) -> Path:
    """Return the weights, downloading them first if they are not there."""
    target = directory / file_name(model)
    if target.is_file() and is_gguf(target):
        return target
    if not install:
        raise SystemExit(
            f"no weights at {target}. Run `py -m enggraph_worker.download`, or pass "
            "--install to take them now."
        )
    return fetch(model, directory)


def command(server: Path, model: Path, args: argparse.Namespace) -> list[str]:
    """Assemble the command line, with the defaults this stack wants."""
    line = [
        str(server),
        "-m",
        str(model),
        "-c",
        str(args.ctx),
        "-ngl",
        str(args.gpu_layers),
        "--host",
        args.host,
        "--port",
        str(args.port),
        # One slot, because -c is divided between slots and a job's text has
        # to fit in one of them.
        "--parallel",
        "1",
    ]
    if not args.embeddings:
        return line
    # Three flags, and all three are needed. `--embeddings` opens the route;
    # without `--pooling` llama.cpp refuses it with "Pooling type 'none' is
    # not OAI compatible"; and the physical batch has to hold a whole chunk
    # or every long one is refused as too large.
    return [
        *line,
        "--embeddings",
        "--pooling",
        args.pooling,
        "--batch-size",
        str(args.ubatch),
        "--ubatch-size",
        str(args.ubatch),
    ]


def main() -> None:
    """Start the server, and keep this process on it until it stops."""
    parser = argparse.ArgumentParser(
        prog="enggraph_worker.runserver",
        description="Start llama-server with the weights and flags this stack uses.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--dest", default="", help="where llama-server lives")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--ctx", type=int, default=DEFAULT_CTX)
    parser.add_argument("--gpu-layers", type=int, default=DEFAULT_GPU_LAYERS)
    parser.add_argument(
        "--install", action="store_true", help="download what is missing first"
    )
    parser.add_argument(
        "--embeddings",
        action="store_true",
        help="serve vectors rather than sentences, on the embedding defaults",
    )
    parser.add_argument(
        "--pooling",
        default="mean",
        help="how token vectors become one; mean is what nomic wants",
    )
    parser.add_argument("--ubatch", type=int, default=DEFAULT_EMBED_UBATCH)
    args, extra = parser.parse_known_args()

    # The embedding defaults apply only where the caller did not choose: a
    # port, a model and a window that suit vectors rather than sentences.
    if args.embeddings:
        if args.model == DEFAULT_MODEL:
            args.model = DEFAULT_EMBED_MODEL
        if args.port == DEFAULT_PORT:
            args.port = DEFAULT_EMBED_PORT
        if args.ctx == DEFAULT_CTX:
            args.ctx = DEFAULT_EMBED_CTX

    dest = Path(args.dest) if args.dest else getserver.default_dest()
    directory = Path(args.model_dir) if args.model_dir else default_dir()
    server = ensure_server(dest, args.install)
    model = ensure_model(args.model, directory, args.install)

    line = command(server, model, args) + extra
    print(" ".join(line), file=sys.stderr)
    raise SystemExit(subprocess.call(line))


if __name__ == "__main__":
    main()
