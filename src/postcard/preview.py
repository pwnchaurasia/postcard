"""`postcard preview`: serve a built site locally with the same URL rules as the nginx config."""

from __future__ import annotations

import re
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PAGE_RE = re.compile(r"^/([A-Za-z0-9]{12})(/[^/]*)?/?$")
ASSET_RE = re.compile(r"^/(_s|[A-Za-z0-9]{12}/a)/[^/]+$")


def route(path: str) -> str | None:
    """Map a request path to a file path inside the site, or None for 404. Mirrors nginx.conf."""
    if m := PAGE_RE.match(path):
        return f"/{m.group(1)}/index.html"
    if ASSET_RE.match(path):
        return path
    return None


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        target = route(urlsplit(self.path).path)
        if target is None:
            self.send_error(404)
            return
        self.path = target
        super().do_GET()

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


def serve(site: Path, page_path: str, port: int = 0, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(_Handler, directory=str(site)))
    url = f"http://127.0.0.1:{server.server_address[1]}{page_path}"
    print(f"Preview: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
