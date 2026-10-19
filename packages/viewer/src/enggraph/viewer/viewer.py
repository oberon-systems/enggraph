"""Serve the graph as a page, rendered from the database on every request.

A page written at index time goes stale the moment the next index runs, so
each request reads the graph and its stored communities and answers with a
fresh page: what is on screen is what is in the database.
"""

from __future__ import annotations

import html
import logging
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from enggraph.core.graphs import communities_of, community_labels, db_to_graph
from enggraph.core.storage import get_db_connection, list_projects
from enggraph.viewer.page import graph_page

LOG = logging.getLogger(__name__)

PORT = int(os.getenv("VIEWER_PORT", "3001"))
PATHS = ("/", "/graph")

# The drawing library, vendored into the image at build time. Serving it from
# here rather than letting the page reach a CDN is what makes the graph render
# on a host with no way out to the internet.
VIS_NETWORK_PATH = os.getenv("VIS_NETWORK_PATH", "/app/vendor/vis-network.min.js")
VIS_NETWORK_ROUTE = "/vis-network.min.js"
VIS_NETWORK_CDN = (
    "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
)


INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Code graph</title></head>
<body style="font-family: sans-serif; margin: 3rem">
<h1>Indexed projects</h1>
<ul>{items}</ul>
</body>
</html>
"""


def render_index(projects: list[tuple[str, str, str, int]]) -> bytes:
    """List the projects in the database, each linking to its own graph.

    Shown when the address names no project and more than one is indexed.
    Guessing would be worse than asking: the graphs are unrelated, and the
    page carries no sign of which one you are looking at.
    """
    items = "".join(
        '<li><a href="/graph?project={name}">{name}</a> '
        "- {kind}, {count} nodes, {root}</li>".format(
            name=html.escape(name),
            kind=html.escape(project_type),
            count=count,
            root=html.escape(root_path),
        )
        for name, root_path, project_type, count in projects
    )
    return INDEX_TEMPLATE.format(items=items or "<li>nothing indexed yet</li>").encode()


def library() -> str:
    """Return where the page loads the drawing library from."""
    if os.path.isfile(VIS_NETWORK_PATH):
        return VIS_NETWORK_ROUTE
    LOG.warning(
        "%s is missing, the page will load vis-network from the CDN",
        VIS_NETWORK_PATH,
    )
    return VIS_NETWORK_CDN


def render(project: str) -> bytes:
    """Render one project's graph as a page."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            graph = db_to_graph(cursor, project)
    finally:
        conn.close()
    grouped = communities_of(graph)
    labels = community_labels(graph, grouped)
    return graph_page(graph, grouped, labels, library()).encode()


class Handler(BaseHTTPRequestHandler):
    """Answers the graph page and nothing else."""

    server_version = "enggraph-viewer"

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by the base class
        """Render the graph, serve the library, or say why neither worked."""
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == VIS_NETWORK_ROUTE:
            self.serve_library()
            return

        if parsed.path not in PATHS:
            self.send_error(404, "Not found")
            return

        requested = urllib.parse.parse_qs(parsed.query).get("project", [""])[0]

        try:
            body = self.render_for(requested)
        except Exception as error:  # noqa: BLE001 - reported to the browser
            LOG.exception("Failed to render the graph")
            self.send_error(503, "Cannot render the graph", str(error))
            return

        # The page is regenerated per request; a cached copy would defeat it.
        self.reply(body, "text/html; charset=utf-8", "no-store")

    def render_for(self, requested: str) -> bytes:
        """Pick the project to draw, and draw it.

        A single indexed project is drawn without being named, which is the
        common case and keeps the address short. Past that the index page is
        the answer, since a rendered graph says nothing about whose it is.
        """
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                projects = list_projects(cursor)
        finally:
            conn.close()

        names = [name for name, _, _, _ in projects]
        if requested:
            if requested not in names:
                raise RuntimeError(
                    f"no project named {requested!r}; indexed: "
                    f"{', '.join(names) or 'none'}"
                )
            return render(requested)
        if len(names) == 1:
            return render(names[0])
        return render_index(projects)

    def serve_library(self) -> None:
        """Send the vendored drawing library.

        Unlike the page, this never changes between requests, so it is worth
        a long cache: it is by far the largest thing on the wire.
        """
        try:
            with open(VIS_NETWORK_PATH, "rb") as handle:
                body = handle.read()
        except OSError:
            LOG.exception("Failed to read %s", VIS_NETWORK_PATH)
            self.send_error(404, "Not found")
            return

        self.reply(body, "application/javascript", "public, max-age=86400")

    def reply(self, body: bytes, content_type: str, cache_control: str) -> None:
        """Send one complete response."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Route access logs through logging rather than stderr directly."""
        LOG.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    """Run the viewer until the container stops it."""
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("Graph viewer listening on port %d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
