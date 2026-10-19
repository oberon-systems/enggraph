"""Entry point of the indexing job: `python -m enggraph.indexer`."""

from __future__ import annotations

import logging

from enggraph.core.config import FORCE_REEXTRACT, PROJECT_TYPE
from enggraph.indexer.indexer import resolve_project, scan_and_build_graph


def main() -> None:
    """Configure logging and run."""
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    project, root_path = resolve_project()
    scan_and_build_graph(project, root_path, PROJECT_TYPE, FORCE_REEXTRACT)


if __name__ == "__main__":
    main()
