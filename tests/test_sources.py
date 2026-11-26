import json
import tempfile
import unittest
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


class SourceTest(unittest.TestCase):
    def rows(self, name):
        return json.loads((ROOT / "data" / (name + ".json")).read_text(encoding="utf-8"))

    def test_record_ids(self):
        ids = []
        for file in (ROOT / "data").glob("*.json"):
            if file.name == "peer.json":
                continue
            ids.extend(row["id"] for row in self.rows(file.stem))
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_local_ledger_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "probe.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE probe (amount INTEGER NOT NULL)")
                db.execute("INSERT INTO probe VALUES (93)")
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT amount FROM probe").fetchone(), (93,))

    def test_links_in_exchange_records(self):
        closing = self.rows("tender")[0]["close_at"]
        first, late, other, island, _ = self.rows("bids")
        self.assertLess(first["receipt_at"], closing)
        self.assertEqual(late["receipt_at"], closing)
        self.assertEqual({p["exclusion"] for p in other["packages"]}, {"B"})
        self.assertEqual(set(island["packages"][0]["lots"]), {"S"})
        self.assertEqual(first["credential"], self.rows("disputes")[0]["orig"])
