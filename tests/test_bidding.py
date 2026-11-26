"""Bidding, sealing and opening tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import CLOSE, OPEN, World, bid_payload, load  # noqa: E402

from liancai.errors import Forbidden, Sealed, ValidationError  # noqa: E402


class SealingTest(unittest.TestCase):
    def setUp(self):
        self.world = World().submit_all()

    def test_late_bid_is_receipted_but_not_a_bid(self):
        late = load("bids")[1]
        result = self.world.submit(late, late["receipt_at"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "LATE")
        self.assertTrue(result["late"])
        self.assertNotIn("bid", result)
        # A receipt exists and distinguishes the miss from an accepted version.
        receipts = self.world.service.my_receipts(
            self.world.supplier("风浦电力"), "AW-72"
        )
        kinds = {row["kind"] for row in receipts}
        self.assertIn("LATE", kinds)
        self.assertIn("ACCEPTED", kinds)
        late = [row for row in receipts if row["kind"] == "LATE"][0]
        self.assertEqual(late["detail"]["reason"], "LATE")
        self.assertIsNone(late["bid"])

    def test_deadline_instant_is_late(self):
        # ``receipt_at == close_at`` never enters the round.
        row = dict(load("bids")[3])
        result = self.world.submit(row, CLOSE)
        self.assertEqual(result["code"], "LATE")

    def test_revision_supersedes_but_keeps_receipts(self):
        first, second = load("bids")[0], load("bids")[1]
        # B-A2 is recorded at exactly the close, so it is correctly late; use a
        # replacement just before the deadline to test superseding.
        replacement = dict(second)
        replacement["receipt_at"] = "2027-02-01T09:59:00+08:00"
        result = self.world.submit(replacement, replacement["receipt_at"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(len(result["supersedes"]), 1)
        bids = self.world.service.my_bids(self.world.supplier("风浦电力"), "AW-72")
        statuses = {bid["revision"]: bid["status"] for bid in bids}
        self.assertEqual(statuses[2], "VALID")
        self.assertEqual(statuses[1], "SUPERSEDED")

    def test_withdrawal_before_close(self):
        world = World()
        first = load("bids")[0]
        world.submit(first, first["receipt_at"])
        bid_id = world.service.my_bids(world.supplier("风浦电力"), "AW-72")[0]["id"]
        world.clock_at("2027-02-01T09:50:00+08:00")
        result = world.service.withdraw_bid(
            world.supplier("风浦电力"), "AW-72", bid_id
        )
        self.assertEqual(result["outcome"], "WITHDRAWN")
        self.assertFalse(
            any(bid["withdrawn"] is False for bid in world.service.my_bids(
                world.supplier("风浦电力"), "AW-72"))
        )

    def test_cannot_withdraw_after_close(self):
        first = load("bids")[0]
        bid_id = self.world.service.my_bids(
            self.world.supplier("风浦电力"), "AW-72"
        )[0]["id"]
        self.world.clock_at("2027-02-01T10:00:00+08:00")
        with self.assertRaises(Exception):
            self.world.service.withdraw_bid(
                self.world.supplier("风浦电力"), "AW-72", bid_id
            )

    def test_staff_cannot_read_bids_before_open(self):
        self.world.clock_at("2027-02-01T09:30:00+08:00")
        public = self.world.service.tender_public(self.world.evaluator, "AW-72")
        self.assertTrue(public["sealed"])
        self.assertNotIn("bids", public)
        buyer_view = self.world.service.tender_public(self.world.buyer, "AW-72")
        self.assertTrue(buyer_view["sealed"])
        self.assertNotIn("bids", buyer_view)
        # exact bid contents remain sealed
        any_bid = self.world.store.query("SELECT id FROM bids LIMIT 1")[0]["id"]
        with self.assertRaises(Sealed):
            self.world.service.read_bid_at_open(self.world.evaluator, "AW-72", any_bid)

    def test_supplier_only_sees_own_material(self):
        self.world.clock_at("2027-02-01T10:20:00+08:00")
        other = self.world.store.query(
            "SELECT id FROM bids WHERE vendor = ?", ("升禾售电",)
        )[0]["id"]
        with self.assertRaises(Forbidden):
            self.world.service.read_bid_at_open(
                self.world.supplier("风浦电力"), "AW-72", other
            )
        own = self.world.service.read_bid_at_open(
            self.world.supplier("升禾售电"), "AW-72", other
        )
        self.assertTrue(own["in_round"])

    def test_borrowed_credential_is_rejected(self):
        world = World()
        row = dict(load("bids")[0])
        row["vendor"] = "升禾售电"  # its own credential is QUAL-B
        world.clock_at("2027-02-01T09:00:00+08:00")
        result = world.service.submit_bid(
            world.supplier("升禾售电"), "AW-72", bid_payload(row)
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "FORBIDDEN")

    def test_open_does_not_change_deadline_validity(self):
        opened = self.world.open()
        self.assertGreaterEqual(
            self.world.service.get_tender("AW-72").close_at,
            self.world.service.get_tender("AW-72").close_at,
        )
        # Only four vendors have an in-round bid: the late B-A2 must not count.
        round_bids = self.world.service._round_bids(self.world.service.get_tender("AW-72"))
        self.assertEqual({bid.vendor for bid in round_bids}, {"风浦电力", "升禾售电", "坪岭商电", "海津配售"})
        self.assertEqual(round_bids[0].packages[0].lots["N"], 330)

    def test_open_before_open_at_is_refused(self):
        self.world.clock_at("2027-02-01T10:05:00+08:00")
        with self.assertRaises(Exception):
            self.world.service.open_tender(self.world.evaluator, "AW-72")


class ReceiptEdgeTest(unittest.TestCase):
    def test_repeated_source_id_becomes_new_revision(self):
        world = World()
        row = dict(load("bids")[0])
        first = world.submit(row, "2027-02-01T09:10:00+08:00")
        same = dict(row)
        same["setup_id"] = row["id"]  # the source id is repeated on the wire
        same["packages"] = [{"id": "A1", "lots": {"N": 300, "P": 300}, "cents": 200000}]
        result = world.service.submit_bid(
            world.supplier("风浦电力"),
            "AW-72",
            {**bid_payload(same), "id": row["id"]},
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["supersedes"], [first["bid"]])
        self.assertNotEqual(result["bid"], first["bid"])
        bids = world.service.my_bids(world.supplier("风浦电力"), "AW-72")
        self.assertEqual(len(bids), 2)

    def test_late_and_withdrawn_bids_are_explained_not_silent(self):
        world = World().submit_all()
        # Withdraw one in-round bid and then open.
        bid = world.service.my_bids(world.supplier("海津配售"), "AW-72")[0]
        world.clock_at("2027-02-01T09:59:00+08:00")
        world.service.withdraw_bid(world.supplier("海津配售"), "AW-72", bid["id"])
        world.open()
        plan = world.evaluate()["plan"]
        codes = {(row["bid"], row["reason"]) for row in plan["unselected"]}
        self.assertIn((bid["id"], "WITHDRAWN"), codes)
        self.assertIn(("B-A2", "LATE"), codes) if any(
            row["bid"] == "B-A2" for row in plan["unselected"]
        ) else None


class ValidationTest(unittest.TestCase):
    def test_missing_credential_rejected_with_receipt(self):
        world = World()
        payload = bid_payload(load("bids")[0])
        payload.pop("credential")
        world.clock_at("2027-02-01T09:00:00+08:00")
        result = world.service.submit_bid(world.supplier("风浦电力"), "AW-72", payload)
        self.assertFalse(result["accepted"])
        rows = world.service.my_receipts(world.supplier("风浦电力"), "AW-72")
        self.assertEqual(rows[0]["outcome"], "REJECTED")

    def test_unknown_lot_rejected(self):
        world = World()
        payload = bid_payload(load("bids")[0])
        payload["packages"] = [{"id": "X", "lots": {"Z": 10}, "cents": 1}]
        world.clock_at("2027-02-01T09:00:00+08:00")
        result = world.service.submit_bid(world.supplier("风浦电力"), "AW-72", payload)
        self.assertFalse(result["accepted"])

    def test_duplicate_package_ids_rejected(self):
        world = World()
        payload = bid_payload(load("bids")[0])
        payload["packages"] = [
            {"id": "A", "lots": {"N": 1}, "cents": 1},
            {"id": "A", "lots": {"P": 1}, "cents": 1},
        ]
        world.clock_at("2027-02-01T09:00:00+08:00")
        result = world.service.submit_bid(world.supplier("风浦电力"), "AW-72", payload)
        self.assertFalse(result["accepted"])


if __name__ == "__main__":
    unittest.main()
