#!/usr/bin/env python3
"""In-memory WebRTC signaling store: viewer offers <-> sharer answers.

Offers are namespaced by room: (room, viewer_id) -> sdp. The default
room is "" so old clients without a room keep working.
Thread-safe. Nothing is written to disk; everything clears on restart.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Dict, List, Tuple

_ROOM_OK = re.compile(r"[a-z0-9\-_]*")


class SignalingStore:
    """Maps (room, viewer id) pairs to pending SDP offers and answers."""

    def __init__(self, max_id_len: int = 64, max_sdp_len: int = 200000,
                 max_room_len: int = 32, sharer_timeout: int = 15) -> None:
        self._max_id_len = max_id_len
        self._max_sdp_len = max_sdp_len
        self._max_room_len = max_room_len
        self._sharer_timeout = sharer_timeout
        self._lock = threading.Lock()
        self._offers: Dict[Tuple[str, str], str] = {}
        self._answers: Dict[Tuple[str, str], str] = {}
        self._sharers: Dict[str, float] = {}  # room -> last heartbeat epoch

    def _room(self, room: object) -> str:
        name = str(room or "")
        if len(name) > self._max_room_len or _ROOM_OK.fullmatch(name) is None:
            raise ValueError("bad room (a-z 0-9 - _ only, max %d)" % self._max_room_len)
        return name

    def _check(self, viewer_id: object, sdp: object) -> tuple:
        vid = str(viewer_id or "")[: self._max_id_len]
        blob = str(sdp or "")
        if not vid or not blob:
            raise ValueError("id and sdp are required")
        if len(blob) > self._max_sdp_len:
            raise ValueError("sdp too big")
        return vid, blob

    def put_offer(self, viewer_id: object, sdp: object, room: object = "") -> Tuple[str, str]:
        """Store a viewer offer, dropping any stale answer. Returns (room, id)."""
        name = self._room(room)
        vid, blob = self._check(viewer_id, sdp)
        with self._lock:
            self._offers[(name, vid)] = blob
            self._answers.pop((name, vid), None)
        return name, vid

    def put_answer(self, viewer_id: object, sdp: object, room: object = "") -> Tuple[str, str]:
        """Store the sharer answer, consuming the offer. Returns (room, id)."""
        name = self._room(room)
        vid, blob = self._check(viewer_id, sdp)
        with self._lock:
            self._answers[(name, vid)] = blob
            self._offers.pop((name, vid), None)
        return name, vid

    def list_offers(self, room: object = "") -> List[dict]:
        """All pending offers of one room (diagnostics/fallback)."""
        name = self._room(room)
        with self._lock:
            return [{"id": vid, "sdp": blob}
                    for (rm, vid), blob in self._offers.items() if rm == name]

    def claim_offer(self, room: object = "") -> Tuple[str, str] | None:
        """Atomically pop one pending offer of a room. First claimer wins."""
        name = self._room(room)
        with self._lock:
            for key in self._offers:
                if key[0] == name:
                    vid = key[1]
                    return vid, self._offers.pop(key)
        return None

    def get_answer(self, viewer_id: object, room: object = "") -> str | None:
        vid = str(viewer_id or "")[: self._max_id_len]
        name = self._room(room)
        with self._lock:
            return self._answers.get((name, vid))

    def remove(self, viewer_id: object, room: object = "") -> None:
        vid = str(viewer_id or "")[: self._max_id_len]
        name = self._room(room)
        with self._lock:
            self._offers.pop((name, vid), None)
            self._answers.pop((name, vid), None)

    def heartbeat_sharer(self, room: object = "") -> str:
        """Mark a room as live. Returns the room name."""
        name = self._room(room)
        with self._lock:
            self._sharers[name] = time.time()
        return name

    def leave_sharer(self, room: object = "") -> None:
        try:
            name = self._room(room)
        except ValueError:
            return
        with self._lock:
            self._sharers.pop(name, None)

    def list_rooms(self) -> List[dict]:
        """Rooms with a live sharer or pending offers (stale sharers pruned)."""
        now = time.time()
        with self._lock:
            stale = [room for room, seen in self._sharers.items()
                     if now - seen > self._sharer_timeout]
            for room in stale:
                del self._sharers[room]
            pending: Dict[str, int] = {}
            for (room, _vid) in self._offers:
                pending[room] = pending.get(room, 0) + 1
            names = [room for room in self._sharers if room not in pending]
            names += list(pending)
            return [{"room": room, "live": room in self._sharers,
                     "pending": pending.get(room, 0)} for room in sorted(names)]

    def clear(self) -> None:
        with self._lock:
            self._offers.clear()
            self._answers.clear()
            self._sharers.clear()
