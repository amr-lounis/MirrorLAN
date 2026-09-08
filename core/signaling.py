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
from typing import Dict, List, Optional, Tuple

_ROOM_OK = re.compile(r"[a-z0-9\-_]*")

# How long an unclaimed offer / undelivered answer survives (seconds).
# Live viewers refresh their offer on every answer-poll, so only viewers
# that vanished (crash, killed tab, dead network) are ever pruned.
# Bounds memory and stops dead offers from delaying live viewers.
OFFER_TTL = 90
ANSWER_TTL = 90

# How long a recorded viewer departure is reported to sharers (seconds).
# Sharers poll every second, so this is generous; it only bounds memory.
DEPARTED_TTL = 120


class SignalingStore:
    """Maps (room, viewer id) pairs to pending SDP offers and answers."""

    def __init__(self, max_id_len: int = 64, max_sdp_len: int = 200000,
                 max_room_len: int = 32, sharer_timeout: int = 15) -> None:
        self._max_id_len = max_id_len
        self._max_sdp_len = max_sdp_len
        self._max_room_len = max_room_len
        self._sharer_timeout = sharer_timeout
        self._lock = threading.Lock()
        self._offers: Dict[Tuple[str, str], Tuple[Optional[int], str]] = {}
        self._answers: Dict[Tuple[str, str], str] = {}
        self._answer_ts: Dict[Tuple[str, str], float] = {}
        self._seen: Dict[Tuple[str, str], float] = {}  # (room, id) -> last live touch
        self._departed: Dict[Tuple[str, str, int], float] = {}  # (room, id, gen) -> left at
        self._sharers: Dict[str, float] = {}  # room -> last heartbeat epoch

    def _room(self, room: object) -> str:
        name = str(room or "")
        if len(name) > self._max_room_len or _ROOM_OK.fullmatch(name) is None:
            raise ValueError("bad room (a-z 0-9 - _ only, max %d)" % self._max_room_len)
        return name

    def _check(self, viewer_id: object, sdp: object) -> tuple:
        vid = str(viewer_id or "")
        blob = str(sdp or "")
        if not vid or not blob:
            raise ValueError("id and sdp are required")
        if len(vid) > self._max_id_len:
            raise ValueError("id too long")
        if len(blob) > self._max_sdp_len:
            raise ValueError("sdp too big")
        return vid, blob

    def _valid_id(self, viewer_id: object) -> str:
        """Viewer id, rejected (never silently truncated: two long ids
        could otherwise collide onto one slot)."""
        vid = str(viewer_id or "")
        if not vid or len(vid) > self._max_id_len:
            raise ValueError("bad id")
        return vid

    def _prune_locked(self) -> None:
        """Drop dead viewers' offers/answers. Call with _lock held."""
        now = time.monotonic()
        for key, seen in list(self._seen.items()):
            if now - seen > OFFER_TTL:
                self._offers.pop(key, None)
                self._seen.pop(key, None)
        for key, posted in list(self._answer_ts.items()):
            if now - posted > ANSWER_TTL:
                self._answers.pop(key, None)
                self._answer_ts.pop(key, None)
        for key, left in list(self._departed.items()):
            if now - left > DEPARTED_TTL:
                self._departed.pop(key, None)

    def put_offer(self, viewer_id: object, sdp: object, room: object = "",
                  gen: object = None) -> Tuple[str, str]:
        """Store a viewer offer, dropping any stale answer. Returns (room, id).

        A fresh offer proves the viewer is alive again, so any recorded
        departure for (room, id) — any generation — is forgotten.
        """
        name = self._room(room)
        vid, blob = self._check(viewer_id, sdp)
        gen = gen if isinstance(gen, int) and not isinstance(gen, bool) else None
        with self._lock:
            self._prune_locked()
            self._offers[(name, vid)] = (gen, blob)
            self._answers.pop((name, vid), None)
            self._answer_ts.pop((name, vid), None)
            self._seen[(name, vid)] = time.monotonic()
            for key in [k for k in self._departed if k[0] == name and k[1] == vid]:
                del self._departed[key]
        return name, vid

    def put_answer(self, viewer_id: object, sdp: object, room: object = "") -> Tuple[str, str]:
        """Store the sharer answer, consuming the offer. Returns (room, id)."""
        name = self._room(room)
        vid, blob = self._check(viewer_id, sdp)
        with self._lock:
            self._prune_locked()
            self._answers[(name, vid)] = blob
            self._answer_ts[(name, vid)] = time.monotonic()
            self._offers.pop((name, vid), None)
        return name, vid

    def list_offers(self, room: object = "") -> List[dict]:
        """All pending offers of one room (diagnostics/fallback)."""
        name = self._room(room)
        with self._lock:
            self._prune_locked()
            return [{"id": vid, "sdp": blob}
                    for (rm, vid), (_, blob) in self._offers.items() if rm == name]

    def claim_offer(self, room: object = "") -> Tuple[str, str, Optional[int]] | None:
        """Atomically pop one pending offer of a room.

        Returns (viewer id, sdp, generation) or None. First claimer wins.
        """
        name = self._room(room)
        with self._lock:
            self._prune_locked()
            for key in self._offers:
                if key[0] == name:
                    vid = key[1]
                    gen, blob = self._offers.pop(key)
                    return vid, blob, gen
        return None

    def check_departed(self, room: object = "", known: object = None) -> List[str]:
        """Ids from the sharer's known {id: gen} map that left the room.

        A generation mismatch means "stale leave vs. fresh connection"
        and is ignored: only an exact (id, gen) match drops a slot.
        """
        name = self._room(room)
        if not isinstance(known, dict) or not known:
            return []
        clean = {k: v for k, v in known.items()
                 if isinstance(k, str) and isinstance(v, int)
                 and not isinstance(v, bool)}
        if not clean:
            return []
        with self._lock:
            self._prune_locked()
            return [vid for (rm, vid, gen) in self._departed
                    if rm == name and clean.get(vid) == gen]

    def get_answer(self, viewer_id: object, room: object = "") -> str | None:
        vid = self._valid_id(viewer_id)
        name = self._room(room)
        with self._lock:
            self._prune_locked()
            if (name, vid) in self._offers:
                self._seen[(name, vid)] = time.monotonic()  # still waiting = still alive
            return self._answers.get((name, vid))

    def remove(self, viewer_id: object, room: object = "", gen: object = None) -> None:
        vid = self._valid_id(viewer_id)
        name = self._room(room)
        if not (isinstance(gen, int) and not isinstance(gen, bool)):
            gen = None
        with self._lock:
            self._offers.pop((name, vid), None)
            self._answers.pop((name, vid), None)
            self._answer_ts.pop((name, vid), None)
            self._seen.pop((name, vid), None)
            if gen is not None:
                # Recorded for the sharer's next poll; forgotten on the
                # viewer's next offer (put_offer) or after DEPARTED_TTL.
                self._departed[(name, vid, gen)] = time.monotonic()

    def heartbeat_sharer(self, room: object = "") -> str:
        """Mark a room as live. Returns the room name."""
        name = self._room(room)
        with self._lock:
            self._sharers[name] = time.monotonic()
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
        now = time.monotonic()  # monotonic: NTP jumps must not kill live rooms
        with self._lock:
            self._prune_locked()
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
            self._answer_ts.clear()
            self._seen.clear()
            self._departed.clear()
            self._sharers.clear()
