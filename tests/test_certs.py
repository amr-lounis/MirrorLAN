#!/usr/bin/env python3
"""Tests for core/certs.py (stdlib unittest only)."""
import os
import tempfile
import unittest
from unittest import mock

from core import certs
from core.config import Config


def make_config(tmpdir, **overrides):
    cfg = Config()
    cfg.cert_file = os.path.join(tmpdir, "cert.pem")
    cfg.key_file = os.path.join(tmpdir, "key.pem")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _read(path):
    with open(path) as handle:
        return handle.read()


def _write(path, data):
    with open(path, "w") as handle:
        handle.write(data)


class TestGenerateAndInspect(unittest.TestCase):
    def test_roundtrip_cn_ips_dns(self):
        cert_pem, key_pem = certs.generate_self_signed(
            ["localhost", "example"], ["127.0.0.1", "192.168.1.5"],
            days=30, common_name="MirrorLAN")
        info = certs.inspect_cert(cert_pem)
        self.assertEqual(info["cn"], "MirrorLAN")
        self.assertIn("127.0.0.1", info["ips"])
        self.assertIn("192.168.1.5", info["ips"])
        self.assertIn("localhost", info["dns"])
        # validity is roughly now + 30 days
        import time
        self.assertGreater(info["not_after"], time.time() + 29 * 86400)
        self.assertLess(info["not_after"], time.time() + 31 * 86400)
        # key parses back to a scalar
        scalar = certs.load_key_scalar(key_pem)
        self.assertGreater(scalar, 0)

    def test_renewal_keeps_key(self):
        _, key_pem = certs.generate_self_signed(["localhost"], ["127.0.0.1"])
        priv = certs.load_key_scalar(key_pem)
        cert2, key2 = certs.generate_self_signed(["localhost"], ["127.0.0.1"],
                                                 priv=priv)
        self.assertEqual(certs.load_key_scalar(key2), priv)
        self.assertIn("BEGIN CERTIFICATE", cert2)

    def test_bad_ip_skipped(self):
        cert_pem, _ = certs.generate_self_signed(["localhost"], ["not-an-ip"])
        info = certs.inspect_cert(cert_pem)
        self.assertEqual(info["ips"], [])

    def test_garbage_raises_valueerror(self):
        with self.assertRaises(ValueError):
            certs.inspect_cert("not a pem at all")
        with self.assertRaises(ValueError):
            certs.load_key_scalar("not a pem at all")
        with self.assertRaises(ValueError):
            certs._pem_decode("!!! not base64 !!!\x00\x01")


class TestEnsureDefaultCert(unittest.TestCase):
    def test_missing_creates_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch("core.certs.local_ips", return_value=["192.168.1.2"]):
                status = certs.ensure_default_cert(cfg)
            self.assertEqual(status, "created")
            self.assertTrue(os.path.exists(cfg.cert_file))
            self.assertTrue(os.path.exists(cfg.key_file))
            info = certs.inspect_cert(_read(cfg.cert_file))
            self.assertEqual(info["cn"], "MirrorLAN")
            self.assertIn("192.168.1.2", info["ips"])

    def test_valid_cert_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch("core.certs.local_ips", return_value=["192.168.1.2"]):
                certs.ensure_default_cert(cfg)
                self.assertEqual(certs.ensure_default_cert(cfg), "ok")

    def test_expired_triggers_renewal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            old_key_scalar = None
            with mock.patch("core.certs.local_ips", return_value=["10.0.0.2"]):
                certs.ensure_default_cert(cfg)
                old_key_scalar = certs.load_key_scalar(_read(cfg.key_file))
                # rewrite an already-expired cert reusing the same key
                expired_cert, _ = certs.generate_self_signed(
                    list(cfg.dns_names), ["127.0.0.1", "10.0.0.2"],
                    days=-1, common_name=cfg.common_name, priv=old_key_scalar)
                _write(cfg.cert_file, expired_cert)
                status = certs.ensure_default_cert(cfg)
            self.assertTrue(status.startswith("renewed:expired"), status)
            # same private key survived the renewal
            self.assertEqual(certs.load_key_scalar(_read(cfg.key_file)),
                             old_key_scalar)

    def test_foreign_cn_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            foreign_cert, foreign_key = certs.generate_self_signed(
                ["localhost"], ["127.0.0.1"], common_name="SomeoneElse")
            _write(cfg.cert_file, foreign_cert)
            _write(cfg.key_file, foreign_key)
            with mock.patch("core.certs.local_ips", return_value=["127.0.0.1"]):
                self.assertEqual(certs.ensure_default_cert(cfg), "ok")
                self.assertIsNone(certs.needs_renewal(cfg))
            # files untouched
            self.assertEqual(_read(cfg.cert_file), foreign_cert)

    def test_new_ip_triggers_renewal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch("core.certs.local_ips", return_value=["10.0.0.2"]):
                certs.ensure_default_cert(cfg)
            with mock.patch("core.certs.local_ips",
                             return_value=["10.0.0.2", "10.0.0.9"]):
                self.assertEqual(certs.needs_renewal(cfg), "ips-changed")
                status = certs.ensure_default_cert(cfg)
                self.assertTrue(status.startswith("renewed:ips-changed"), status)

    def test_unreadable_cert_renews(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            _write(cfg.cert_file, "garbage")
            _write(cfg.key_file, "garbage")
            with mock.patch("core.certs.local_ips", return_value=[]):
                self.assertEqual(certs.needs_renewal(cfg), "unreadable")


if __name__ == "__main__":
    unittest.main()
