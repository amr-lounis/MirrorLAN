#!/usr/bin/env python3
"""Tests for core/turn.py: codec, auth, and a real UDP relay between two
loopback clients (stdlib unittest only)."""
import base64
import http.client
import os
import secrets
import socket
import struct
import tempfile
import threading
import time
import unittest

from core import turn as T


def _attrs_dict(attrs):
    out = {}
    for tag, val in attrs:
        out.setdefault(tag, []).append(val)
    return out


class _Client:
    """Minimal STUN/TURN client speaking to a TurnServer on 127.0.0.1."""

    def __init__(self, port):
        self.server = ("127.0.0.1", port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3)
        self.user = None
        self.key = None
        self.nonce = None
        self.realm = None

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _send_req(self, mtype, attrs=b"", key=None, raw_txn=None):
        txn = raw_txn or secrets.token_bytes(12)
        if key is not None:
            pkt = T._sign(key, mtype, txn, attrs)
        else:
            pkt = struct.pack("!HHI12s", mtype, len(attrs), T.MAGIC, txn) + attrs
        self.sock.sendto(pkt, self.server)
        data, _ = self.sock.recvfrom(65535)
        rtype, rtxn, rattrs = T._parse_msg(data)
        assert rtxn == txn, "transaction mismatch"
        return rtype, _attrs_dict(rattrs)

    def _authed(self, mtype, attrs=b""):
        assert self.user and self.key and self.nonce and self.realm
        full = (T._attr(0x0006, self.user.encode())
                + T._attr(0x0014, self.realm.encode())
                + T._attr(0x0015, self.nonce.encode()) + attrs)
        return self._send_req(mtype, full, key=self.key)

    def login(self, user, password_b64, alloc_extra=b""):
        """Full 401 -> Allocate dance. Returns (relay_ip, relay_port)."""
        self.user = user
        self.key = base64.b64decode(password_b64)
        base = T._attr(0x0019, b"\x11\x00\x00\x00") + alloc_extra  # UDP transport
        rtype, attrs = self._send_req(0x0003, base)
        assert rtype == 0x0113, hex(rtype)
        err = attrs[0x0009][0]
        assert err[2] == 4 and err[3] == 1, "expected 401, got %r" % err
        self.nonce = attrs[0x0015][0].decode()
        self.realm = attrs[0x0014][0].decode()
        rtype, attrs = self._authed(0x0003, base)
        assert rtype == 0x0103, hex(rtype)
        relay = T._xor_decode(attrs[0x0016][0])
        mapped = T._xor_decode(attrs[0x0020][0])
        assert mapped[0] == "127.0.0.1", mapped
        return relay

    def permit(self, peer):
        attrs = T._attr(0x0012, T._xor_encode(peer[0], peer[1]))
        rtype, _ = self._authed(0x0008, attrs)
        assert rtype == 0x0108, hex(rtype)

    def bind_channel(self, chan, peer):
        attrs = (T._attr(0x000C, struct.pack("!HH", chan, 0))
                 + T._attr(0x0012, T._xor_encode(peer[0], peer[1])))
        rtype, _ = self._authed(0x0009, attrs)
        assert rtype == 0x0109, hex(rtype)

    def send_channel(self, chan, payload):
        self.sock.sendto(struct.pack("!HH", chan, len(payload)) + payload,
                         self.server)

    def send_ind(self, peer, payload):
        attrs = (T._attr(0x0012, T._xor_encode(peer[0], peer[1]))
                 + T._attr(0x0013, payload))
        pkt = struct.pack("!HHI12s", 0x0016, len(attrs), T.MAGIC,
                          secrets.token_bytes(12)) + attrs
        self.sock.sendto(pkt, self.server)

    def refresh(self, lifetime):
        attrs = T._attr(0x000D, struct.pack("!I", lifetime))
        return self._authed(0x0004, attrs)


class TestCodec(unittest.TestCase):
    def test_attr_roundtrip_with_padding(self):
        payload = T._attr(0x0006, b"abc") + T._attr(0x0014, b"xy")
        self.assertEqual(T._parse_attrs(payload),
                         [(0x0006, b"abc"), (0x0014, b"xy")])

    def test_xor_roundtrip(self):
        for ip, port in (("127.0.0.1", 3478), ("192.168.11.132", 53465),
                         ("10.0.0.9", 9)):
            self.assertEqual(T._xor_decode(T._xor_encode(ip, port)), (ip, port))

    def test_sign_verify_and_tamper(self):
        key = b"test-password"
        txn = secrets.token_bytes(12)
        pkt = T._sign(key, 0x0103, txn, T._attr(0x0020, T._xor_encode("1.2.3.4", 5)))
        self.assertTrue(T._verify_mi(pkt, key))
        self.assertFalse(T._verify_mi(pkt, b"wrong"))
        bad = bytearray(pkt)
        bad[30] ^= 0xFF
        self.assertFalse(T._verify_mi(bytes(bad), key))
        # fingerprint-style trailing garbage after MI must not verify as MI
        self.assertFalse(T._verify_mi(pkt[:10], key))

    def test_parse_rejects_garbage(self):
        with self.assertRaises(ValueError):
            T._parse_msg(b"short")
        with self.assertRaises(ValueError):
            T._parse_msg(struct.pack("!HHI12s", 1, 0, 0xDEADBEEF, b"0" * 12))


class TestTurnServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = T.TurnServer(port=0, ips=["127.0.0.1"])
        cls.port = cls.srv.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _creds(self, ttl=3600):
        return self.srv.mint_credential(ttl=ttl)

    def test_allocate_needs_auth(self):
        cli = _Client(self.port)
        try:
            user, pw = self._creds()
            relay = cli.login(user, pw)
            self.assertEqual(relay[0], "127.0.0.1")
            self.assertGreater(relay[1], 0)
            # second Allocate on the same 5-tuple is a mismatch
            rtype, _ = cli._authed(0x0003, T._attr(0x0019, b"\x11\x00\x00\x00"))
            self.assertEqual(rtype, 0x0113)
        finally:
            cli.close()

    def test_wrong_password_rejected(self):
        cli = _Client(self.port)
        try:
            user, _pw = self._creds()
            cli.user, cli.key = user, b"wrong-key"
            rtype, attrs = cli._send_req(
                0x0003, T._attr(0x0006, user.encode()) + T._attr(0x0019, b"\x11\x00\x00\x00"))
            self.assertEqual(rtype, 0x0113)
            self.assertEqual(attrs[0x0009][0][2:4], b"\x04\x01")
        finally:
            cli.close()

    def test_expired_credential_rejected(self):
        self.assertIsNone(self.srv._password("1:deadbeef"))  # long past
        user, pw = self.srv.mint_credential(ttl=3600)
        self.assertIsNotNone(self.srv._password(user))
        self.assertIsNone(self.srv._password("not-a-user"))

    def _recv_channel(self, cli):
        data, _ = cli.sock.recvfrom(65535)
        chan = struct.unpack("!H", data[0:2])[0]
        ln = struct.unpack("!H", data[2:4])[0]
        return chan, data[4:4 + ln]

    def test_relay_both_directions(self):
        """A -> B -> A through the relay, arriving as ChannelData."""
        a = _Client(self.port)
        b = _Client(self.port)
        try:
            ra = a.login(*self._creds())
            rb = b.login(*self._creds())
            a.permit(rb)
            b.permit(ra)
            a.bind_channel(0x4000, rb)
            b.bind_channel(0x4000, ra)
            a.send_channel(0x4000, b"hello-b")
            self.assertEqual(self._recv_channel(b), (0x4000, b"hello-b"))
            b.send_channel(0x4000, b"hello-a")
            self.assertEqual(self._recv_channel(a), (0x4000, b"hello-a"))
        finally:
            a.close()
            b.close()

    def test_send_indication_arrives_as_data(self):
        """Send indication without any channel -> Data indication."""
        d = _Client(self.port)
        e = _Client(self.port)
        try:
            rd = d.login(*self._creds())
            re = e.login(*self._creds())
            d.permit(re)
            e.permit(rd)
            d.send_ind(re, b"via-send")
            data, _ = e.sock.recvfrom(65535)
            rtype, _, rattrs = T._parse_msg(data)
            self.assertEqual(rtype, 0x0017)
            got = _attrs_dict(rattrs)
            self.assertEqual(got[0x0013][0], b"via-send")
            e.send_ind(rd, b"back-send")
            data, _ = d.sock.recvfrom(65535)
            rtype, _, rattrs = T._parse_msg(data)
            self.assertEqual(rtype, 0x0017)
            self.assertEqual(_attrs_dict(rattrs)[0x0013][0], b"back-send")
        finally:
            d.close()
            e.close()

    def test_peer_to_client_wraps(self):
        """Raw packets at the relay address come back as Data/ChannelData."""
        a = _Client(self.port)
        try:
            ra = a.login(*self._creds())
            a.permit(("127.0.0.1", 9999))
            spoof = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            spoof.bind(("127.0.0.1", 0))
            try:
                spoof.sendto(b"ping-data", ra)
                data, _ = a.sock.recvfrom(65535)
                rtype, _, rattrs = T._parse_msg(data)
                self.assertEqual(rtype, 0x0017)  # Data indication
                d = _attrs_dict(rattrs)
                self.assertEqual(d[0x0013][0], b"ping-data")
                # after a ChannelBind the same path arrives as ChannelData
                a.bind_channel(0x4001, ("127.0.0.1", spoof.getsockname()[1]))
                spoof.sendto(b"ping-chan", ra)
                data, _ = a.sock.recvfrom(65535)
                self.assertEqual(data[:2], struct.pack("!H", 0x4001))
                ln = struct.unpack("!H", data[2:4])[0]
                self.assertEqual(data[4:4 + ln], b"ping-chan")
            finally:
                spoof.close()
        finally:
            a.close()

    def test_send_without_permission_dropped(self):
        a = _Client(self.port)
        b = _Client(self.port)
        try:
            a.login(*self._creds())
            rb = b.login(*self._creds())
            # no permit() call: the Send must arrive nowhere observable.
            b.sock.settimeout(1)
            a.send_ind(rb, b"nope")
            with self.assertRaises(socket.timeout):
                b.sock.recvfrom(65535)
        finally:
            a.close()
            b.close()

    def test_refresh_delete(self):
        a = _Client(self.port)
        try:
            a.login(*self._creds())
            rtype, attrs = a.refresh(0)
            self.assertEqual(rtype, 0x0104)
            rtype, _ = a.refresh(600)  # gone now
            self.assertEqual(rtype, 0x0114)
        finally:
            a.close()

    def test_alloc_expiry(self):
        a = _Client(self.port)
        try:
            user, pw = self._creds()
            a.user, a.key = user, base64.b64decode(pw)
            base = (T._attr(0x0019, b"\x11\x00\x00\x00")
                    + T._attr(0x000D, struct.pack("!I", 1)))
            rtype, attrs = a._send_req(0x0003, base)
            self.assertEqual(rtype, 0x0113)  # 401 first
            a.nonce = attrs[0x0015][0].decode()
            a.realm = attrs[0x0014][0].decode()
            rtype, _ = a._authed(0x0003, base)
            self.assertEqual(rtype, 0x0103)
            time.sleep(1.6)
            rtype, _ = a._authed(
                0x0008, T._attr(0x0012, T._xor_encode("127.0.0.1", 9)))
            self.assertEqual(rtype, 0x0118)  # allocation is gone
        finally:
            a.close()


