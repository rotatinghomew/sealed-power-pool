"""Shared fixtures for the procurement service tests.

The fixed clock and explicit bid payloads reproduce the sample material in
``data/`` deterministically.  These helpers are imported by every test module.
"""

from __future__ import annotations

import json
from pathlib import Path

from liancai.service import ProcurementService


def service_token(identity) -> str:
    """Issue a fresh token for an already-registered identity (test helper)."""
    return _TOKENS[identity.id]
from liancai.storage import Store
from liancai.timeutil import FixedClock

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

_TOKENS: dict[str, str] = {}

CLOSE = "2027-02-01T10:00:00+08:00"
OPEN = "2027-02-01T10:10:00+08:00"


def load(name: str):
    return json.loads((DATA / (name + ".json")).read_text(encoding="utf-8"))


def bid_payload(row: dict) -> dict:
    """The supplier-submitted part of a bid record (no server fields)."""
    return {key: row[key] for key in ("credential", "expires_at", "packages") if key in row}


class World:
    """A bootstrapped service with buyer, evaluator and one login per vendor."""

    def __init__(self, *, at: str = "2027-02-01T08:00:00+08:00", tender: dict | None = None,
                 credentials: bool = True):
        self.clock = FixedClock(at)
        self.store = Store(":memory:")
        self.service = ProcurementService(self.store, clock=self.clock)
        _TOKENS.clear()
        self.tender_raw = tender or load("tender")[0]
        buyer = self.service.register_user(user_id="buyer-1", role="buyer", display="采购组")
        _TOKENS["buyer-1"] = buyer["token"]
        self.buyer = self.service.resolve_token(buyer["token"])
        evaluator = self.service.register_user(user_id="eval-1", role="evaluator", display="评审组")
        _TOKENS["eval-1"] = evaluator["token"]
        self.evaluator = self.service.resolve_token(evaluator["token"])
        if credentials:
            for row in load("credentials"):
                self.service.import_credential(self.buyer, row)
        self.service.create_tender(self.buyer, self.tender_raw)
        self.tokens: dict[str, str] = {}
        self.identities: dict[str, object] = {}
        # Raw tokens for HTTP callers (the Identity objects above cannot be
        # replayed into an Authorization header).
        self.tokens["buyer-1"] = service_token(self.buyer)
        self.tokens["eval-1"] = service_token(self.evaluator)

    def clock_at(self, value: str) -> "World":
        from datetime import datetime

        self.clock._value = datetime.fromisoformat(value)
        return self

    def supplier(self, vendor: str):
        if vendor not in self.identities:
            created = self.service.register_user(
                user_id=f"supplier-{len(self.identities) + 1}",
                role="supplier",
                display=vendor,
                vendor=vendor,
            )
            self.tokens[vendor] = created["token"]
            self.identities[vendor] = self.service.resolve_token(created["token"])
        return self.identities[vendor]

    def token(self, vendor: str) -> str:
        self.supplier(vendor)
        return self.tokens[vendor]

    def submit(self, row: dict, at: str):
        self.clock_at(at)
        tender_id = self.tender_raw["id"]
        return self.service.submit_bid(self.supplier(row["vendor"]), tender_id, bid_payload(row))

    def submit_all(self):
        """Submit every sample bid at its recorded receipt time."""
        for row in load("bids"):
            self.submit(row, row["receipt_at"])
        return self

    @property
    def tender_id(self) -> str:
        return self.tender_raw["id"]

    def open(self, at: str = "2027-02-01T10:15:00+08:00"):
        self.clock_at(at)
        return self.service.open_tender(self.evaluator, self.tender_id)

    def evaluate(self, at: str = "2027-02-01T10:15:00+08:00"):
        self.clock_at(at)
        return self.service.evaluate(self.evaluator, self.tender_id)

    def awards(self):
        return self.service.awards_view(self.buyer, self.tender_id)
