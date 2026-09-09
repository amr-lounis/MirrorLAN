#!/usr/bin/env python3
"""Tests for core/signaling.py (stdlib unittest only)."""
import time
import unittest

from core.signaling import SignalingStore


class TestOfferAnswerFlow(unittest.TestCase):
    def setUp(self):
        self.store = SignalingStore()

    def test_put_claim_answer(self):
        self.store.put_offer("v-1", "offer-sdp", "room1")
        claimed = self.store.claim_offer("room1")
        self.assertEqual(claimed, ("v-1", "offer-sdp"))
        # queue is drained
        self.assertIsNone(self.store.claim_offer("room1"))
        # answer is deliverable to the same viewer
        self.store.put_answer("v-1", "answer-sdp", "room1")
        self.assertEqual(self.store.get_answer("v-1", "room1"), "answer-sdp")

    def test_put_offer_drops_stale_answer(self):
        self.store.put_offer("v-1", "offer1", "r")
        self.store.claim_offer("r")
        self.store.put_answer("v-1", "answer1", "r")
        self.assertEqual(self.store.get_answer("v-1", "r"), "answer1")
        # fresh offer = viewer is alive again -> old answer gone
        self.store.put_offer("v-1", "offer2", "r")
        self.assertIsNone(self.store.get_answer("v-1", "r"))

    def test_get_answer_missing_is_none(self):
        self.store.put_offer("v-9", "offer", "r")
        self.assertIsNone(self.store.get_answer("v-9", "r"))

    def test_rooms_are_namespaced(self):
        self.store.put_offer("v-1", "sdp-a", "room-a")
        self.store.put_offer("v-1", "sdp-b", "room-b")
        self.assertEqual(len(self.store.list_offers("room-a")), 1)
        self.assertEqual(len(self.store.list_offers("room-b")), 1)
        vid, _ = self.store.claim_offer("room-a")
        self.assertEqual(vid, "v-1")
        self.assertEqual(len(self.store.list_offers("room-a")), 0)
        self.assertEqual(len(self.store.list_offers("room-b")), 1)


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.store = SignalingStore(max_id_len=8, max_sdp_len=16,
                                    max_room_len=4, sharer_timeout=15)

    def test_bad_room_rejected(self):
        for bad in ("ROOM!", "has space", "UPPER", "a" * 5):
            with self.assertRaises(ValueError, msg=bad):
                self.store.put_offer("v-1", "sdp", bad)
            with self.assertRaises(ValueError, msg=bad):
                self.store.list_offers(bad)

    def test_default_room_ok(self):
        room, vid = self.store.put_offer("v-1", "sdp", "")
        self.assertEqual((room, vid), ("", "v-1"))

    def test_id_and_sdp_required(self):
        with self.assertRaises(ValueError):
            self.store.put_offer("", "sdp", "r")
        with self.assertRaises(ValueError):
            self.store.put_offer("v-1", "", "r")

    def test_limits_enforced(self):
        with self.assertRaises(ValueError):
            self.store.put_offer("v-too-long-id", "sdp", "r")
        with self.assertRaises(ValueError):
            self.store.put_offer("v-1", "x" * 17, "r")
        with self.assertRaises(ValueError):
            self.store.get_answer("", "r")
        with self.assertRaises(ValueError):
            self.store.remove("", "r")


