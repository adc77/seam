"""A capability-URL, same-origin, loopback-only HTTP reader with no filesystem browsing or write routes."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import re
import secrets
from socketserver import TCPServer
from urllib.parse import urlsplit

from seam.canon import dumps
from seam_viewer.evidence import (
    EvidenceError,
    MAX_RUN_BYTES,
    open_experiments,
    transport,
)

ASSETS = {
    "": ("index.html", "text/html"),
    "app.js": ("app.js", "text/javascript"),
    "format.js": ("format.js", "text/javascript"),
    "style.css": ("style.css", "text/css"),
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
RUN_ROUTE = re.compile(r"api/experiments/([0-9]{1,2})/runs/([0-9]{1,4})\Z")


class ViewerServer(HTTPServer):
    def __init__(self, selections, port=0):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid viewer port")
        self.experiments = open_experiments(selections)
        self.prefix = f"/v/{secrets.token_urlsafe(24)}/"
        asset_root = Path(__file__).parent / "static"
        self.assets = {
            key: ((asset_root / name).read_bytes(), mime)
            for key, (name, mime) in ASSETS.items()
        }
        super().__init__(("localhost", port), Handler)
        self.origin = f"http://localhost:{self.server_port}"
        self.url = self.origin + self.prefix

    def server_bind(self):
        """Bind locally without HTTPServer's unused, potentially blocking reverse-DNS lookup."""
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, status, body=b"", mime="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, value):
        data = (dumps(transport(value)) + "\n").encode("ascii")
        if len(data) > MAX_RUN_BYTES:
            self._send(413, b'{"error":"viewer_response_size"}\n')
        else:
            self._send(status, data)

    def do_GET(self):
        origin = self.server.origin
        if (
            self.headers.get_all("Host", []) != [origin.removeprefix("http://")]
            or self.headers.get("Origin", origin) != origin
            or self.headers.get("Sec-Fetch-Site", "none") not in ("none", "same-origin")
        ):
            self._json(403, {"error": "viewer_origin"})
            return
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._json(404, {"error": "viewer_route"})
            return
        if (
            target.scheme
            or target.netloc
            or target.query
            or not target.path.startswith(self.server.prefix)
        ):
            self._json(404, {"error": "viewer_route"})
            return
        route = target.path[len(self.server.prefix) :]
        if route in self.server.assets:
            body, mime = self.server.assets[route]
            self._send(200, body, mime)
        elif route == "api/experiments":
            self._json(
                200,
                {
                    "experiments": [
                        item.overview(index)
                        for index, item in enumerate(self.server.experiments)
                    ]
                },
            )
        elif match := RUN_ROUTE.fullmatch(route):
            experiment, run = map(int, match.groups())
            if experiment >= len(self.server.experiments):
                self._json(404, {"error": "viewer_experiment_missing"})
                return
            try:
                self._json(200, self.server.experiments[experiment].run(run))
            except EvidenceError as exc:
                self._json(409, {"error": str(exc)})
        else:
            self._json(404, {"error": "viewer_route"})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self._json(405, {"error": "viewer_read_only"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
