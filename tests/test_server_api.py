#!/usr/bin/env python3
"""End-to-end HTTP tests for the signaling API in core/server.py.

Spins a real _ThreadedServer with create_api_handler (no TURN) and drives
the exact viewer -> sharer handshake the JS pages use:
offer -> claim -> answer -> answer-poll, plus rooms/heartbeat/leave,
full-room claim_known, gone reporting, validation, limits and headers.
Stdlib unittest only.
"""
import http.client
import json
import tempfile
import threading
import unittest

from core.server import _ThreadedServer, create_api_handler
from core.signaling import SignalingStore


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _post(port, path, obj):
    data = json.dumps(obj).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=data,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(data))})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _post_raw(port, path, raw: bytes):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=raw,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(raw))})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _options(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("OPTIONS", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _json(body: bytes):
    return json.loads(body.decode() or "{}")


class TestSignalingApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = SignalingStore()
        handler = create_api_handler(cls.store, cls.tmp.name, None)
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

    def setUp(self):
        self.store.clear()

    # -- happy path ----------------------------------------------------
    def test_offer_claim_answer_poll(self):
        p = self.port
        st, _, body = _post(p, "/api/offer",
                            {"id": "v-1", "sdp": "offer-sdp", "room": "r1"})
        self.assertEqual(st, 200)
        self.assertEqual(_json(body), {"ok": True})

        # offer is listed before it is claimed
        st, _, body = _get(p, "/api/offers?room=r1")
        self.assertEqual(st, 200)
        self.assertEqual(_json(body)["offers"],
                         [{"id": "v-1", "sdp": "offer-sdp"}])

        # answer poll waits before the sharer answers
        st, _, body = _get(p, "/api/answer?id=v-1&room=r1")
        self.assertEqual(st, 200)
        self.assertEqual(_json(body), {"waiting": True})

        # sharer claims the only offer
        st, _, body = _post(p, "/api/claim", {"room": "r1", "known": []})
        self.assertEqual(st, 200)
        claimed = _json(body)
        self.assertEqual((claimed["id"], claimed["sdp"]), ("v-1", "offer-sdp"))
        self.assertEqual(claimed["gone"], [])

        # empty queue reports waiting
        st, _, body = _post(p, "/api/claim", {"room": "r1", "known": []})
        self.assertEqual(_json(body)["waiting"], True)

        # sharer posts the answer, viewer picks it up
        st, _, body = _post(p, "/api/answer",
                            {"id": "v-1", "sdp": "answer-sdp", "room": "r1"})
        self.assertEqual(st, 200)
        st, _, body = _get(p, "/api/answer?id=v-1&room=r1")
        self.assertEqual(_json(body), {"sdp": "answer-sdp"})

    def test_rooms_heartbeat_and_leave(self):
        p = self.port
        st, _, body = _get(p, "/api/rooms")
        self.assertEqual(_json(body), {"rooms": []})

        _post(p, "/api/sharer/heartbeat", {"room": "live1"})
        _post(p, "/api/offer", {"id": "v-1", "sdp": "s", "room": "live1"})
        st, _, body = _get(p, "/api/rooms")
        rooms = {r["room"]: r for r in _json(body)["rooms"]}
        self.assertTrue(rooms["live1"]["live"])
        self.assertEqual(rooms["live1"]["pending"], 1)

        _post(p, "/api/sharer/leave", {"room": "live1"})
        # offer without a sharer is still listed as pending-only room
        st, _, body = _get(p, "/api/rooms")
        rooms = {r["room"]: r for r in _json(body)["rooms"]}
        self.assertFalse(rooms["live1"]["live"])

    def test_leave_clears_offer_and_answer(self):
        p = self.port
        _post(p, "/api/offer", {"id": "v-9", "sdp": "s", "room": "r"})
        _post(p, "/api/leave", {"id": "v-9", "room": "r"})
        st, _, body = _get(p, "/api/offers?room=r")
        self.assertEqual(_json(body), {"offers": []})
        st, _, body = _get(p, "/api/answer?id=v-9&room=r")
        # unknown id after leave: no answer row -> waiting (not 400)
        self.assertEqual(_json(body), {"waiting": True})

    # -- full room + gone ----------------------------------------------
    def test_full_room_serves_only_known_reoffers(self):
        p = self.port
        _post(p, "/api/offer", {"id": "served", "sdp": "s1", "room": "full"})
        _post(p, "/api/offer", {"id": "new-guy", "sdp": "s2", "room": "full"})
        # accept=false must not touch the waiting queue
        st, _, body = _post(p, "/api/claim",
                            {"room": "full", "known": ["served"],
                             "accept": False})
        claimed = _json(body)
        self.assertEqual(claimed["id"], "served")
        st, _, body = _get(p, "/api/offers?room=full")
        self.assertEqual(len(_json(body)["offers"]), 1)

    def test_gone_reported_on_next_claim(self):
        p = self.port
        _post(p, "/api/offer", {"id": "v-1", "sdp": "s", "room": "g"})
        _post(p, "/api/leave", {"id": "v-1", "room": "g"})
        st, _, body = _post(p, "/api/claim",
                            {"room": "g", "known": ["v-1"]})
        data = _json(body)
        self.assertEqual(data["waiting"], True)
        self.assertEqual(data["gone"], ["v-1"])

    def test_reoffer_replaces_stale_link(self):
        # viewer retries with the same id: fresh offer drops the stale answer
        p = self.port
        _post(p, "/api/offer", {"id": "v-1", "sdp": "o1", "room": "r"})
        _post(p, "/api/claim", {"room": "r", "known": []})
        _post(p, "/api/answer", {"id": "v-1", "sdp": "a1", "room": "r"})
        _post(p, "/api/offer", {"id": "v-1", "sdp": "o2", "room": "r"})
        st, _, body = _get(p, "/api/answer?id=v-1&room=r")
        self.assertEqual(_json(body), {"waiting": True})

    # -- validation / limits -------------------------------------------
    def test_bad_room_is_400(self):
        p = self.port
        st, _, _ = _post(p, "/api/offer",
                         {"id": "v-1", "sdp": "s", "room": "BAD!!"})
        self.assertEqual(st, 400)
        st, _, _ = _get(p, "/api/offers?room=BAD!!")
        self.assertEqual(st, 400)
        st, _, _ = _get(p, "/api/answer?id=v-1&room=BAD!!")
        self.assertEqual(st, 400)

    def test_missing_id_or_sdp_is_400(self):
        p = self.port
        st, _, _ = _post(p, "/api/offer", {"id": "", "sdp": "", "room": "r"})
        self.assertEqual(st, 400)
        st, _, _ = _post(p, "/api/answer", {"id": "v-1", "room": "r"})
        self.assertEqual(st, 400)

    def test_oversize_body_is_413(self):
        p = self.port
        st, _, body = _post_raw(p, "/api/offer", b"x" * (256 * 1024 + 1))
        self.assertEqual(st, 413)
        self.assertIn("too large", body.decode())

    def test_unknown_post_is_404_with_length(self):
        p = self.port
        st, headers, body = _post(p, "/api/nope", {"a": 1})
        self.assertEqual(st, 404)
        # explicit length keeps HTTP/1.1 keep-alive connections reusable
        self.assertEqual(body, b"")
        self.assertIn("Content-Length", headers)

    # -- headers / diag --------------------------------------------------
    def test_api_headers_cors_no_store(self):
        p = self.port
        st, headers, _ = _get(p, "/api/rooms")
        self.assertEqual(st, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_options_preflight(self):
        st, headers, _ = _options(self.port, "/api/offer")
        self.assertEqual(st, 204)
        self.assertIn("GET", headers.get("Access-Control-Allow-Methods", ""))

    def test_diag_records_offer_answer_leave(self):
        p = self.port
        _post(p, "/api/offer",
              {"id": "v-d", "sdp": "s", "room": "dr", "cands": 3})
        _post(p, "/api/claim", {"room": "dr", "known": []})
        _post(p, "/api/answer",
              {"id": "v-d", "sdp": "a", "room": "dr", "cands": 2})
        _post(p, "/api/leave", {"id": "v-d", "room": "dr"})
        st, _, body = _get(p, "/api/diag")
        kinds = [e["kind"] for e in _json(body)["events"]]
        self.assertEqual(kinds, ["offer", "answer", "leave"])
        cands = [e["cands"] for e in _json(body)["events"]]
        self.assertEqual(cands[:2], [3, 2])

    # -- long-run observability / overload --------------------------------
    def test_stats_counters_no_side_effects(self):
        p = self.port
        _post(p, "/api/sharer/heartbeat", {"room": "s1"})
        _post(p, "/api/offer", {"id": "v-1", "sdp": "s", "room": "s1"})
        st, headers, body = _get(p, "/api/stats")
        self.assertEqual(st, 200)
        data = _json(body)
        for key in ("rooms", "offers", "answers", "departed",
                    "allocs", "nonces", "turn_running", "threads"):
            self.assertIn(key, data, key)
        self.assertEqual(data["rooms"], 1)
        self.assertEqual(data["offers"], 1)
        self.assertEqual(data["turn_running"], False)
        self.assertGreaterEqual(data["threads"], 1)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        # read-only: data still there afterwards
        st, _, body = _get(p, "/api/offers?room=s1")
        self.assertEqual(len(_json(body)["offers"]), 1)

    def test_handler_timeout_is_bounded(self):
        from core.server import _HANDLER_TIMEOUT, create_api_handler as _mk

        handler = _mk(SignalingStore(), tempfile.gettempdir(), None)
        self.assertEqual(handler.timeout, _HANDLER_TIMEOUT)
        self.assertLessEqual(handler.timeout, 30)

    def test_overload_gate_returns_503_and_recovers(self):
        from core.server import _MAX_CONNS, create_api_handler as _mk

        handler = _mk(SignalingStore(), tempfile.gettempdir(), None)
        tmp_srv = _ThreadedServer(("127.0.0.1", 0), handler)
        port = tmp_srv.server_address[1]
        thread = threading.Thread(target=tmp_srv.serve_forever, daemon=True)
        thread.start()
        # hold every slot, then one more must get a fast 503 (reject-new)
        held = []
        try:
            for _ in range(_MAX_CONNS):
                self.assertTrue(handler._slots.acquire(blocking=False))
                held.append(True)
            st, headers, body = _get(port, "/api/rooms")
            self.assertEqual(st, 503)
            self.assertIn(b"busy", body)
        finally:
            for _ in held:
                handler._slots.release()
            tmp_srv.shutdown()
            tmp_srv.server_close()
        # after release the same server answers normally again
        tmp_srv2 = _ThreadedServer(("127.0.0.1", 0), handler)
        thread2 = threading.Thread(target=tmp_srv2.serve_forever, daemon=True)
        thread2.start()
        try:
            st, _, _ = _get(tmp_srv2.server_address[1], "/api/rooms")
            self.assertEqual(st, 200)
        finally:
            tmp_srv2.shutdown()
            tmp_srv2.server_close()


class TestManagerApiIntegration(unittest.TestCase):
    """ServerManager with the TURN relay disabled still serves the API."""

    def test_manager_turn_off_serves_signaling(self):
        import os
        import socket

        from core.config import Config
        from core.server import ServerManager

        tmp = tempfile.TemporaryDirectory()
        try:
            www = os.path.join(tmp.name, "www")
            os.makedirs(www)
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            http_port = probe.getsockname()[1]
            probe.close()
            cfg = Config()
            cfg.www_dir = www
            cfg.port = http_port
            cfg.turn_port = 0  # relay disabled
            mgr = ServerManager(cfg)
            mgr.start()
            try:
                self.assertFalse(mgr.turn_ok)
                st, _, body = _post(http_port, "/api/offer",
                                    {"id": "v-1", "sdp": "s", "room": "m"})
                self.assertEqual(st, 200)
                st, _, body = _post(http_port, "/api/claim",
                                    {"room": "m", "known": []})
                self.assertEqual(_json(body)["id"], "v-1")
                # /api/turn is 503 so pages fall back to host candidates
                st, _, _ = _get(http_port, "/api/turn")
                self.assertEqual(st, 503)
            finally:
                mgr.stop()
        finally:
            tmp.cleanup()

    def test_manager_stats_reports_turn(self):
        import os
        import socket

        from core.config import Config
        from core.server import ServerManager

        tmp = tempfile.TemporaryDirectory()
        try:
            www = os.path.join(tmp.name, "www")
            os.makedirs(www)
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.bind(("127.0.0.1", 0))
            http_port = tcp.getsockname()[1]
            tcp.close()
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(("127.0.0.1", 0))
            turn_port = udp.getsockname()[1]
            udp.close()
            cfg = Config()
            cfg.www_dir = www
            cfg.port = http_port
            cfg.turn_port = turn_port
            mgr = ServerManager(cfg)
            mgr.start()
            try:
                st, _, body = _get(http_port, "/api/stats")
                self.assertEqual(st, 200)
                data = _json(body)
                self.assertTrue(data["turn_running"])
                self.assertGreaterEqual(data["allocs"], 0)
                self.assertGreaterEqual(data["nonces"], 0)
            finally:
                mgr.stop()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
