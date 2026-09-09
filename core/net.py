#!/usr/bin/env python3
"""Network helpers: local addresses and public URLs of this server."""
from __future__ import annotations

import socket
from typing import List


def local_ips() -> List[str]:
    """All IPv4 addresses of this machine, best route first."""
    out: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if "." in addr and not addr.startswith("127.") and addr not in out:
                out.append(addr)
    except Exception:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # no traffic sent, only route lookup
        addr = probe.getsockname()[0]
        probe.close()
        if addr not in out:
            out.insert(0, addr)
    except Exception:
        pass
    return out or ["127.0.0.1"]


def server_urls(port: int) -> List[str]:
    """Full http:// URLs clients can open, localhost first."""
    urls = ["http://localhost:%d/" % port]
    for addr in local_ips():
        urls.append("http://%s:%d/" % (addr, port))
    return urls
