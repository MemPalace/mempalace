"""
visualize_server.py — Local HTTP server for the knowledge graph visualization.

Serves the interactive visualization with live data via auto-refresh polling.

Usage:
    python -m mempalace.visualize_server
    python -m mempalace.visualize_server --port 8080
"""

import argparse
import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

from .knowledge_graph import DEFAULT_KG_PATH
from .visualizer import export_kg_json, render_kg_web


class KGHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves KG visualization and JSON API."""

    kg_path = None

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/graph.json":
            self._serve_json()
        else:
            super().do_GET()

    def _serve_html(self):
        """Serve the visualization HTML."""
        try:
            html = render_kg_web(
                kg_path=self.kg_path,
                output_html="_temp.html",
            )
            with open(html, "r", encoding="utf-8") as f:
                content = f.read()
            os.remove(html)
        except Exception as e:
            content = f"<html><body><h1>Error</h1><p>{e}</p></body></html>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _serve_json(self):
        """Serve the KG data as JSON."""
        try:
            content = export_kg_json(kg_path=self.kg_path)
        except Exception as e:
            content = json.dumps({"error": str(e)})

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress noisy logging."""
        pass


def serve(
    kg_path: str = None,
    port: int = 8765,
):
    """
    Start the visualization server.

    Args:
        kg_path: Path to the knowledge graph SQLite database.
        port: Port to listen on.
    """
    kg_path = kg_path or DEFAULT_KG_PATH
    kg_path = os.path.expanduser(kg_path)

    if not os.path.exists(kg_path):
        raise FileNotFoundError(f"Knowledge graph not found: {kg_path}")

    KGHandler.kg_path = kg_path

    addr = ("", port)
    server = HTTPServer(addr, KGHandler)

    print(f"  Serving at http://localhost:{port}")
    print(f"  KG: {kg_path}")
    print(f"\n  Open http://localhost:{port} in your browser")
    print("  Refresh browser after making changes to KG")
    print("  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping server...")
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Serve MemPalace visualization")
    parser.add_argument(
        "--kg-path",
        default=None,
        help="Path to knowledge graph (default: ~/.mempalace/knowledge_graph.sqlite3)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to listen on (default: 8765)",
    )
    args = parser.parse_args()

    serve(
        kg_path=args.kg_path,
        port=args.port,
    )


if __name__ == "__main__":
    main()
