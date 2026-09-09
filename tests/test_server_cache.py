#!/usr/bin/env python3
"""Tests for Cache-Control headers in core/server.py (stdlib unittest only)."""
import http.client
import os
import tempfile
import threading
import unittest

from core.server import _ThreadedServer, create_api_handler
from core.signaling import SignalingStore


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, resp.getheader("Cache-Control"), body
    finally:
        conn.close()


class TestCacheControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files = {
            "index.html": b"<html>rooms</html>",
            "shared.js": b"const SHARED_V = 3;",
            "shared.css": b"body{}",
            "notes.txt": b"plain text",
        }
        for name, data in cls.files.items():
            with open(os.path.join(cls.tmp.name, name), "wb") as handle:
                handle.write(data)
        store = SignalingStore()
        handler = create_api_handler(store, cls.tmp.name)
        cls.server = _ThreadedServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
        finally:
            cls.server.server_close()
            cls.tmp.cleanup()

    def test_html_is_no_cache(self):
        status, cache, body = _get(self.port, "/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(cache, "no-cache")
        self.assertEqual(body, self.files["index.html"])

    def test_js_is_no_cache(self):
        status, cache, _ = _get(self.port, "/shared.js?v=3")
        self.assertEqual(status, 200)
        self.assertEqual(cache, "no-cache")

    def test_css_is_no_cache(self):
        status, cache, _ = _get(self.port, "/shared.css?v=3")
        self.assertEqual(status, 200)
        self.assertEqual(cache, "no-cache")

    def test_api_is_no_store(self):
        status, cache, _ = _get(self.port, "/api/rooms")
        self.assertEqual(status, 200)
        self.assertEqual(cache, "no-store")

    def test_other_static_has_no_cache_header(self):
        status, cache, _ = _get(self.port, "/notes.txt")
        self.assertEqual(status, 200)
        self.assertIsNone(cache)

    def test_directory_listing_still_blocked(self):
        # list_directory answers 404; must not crash the Cache-Control hook.
        status, _, _ = _get(self.port, "/")
        # "/" maps to index.html when present, otherwise 404 from our guard.
        self.assertIn(status, (200, 404))


if __name__ == "__main__":
    unittest.main()
