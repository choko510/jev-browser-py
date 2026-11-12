"""Local fixture pages served over loopback HTTP.

The E2E test needs a real browser navigating real pages, but it must never
touch the public internet or need an API key. These fixtures implement a small
multi-step task: search → results list → detail page → a sought string.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

__all__ = ["FIXTURE_PORT", "RESULT_BODY", "RESULT_TITLE", "SEARCH_TERM", "fixture_server"]

FIXTURE_PORT = 0  # OS-assigned free port
SEARCH_TERM = "annual report"
RESULT_TITLE = "Annual Report 2026"
RESULT_BODY = "The Annual Report 2026 shows revenue of 42 million."

_INDEX = """<!doctype html>
<html><head><title>Fixture Search</title></head>
<body>
  <h1>Fixture Search</h1>
  <form action="/results" method="get">
    <label for="q">Search documents</label>
    <input id="q" name="q" type="search" placeholder="Search documents">
    <button type="submit">Go</button>
  </form>
  <p>Nothing has been searched yet.</p>
</body></html>
"""

_RESULTS = """<!doctype html>
<html><head><title>Results for {query}</title></head>
<body>
  <h1>Results for "{query}"</h1>
  <ul>
    <li><a href="/detail">Annual Report 2026</a></li>
    <li><a href="/other">Quarterly summary</a></li>
  </ul>
  <a href="/">Search again</a>
</body></html>
"""

_DETAIL = """<!doctype html>
<html><head><title>{title}</title></head>
<body>
  <h1>{title}</h1>
  <p id="body">{body}</p>
  <a href="/">Back to search</a>
</body></html>
"""

_JS_POST = """<!doctype html>
<html><body>
  <a id="write" href="/noop">Open preferences</a>
  <p id="status"></p>
  <script>
    document.querySelector("#write").addEventListener("click", (event) => {
      event.preventDefault();
      fetch("/mutate", {method: "POST", body: "x"})
        .then((response) => {
          document.querySelector("#status").textContent = response.ok ? "stored" : "failed";
        })
        .catch(() => {
          document.querySelector("#status").textContent = "blocked";
        });
    });
  </script>
</body></html>
"""

_POST_FORM = """<!doctype html>
<html><body>
  <form action="/mutate" method="post"><button type="submit">Submit record</button></form>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "jev-fixture/1.0"

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/":
            self._send(200, _INDEX)
            return
        if parts.path == "/redirect-unlisted":
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://unlisted.example:{self.server.server_address[1]}/detail",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parts.path == "/subresource":
            port = self.server.server_address[1]
            self._send(200, f'<html><body><img src="http://unlisted.example:{port}/pixel"></body></html>')
            return
        if parts.path == "/pixel":
            body = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts.path == "/results":
            query = (parse_qs(parts.query).get("q") or [""])[0]
            if not query.strip():
                self._send(400, "<html><body><p>no query</p></body></html>")
                return
            self._send(200, _RESULTS.format(query=_escape(query)))
            return
        if parts.path == "/detail":
            self._send(200, _DETAIL.format(title=RESULT_TITLE, body=RESULT_BODY))
            return
        if parts.path == "/other":
            self._send(200, _DETAIL.format(title="Quarterly summary", body="Quarterly figures."))
            return
        if parts.path == "/js-post":
            self._send(200, _JS_POST)
            return
        if parts.path == "/post-form":
            self._send(200, _POST_FORM)
            return
        if parts.path == "/download":
            self._send(200, "<html><body><p>download</p></body></html>")
            return
        self._send(404, "<html><body><p>Page not found</p></body></html>")

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if parts.path == "/mutate":
            self._send(200, "<html><body><p>stored</p></body></html>")
            return
        self._send(404, "<html><body>not found</body></html>")

    def _send(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@contextmanager
def fixture_server() -> Iterator[str]:
    """Serve the fixtures on loopback; yields the base URL."""
    server = HTTPServer(("127.0.0.1", FIXTURE_PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
