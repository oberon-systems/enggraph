"""Run the MCP server: `python -m enggraph.mcp`."""

from __future__ import annotations

import logging

import uvicorn

from enggraph.mcp.server import PORT, app


def main() -> None:
    """Serve until stopped."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")  # noqa: S104


if __name__ == "__main__":
    main()
