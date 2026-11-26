"""Award lifecycle: offers, signing, expiry, standby and idempotency."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import World  # noqa: E402

from liancai.errors import Forbidden  # noqa: E402


def sample_world() -> World:
    world = World().submit_all()
    world.open()
    world.evaluate()
    return world


def award_for(world: World, vendor: str, status: str | None = None):
    rows = world.service.awards_view(world.buyer, "AW-72")
    matches = [row for row in rows if row["vendor"] == vendor]
    if status:
        matches = [row for row in matches if row["status"] == status]
    assert len(matches) == 1, (vendor, status, matches)
    return matches[0]


class PublishedProposalTest(unittest.TestCase):
    def test_supplier_sees_proposal_before_formal_offer(self):
        world = sample_world()
        status = world.service.supplier_status(world.supplier("风浦电力"), "AW-72")
        self.assertEqual(status["phase"], "PROPOSED")
        self.assertEqual([row["status"] for row in status["awards"]], ["DRAFT"])

    def test_proposal_is_not_signable_until_offered(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        result = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="too-early"
        )
        self.assertEqual(result["code"], "NOT_OFFERED")
        self.assertEqual(world.service.contracts(world.buyer, "AW-72"), [])


class OfferTest(unittest.TestCase):
    def test_offer_sets_deadline_to_earliest_expiry(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        offered = world.service.offer_award(world.buyer, award["id"])
        self.assertEqual(offered["status"], "OFFERED")
        # B-A1 expires at 16:00; the tender has no separate valid_until.
        self.assertEqual(offered["signature_deadline"], "2027-02-01T16:00:00+08:00")

    def test_offer_after_expiry_marks_lapsed(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        world.clock_at("2027-02-01T17:00:00+08:00")
        offered = world.service.offer_award(world.buyer, award["id"])
        self.assertEqual(offered["status"], "EXPIRED")
        self.assertIsNotNone(offered["expired_at"])

    def test_cannot_accept_before_offer(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        result = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="k1"
        )
        self.assertEqual(result["code"], "NOT_OFFERED")
        self.assertNotIn("contract", result)

    def test_offer_requires_staff(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        with self.assertRaises(Forbidden):
            world.service.offer_award(world.supplier("风浦电力"), award["id"])


class SigningTest(unittest.TestCase):
    def test_accept_creates_exactly_one_contract(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        world.service.offer_award(world.buyer, award["id"])
        first = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="sign-1"
        )
        self.assertEqual(first["outcome"], "SIGNED")
        second = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="sign-1"
        )
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(second["contract"], first["contract"])
        # A *different* key on an already-signed award must not create anew.
        third = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="sign-2"
        )
        self.assertEqual(third["outcome"], "SIGNED")
        self.assertEqual(third["contract"], first["contract"])
        contracts = world.service.contracts(world.buyer, "AW-72")
        self.assertEqual(len(contracts), 1)

    def test_reject_is_distinct_from_expiry(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        world.service.offer_award(world.buyer, award["id"])
        result = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="reject", idempotency_key="no-1"
        )
        self.assertEqual(result["outcome"], "DECLINED")
        again = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="late-try"
        )
        self.assertEqual(again["code"], "ALREADY_FINAL")
        self.assertNotIn("contract", again)

    def test_expired_offer_cannot_be_accepted(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        world.service.offer_award(world.buyer, award["id"])
        world.clock_at("2027-02-01T16:00:00+08:00")
        result = world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="late"
        )
        self.assertEqual(result["outcome"], "EXPIRED")
        self.assertEqual(world.service.contracts(world.buyer, "AW-72"), [])

    def test_only_assigned_vendor_can_confirm(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        world.service.offer_award(world.buyer, award["id"])
        with self.assertRaises(Forbidden):
            world.service.confirm_award(
                world.supplier("升禾售电"), award["id"], action="accept", idempotency_key="x"
            )

    def test_idempotency_key_conflict(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        world.service.offer_award(world.buyer, award["id"])
        world.service.confirm_award(
            world.supplier("风浦电力"), award["id"], action="accept", idempotency_key="same"
        )
        with self.assertRaises(Exception):
            world.service.confirm_award(
                world.supplier("风浦电力"), award["id"], action="reject", idempotency_key="same"
            )


class StandbyTest(unittest.TestCase):
    def test_standby_only_fills_uncontracted_scope(self):
        world = sample_world()
        # Only sign the island award; the main-group scope is still open.
        island = award_for(world, "坪岭商电")
        world.service.offer_award(world.buyer, island["id"])
        world.service.confirm_award(
            world.supplier("坪岭商电"), island["id"], action="accept", idempotency_key="s1"
        )
        standing = world.service.standby(world.buyer, "AW-72")
        self.assertIn(island["id"], standing["reserved"]["signed_awards"])
        candidate_vendors = {row["vendor"] for row in standing["candidates"]}
        # The signed island is fixed; remaining main scope is re-planned.
        self.assertNotIn("坪岭商电", candidate_vendors)
        self.assertTrue(candidate_vendors)

    def test_standby_does_not_replace_signed_contract(self):
        world = sample_world()
        island = award_for(world, "坪岭商电")
        world.service.offer_award(world.buyer, island["id"])
        world.service.confirm_award(
            world.supplier("坪岭商电"), island["id"], action="accept", idempotency_key="s1"
        )
        world.service.standby(world.buyer, "AW-72")
        contracts = world.service.contracts(world.buyer, "AW-72")
        self.assertEqual([row["award"] for row in contracts], [island["id"]])
        still = award_for(world, "坪岭商电")
        self.assertEqual(still["status"], "SIGNED")

    def test_standby_respects_remaining_budget(self):
        world = sample_world()
        island = award_for(world, "坪岭商电")
        world.service.offer_award(world.buyer, island["id"])
        world.service.confirm_award(
            world.supplier("坪岭商电"), island["id"], action="accept", idempotency_key="s1"
        )
        standing = world.service.standby(world.buyer, "AW-72")
        solution = standing["reserved"]
        self.assertLessEqual(solution["total_cents"], 510000)
        self.assertEqual(solution["group_spend"]["island"], 70000)

    def test_lapsed_bid_not_offered_in_standby(self):
        world = sample_world()
        # B-C1 is the only island bid; let it lapse, then standby must not
        # propose it for signing.
        island = award_for(world, "坪岭商电")
        world.service.offer_award(world.buyer, island["id"])
        world.clock_at("2027-02-01T22:00:00+08:00")
        standing = world.service.standby(world.buyer, "AW-72")
        for row in standing["candidates"]:
            self.assertNotEqual(row["vendor"], "坪岭商电")


class HoldExpiryTest(unittest.TestCase):
    def test_hold_past_deadline_becomes_expired_not_offered(self):
        world = sample_world()
        award = award_for(world, "风浦电力")
        world.service.offer_award(world.buyer, award["id"])
        world.store.execute(
            "UPDATE awards SET status = 'HELD_FOR_REVIEW', hold_reason = 'x' WHERE id = ?",
            (award["id"],),
        )
        world.clock_at("2027-02-01T16:30:00+08:00")
        world.service.awards_view(world.buyer, "AW-72")
        world.service._refresh_holds(
            world.service.get_tender("AW-72"), world.clock.now()
        )
        row = world.store.one("SELECT status, expired_at FROM awards WHERE id = ?", (award["id"],))
        self.assertEqual(row["status"], "EXPIRED")
        self.assertIsNotNone(row["expired_at"])


if __name__ == "__main__":
    unittest.main()
