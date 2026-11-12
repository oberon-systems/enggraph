"""The weights the summarizer can run, and where they come from.

The only copy of this table. The root Makefile reads it through
`python3 -m enggraph_worker.catalogue`, so the download the Linux host does and the
one the Windows worker does can never drift apart.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HUGGINGFACE = "https://huggingface.co/{repo}/resolve/{revision}/{name}"

# Quantized to q4_k_m throughout: it is the size that fits a 6 GB laptop GPU
# with room for the context window, and the summaries are one sentence long.
# Each with the commit of its repository and the SHA-256 of the file there, so
# a download is the file this table was written for and no later one.
MODELS: dict[str, tuple[str, str, str, str]] = {
    "qwen-1.5b": (
        "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
        "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
        "f86cb2c1fa58255f8052cc32aeede1b7482d4361",
        "cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046",
    ),
    "qwen-3b": (
        "Qwen/Qwen2.5-Coder-3B-Instruct-GGUF",
        "qwen2.5-coder-3b-instruct-q4_k_m.gguf",
        "f74adce6aa16316c625447af059dbebe4983757c",
        "724fb256bec1ff062b2f65e4569e871ad2e95ab2a3989723d1769c54294730b7",
    ),
    "qwen-7b": (
        "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "13fb94bfda8c8cf22497dc57b78f391a9acb426a",
        "509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c",
    ),
    "qwen-0.5b": (
        "Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF",
        "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf",
        "ebb2015119c907b064c512bf053e945850b5875f",
        "1d9614638d18024d0fbb36575a15f1302a3adf044df10345688ec4f6e1c4ff32",
    ),
    "smollm2": (
        "HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF",
        "smollm2-1.7b-instruct-q4_k_m.gguf",
        "2d4a76a30b4af41ecd395c35725ac11688d4cfe4",
        "decd2598bc2c8ed08c19adc3c8fdd461ee19ed5708679d1c54ef54a5a30d4f33",
    ),
    # The embedding model, not a summarizer: it answers `/v1/embeddings` and
    # nothing else. Kept at f16 rather than quantized - the file is 274 MB
    # either way at 137M parameters, and a quantized embedding shifts every
    # vector already written by the unquantized one.
    "nomic-embed": (
        "nomic-ai/nomic-embed-text-v1.5-GGUF",
        "nomic-embed-text-v1.5.f16.gguf",
        "0188c9bf409793f810680a5a431e7b899c46104c",
        "f7af6f66802f4df86eda10fe9bbcfc75c39562bed48ef6ace719a251cf1c2fdb",
    ),
}
DEFAULT_MODEL = "qwen-1.5b"


def file_name(model: str) -> str:
    """Return the GGUF file name of a model, or raise naming the choices."""
    if model not in MODELS:
        known = ", ".join(sorted(MODELS))
        raise SystemExit(f"unknown model {model!r}, pick one of: {known}")
    return MODELS[model][1]


def url(model: str) -> str:
    """Return where the weights of a model are downloaded from."""
    repo, name, revision, _ = MODELS[model]
    return HUGGINGFACE.format(repo=repo, name=name, revision=revision)


def sha256(model: str) -> str:
    """Return the digest the weights of a model must have."""
    file_name(model)
    return MODELS[model][3]


def default_dir() -> Path:
    """Return where weights are kept, which is not the same place on Windows."""
    if sys.platform == "win32":
        root = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "enggraph" / "models"
    return Path.home() / ".local" / "share" / "enggraph" / "models"


def main() -> None:
    """Answer one question about the table, for the Makefile to read."""
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python -m enggraph_worker.catalogue <file|url|list> [model]"
        )
    question = sys.argv[1]
    if question == "list":
        print(" ".join(sorted(MODELS)))
        return
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    file_name(model)
    print(file_name(model) if question == "file" else url(model))


if __name__ == "__main__":
    main()
