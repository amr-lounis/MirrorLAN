#!/usr/bin/env python3
"""Central configuration. Change defaults here, not across modules."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Tuple


def _app_dir() -> str:
    """Folder holding user data: next to the exe when frozen, else project root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_DIR = _app_dir()
BASE_DIR = APP_DIR  # backward-compatible alias


def _resource(name: str) -> str:
    """Prefer <app>/<name>, fall back to the PyInstaller bundle copy."""
    external = os.path.join(APP_DIR, name)
    if os.path.exists(external):
        return external
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidate = os.path.join(bundle, name)
        if os.path.exists(candidate):
            return candidate
    return external


@dataclass
class Config:
    """All tunable settings of the server."""

    https_port: int = 443
    http_port: int = 80
    www_dir: str = field(default_factory=lambda: _resource("www"))
    cert_file: str = field(default_factory=lambda: os.path.join(APP_DIR, "cert.pem"))
    key_file: str = field(default_factory=lambda: os.path.join(APP_DIR, "key.pem"))
    common_name: str = "MirrorLAN"
    cert_days: int = 3650
    dns_names: Tuple[str, ...] = ("localhost",)
    max_id_len: int = 64
    max_sdp_len: int = 200000
    max_room_len: int = 32  # room names: [a-z0-9-_], "" = default room
    sharer_timeout: int = 15  # seconds without heartbeat before a room drops

    def validate(self) -> None:
        """Raise ValueError if any setting is invalid."""
        for name in ("https_port", "http_port"):
            port = getattr(self, name)
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("%s must be 1-65535, got %r" % (name, port))
        if self.max_sdp_len <= 0 or self.max_id_len <= 0 or self.max_room_len <= 0:
            raise ValueError("limits must be positive")
        if self.sharer_timeout <= 0:
            raise ValueError("sharer_timeout must be positive")

    @property
    def https_suffix(self) -> str:
        """Host suffix for URLs, e.g. ':8443' or '' for default 443."""
        return "" if self.https_port == 443 else ":" + str(self.https_port)