class TestTurnEndpoint(unittest.TestCase):
    def test_api_turn_ok_and_unavailable(self):
        from core.server import _ThreadedServer, create_api_handler
        from core.signaling import SignalingStore

        tmp = tempfile.TemporaryDirectory()
        srv = T.TurnServer(port=0, ips=["127.0.0.1"])
        srv.start()
        try:
            handler = create_api_handler(SignalingStore(), tmp.name, srv)
            web = _ThreadedServer(("127.0.0.1", 0), handler)
            port = web.server_address[1]
            thread = threading.Thread(target=web.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/turn", headers={"Host": "192.168.1.7"})
                resp = conn.getresponse()
                import json
                body = json.loads(resp.read().decode())
                self.assertEqual(resp.status, 200)
                self.assertTrue(body["urls"].startswith("turn:192.168.1.7:"),
                                body["urls"])
                self.assertIn("username", body)
                self.assertIn("credential", body)
                conn.close()
            finally:
                web.shutdown()
                web.server_close()
        finally:
            srv.stop()
            tmp.cleanup()
        # no relay attached -> 503 so pages fall back to host candidates
        from core.server import create_api_handler as _mk
        handler = _mk(SignalingStore(), tempfile.gettempdir(), None)
        web = _ThreadedServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=web.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", web.server_address[1],
                                              timeout=5)
            conn.request("GET", "/api/turn")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 503)
            conn.close()
        finally:
            web.shutdown()
            web.server_close()


class TestManagerTurnIntegration(unittest.TestCase):
    """ServerManager starts TURN next to HTTPS and serves /api/turn."""

    def _free_port(self, udp=False):
        probe = socket.socket(socket.AF_INET,
                              socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_manager_turn_wiring(self):
        import json
        import ssl
        from unittest import mock

        from core.certs import ensure_default_cert
        from core.config import Config
        from core.server import ServerManager

        tmp = tempfile.TemporaryDirectory()
        www = os.path.join(tmp.name, "www")
        os.makedirs(www)
        with open(os.path.join(www, "index.html"), "w") as handle:
            handle.write("<html></html>")
        cfg = Config()
        cfg.www_dir = www
        cfg.cert_file = os.path.join(tmp.name, "cert.pem")
        cfg.key_file = os.path.join(tmp.name, "key.pem")
        cfg.https_port = self._free_port()
        cfg.http_port = self._free_port()
        cfg.turn_port = self._free_port(udp=True)
        with mock.patch("core.certs.local_ips", return_value=[]):
            ensure_default_cert(cfg)
        mgr = ServerManager(cfg)
        try:
            urls = mgr.start()
            self.assertTrue(urls)
            self.assertTrue(mgr.turn_ok)
            self.assertIsNotNone(mgr.turn)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection("127.0.0.1", cfg.https_port,
                                               timeout=5, context=ctx)
            conn.request("GET", "/api/turn")
            resp = conn.getresponse()
            body = json.loads(resp.read().decode())
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertIn("turn:127.0.0.1:", body["urls"])
            self.assertIn("credential", body)
            mgr.stop()
            self.assertFalse(mgr.turn_ok)
            # restart works: no socket leaked by the first start/stop cycle
            cfg.https_port = self._free_port()
            cfg.http_port = self._free_port()
            mgr.start()
            self.assertTrue(mgr.turn_ok)
        finally:
            mgr.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
