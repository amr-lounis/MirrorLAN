#!/usr/bin/env python3
"""Self-signed ECDSA P-256 certificates using stdlib only.

No network, no openssl, no pip packages. Used for offline HTTPS.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:  # config never imports certs, so this cannot cycle
    from .config import Config

# secp256r1 (prime256v1) domain parameters.
_EC_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_EC_A = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC
_EC_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_EC_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_EC_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

Point = Optional[Tuple[int, int]]


def _ec_inv(a: int, m: int) -> int:
    return pow(a, m - 2, m)


def _ec_add(p: Point, q: Point) -> Point:
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and (y1 + y2) % _EC_P == 0:
        return None
    if p == q:
        if y1 == 0:
            return None
        s = (3 * x1 * x1 + _EC_A) * _ec_inv((2 * y1) % _EC_P, _EC_P) % _EC_P
    else:
        s = (y2 - y1) * _ec_inv((x2 - x1) % _EC_P, _EC_P) % _EC_P
    x3 = (s * s - x1 - x2) % _EC_P
    return (x3, (s * (x1 - x3) - y1) % _EC_P)


def _ec_mul(d: int, p: Point = None) -> Point:
    if p is None:
        p = (_EC_GX, _EC_GY)
    result: Point = None
    while d:
        if d & 1:
            result = _ec_add(result, p)
        p = _ec_add(p, p)
        d >>= 1
    return result


def _der_len(n: int) -> bytes:
    if n < 128:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, val: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(val)) + val


def _der_int(x: int) -> bytes:
    raw = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _tlv(0x02, raw)


def _der_oid(*arcs: int) -> bytes:
    out = bytearray([arcs[0] * 40 + arcs[1]])
    for arc in arcs[2:]:
        groups = [arc & 0x7F]
        arc >>= 7
        while arc:
            groups.append(arc & 0x7F)
            arc >>= 7
        for group in reversed(groups[1:]):
            out.append(group | 0x80)
        out.append(groups[0])
    return _tlv(0x06, bytes(out))


def _der_utf8(text: str) -> bytes:
    return _tlv(0x0C, text.encode("utf-8"))


def _der_utctime(when: int) -> bytes:
    return _tlv(0x17, time.strftime("%y%m%d%H%M%SZ", time.gmtime(when)).encode("ascii"))


def _der_seq(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _der_set(*parts: bytes) -> bytes:
    return _tlv(0x31, b"".join(parts))


def _pem(kind: str, der: bytes) -> str:
    body = base64.encodebytes(der).decode("ascii")
    return "-----BEGIN %s-----\n%s-----END %s-----\n" % (kind, body, kind)


_OID_EC_PUBKEY: Optional[bytes] = None
_OID_PRIME256V1: Optional[bytes] = None
_OID_ECDSA_SHA256: Optional[bytes] = None


def _oids() -> Tuple[bytes, bytes, bytes]:
    global _OID_EC_PUBKEY, _OID_PRIME256V1, _OID_ECDSA_SHA256
    if _OID_EC_PUBKEY is None:
        _OID_EC_PUBKEY = _der_oid(1, 2, 840, 10045, 2, 1)
        _OID_PRIME256V1 = _der_oid(1, 2, 840, 10045, 3, 1, 7)
        _OID_ECDSA_SHA256 = _der_oid(1, 2, 840, 10045, 4, 3, 2)
    return _OID_EC_PUBKEY, _OID_PRIME256V1, _OID_ECDSA_SHA256


def _ecdsa_sign(priv: int, digest: bytes) -> Tuple[int, int]:
    num = int.from_bytes(digest, "big")
    while True:
        k = (int.from_bytes(os.urandom(32), "big") % (_EC_N - 1)) + 1
        r = (_ec_mul(k) or (0, 0))[0] % _EC_N
        if r == 0:
            continue
        s = (_ec_inv(k, _EC_N) * (num + r * priv)) % _EC_N
        if s == 0:
            continue
        return r, s


def generate_self_signed(dns_names: List[str], ip_list: List[str],
                         days: int = 3650,
                         common_name: str = "MirrorLAN") -> Tuple[str, str]:
    """Build a self-signed server certificate. Returns (cert_pem, key_pem)."""
    oid_pub, oid_curve, oid_sig = _oids()
    priv = (int.from_bytes(os.urandom(32), "big") % (_EC_N - 1)) + 1
    pub = _ec_mul(priv) or (0, 0)
    pub_raw = b"\x04" + pub[0].to_bytes(32, "big") + pub[1].to_bytes(32, "big")

    name = _der_seq(_der_set(_der_seq(_der_oid(2, 5, 4, 3) + _der_utf8(common_name))))

    now = int(time.time())
    validity = _der_seq(_der_utctime(now - 3600), _der_utctime(now + days * 86400))
    spki = _der_seq(
        _der_seq(oid_pub + oid_curve),
        _tlv(0x03, b"\x00" + pub_raw),
    )

    san = b""
    for dns in dns_names:
        san += _tlv(0x82, dns.encode("ascii"))
    for ip in ip_list:
        try:
            san += _tlv(0x87, socket.inet_aton(ip))
        except Exception:
            pass

    def ext(oid_arcs: Tuple[int, ...], value: bytes, critical: bool = False) -> bytes:
        body = _der_oid(*oid_arcs)
        if critical:
            body += _tlv(0x01, b"\xff")
        return _der_seq(body + _tlv(0x04, value))

    extensions = (
        ext((2, 5, 29, 19), _der_seq())
        + ext((2, 5, 29, 15), _tlv(0x03, b"\x07\x80"), critical=True)
        + ext((2, 5, 29, 37), _der_seq(_der_oid(1, 3, 6, 1, 5, 5, 7, 3, 1)))
        + ext((2, 5, 29, 17), _der_seq(san))
    )

    sig_alg = _der_seq(oid_sig)
    serial = int.from_bytes(os.urandom(8), "big") | (1 << 63)
    tbs = _der_seq(
        _tlv(0xA0, _der_int(2)),
        _der_int(serial),
        sig_alg,
        name,
        validity,
        name,
        spki,
        _tlv(0xA3, _der_seq(extensions)),
    )
    r, s = _ecdsa_sign(priv, hashlib.sha256(tbs).digest())
    cert_der = _der_seq(tbs, sig_alg, _tlv(0x03, b"\x00" + _der_seq(_der_int(r) + _der_int(s))))

    key_der = _der_seq(
        _der_int(1),
        _tlv(0x04, priv.to_bytes(32, "big")),
        _tlv(0xA0, oid_curve),
        _tlv(0xA1, _tlv(0x03, b"\x00" + pub_raw)),
    )
    return _pem("CERTIFICATE", cert_der), _pem("EC PRIVATE KEY", key_der)


def ensure_cert_files(cert_path: str, key_path: str, dns_names: List[str],
                      ip_list: List[str], days: int = 3650,
                      common_name: str = "MirrorLAN") -> bool:
    """Write cert/key files if missing. Returns True when files exist."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return True
    cert_pem, key_pem = generate_self_signed(dns_names, ip_list, days, common_name)
    with open(cert_path, "w") as handle:
        handle.write(cert_pem)
    with open(key_path, "w") as handle:
        handle.write(key_pem)
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    return True


def ensure_default_cert(config: "Config") -> bool:
    """Create cert/key for this machine's LAN IPs if missing.

    Single place for the logic previously duplicated in main.py and gui.py.
    Returns True when the files exist (created now or already there).
    """
    from .net import local_ips

    ips = ["127.0.0.1"] + [ip for ip in local_ips() if ip != "127.0.0.1"]
    return ensure_cert_files(config.cert_file, config.key_file,
                             list(config.dns_names), ips,
                             config.cert_days, config.common_name)
