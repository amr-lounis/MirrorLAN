#!/usr/bin/env python3
"""Central configuration. Change defaults here, not across modules."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field


def _app_dir() -> str:
    """Folder holding user data: next to the exe when frozen, else project root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_DIR = _app_dir()


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

    # Plain HTTP only (no TLS). Consequence, by browser design: screen
    # capture works solely from http://localhost on the sharing PC;
    # other LAN devices can watch but not share.
    port: int = 80
    turn_port: int = 3478  # TURN/UDP relay fallback (0 = disabled)
    turn_realm: str = "MirrorLAN"
    www_dir: str = field(default_factory=lambda: _resource("www"))
    max_id_len: int = 64
    max_sdp_len: int = 200000
    max_room_len: int = 32  # room names: [a-z0-9-_], "" = default room
    sharer_timeout: int = 15  # seconds without heartbeat before a room drops

    def validate(self) -> None:
        """Raise ValueError if any setting is invalid."""
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port must be 1-65535, got %r" % (self.port,))
        if not isinstance(self.turn_port, int) or not 0 <= self.turn_port <= 65535:
            raise ValueError("turn_port must be 0-65535, got %r" % (self.turn_port,))
        if self.max_sdp_len <= 0 or self.max_id_len <= 0 or self.max_room_len <= 0:
            raise ValueError("limits must be positive")
        if self.sharer_timeout <= 0:
            raise ValueError("sharer_timeout must be positive")
