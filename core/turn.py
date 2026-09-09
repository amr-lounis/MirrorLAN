#!/usr/bin/env python3
"""Minimal TURN/UDP relay (RFC 5766 subset) using stdlib only.

Why this exists: browsers hide LAN IPs behind mDNS ``*.local`` hostnames.
When multicast DNS (UDP 5353) is blocked on the LAN, no ICE pair can form
and viewers sit on a black screen (conn=new/ice=new forever) even though
signaling works. A TURN relay fixes it permanently: relay candidates carry
the server's *literal* IP, which both sides already reach over HTTPS, so no
mDNS resolution is needed at all. The relayed media stays DTLS-SRTP
encrypted end-to-end; this server only forwards opaque UDP payloads.

Scope (enough for Chrome/Edge/Firefox/Safari over UDP):
  - STUN Binding (for diagnostics/clients that ask)
  - Allocate / Refresh / CreatePermission / ChannelBind + ChannelData
  - Send / Data indications, long-term credentials (TURN REST flavour:
    username = "<expiry>:<id>", password = base64(HMAC(secret, username)))
  - one relay socket per allocation, permissions (300 s), channels (600 s)

NOT implemented (not needed on a LAN): TCP/TLS transport, IPv6 relay,
EVEN-PORT reservation (ignored), BANDWIDTH/RESERVATION-TOKEN, admin API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import selectors
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

MAGIC = 0x2112A442
SOFTWARE = b"MirrorLAN-TURN"

# Message types we speak.
_T_BIND, _T_BIND_R = 0x0001, 0x0101
_T_ALLOC, _T_ALLOC_R, _T_ALLOC_E = 0x0003, 0x0103, 0x0113
_T_REFRESH, _T_REFRESH_R, _T_REFRESH_E = 0x0004, 0x0104, 0x0114
_T_PERM, _T_PERM_R, _T_PERM_E = 0x0008, 0x0108, 0x0118
_T_CHAN, _T_CHAN_R, _T_CHAN_E = 0x0009, 0x0109, 0x0119
_T_SEND, _T_DATA = 0x0016, 0x0017

# Attribute types we speak (unknown ones are ignored, per RFC).
_A_USERNAME = 0x0006
_A_MI = 0x0008
_A_ERR = 0x0009
_A_REALM = 0x0014
_A_NONCE = 0x0015
_A_DATA = 0x0013
_A_XOR_PEER = 0x0012
_A_XOR_RELAY = 0x0016
_A_REQ_TP = 0x0019
_A_CHAN = 0x000C
_A_LIFE = 0x000D
_A_XOR_MAPPED = 0x0020
_A_SOFTWARE = 0x8022

_PERM_SECS = 300
_CHAN_SECS = 600
_ALLOC_MAX_SECS = 3600
_NONCE_SECS = 3600
_MAX_ALLOCS = 256


def _attr(attr_type: int, value: bytes) -> bytes:
    return struct.pack("!HH", attr_type, len(value)) + value + b"\x00" * ((-len(value)) % 4)


def _parse_attrs(buf: bytes) -> List[Tuple[int, bytes]]:
    out: List[Tuple[int, bytes]] = []
    pos = 0
    while pos + 4 <= len(buf):
        attr_type, alen = struct.unpack("!HH", buf[pos:pos + 4])
        end = pos + 4 + alen
        if end > len(buf):
            raise ValueError("truncated attribute")
        out.append((attr_type, buf[pos + 4:end]))
        pos = end + ((-alen) % 4)
    return out


def _parse_msg(data: bytes) -> Tuple[int, bytes, List[Tuple[int, bytes]]]:
    """Split a STUN message into (type, transaction id, attributes)."""
    if len(data) < 20:
        raise ValueError("too short")
    mtype, mlen, magic, txn = struct.unpack("!HHI12s", data[:20])
    if magic != MAGIC or data[0] & 0xC0:
        raise ValueError("not stun")
    if len(data) < 20 + mlen:
        raise ValueError("truncated")
    return mtype, txn, _parse_attrs(data[20:20 + mlen])


def _xor_encode(ip: str, port: int) -> bytes:
    raw = socket.inet_aton(ip)  # IPv4 relay only (LAN scope)
    xip = bytes(a ^ b for a, b in zip(raw, struct.pack("!I", MAGIC)))
    return struct.pack("!BBH", 0, 0x01, port ^ (MAGIC >> 16)) + xip


def _xor_decode(value: bytes) -> Tuple[str, int]:
    if len(value) < 8 or value[1] != 0x01:
        raise ValueError("bad xor address")
    port = struct.unpack("!H", value[2:4])[0] ^ (MAGIC >> 16)
    ip = socket.inet_ntoa(bytes(a ^ b for a, b in zip(value[4:8], struct.pack("!I", MAGIC))))
    return ip, port


def _first(attrs: List[Tuple[int, bytes]], attr_type: int) -> Optional[bytes]:
    for tag, val in attrs:
        if tag == attr_type:
            return val
    return None


def _all(attrs: List[Tuple[int, bytes]], attr_type: int) -> List[bytes]:
    return [val for tag, val in attrs if tag == attr_type]


def _err_attr(code: int, reason: str) -> bytes:
    return struct.pack("!HBB", 0, code // 100, code % 100) + reason.encode("ascii", "replace")


def _sign(key: bytes, mtype: int, txn: bytes, attrs_payload: bytes) -> bytes:
    """Build a response with trailing MESSAGE-INTEGRITY (RFC 5389 14.5)."""
    mi_head = struct.pack("!HH", _A_MI, 20)
    head = struct.pack("!HHI12s", mtype, len(attrs_payload) + 4 + 20, MAGIC, txn)
    mac = hmac.new(key, head + attrs_payload + mi_head, hashlib.sha1).digest()
    return head + attrs_payload + mi_head + mac


def _mi_end(data: bytes) -> Optional[int]:
    """End offset of the MESSAGE-INTEGRITY value, or None."""
    mlen = struct.unpack("!H", data[2:4])[0]
    pos, end = 20, 20 + mlen
    while pos + 4 <= end and pos + 4 <= len(data):
        tag, alen = struct.unpack("!HH", data[pos:pos + 4])
        vend = pos + 4 + alen
        if tag == _A_MI and alen == 20 and vend <= len(data):
            return vend
        pos = vend + ((-alen) % 4)
    return None


def _verify_mi(data: bytes, key: bytes) -> bool:
    end = _mi_end(data)
    if end is None or end > len(data) or end - 20 < 20:
        return False
    # HMAC input = header (length adjusted to MI end) + attrs + MI header,
    # i.e. everything EXCEPT the 20-byte MI value itself (RFC 5389 14.5).
    adj = end - 20
    if adj > 0xFFFF:
        return False
    mac = hmac.new(key, data[0:2] + struct.pack("!H", adj) + data[4:end - 20],
                   hashlib.sha1).digest()
    return hmac.compare_digest(mac, data[end - 20:end])


class _Alloc:
    """One TURN allocation: a relay socket plus its permissions/channels."""

    __slots__ = ("client", "listen", "relay", "relay_addr", "user", "expiry",
                 "perms", "chans", "rchan")

    def __init__(self, client: Tuple[str, int], listen: socket.socket,
                 relay: socket.socket, relay_addr: Tuple[str, int],
                 user: str, expiry: float) -> None:
        self.client = client
        self.listen = listen  # socket facing this client (for indications)
        self.relay = relay
        self.relay_addr = relay_addr
        self.user = user
        self.expiry = expiry
        self.perms: Dict[str, float] = {}  # peer ip -> valid until
        self.chans: Dict[int, Tuple[str, int, float]] = {}  # chan -> (ip, port, until)
        self.rchan: Dict[Tuple[str, int], int] = {}  # (ip, port) -> chan


class TurnServer:
    """UDP TURN server. One thread, stdlib selectors, no dependencies."""

    def __init__(self, port: int = 3478, realm: str = "MirrorLAN",
                 ips: Optional[List[str]] = None) -> None:
        self.port = port
        self.realm = realm
        self._secret = secrets.token_bytes(32)
        self._ips = ips  # override (tests); else all local IPv4
        self._socks: Dict[socket.socket, str] = {}
        self._allocs: Dict[Tuple[str, str, int], _Alloc] = {}
        self._by_relay: Dict[socket.socket, _Alloc] = {}
        self._nonces: Dict[str, float] = {}
        self._sel: Optional[selectors.BaseSelector] = None
        self._thread: Optional[threading.Thread] = None
        self._run = False
        self.bound_port = port

    # -- credentials (TURN REST flavour) -------------------------------

    def mint_credential(self, ttl: int = 3600) -> Tuple[str, str]:
        """Fresh (username, password) pair, valid for ttl seconds."""
        user = "%d:%s" % (int(time.time()) + ttl, secrets.token_hex(4))
        pw = base64.b64encode(hmac.new(self._secret, user.encode(), hashlib.sha1).digest())
        return user, pw.decode("ascii")

    def _password(self, user: str) -> Optional[bytes]:
        """Raw HMAC digest used as the MESSAGE-INTEGRITY key (RFC 5769)."""
        try:
            exp, _uid = user.split(":", 1)
            if int(exp) <= time.time():
                return None
        except Exception:
            return None
        return hmac.new(self._secret, user.encode(), hashlib.sha1).digest()

    @property
    def running(self) -> bool:
        return self._run and self._thread is not None

    # -- lifetime -------------------------------------------------------

    def start(self) -> int:
        """Bind (one UDP socket per local IPv4) and serve. Returns the port."""
        if self._run:
            raise RuntimeError("already running")
        from .net import local_ips  # local import: net never imports turn
        ips = list(self._ips) if self._ips is not None else (["127.0.0.1"] + local_ips())
        sel = selectors.DefaultSelector()
        bound = 0
        try:
            for ip in ips:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    except Exception:
                        pass
                    sock.bind((ip, self.port))
                    sock.setblocking(False)
                    sel.register(sock, selectors.EVENT_READ, ("listen", ip))
                    self._socks[sock] = ip
                    bound = sock.getsockname()[1]
                except OSError:
                    continue
            if not self._socks:
                sel.close()
                raise OSError("cannot bind TURN udp port %d" % self.port)
        except Exception:
            for sock in list(self._socks):
                try:
                    sel.unregister(sock)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
            self._socks.clear()
            raise
        self._sel = sel
        self.bound_port = bound
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return bound

    def stop(self) -> None:
        self._run = False
        sel, socks = self._sel, list(self._socks)
        self._sel = None
        allocs = list(self._allocs.values())
        self._allocs.clear()
        self._by_relay.clear()
        for sock in socks:
            if sel is not None:
                try:
                    sel.unregister(sock)
                except Exception:
                    pass
            try:
                sock.close()
            except Exception:
                pass
        self._socks.clear()
        for alloc in allocs:
            try:
                alloc.relay.close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        if sel is not None:
            try:
                sel.close()
            except Exception:
                pass

    # -- main loop ------------------------------------------------------

    def _loop(self) -> None:
        assert self._sel is not None
        while self._run:
            try:
                events = self._sel.select(timeout=1.0)
            except Exception:
                if not self._run:
                    break
                continue
            for key, _mask in events:
                kind = key.data[0]
                try:
                    if kind == "listen":
                        self._handle_listen(key.fileobj, key.data[1])
                    else:
                        self._handle_relay(key.fileobj)
                except Exception:
                    continue
            try:
                self._prune()
            except Exception:
                continue

    def _prune(self) -> None:
        now = time.monotonic()
        for key, alloc in list(self._allocs.items()):
            if alloc.expiry <= now:
                self._drop_alloc(key, alloc)
        for nonce, exp in list(self._nonces.items()):
            if exp <= time.time():
                self._nonces.pop(nonce, None)

    # -- per-socket handlers --------------------------------------------

    def _handle_listen(self, sock: socket.socket, self_ip: str) -> None:
        try:
            data, src = sock.recvfrom(65535)
        except Exception:
            return
        if not data:
            return
        if data[0] & 0xC0 == 0x40:  # ChannelData (01....)
            self._on_channel_data(sock, self_ip, src, data)
            return
        try:
            mtype, txn, attrs = _parse_msg(data)
        except ValueError:
            return
        if mtype == _T_BIND:
            self._reply(sock, src, _T_BIND_R, txn,
                        [_attr(_A_XOR_MAPPED, _xor_encode(src[0], src[1])),
                         _attr(_A_SOFTWARE, SOFTWARE)])
        elif mtype == _T_ALLOC:
            self._on_allocate(sock, self_ip, src, data, txn, attrs)
        elif mtype == _T_REFRESH:
            self._on_refresh(sock, src, data, txn, attrs)
        elif mtype == _T_PERM:
            self._on_permission(sock, src, data, txn, attrs)
        elif mtype == _T_CHAN:
            self._on_channel_bind(sock, src, data, txn, attrs)
        elif mtype in (_T_SEND, _T_DATA):
            self._on_send(sock, self_ip, src, txn, attrs)
        # responses / unknown: ignore.

    def _handle_relay(self, sock: socket.socket) -> None:
        alloc = self._by_relay.get(sock)
        if alloc is None:
            return
        try:
            data, peer = sock.recvfrom(65535)
        except Exception:
            return
        if not data:
            return
        now = time.monotonic()
        chan = alloc.rchan.get((peer[0], peer[1]))
        if chan is not None:
            _, _, until = alloc.chans.get(chan, (None, None, -1))
            if until >= now:
                self._send_chan(alloc, chan, data)
                return
        if alloc.perms.get(peer[0], -1) >= now:
            self._send_data_ind(alloc, peer, data)
        # else: no permission for this peer — drop (RFC 5766 10.3).

    # -- requests --------------------------------------------------------

    def _auth(self, sock: socket.socket, src: Tuple[str, int], data: bytes,
              txn: bytes, attrs: List[Tuple[int, bytes]],
              err_type: int) -> Optional[Tuple[str, bytes]]:
        """Long-term auth. Sends 401 on failure, returns (user, key) on success."""
        user_b = _first(attrs, _A_USERNAME)
        nonce_b = _first(attrs, _A_NONCE)
        realm_b = _first(attrs, _A_REALM)
        user = nonce = realm = None
        try:
            user = user_b.decode() if user_b else None
            nonce = nonce_b.decode() if nonce_b else None
            realm = realm_b.decode() if realm_b else None
        except Exception:
            user = nonce = realm = None
        key = self._password(user) if user else None
        ok = (key is not None and realm == self.realm
              and nonce in self._nonces and self._nonces[nonce] > time.time()
              and _verify_mi(data, key))
        if not ok:
            fresh = secrets.token_urlsafe(12)
            self._nonces[fresh] = time.time() + _NONCE_SECS
            self._reply(sock, src, err_type, txn,
                        [_attr(_A_ERR, _err_attr(401, "Unauthorized")),
                         _attr(_A_REALM, self.realm.encode()),
                         _attr(_A_NONCE, fresh.encode()),
                         _attr(_A_SOFTWARE, SOFTWARE)])
            return None
        assert user is not None and key is not None
        return user, key

    def _on_allocate(self, sock: socket.socket, self_ip: str, src: Tuple[str, int],
                     data: bytes, txn: bytes, attrs: List[Tuple[int, bytes]]) -> None:
        auth = self._auth(sock, src, data, txn, attrs, _T_ALLOC_E)
        if auth is None:
            return
        user, key = auth
        req_tp = _first(attrs, _A_REQ_TP)
        if req_tp is None or req_tp[0] != 17:  # UDP only
            self._reply_auth(sock, src, _T_ALLOC_E, txn, key,
                             [_attr(_A_ERR, _err_attr(400, "Bad Request"))])
            return
        if (self_ip, src[0], src[1]) in self._allocs:
            self._reply_auth(sock, src, _T_ALLOC_E, txn, key,
                             [_attr(_A_ERR, _err_attr(437, "Allocation Mismatch"))])
            return
        if len(self._allocs) >= _MAX_ALLOCS:
            self._reply_auth(sock, src, _T_ALLOC_E, txn, key,
                             [_attr(_A_ERR, _err_attr(508, "Insufficient Capacity"))])
            return
        want_even = _first(attrs, 0x0018) is not None  # EVEN-PORT: best effort
        relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bound_addr: Optional[Tuple[str, int]] = None
        try:
            for _try in range(30):
                try:
                    relay.bind((self_ip, 0))
                except OSError:
                    continue
                bound_addr = relay.getsockname()
                if not want_even or bound_addr[1] % 2 == 0:
                    break
            if bound_addr is None:
                raise OSError("no relay port")
        except OSError:
            try:
                relay.close()
            except Exception:
                pass
            self._reply_auth(sock, src, _T_ALLOC_E, txn, key,
                             [_attr(_A_ERR, _err_attr(508, "Insufficient Capacity"))])
            return
        life_b = _first(attrs, _A_LIFE)
        if life_b is not None and len(life_b) == 4:
            asked = struct.unpack("!I", life_b)[0]
            if asked == 0:
                relay.close()
                self._reply_auth(sock, src, _T_ALLOC_E, txn, key,
                                 [_attr(_A_ERR, _err_attr(400, "Bad Request"))])
                return
            lifetime = max(1, min(asked, _ALLOC_MAX_SECS))
        else:
            lifetime = 600
        relay.setblocking(False)
        alloc = _Alloc(src, sock, relay, bound_addr, user, time.monotonic() + lifetime)
        self._allocs[(self_ip, src[0], src[1])] = alloc
        self._by_relay[relay] = alloc
        assert self._sel is not None
        try:
            self._sel.register(relay, selectors.EVENT_READ, ("relay",))
        except Exception:
            pass
        self._reply_auth(sock, src, _T_ALLOC_R, txn, key,
                         [_attr(_A_XOR_RELAY, _xor_encode(bound_addr[0], bound_addr[1])),
                          _attr(_A_XOR_MAPPED, _xor_encode(src[0], src[1])),
                          _attr(_A_LIFE, struct.pack("!I", lifetime)),
                          _attr(_A_SOFTWARE, SOFTWARE)])

    def _alloc_for(self, self_ip: str, src: Tuple[str, int]) -> Optional[_Alloc]:
        alloc = self._allocs.get((self_ip, src[0], src[1]))
        if alloc is not None and alloc.expiry <= time.monotonic():
            self._drop_alloc((self_ip, src[0], src[1]), alloc)
            return None
        return alloc

    def _drop_alloc(self, key: Tuple[str, str, int], alloc: _Alloc) -> None:
        self._allocs.pop(key, None)
        self._by_relay.pop(alloc.relay, None)
        if self._sel is not None:
            try:
                self._sel.unregister(alloc.relay)
            except Exception:
                pass
        try:
            alloc.relay.close()
        except Exception:
            pass

    def _on_refresh(self, sock: socket.socket, src: Tuple[str, int],
                    data: bytes, txn: bytes, attrs: List[Tuple[int, bytes]]) -> None:
        auth = self._auth(sock, src, data, txn, attrs, _T_REFRESH_E)
        if auth is None:
            return
        _user, key = auth
        self_ip = self._socks.get(sock, "")
        alloc = self._alloc_for(self_ip, src)
        if alloc is None:
            self._reply_auth(sock, src, _T_REFRESH_E, txn, key,
                             [_attr(_A_ERR, _err_attr(437, "Allocation Mismatch"))])
            return
        life_b = _first(attrs, _A_LIFE)
        asked = struct.unpack("!I", life_b)[0] if life_b is not None and len(life_b) == 4 else 600
        if asked == 0:  # goodbye: delete the allocation, then answer
            for akey, cand in list(self._allocs.items()):
                if cand is alloc:
                    self._drop_alloc(akey, cand)
                    break
            self._reply_auth(sock, src, _T_REFRESH_R, txn, key,
                             [_attr(_A_LIFE, struct.pack("!I", 0)),
                              _attr(_A_SOFTWARE, SOFTWARE)])
            return
        lifetime = max(1, min(asked, _ALLOC_MAX_SECS))
        alloc.expiry = time.monotonic() + lifetime
        self._reply_auth(sock, src, _T_REFRESH_R, txn, key,
                         [_attr(_A_LIFE, struct.pack("!I", lifetime)),
                          _attr(_A_SOFTWARE, SOFTWARE)])

    def _on_permission(self, sock: socket.socket, src: Tuple[str, int],
                       data: bytes, txn: bytes, attrs: List[Tuple[int, bytes]]) -> None:
        auth = self._auth(sock, src, data, txn, attrs, _T_PERM_E)
        if auth is None:
            return
        _user, key = auth
        self_ip = self._socks.get(sock, "")
        alloc = self._alloc_for(self_ip, src)
        if alloc is None:
            self._reply_auth(sock, src, _T_PERM_E, txn, key,
                             [_attr(_A_ERR, _err_attr(437, "Allocation Mismatch"))])
            return
        peers = _all(attrs, _A_XOR_PEER)
        if not peers:
            self._reply_auth(sock, src, _T_PERM_E, txn, key,
                             [_attr(_A_ERR, _err_attr(400, "Bad Request"))])
            return
        now = time.monotonic()
        try:
            for raw in peers:
                ip, _port = _xor_decode(raw)
                alloc.perms[ip] = now + _PERM_SECS
        except ValueError:
            self._reply_auth(sock, src, _T_PERM_E, txn, key,
                             [_attr(_A_ERR, _err_attr(400, "Bad Request"))])
            return
        self._reply_auth(sock, src, _T_PERM_R, txn, key,
                         [_attr(_A_SOFTWARE, SOFTWARE)])

    def _on_channel_bind(self, sock: socket.socket, src: Tuple[str, int],
                         data: bytes, txn: bytes, attrs: List[Tuple[int, bytes]]) -> None:
        auth = self._auth(sock, src, data, txn, attrs, _T_CHAN_E)
        if auth is None:
            return
        _user, key = auth
        self_ip = self._socks.get(sock, "")
        alloc = self._alloc_for(self_ip, src)
        if alloc is None:
            self._reply_auth(sock, src, _T_CHAN_E, txn, key,
                             [_attr(_A_ERR, _err_attr(437, "Allocation Mismatch"))])
            return
        chan_b = _first(attrs, _A_CHAN)
        peer_b = _first(attrs, _A_XOR_PEER)
        if chan_b is None or len(chan_b) < 2 or peer_b is None:
            self._reply_auth(sock, src, _T_CHAN_E, txn, key,
                             [_attr(_A_ERR, _err_attr(400, "Bad Request"))])
            return
        number = struct.unpack("!H", chan_b[0:2])[0]
        if not 0x4000 <= number <= 0x7FFE:
            self._reply_auth(sock, src, _T_CHAN_E, txn, key,
                             [_attr(_A_ERR, _err_attr(400, "Bad Request"))])
            return
        try:
            ip, port = _xor_decode(peer_b)
        except ValueError:
            self._reply_auth(sock, src, _T_CHAN_E, txn, key,
                             [_attr(_A_ERR, _err_attr(400, "Bad Request"))])
            return
        now = time.monotonic()
        alloc.chans[number] = (ip, port, now + _CHAN_SECS)
        alloc.rchan[(ip, port)] = number
        alloc.perms[ip] = now + _PERM_SECS  # bind refreshes the permission
        self._reply_auth(sock, src, _T_CHAN_R, txn, key,
                         [_attr(_A_SOFTWARE, SOFTWARE)])

    # -- indications / channel data --------------------------------------

    def _on_send(self, sock: socket.socket, self_ip: str, src: Tuple[str, int],
                 txn: bytes, attrs: List[Tuple[int, bytes]]) -> None:
        alloc = self._alloc_for(self_ip, src)
        if alloc is None:
            return
        peer_b = _first(attrs, _A_XOR_PEER)
        data_b = _first(attrs, _A_DATA)
        if peer_b is None or data_b is None:
            return
        try:
            ip, port = _xor_decode(peer_b)
        except ValueError:
            return
        if alloc.perms.get(ip, -1) < time.monotonic():
            return  # no permission for this peer: drop (RFC 5766 10.3)
        try:
            alloc.relay.sendto(data_b, (ip, port))
        except Exception:
            pass

    def _on_channel_data(self, sock: socket.socket, self_ip: str,
                         src: Tuple[str, int], data: bytes) -> None:
        if len(data) < 4:
            return
        number, length = struct.unpack("!HH", data[0:4])
        payload = data[4:4 + length]
        if len(payload) < length:
            return
        alloc = self._alloc_for(self_ip, src)
        if alloc is None:
            return
        binding = alloc.chans.get(number)
        if binding is None or binding[2] < time.monotonic():
            return
        try:
            alloc.relay.sendto(payload, (binding[0], binding[1]))
        except Exception:
            pass

    def _send_chan(self, alloc: _Alloc, channel: int, payload: bytes) -> None:
        pkt = struct.pack("!HH", channel, len(payload)) + payload
        try:
            alloc.listen.sendto(pkt, alloc.client)
        except Exception:
            pass

    def _send_data_ind(self, alloc: _Alloc, peer: Tuple[str, int], payload: bytes) -> None:
        body = (_attr(_A_XOR_PEER, _xor_encode(peer[0], peer[1]))
                + _attr(_A_DATA, payload)
                + _attr(_A_SOFTWARE, SOFTWARE))
        head = struct.pack("!HHI12s", _T_DATA, len(body), MAGIC, secrets.token_bytes(12))
        try:
            alloc.listen.sendto(head + body, alloc.client)
        except Exception:
            pass

    # -- low-level send ---------------------------------------------------

    def _reply(self, sock: socket.socket, dst: Tuple[str, int],
               mtype: int, txn: bytes, attrs: List[bytes]) -> None:
        payload = b"".join(attrs)
        try:
            sock.sendto(struct.pack("!HHI12s", mtype, len(payload), MAGIC, txn) + payload, dst)
        except Exception:
            pass

    def _reply_auth(self, sock: socket.socket, dst: Tuple[str, int],
                    mtype: int, txn: bytes, key: bytes, attrs: List[bytes]) -> None:
        try:
            sock.sendto(_sign(key, mtype, txn, b"".join(attrs)), dst)
        except Exception:
            pass
