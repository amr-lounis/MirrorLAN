#!/usr/bin/env python3
"""HTTPS static server: serves a folder, redirects http to https,
and exposes the tiny /api signaling endpoints used by Sharer/Viewer.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .net import local_ips, server_urls
from .signaling import SignalingStore


class ServerError(Exception):
    """Raised when the server cannot start (bad cert, busy port, ...)."""


def create_redirect_handler(suffix: str) -> type:
    """301 redirector: every http:// request becomes the https:// twin."""

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def _options(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _go(self):
            host = (self.headers.get("Host") or "localhost").split(":")[0]
            self.send_response(301)
            self.send_header("Location", "https://%s%s%s" % (host, suffix, self.path))
            self.end_headers()

        do_GET = _go
        do_HEAD = _go
        do_POST = _go
        do_OPTIONS = _options

    return RedirectHandler


def create_api_handler(store: SignalingStore, www_dir: str) -> type:
    """Static files plus /api/offers, /api/claim, /api/answer, /api/offer,
    /api/leave, /api/rooms, /api/sharer/heartbeat, /api/sharer/leave."""

    class ApiHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=www_dir, **kwargs)

        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _json(self, obj: object, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            try:
                size = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(size).decode() or "{}")
            except Exception:
                return {}

        def do_GET(self):
            path = urlparse(self.path)
            query = parse_qs(path.query)
            if path.path == "/api/rooms":
                return self._json({"rooms": store.list_rooms()})
            if path.path == "/api/offers":
                room = (query.get("room", [""])[0] or "")
                try:
                    return self._json({"offers": store.list_offers(room)})
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if path.path == "/api/answer":
                vid = (query.get("id", [""])[0] or "")
                room = (query.get("room", [""])[0] or "")
                try:
                    sdp = store.get_answer(vid, room)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                if sdp:
                    return self._json({"sdp": sdp})
                return self._json({"error": "not-ready"}, 404)
            return super().do_GET()

        def do_POST(self):
            path = urlparse(self.path)
            if path.path in ("/api/offer", "/api/answer", "/api/leave", "/api/claim",
                             "/api/sharer/heartbeat", "/api/sharer/leave"):
                data = self._body()
                try:
                    if path.path == "/api/offer":
                        store.put_offer(data.get("id"), data.get("sdp"), data.get("room", ""))
                    elif path.path == "/api/answer":
                        store.put_answer(data.get("id"), data.get("sdp"), data.get("room", ""))
                    elif path.path == "/api/sharer/heartbeat":
                        store.heartbeat_sharer(data.get("room", ""))
                    elif path.path == "/api/sharer/leave":
                        store.leave_sharer(data.get("room", ""))
                    elif path.path == "/api/claim":
                        claimed = store.claim_offer(data.get("room", ""))
                        if claimed is None:
                            return self._json({"error": "empty"}, 404)
                        return self._json({"id": claimed[0], "sdp": claimed[1]})
                    else:
                        store.remove(data.get("id"), data.get("room", ""))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json({"ok": True})
            self.send_response(404)
            self.end_headers()

    return ApiHandler


class ServerManager:
    """Owns both listeners (https + http redirect) and the signaling store."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = SignalingStore(config.max_id_len, config.max_sdp_len,
                                    config.max_room_len, config.sharer_timeout)
        self._https: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()
        self.redirect_ok = False

    @property
    def running(self) -> bool:
        return self._https is not None

    def start(self) -> list:
        """Bind ports and serve in background threads. Returns client URLs."""
        with self._lock:
            if self._https is not None:
                raise ServerError("already running")
            self.config.validate()
            os.makedirs(self.config.www_dir, exist_ok=True)
            if not (os.path.exists(self.config.cert_file)
                    and os.path.exists(self.config.key_file)):
                raise ServerError("missing cert.pem/key.pem - press Make Cert first")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                context.load_cert_chain(self.config.cert_file, self.config.key_file)
            except Exception as exc:
                raise ServerError("bad cert.pem/key.pem (%s)" % exc)
            handler = create_api_handler(self.store, self.config.www_dir)
            try:
                server = ThreadingHTTPServer(("0.0.0.0", self.config.https_port), handler)
            except PermissionError:
                raise ServerError("cannot bind port %d (admin/root required)"
                                  % self.config.https_port)
            except OSError as exc:
                raise ServerError("cannot bind port %d (%s)" % (self.config.https_port, exc))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            self._https = server
            redirector = create_redirect_handler(self.config.https_suffix)
            self.redirect_ok = self._launch_redirect(redirector)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server_urls(self.config.https_port)

    def _launch_redirect(self, handler: type) -> bool:
        """Best-effort http->https redirect on the plain http port."""
        try:
            redirect = ThreadingHTTPServer(("0.0.0.0", self.config.http_port), handler)
        except OSError:
            return False
        threading.Thread(target=redirect.serve_forever, daemon=True).start()
        self._redirect = redirect
        return True

    def stop(self) -> None:
        """Shut everything down and drop all pending signaling state."""
        with self._lock:
            for server in (getattr(self, "_redirect", None), self._https):
                if server is None:
                    continue
                try:
                    server.shutdown()
                except Exception:
                    pass
                try:
                    server.server_close()
                except Exception:
                    pass
            self._https = None
            self._redirect = None
            self.store.clear()


def build_manager(port: int | None = None, www_dir: str | None = None) -> ServerManager:
    """Shortcut: configured manager with optional overrides."""
    config = Config()
    if port is not None:
        config.https_port = port
    if www_dir is not None:
        config.www_dir = os.path.abspath(www_dir)
    return ServerManager(config)
