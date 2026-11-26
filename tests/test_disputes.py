"""Disputes, holds and re-evaluation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import World  # noqa: E402

from liancai.errors import Forbidden, NotFound  # noqa: E402


def prepared() -> World:
    world = World().submit_all()
    world.open()
    world.evaluate()
    return world


def awards_by_vendor(world: World):
    return {row["vendor"]: row for row in world.service.awards_view(world.buyer, "AW-72")}


def publish_all(world: World):
    for row in world.service.awards_view(world.buyer, "AW-72"):
        if row["status"] == "DRAFT":
            world.service.offer_award(world.buyer, row["id"])
    return awards_by_vendor(world)


DISPUTE = {
    "id": "OB-12",
    "orig": "QUAL-A",
    "affected_scope": ["N"],
    "claim": "核准期与公告窗口不符",
}


def file_sample_dispute(world: World, awards, *, target: str | None = None):
    dip = dict(DISPUTE)
    dip.update(
        {
            "target_type": "AWARD",
            "target_id": target or awards["风浦电力"]["id"],
        }
    )
    return world.service.file_dispute(world.supplier("升禾售电"), "AW-72", dip)


class DisputeScopeTest(unittest.TestCase):
    def test_competitor_cannot_contest_unpublished_material(self):
        world = prepared()
        award = awards_by_vendor(world)["风浦电力"]
        # Simulate a decision that has not yet been published to suppliers.
        world.store.execute(
            "UPDATE awards SET published_at = NULL WHERE id = ?", (award["id"],)
        )
        with self.assertRaises(Forbidden):
            world.service.file_dispute(
                world.supplier("升禾售电"),
                "AW-72",
                {
                    "orig": "QUAL-A",
                    "target_type": "AWARD",
                    "target_id": award["id"],
                    "claim": "x",
                },
            )

    def test_competitor_cannot_target_another_vendors_bid(self):
        world = prepared()
        publish_all(world)
        other_bid = world.store.query(
            "SELECT id FROM bids WHERE vendor = ?", ("风浦电力",)
        )[0]["id"]
        with self.assertRaises(Forbidden):
            world.service.file_dispute(
                world.supplier("升禾售电"),
                "AW-72",
                {"target_type": "BID", "target_id": other_bid, "claim": "x"},
            )

    def test_vendor_may_contest_own_bid(self):
        world = prepared()
        own_bid = world.store.query(
            "SELECT id FROM bids WHERE vendor = ?", ("升禾售电",)
        )[0]["id"]
        dispute = world.service.file_dispute(
            world.supplier("升禾售电"),
            "AW-72",
            {"target_type": "BID", "target_id": own_bid, "claim": "登记有误"},
        )
        self.assertEqual(dispute.who, "升禾售电")

    def test_unknown_scope_lot_rejected(self):
        world = prepared()
        publish_all(world)
        with self.assertRaises(Exception):
            world.service.file_dispute(
                world.supplier("升禾售电"),
                "AW-72",
                {
                    "target_type": "AWARD",
                    "target_id": awards_by_vendor(world)["风浦电力"]["id"],
                    "affected_scope": ["ZZ"],
                    "claim": "x",
                },
            )


class ImpactTest(unittest.TestCase):
    def test_impact_separates_affected_and_unaffected_groups(self):
        world = prepared()
        awards = publish_all(world)
        dispute = file_sample_dispute(world, awards)
        impact = world.service.dispute_impact(world.buyer, dispute.id)
        affected = {row["vendor"] for row in impact["affected_awards"]}
        unaffected = {row["vendor"] for row in impact["unaffected_awards"]}
        self.assertIn("风浦电力", affected)
        self.assertIn("坪岭商电", unaffected)
        contested = impact["contested_bids"]
        self.assertEqual({row["vendor"] for row in contested}, {"风浦电力"})
        self.assertEqual(contested[0]["labels"], ["A"])

    def test_affected_award_cannot_be_offered_or_signed(self):
        world = prepared()
        awards = publish_all(world)
        file_sample_dispute(world, awards)
        # Model a not-yet-published draft to prove the offer gate itself holds.
        world.store.execute(
            "UPDATE awards SET status = 'DRAFT', published_at = NULL WHERE id = ?",
            (awards["风浦电力"]["id"],),
        )
        with self.assertRaises(Exception):
            world.service.offer_award(world.buyer, awards["风浦电力"]["id"])
        # And a supplier cannot accept it either (it is no longer offered).
        result = world.service.confirm_award(
            world.supplier("风浦电力"),
            awards["风浦电力"]["id"],
            action="accept",
            idempotency_key="blocked",
        )
        self.assertNotEqual(result["outcome"], "SIGNED")

    def test_unaffected_island_stays_signable_and_signs(self):
        world = prepared()
        awards = publish_all(world)
        file_sample_dispute(world, awards)
        result = world.service.confirm_award(
            world.supplier("坪岭商电"),
            awards["坪岭商电"]["id"],
            action="accept",
            idempotency_key="island-1",
        )
        self.assertEqual(result["outcome"], "SIGNED")
        contracts = world.service.contracts(world.buyer, "AW-72")
        self.assertEqual({row["vendor"] for row in contracts}, {"坪岭商电"})


class ResolutionTest(unittest.TestCase):
    def test_upheld_dispute_recomputes_and_keeps_signed_contract(self):
        world = prepared()
        awards = publish_all(world)
        # Island contract signs before the dispute is resolved.
        signed = world.service.confirm_award(
            world.supplier("坪岭商电"),
            awards["坪岭商电"]["id"],
            action="accept",
            idempotency_key="island-1",
        )
        dispute = file_sample_dispute(world, awards)
        result = world.service.resolve_dispute(
            world.buyer, dispute.id, {"decision": "upheld", "notes": "核实属实"}
        )
        self.assertEqual(result["status"], "UPHELD")
        self.assertEqual(result["affected_vendor"], "风浦电力")
        self.assertIsNotNone(result["reevaluation_plan"])
        evaluation = world.service.get_evaluation(
            world.evaluator, int(result["reevaluation_plan"])
        )
        # The disqualified bid is gone from the plan and the signed contract's
        # scope is still reserved.
        self.assertIn(signed["award"], evaluation["plan"]["fixed_awards"])
        awarded_vendors = {row["vendor"] for row in evaluation["plan"]["awards"]}
        self.assertNotIn("风浦电力", awarded_vendors)
        contracts = world.service.contracts(world.buyer, "AW-72")
        self.assertEqual([row["id"] for row in contracts], [signed["contract"]])

    def test_rejected_dispute_changes_nothing(self):
        world = prepared()
        awards = publish_all(world)
        before = {(row["id"], row["status"]) for row in world.service.awards_view(world.buyer, "AW-72")}
        dispute = file_sample_dispute(world, awards)
        result = world.service.resolve_dispute(
            world.buyer, dispute.id, {"decision": "rejected", "notes": "不成立"}
        )
        self.assertEqual(result["status"], "REJECTED")
        after = {(row["id"], row["status"]) for row in world.service.awards_view(world.buyer, "AW-72")}
        self.assertEqual(before, after)

    def test_rejected_dispute_releases_hold(self):
        world = prepared()
        awards = publish_all(world)
        dispute = file_sample_dispute(world, awards)
        # Force the island award into a review hold, as if staff had paused it
        # while the dispute was open.
        world.store.execute(
            "UPDATE awards SET status = 'HELD_FOR_REVIEW', hold_reason = 'pending' WHERE id = ?",
            (awards["坪岭商电"]["id"],),
        )
        world.service.resolve_dispute(world.buyer, dispute.id, {"decision": "rejected"})
        island = awards_by_vendor(world)["坪岭商电"]
        # It had been published before the pause, so lifting the hold restores
        # the offer rather than demoting it back to draft.
        self.assertEqual(island["status"], "OFFERED")

    def test_disallowed_double_resolution(self):
        world = prepared()
        awards = publish_all(world)
        dispute = file_sample_dispute(world, awards)
        world.service.resolve_dispute(world.buyer, dispute.id, {"decision": "rejected"})
        with self.assertRaises(Exception):
            world.service.resolve_dispute(world.buyer, dispute.id, {"decision": "upheld"})

    def test_unknown_dispute(self):
        world = prepared()
        with self.assertRaises(NotFound):
            world.service.dispute_impact(world.buyer, "OB-missing")


class CredentialOnlyDisputeTest(unittest.TestCase):
    def test_upheld_claim_against_unused_credential_does_not_freeze_forever(self):
        world = World().submit_all()
        # Remove the only bid using QUAL-A so the credential is unused.
        world.store.execute(
            "UPDATE bids SET withdrawn = 1 WHERE vendor = ?", ("风浦电力",)
        )
        world.open()
        world.evaluate()
        awards = publish_all(world)
        dispute = world.service.file_dispute(
            world.supplier("升禾售电"),
            "AW-72",
            {
                "target_type": "EVALUATION",
                "target_id": str(world.service.list_evaluations(world.evaluator, "AW-72")[0]["id"]),
                "orig": "QUAL-A",
                "claim": "凭证存疑",
            },
        )
        result = world.service.resolve_dispute(
            world.buyer, dispute.id, {"decision": "upheld"}
        )
        self.assertEqual(result["status"], "UPHELD")
        self.assertIsNotNone(result["reevaluation_plan"])
        island = awards_by_vendor(world)["坪岭商电"]
        self.assertEqual(island["status"], "OFFERED")


if __name__ == "__main__":
    unittest.main()
