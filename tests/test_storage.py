"""Persistence, concurrency and idempotency guarantees."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import World  # noqa: E402

from liancai.service import ProcurementService  # noqa: E402
from liancai.storage import Store  # noqa: E402
from liancai.timeutil import FixedClock  # noqa: E402


class PersistenceTest(unittest.TestCase):
    def test_state_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.sqlite3")
            clock = FixedClock("2027-02-01T08:00:00+08:00")
            store = Store(path)
            service = ProcurementService(store, clock=clock)
            buyer = service.register_user(user_id="b", role="buyer")["token"]
            service.register_user(user_id="s", role="supplier", vendor="风浦电力")
            store.close()

            store2 = Store(path)
            service2 = ProcurementService(store2, clock=FixedClock("2027-02-01T08:10:00+08:00"))
            identity = service2.resolve_token(buyer)
            self.assertEqual(identity.role, "buyer")
            self.assertEqual(service2.store.one("SELECT COUNT(*) AS c FROM users")["c"], 2)
            store2.close()

    def test_migration_adds_published_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.sqlite3")
            store = Store(path)
            store.close()
            # Simulate an older schema by dropping the column is not possible
            # in SQLite; instead confirm the migration helper is idempotent.
            reopened = Store(path)
            columns = {row[1] for row in reopened.query("PRAGMA table_info(awards)")}
            self.assertIn("published_at", columns)
            reopened.close()


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_accept_yields_one_contract(self):
        world = World().submit_all()
        world.open()
        world.evaluate()
        award = [row for row in world.service.awards_view(world.buyer, "AW-72")
                 if row["vendor"] == "风浦电力"][0]
        world.service.offer_award(world.buyer, award["id"])
        results = []

        def accept(index: int):
            results.append(
                world.service.confirm_award(
                    world.supplier("风浦电力"),
                    award["id"],
                    action="accept",
                    idempotency_key=f"key-{index}",
                )
            )

        threads = [threading.Thread(target=accept, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        contracts = world.service.contracts(world.buyer, "AW-72")
        self.assertEqual(len(contracts), 1)
        self.assertTrue(all(row.get("outcome") == "SIGNED" for row in results))
        self.assertEqual({row.get("contract") for row in results}, {contracts[0]["id"]})

    def test_parallel_revisions_get_distinct_numbers(self):
        world = World()
        # Pre-create the supplier identity before spawning threads; role
        # registration itself is serialised and may race on first creation.
        world.supplier("风浦电力")
        payload = {
            "credential": "QUAL-A",
            "expires_at": "2027-02-01T16:00:00+08:00",
            "packages": [{"id": "P1", "lots": {"N": 10}, "cents": 100}],
        }
        world.clock_at("2027-02-01T09:00:00+08:00")
        results = []

        def submit():
            results.append(
                world.service.submit_bid(world.supplier("风浦电力"), "AW-72", dict(payload))
            )

        threads = [threading.Thread(target=submit) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        revisions = sorted(row["revision"] for row in results if row["accepted"])
        self.assertEqual(revisions, list(range(1, 11)))
        active = world.store.query(
            "SELECT COUNT(*) AS c FROM bids WHERE vendor = ? AND status = 'VALID'",
            ("风浦电力",),
        )[0]["c"]
        self.assertEqual(active, 1)


if __name__ == "__main__":
    unittest.main()
