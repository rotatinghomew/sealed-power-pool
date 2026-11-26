"""Award optimisation and explanation tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import World  # noqa: E402

from liancai import evaluation as ev  # noqa: E402
from liancai.models import Bid, Tender  # noqa: E402


def make_tender(**overrides):
    raw = {
        "id": "T-1",
        "close_at": "2027-02-01T10:00:00+08:00",
        "open_at": "2027-02-01T10:10:00+08:00",
        "partial": True,
        "limits": {"total": 1000, "main": 1000, "island": 1000},
        "lots": [
            {"id": "N", "group": "main", "min_kwh": 100, "max_kwh": 500, "priority": 1},
            {"id": "P", "group": "main", "min_kwh": 100, "max_kwh": 500, "priority": 2},
            {"id": "S", "group": "island", "min_kwh": 100, "max_kwh": 500, "priority": 3},
        ],
    }
    raw.update(overrides)
    return Tender.parse(raw)


def make_bid(tender, bid_id, vendor, packages, *, whole=False, credential=None):
    raw = {
        "id": bid_id,
        "credential": credential or ("Q-" + vendor),
        "expires_at": "2027-02-01T18:00:00+08:00",
        "packages": packages,
        "whole": whole,
    }
    return Bid.parse(
        raw,
        tender=tender,
        receipt_at=tender.close_at.replace(hour=9),
        vendor=vendor,
    )


class OptimisationTest(unittest.TestCase):
    def test_sample_tender_plan(self):
        world = World().submit_all()
        world.open()
        evaluation = world.evaluate()
        plan = evaluation["plan"]
        self.assertEqual(plan["solution"]["volumes"], {"N": 330, "P": 310, "S": 230})
        awarded = {(row["vendor"], tuple(row["packages"])) for row in plan["awards"]}
        self.assertEqual(awarded, {("风浦电力", ("A1",)), ("坪岭商电", ("C1",))})
        self.assertEqual(plan["solution"]["total_cents"], 290000)

    def test_priority_beats_total_volume(self):
        # Budget forces a choice: the P bid fills 500 kWh for 100, the N bid
        # fills only 200 kWh for 900.  N has the higher priority, so it wins
        # even though the total traded volume is smaller.
        tender = make_tender(limits={"total": 950, "main": 950, "island": 950})
        bids = [
            make_bid(tender, "B1", "v1", [{"id": "p", "lots": {"P": 500}, "cents": 100}]),
            make_bid(tender, "B2", "v2", [{"id": "n", "lots": {"N": 200}, "cents": 900}]),
        ]
        solution = ev.solve(tender, bids)
        self.assertEqual(solution.volumes["N"], 200)
        self.assertEqual(solution.volumes["P"], 0)

    def test_partial_lot_below_minimum_is_infeasible(self):
        tender = make_tender(limits={"total": 1000, "main": 1000, "island": 1000})
        bids = [make_bid(tender, "B1", "v1", [{"id": "p", "lots": {"N": 50}, "cents": 10}])]
        solution = ev.solve(tender, bids)
        self.assertEqual(solution.volumes["N"], 0)

    def test_non_partial_requires_full_lot(self):
        tender = make_tender(partial=False)
        bids = [make_bid(tender, "B1", "v1", [{"id": "p", "lots": {"N": 500}, "cents": 10}])]
        solution = ev.solve(tender, bids)
        self.assertEqual(solution.volumes["N"], 500)

    def test_exclusion_group_at_most_one(self):
        tender = make_tender()
        bids = [
            make_bid(
                tender,
                "B1",
                "v1",
                [
                    {"id": "a", "lots": {"N": 300}, "cents": 100, "exclusion": "X"},
                    {"id": "b", "lots": {"N": 400}, "cents": 100, "exclusion": "X"},
                ],
            )
        ]
        solution = ev.solve(tender, bids)
        self.assertEqual(len(solution.selected_packages["B1"]), 1)

    def test_whole_bid_is_indivisible(self):
        tender = make_tender()
        bids = [
            make_bid(
                tender,
                "B1",
                "v1",
                [
                    {"id": "a", "lots": {"N": 200}, "cents": 100},
                    {"id": "b", "lots": {"P": 200}, "cents": 100},
                ],
                whole=True,
            )
        ]
        solution = ev.solve(tender, bids)
        self.assertEqual(set(solution.selected_packages["B1"]), {"a", "b"})
        self.assertEqual(solution.volumes["N"], 200)
        self.assertEqual(solution.volumes["P"], 200)

    def test_group_budget_is_independent(self):
        tender = make_tender(
            limits={"total": 10_000, "main": 100, "island": 10_000}
        )
        bids = [
            make_bid(tender, "B1", "v1", [{"id": "n", "lots": {"N": 300}, "cents": 200}]),
            make_bid(tender, "B2", "v2", [{"id": "s", "lots": {"S": 300}, "cents": 200}]),
        ]
        solution = ev.solve(tender, bids)
        self.assertEqual(solution.volumes["N"], 0)
        self.assertEqual(solution.volumes["S"], 300)
        self.assertEqual(solution.group_spend["island"], 200)

    def test_bundle_spanning_groups_is_allocated_without_loss(self):
        tender = make_tender()
        package = {"id": "a", "lots": {"N": 200, "S": 200}, "cents": 101}
        bid = make_bid(tender, "B1", "v1", [package])
        option = [opt for opt in ev.bid_options(bid, tender) if opt.package_ids][0]
        self.assertEqual(sum(option.group_cents.values()), 101)

    def test_tie_break_by_bid_number(self):
        tender = make_tender(partial=False)
        # Two identical, fully substitutable bids; only one can fill the N lot.
        bids = [
            make_bid(tender, "B-2", "v2", [{"id": "p", "lots": {"N": 500}, "cents": 500}]),
            make_bid(tender, "B-10", "v10", [{"id": "p", "lots": {"N": 500}, "cents": 500}]),
        ]
        solution = ev.solve(tender, bids)
        self.assertEqual(set(solution.selected_packages), {"B-10"})

    def test_explanations_name_the_blocking_constraint(self):
        world = World().submit_all()
        world.open()
        plan = world.evaluate()["plan"]
        reasons = {(row["bid"], row["package"]): row["reason"] for row in plan["unselected"]}
        self.assertTrue(all(reason for reason in reasons.values()))
        self.assertIn("LOT_CAPACITY", reasons.values())


if __name__ == "__main__":
    unittest.main()
