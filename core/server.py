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


class _ThreadedServer(ThreadingHTTPServer):
    """Threaded server sized for join storms (up to 99 viewers at once).

    daemon_threads: stuck clients never block shutdown.
    request_queue_size MUST be a class attribute: listen() runs inside
    __init__ (server_bind), so assigning it on the instance afterwards
    would silently keep the default backlog of 5 and SYN-flood bursts
    would get RST (observed as ConnectionResetError on clients).
    """

    daemon_threads = True
    request_queue_size = 512


_CORS_METHODS = "GET, POST, OPTIONS"
_CORS_HEADERS = "Content-Type"

# Bigger than any valid signaling payload (SDP cap is 200 KB).
# Bodies beyond this are rejected before reading (see _body).
_MAX_BODY_BYTES = 256 * 1024


class _CorsMixin:
    """Identical CORS answers on both listeners (API + http redirect)."""

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def _serve_preflight(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", _CORS_METHODS)
        self.send_header("Access-Control-Allow-Headers", _CORS_HEADERS)
        self.end_headers()


def create_redirect_handler(suffix: str) -> type:
    """301 redirector: every http:// request becomes the https:// twin."""

    class RedirectHandler(_CorsMixin, BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _go(self):
            host = (self.headers.get("Host") or "localhost").split(":")[0]
            self.send_response(301)
            self.send_header("Location", "https://%s%s%s" % (host, suffix, self.path))
            self.end_headers()

        do_GET = _go
        do_HEAD = _go
        do_POST = _go

        def do_OPTIONS(self):
            self._serve_preflight()

    return RedirectHandler


def create_api_handler(store: SignalingStore, www_dir: str) -> type:
    """Static files plus /api/offers, /api/claim, /api/answer, /api/offer,
    /api/leave, /api/rooms, /api/diag, /api/sharer/heartbeat, /api/sharer/leave."""

    class ApiHandler(_CorsMixin, SimpleHTTPRequestHandler):
        # HTTP/1.1 keep-alive: browsers reuse one TLS connection for the
        # 1.2s polls instead of a full handshake per request (battery/CPU).
        # Every response path below carries an explicit length so a reused
        # connection never stalls waiting for a body that never comes.
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=www_dir, **kwargs)

        def log_message(self, *args):
            pass

        def do_OPTIONS(self):
            self._serve_preflight()

        def list_directory(self, path):
            # Never expose directory listings (e.g. when serving --dir).
            self.send_error(404, "Not found")
            return None

        def _json(self, obj: object, code: int = 200, close: bool = False) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close:
                # Unread request bytes remain in the buffer: the connection
                # cannot be reused, tell the client to open a fresh one.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict | None:
            # None = unreadable/oversized body (caller answers 413).
            # A lying Content-Length must not force us to buffer gigabytes.
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if size < 0 or size > _MAX_BODY_BYTES:
                return None
            try:
                raw = self.rfile.read(size) if size else b""
                return json.loads(raw.decode() or "{}")
            except Exception:
                return {}

        def do_GET(self):
            path = urlparse(self.path)
            query = parse_qs(path.query)
            if path.path == "/api/rooms":
                return self._json({"rooms": store.list_rooms()})
            if path.path == "/api/diag":
                return self._json({"events": store.recent_events()})
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
                return self._json({"waiting": True})
            return super().do_GET()

        def do_POST(self):
            path = urlparse(self.path)
            if path.path in ("/api/offer", "/api/answer", "/api/leave", "/api/claim",
                             "/api/sharer/heartbeat", "/api/sharer/leave"):
                data = self._body()
                if data is None:
                    return self._json({"error": "body too large"}, 413, close=True)
                try:
                    if path.path == "/api/offer":
                        store.put_offer(data.get("id"), data.get("sdp"), data.get("room", ""))
                        store.note("offer", data.get("room", ""), data.get("id"),
                                   self.client_address[0], data.get("cands"))
                    elif path.path == "/api/answer":
                        store.put_answer(data.get("id"), data.get("sdp"), data.get("room", ""))
                        store.note("answer", data.get("room", ""), data.get("id"),
                                   self.client_address[0], data.get("cands"))
                    elif path.path == "/api/sharer/heartbeat":
                        store.heartbeat_sharer(data.get("room", ""))
                    elif path.path == "/api/sharer/leave":
                        store.leave_sharer(data.get("room", ""))
                    elif path.path == "/api/claim":
                        room = data.get("room", "")
                        known = data.get("known")
                        gone = store.check_departed(room, known)
                        # Full room (accept=false): hand back ONLY re-offers
                        # from already-served viewers; the waiting queue stays.
                        if data.get("accept", True):
                            claimed = store.claim_offer(room)
                        else:
                            claimed = store.claim_known(room, known)
                        if claimed is None:
                            return self._json({"waiting": True, "gone": gone})
                        vid, sdp = claimed
                        return self._json({"id": vid, "sdp": sdp, "gone": gone})
                    else:
                        store.remove(data.get("id"), data.get("room", ""))
                        store.note("leave", data.get("room", ""), data.get("id"),
                                   self.client_address[0])
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json({"ok": True})
            self.send_response(404)
            self.send_header("Content-Length", "0")
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
                server = _ThreadedServer(("0.0.0.0", self.config.https_port), handler)
            except PermissionError:
                raise ServerError("cannot bind port %d (admin/root required)"
                                  % self.config.https_port)
            except OSError as exc:
                raise ServerError("cannot bind port %d (%s)" % (self.config.https_port, exc))
            try:
                server.socket = context.wrap_socket(server.socket, server_side=True)
            except Exception as exc:
                server.server_close()  # else the bound port leaks for the next start
                raise ServerError("TLS wrap failed (%s)" % exc)
            self._https = server
            redirector = create_redirect_handler(self.config.https_suffix)
            self.redirect_ok = self._launch_redirect(redirector)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server_urls(self.config.https_port)

    def _launch_redirect(self, handler: type) -> bool:
        """Best-effort http->https redirect on the plain http port."""
        try:
            redirect = _ThreadedServer(("0.0.0.0", self.config.http_port), handler)
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
