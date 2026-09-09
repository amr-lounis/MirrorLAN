#!/usr/bin/env python3
"""Self-signed ECDSA P-256 certificates using stdlib only.

No network, no openssl, no pip packages. Used for offline HTTPS.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import os
import socket
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

from .net import local_ips

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
                         common_name: str = "MirrorLAN",
                         priv: Optional[int] = None) -> Tuple[str, str]:
    """Build a self-signed server certificate. Returns (cert_pem, key_pem).

    priv: reuse an existing private scalar (renewal keeps the key);
    None generates a fresh key.
    """
    oid_pub, oid_curve, oid_sig = _oids()
    if priv is None:
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


def _write_pair(cert_path: str, key_path: str, cert_pem: str, key_pem: str) -> None:
    """Write cert/key files, preserving the key file when unchanged."""
    with open(cert_path, "w") as handle:
        handle.write(cert_pem)
    unchanged = False
    if os.path.exists(key_path):
        try:
            unchanged = open(key_path).read() == key_pem
        except Exception:
            unchanged = False
    if not unchanged:
        with open(key_path, "w") as handle:
            handle.write(key_pem)
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass


def ensure_default_cert(config: "Config") -> str:
    """Make sure a valid cert/key pair exists, renewing only when needed.

    Returns "ok" (kept as-is), "created", or "renewed:<reason>".
    Renewal reuses the existing private key when readable (identity kept,
    renewal is one scalar-mul ≈ 40ms); a fresh key is generated only when
    the key file is missing or corrupt. Foreign certificates (different CN)
    are never touched.
    """
    reason = needs_renewal(config)
    if reason is None:
        return "ok"
    priv: Optional[int] = None
    if reason not in ("missing", "unreadable"):
        try:
            with open(config.key_file) as handle:
                priv = load_key_scalar(handle.read())
        except Exception:
            priv = None
    ips = ["127.0.0.1"] + [ip for ip in local_ips() if ip != "127.0.0.1"]
    cert_pem, key_pem = generate_self_signed(
        list(config.dns_names), ips, config.cert_days, config.common_name,
        priv=priv)
    _write_pair(config.cert_file, config.key_file, cert_pem, key_pem)
    return "created" if reason == "missing" else "renewed:" + reason


# Renew when the cert expires within this window (checked at startup).
RENEW_BEFORE_SECS = 30 * 86400


def needs_renewal(config: "Config") -> Optional[str]:
    """Why the current cert/key cannot be kept: missing, unreadable,
    expired, expiring, or ips-changed. None means keep it untouched.

    A parseable certificate with a foreign CN is left alone (None):
    we never destroy credentials we did not create.
    """
    if not (os.path.exists(config.cert_file) and os.path.exists(config.key_file)):
        return "missing"
    try:
        with open(config.cert_file) as handle:
            info = inspect_cert(handle.read())
    except Exception:
        return "unreadable"
    if info["cn"] != config.common_name:
        return None  # foreign certificate: not ours to renew
    now = time.time()
    if info["not_after"] <= now:
        return "expired"
    if info["not_after"] - now < RENEW_BEFORE_SECS:
        return "expiring"
    want = {"127.0.0.1"} | {ip for ip in local_ips() if ip != "127.0.0.1"}
    if not want <= set(info["ips"]):
        return "ips-changed"
    try:
        with open(config.key_file) as handle:
            load_key_scalar(handle.read())
    except Exception:
        return "unreadable"
    return None


def load_key_scalar(key_pem: str) -> int:
    """Private scalar from our SEC1 EC private-key PEM. Raises ValueError."""
    der = _pem_decode(key_pem)
    _, outer, end = _der_read(der, 0)
    if end != len(der):
        raise ValueError("trailing data")
    pos = 0
    while pos < len(outer):
        tag, val, pos = _der_read(outer, pos)
        if tag == 0x04 and len(val) == 32:  # OCTET STRING: the scalar
            return int.from_bytes(val, "big")
    raise ValueError("no private scalar")


def inspect_cert(cert_pem: str) -> dict:
    """Read back {"cn", "not_after", "ips", "dns"} from a PEM certificate.

    Tolerant reader for our own format; raises ValueError on anything
    unexpected (callers treat that as "unreadable", never crash).
    """
    der = _pem_decode(cert_pem)
    _, outer, end = _der_read(der, 0)
    if end != len(der):
        raise ValueError("trailing data")
    _, tbs, _ = _der_read(outer, 0)
    kids: List[Tuple[int, bytes]] = []
    pos = 0
    while pos < len(tbs):
        tag, val, pos = _der_read(tbs, pos)
        kids.append((tag, val))
    idx = 0
    if kids[0][0] == 0xA0:  # explicit [0] version
        idx = 1
    # serial, sigAlg, issuer, validity, subject, spki, then optionals
    validity = kids[idx + 3][1]
    q = 0
    _, _, q = _der_read(validity, q)  # notBefore
    _, na_raw, _ = _der_read(validity, q)  # notAfter (UTCTime)
    try:
        not_after = calendar.timegm(
            time.strptime(na_raw.decode("ascii"), "%y%m%d%H%M%SZ"))
    except Exception:
        raise ValueError("bad validity")
    cn = _find_cn(kids[idx + 4][1])
    ips: List[str] = []
    dns: List[str] = []
    for tag, val in kids[idx + 6:]:
        if tag != 0xA3:
            continue
        _, exts, _ = _der_read(val, 0)  # explicit wrapper -> SEQ
        e = 0
        while e < len(exts):
            _, one, e = _der_read(exts, e)
            o = 0
            _, oid, o = _der_read(one, o)
            otag, oval, o = _der_read(one, o)
            if otag == 0x01:  # BOOLEAN critical: the octet STRING follows
                _, oval, _ = _der_read(one, o)
            elif otag != 0x04:
                continue
            octets = oval
            if oid != _der_oid(2, 5, 29, 17)[2:] or not isinstance(octets, bytes):
                continue
            s = 0
            _, san, _ = _der_read(octets, 0)
            while s < len(san):
                stag, sval, s = _der_read(san, s)
                if stag == 0x87 and len(sval) == 4:  # iPAddress
                    ips.append(".".join(str(b) for b in sval))
                elif stag == 0x82:  # dNSName
                    dns.append(sval.decode("ascii", "replace"))
    return {"cn": cn, "not_after": not_after, "ips": ips, "dns": dns}


def _find_cn(subject: bytes) -> str:
    """CommonName from a Name SEQ. Empty string when absent."""
    cn_oid = _der_oid(2, 5, 4, 3)[2:]
    pos = 0
    try:
        while pos < len(subject):
            _, aset, pos = _der_read(subject, pos)  # SET
            ap = 0
            while ap < len(aset):
                _, attr, ap = _der_read(aset, ap)  # SEQ {oid, value}
                bp = 0
                _, oid, bp = _der_read(attr, bp)
                if oid == cn_oid:
                    _, cval, _ = _der_read(attr, bp)
                    return cval.decode("utf-8", "replace")
    except ValueError:
        pass
    return ""


def _pem_decode(text: str) -> bytes:
    """DER bytes from PEM text. Raises ValueError on garbage."""
    try:
        body = "".join(line.strip() for line in text.splitlines()
                       if line.strip() and "-----" not in line)
        return base64.b64decode(body)
    except Exception:
        raise ValueError("not PEM")


def _der_read(buf: bytes, pos: int = 0) -> Tuple[int, bytes, int]:
    """Read one TLV. Returns (tag, value, next_pos). Raises ValueError."""
    if pos + 2 > len(buf):
        raise ValueError("truncated")
    tag = buf[pos]
    first = buf[pos + 1]
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or pos + 2 + count > len(buf):
            raise ValueError("bad length")
        size = int.from_bytes(buf[pos + 2:pos + 2 + count], "big")
        start = pos + 2 + count
    else:
        size = first
        start = pos + 2
    end = start + size
    if end > len(buf):
        raise ValueError("truncated")
    return tag, buf[start:end], end
