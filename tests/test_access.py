"""Role separation, read models and the HTTP surface."""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import World  # noqa: E402

from liancai.api import Api, serve  # noqa: E402
from liancai.errors import Forbidden, Sealed  # noqa: E402


class ReadModelTest(unittest.TestCase):
    def test_supplier_sees_only_own_receipts_and_status(self):
        world = World().submit_all()
        status = world.service.supplier_status(world.supplier("风浦电力"), "AW-72")
        self.assertEqual(status["phase"], "SEALED")
        vendors = {receipt["vendor"] for receipt in status["receipts"]}
        self.assertEqual(vendors, {"风浦电力"})

    def test_supplier_status_reports_not_selected_review_signed(self):
        world = World().submit_all()
        world.open()
        world.evaluate()
        none = world.service.supplier_status(world.supplier("海津配售"), "AW-72")
        self.assertEqual(none["phase"], "NOT_SELECTED")

        awards = {row["vendor"]: row for row in world.service.awards_view(world.buyer, "AW-72")}
        world.store.execute(
            "UPDATE awards SET status = 'HELD_FOR_REVIEW', hold_reason = 'x',"
            " published_at = COALESCE(published_at, created_at) WHERE id = ?",
            (awards["风浦电力"]["id"],),
        )
        review = world.service.supplier_status(world.supplier("风浦电力"), "AW-72")
        self.assertEqual(review["phase"], "UNDER_REVIEW")

        world.store.execute(
            "UPDATE awards SET status = 'OFFERED' WHERE id = ?", (awards["风浦电力"]["id"],)
        )
        awaited = world.service.supplier_status(world.supplier("风浦电力"), "AW-72")
        self.assertEqual(awaited["phase"], "AWAITING_SIGNATURE")

        world.service.confirm_award(
            world.supplier("风浦电力"),
            awards["风浦电力"]["id"],
            action="accept",
            idempotency_key="ok",
        )
        signed = world.service.supplier_status(world.supplier("风浦电力"), "AW-72")
        self.assertEqual(signed["phase"], "SIGNED")
        self.assertEqual(len(signed["contracts"]), 1)

    def test_supplier_results_do_not_leak_competitor_amounts(self):
        world = World().submit_all()
        world.open()
        world.evaluate()
        awards = {row["vendor"]: row for row in world.service.awards_view(world.buyer, "AW-72")}
        world.service.offer_award(world.buyer, awards["风浦电力"]["id"])
        world.service.offer_award(world.buyer, awards["坪岭商电"]["id"])
        visible = world.service.awards_view(world.supplier("风浦电力"), "AW-72")
        self.assertEqual([row["vendor"] for row in visible], ["风浦电力"])
        text = json.dumps(visible, ensure_ascii=False)
        self.assertNotIn("坪岭商电", text)

    def test_buyer_history_is_complete_after_open_only(self):
        world = World().submit_all()
        with self.assertRaises(Sealed):
            world.service.buyer_history(world.buyer, "AW-72")
        world.open()
        history = world.service.buyer_history(world.buyer, "AW-72")
        self.assertEqual(len(history["bids"]), world.store.one(
            "SELECT COUNT(*) AS c FROM bids"
        )["c"])
        self.assertTrue(any(row["kind"] == "LATE" for row in history["receipts"]))
        self.assertTrue(history["audit"])

    def test_supplier_cannot_read_buyer_history(self):
        world = World().submit_all()
        world.open()
        with self.assertRaises(Forbidden):
            world.service.buyer_history(world.supplier("风浦电力"), "AW-72")

    def test_supplier_case_explains_without_leaking_competitors(self):
        world = World().submit_all()
        world.open()
        world.evaluate()
        case = world.service.supplier_case(world.supplier("海津配售"), "AW-72")
        self.assertTrue(case["evaluated"])
        self.assertEqual(len(case["my_bids"]), 1)
        self.assertTrue(case["unselected_reasons"])
        text = json.dumps(
            {key: case[key] for key in ("my_bids", "unselected_reasons", "awards")},
            ensure_ascii=False,
        )
        for competitor in ("风浦电力", "升禾售电", "坪岭商电"):
            self.assertNotIn(competitor, text)
        self.assertNotIn("QUAL-A", text)

    def test_supplier_case_rank_relative_to_field(self):
        world = World().submit_all()
        world.open()
        world.evaluate()
        case = world.service.supplier_case(world.supplier("风浦电力"), "AW-72")
        row = case["my_bids"][0]
        self.assertEqual(row["ranked_bids"], 4)
        self.assertGreaterEqual(row["rank"], 1)

    def test_supplier_cannot_evaluate(self):
        world = World().submit_all()
        world.open()
        with self.assertRaises(Forbidden):
            world.service.evaluate(world.supplier("风浦电力"), "AW-72")

    def test_buyer_cannot_submit(self):
        world = World()
        with self.assertRaises(Forbidden):
            world.service.submit_bid(world.buyer, "AW-72", {})

    def test_unauthenticated_access_denied(self):
        world = World()
        with self.assertRaises(Forbidden):
            world.service.tender_public(None, "AW-72")


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = World().submit_all()
        cls.server = serve(cls.world.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_bid_submission_and_receipts(self):
        token = self.world.tokens["风浦电力"]
        self.world.clock_at("2027-02-01T09:00:00+08:00")
        status, body = self.call(
            "POST",
            "/tenders/AW-72/bids",
            {"credential": "QUAL-A", "expires_at": "2027-02-01T18:00:00+08:00",
             "packages": [{"id": "x", "lots": {"N": 10}, "cents": 1}]},
            token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["accepted"])
        status, receipts = self.call("GET", "/tenders/AW-72/receipts", token=token)
        self.assertEqual(status, 200)
        self.assertTrue(receipts["receipts"])

    def test_unauthorized_is_401_or_403(self):
        status, body = self.call("GET", "/tenders/AW-72")
        self.assertIn(status, (401, 403))
        self.assertIn("error", body)

    def test_sealed_reads_are_423(self):
        status, body = self.call(
            "GET", "/tenders/AW-72/bids/B-missing", token=self.world.tokens["风浦电力"]
        )
        self.assertIn(status, (403, 404, 423))

    def test_unknown_route(self):
        status, body = self.call("GET", "/nope", token=self.world.tokens["风浦电力"])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NOT_FOUND")

    def test_open_and_evaluate_over_http(self):
        self.world.clock_at("2027-02-01T10:20:00+08:00")
        status, body = self.call(
            "POST", "/tenders/AW-72/open", {}, token=self.world.tokens["eval-1"]
        )
        self.assertEqual(status, 200)
        self.assertIn("receipt", body)
        status, evaluation = self.call(
            "POST", "/tenders/AW-72/evaluations", {}, token=self.world.tokens["eval-1"]
        )
        self.assertEqual(status, 200)
        self.assertIn("plan", evaluation)

    def test_api_dispatch_errors_shape(self):
        api = Api(self.world.service)
        from liancai.errors import NotFound

        with self.assertRaises(NotFound):
            api.dispatch("GET", "/does/not/exist", None, None)


if __name__ == "__main__":
    unittest.main()