class TestKnownAndDeparted(unittest.TestCase):
    def setUp(self):
        self.store = SignalingStore()

    def test_claim_known_only_serves_known(self):
        self.store.put_offer("new-guy", "sdp-new", "r")
        self.assertIsNone(self.store.claim_known("r", known=["old-guy"]))
        self.store.put_offer("old-guy", "sdp-old", "r")
        claimed = self.store.claim_known("r", known=["old-guy"])
        self.assertEqual(claimed, ("old-guy", "sdp-old"))
        # waiting queue untouched
        self.assertEqual(len(self.store.list_offers("r")), 1)

    def test_claim_known_accepts_legacy_map(self):
        self.store.put_offer("v-1", "sdp", "r")
        claimed = self.store.claim_known("r", known={"v-1": 3})
        self.assertEqual(claimed, ("v-1", "sdp"))

    def test_claim_known_empty_known_is_none(self):
        self.store.put_offer("v-1", "sdp", "r")
        self.assertIsNone(self.store.claim_known("r", known=[]))
        self.assertIsNone(self.store.claim_known("r", known=None))
        self.assertIsNone(self.store.claim_known("r", known="not-a-list"))

    def test_remove_reports_departed(self):
        self.store.put_offer("v-1", "sdp", "r")
        self.store.remove("v-1", "r")
        self.assertEqual(self.store.check_departed("r", ["v-1"]), ["v-1"])
        # unknown ids are never reported
        self.assertEqual(self.store.check_departed("r", ["someone-else"]), [])
        # other rooms are not affected
        self.assertEqual(self.store.check_departed("other", ["v-1"]), [])

    def test_fresh_offer_forgives_departure(self):
        self.store.put_offer("v-1", "sdp", "r")
        self.store.remove("v-1", "r")
        self.assertEqual(self.store.check_departed("r", ["v-1"]), ["v-1"])
        self.store.put_offer("v-1", "sdp2", "r")
        self.assertEqual(self.store.check_departed("r", ["v-1"]), [])

    def test_claim_forgives_departure(self):
        self.store.put_offer("v-1", "sdp", "r")
        self.store.remove("v-1", "r")
        self.store.put_offer("v-1", "sdp2", "r")
        self.store.remove("v-1", "r")
        self.store.put_offer("v-1", "sdp3", "r")
        self.store.claim_offer("r")
        self.assertEqual(self.store.check_departed("r", ["v-1"]), [])


class TestRooms(unittest.TestCase):
    def test_heartbeat_and_leave(self):
        store = SignalingStore(sharer_timeout=15)
        self.assertEqual(store.list_rooms(), [])
        store.heartbeat_sharer("live-room")
        rooms = {r["room"]: r for r in store.list_rooms()}
        self.assertTrue(rooms["live-room"]["live"])
        self.assertEqual(rooms["live-room"]["pending"], 0)
        store.leave_sharer("live-room")
        self.assertEqual(store.list_rooms(), [])

    def test_stale_sharer_pruned(self):
        store = SignalingStore(sharer_timeout=15)
        store.heartbeat_sharer("gone")
        # fake an old heartbeat without sleeping
        store._sharers["gone"] = time.monotonic() - 30
        self.assertEqual(store.list_rooms(), [])

    def test_pending_counts(self):
        store = SignalingStore()
        store.put_offer("v-1", "sdp", "r")
        store.put_offer("v-2", "sdp", "r")
        rooms = {r["room"]: r for r in store.list_rooms()}
        self.assertEqual(rooms["r"]["pending"], 2)

    def test_leave_sharer_bad_room_ignored(self):
        store = SignalingStore()
        store.leave_sharer("BAD ROOM!!")  # must not raise
        store.leave_sharer("never-existed")  # must not raise


class TestExpiry(unittest.TestCase):
    def test_offer_ttl_prunes_dead_viewers(self):
        import core.signaling as sig

        store = SignalingStore()
        store.put_offer("dead", "sdp", "r")
        store._seen[("r", "dead")] = time.monotonic() - (sig.OFFER_TTL + 1)
        self.assertEqual(store.list_offers("r"), [])

    def test_answer_ttl_prunes(self):
        import core.signaling as sig

        store = SignalingStore()
        store.put_offer("v-1", "sdp", "r")
        store.claim_offer("r")
        store.put_answer("v-1", "ans", "r")
        store._answer_ts[("r", "v-1")] = time.monotonic() - (sig.ANSWER_TTL + 1)
        self.assertIsNone(store.get_answer("v-1", "r"))

    def test_events_and_clear(self):
        store = SignalingStore()
        store.note("offer", "r", "v-1", "127.0.0.1", 3)
        store.note("bogus-kind-is-still-recorded", "r", "v-1", "127.0.0.1")
        events = store.recent_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["cands"], 3)
        self.assertIsNone(events[1]["cands"])
        store.put_offer("v-1", "sdp", "r")
        store.heartbeat_sharer("r")
        store.clear()
        self.assertEqual(store.list_offers("r"), [])
        self.assertEqual(store.list_rooms(), [])
        self.assertEqual(store.recent_events(), [])


if __name__ == "__main__":
    unittest.main()
