"""Joint procurement service: bidding, sealed opening, awards and signing.

The service is the only place that enforces the rules; the HTTP layer is a thin
adapter and the CLI mirrors it.  Design points that matter for the business:

* **Sealing.** Before ``open_at`` nobody can list or read another vendor's bid
  content.  Even a buyer may only see aggregate counters.  Opening is a
  bookkeeping event: it never changes whether a bid was valid at the deadline.
* **Validity at deadline.** A bid is *in the round* iff ``receipt_at`` is
  strictly before ``close_at`` (a message at the closing instant is late), it
  was not withdrawn, and its latest revision at that instant is the one used.
  An invalid credential does not delete the bid: it is marked INELIGIBLE and
  remains auditable, and disputes can later prove it valid.
* **Awards vs. signatures.** An evaluation produces *draft* awards.  A draft
  can only be sent for signature when no open dispute can still change it.
  Signed contracts are fixed; standby rounds cannot overwrite them.
* **Idempotent confirmation.** Signing/declining is keyed by an idempotency
  key per award; replaying the same key never creates a second contract.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from . import evaluation as ev
from .errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    Sealed,
    ValidationError,
)
from .models import (
    ROLE_BUYER,
    ROLE_EVALUATOR,
    ROLE_SUPPLIER,
    Bid,
    Credential,
    Dispute,
    Identity,
    Package,
    Tender,
    require_id,
    require_text,
)
from .storage import Store, audit, list_audit
from .timeutil import Clock, SystemClock, format_dt, parse_dt

# Bid statuses
BID_VALID = "VALID"
BID_WITHDRAWN = "WITHDRAWN"
BID_INELIGIBLE = "INELIGIBLE"
BID_DISQUALIFIED = "DISQUALIFIED"
BID_SUPERSEDED = "SUPERSEDED"

AWARD_DRAFT = "DRAFT"
AWARD_HELD = "HELD_FOR_REVIEW"
AWARD_OFFERED = "OFFERED"
AWARD_SIGNED = "SIGNED"
AWARD_DECLINED = "DECLINED"
AWARD_EXPIRED = "EXPIRED"
AWARD_SUPERSEDED = "SUPERSEDED"
AWARD_NOT_SELECTED = "NOT_SELECTED"

DISPUTE_OPEN = "OPEN"
DISPUTE_VALID = "UPHELD"           # claim proven, affected bids disqualified
DISPUTE_REJECTED = "REJECTED"      # claim disproven, nothing changes

RECEIPT_ACCEPTED = "ACCEPTED"
RECEIPT_REVISION = "REVISION"
RECEIPT_WITHDRAWN = "WITHDRAWN"
RECEIPT_REJECTED = "REJECTED"
RECEIPT_LATE = "LATE"
RECEIPT_OPEN = "OPENED"

_TOKEN_PREFIX = "lc_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mutating(method):
    """Serialise a mutating operation on the service mutex."""

    def wrapper(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


@dataclass
class ServiceConfig:
    token_factory: Callable[[], str] = lambda: _TOKEN_PREFIX + secrets.token_urlsafe(24)


class ProcurementService:
    def __init__(self, store: Store, *, clock: Clock | None = None, config: ServiceConfig | None = None):
        self.store = store
        self.clock = clock or SystemClock()
        self.config = config or ServiceConfig()
        # Serialise mutating service calls.  SQLite storage already guards
        # individual statements; this guard makes check-then-write sequences
        # (revision numbering, award confirmation) atomic for threaded servers.
        self._mutex = threading.RLock()

    # ------------------------------------------------------------------
    # Bootstrap / identities
    # ------------------------------------------------------------------
    def register_user(
        self,
        *,
        user_id: str,
        role: str,
        display: str | None = None,
        vendor: str | None = None,
    ) -> dict:
        """Create an identity.  In production this would be an IdP hook."""
        user_id = require_id(user_id, "id")
        if role not in (ROLE_BUYER, ROLE_EVALUATOR, ROLE_SUPPLIER):
            raise ValidationError("role 必须是 buyer、evaluator 或 supplier", role=role)
        if role == ROLE_SUPPLIER:
            if not vendor:
                raise ValidationError("供应商身份必须绑定 vendor", field="vendor")
            vendor = require_text(vendor, "vendor")
        else:
            if vendor:
                raise ValidationError("非供应商身份不能绑定 vendor", field="vendor")
            vendor = None
        if self.store.one("SELECT id FROM users WHERE id = ?", (user_id,)):
            raise Conflict("身份已存在", id=user_id)
        token = self.config.token_factory()
        self.store.execute(
            "INSERT INTO users (id, role, display, vendor, token, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, role, display or user_id, vendor, _hash_token(token), format_dt(self.clock.now())),
        )
        audit(
            self.store,
            at=format_dt(self.clock.now()),
            actor=user_id,
            action="user.register",
            entity=user_id,
            detail={"role": role, "vendor": vendor},
        )
        return {"id": user_id, "role": role, "display": display or user_id, "vendor": vendor, "token": token}

    def resolve_token(self, token: str | None) -> Identity:
        if not token:
            raise Forbidden("缺少身份令牌")
        row = self.store.one(
            "SELECT * FROM users WHERE token = ?", (_hash_token(token.strip()),)
        )
        if not row:
            raise Forbidden("身份令牌无效")
        return Identity(
            id=row["id"], role=row["role"], display=row["display"], vendor=row["vendor"]
        )

    def _require(self, identity: Identity | None, *roles: str) -> Identity:
        if identity is None:
            raise Forbidden("需要身份认证")
        if identity.role not in roles:
            raise Forbidden(
                "当前身份无权执行该操作",
                required=list(roles),
                actual=identity.role,
            )
        return identity

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def import_credential(self, identity: Identity | None, raw: Any) -> dict:
        actor = self._require(identity, ROLE_BUYER)
        credential = Credential.parse(raw)
        self.store.execute(
            "INSERT INTO credentials (id, supplier, zones, active) VALUES (?, ?, ?, 1)"
            " ON CONFLICT(id) DO UPDATE SET supplier = excluded.supplier, zones = excluded.zones",
            (credential.id, credential.supplier, json.dumps(list(credential.zones), ensure_ascii=False)),
        )
        audit(
            self.store,
            at=format_dt(self.clock.now()),
            actor=actor.id,
            action="credential.import",
            entity=credential.id,
            detail=credential.as_dict(),
        )
        return credential.as_dict()

    def get_credential(self, credential_id: str) -> Credential | None:
        row = self.store.one("SELECT * FROM credentials WHERE id = ?", (credential_id,))
        if not row:
            return None
        return Credential(row["id"], row["supplier"], tuple(json.loads(row["zones"])))

    # ------------------------------------------------------------------
    # Tenders
    # ------------------------------------------------------------------
    def create_tender(self, identity: Identity | None, raw: Any) -> Tender:
        actor = self._require(identity, ROLE_BUYER)
        tender = Tender.parse(raw)
        if self.store.one("SELECT id FROM tenders WHERE id = ?", (tender.id,)):
            raise Conflict("采购编号已存在", tender=tender.id)
        self.store.execute(
            "INSERT INTO tenders (id, data, created_by, created_at) VALUES (?, ?, ?, ?)",
            (tender.id, json.dumps(tender.as_dict(), ensure_ascii=False), actor.id, format_dt(self.clock.now())),
        )
        audit(
            self.store,
            at=format_dt(self.clock.now()),
            actor=actor.id,
            action="tender.create",
            entity=tender.id,
            detail=tender.as_dict(),
        )
        return tender

    def get_tender(self, tender_id: str) -> Tender:
        row = self.store.one("SELECT * FROM tenders WHERE id = ?", (tender_id,))
        if not row:
            raise NotFound("采购公告不存在", tender=tender_id)
        raw = json.loads(row["data"])
        tender = Tender.parse(raw, tender_id=row["id"])
        return tender

    def list_tenders(self) -> list[dict]:
        rows = self.store.query("SELECT * FROM tenders ORDER BY id")
        result = []
        for row in rows:
            tender = Tender.parse(json.loads(row["data"]), tender_id=row["id"])
            status = tender.status_at(self.clock.now())
            result.append({**tender.as_dict(), "status": status})
        return result

    def tender_status(self, tender: Tender) -> str:
        return tender.status_at(self.clock.now())

    # ------------------------------------------------------------------
    # Bids: receipt, revision, withdrawal
    # ------------------------------------------------------------------
    @_mutating
    def submit_bid(
        self,
        identity: Identity | None,
        tender_id: str,
        payload: Any,
    ) -> dict:
        actor = self._require(identity, ROLE_SUPPLIER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        receipt_at = now  # receipts record arrival, never the sender's claim
        late = not (receipt_at < tender.close_at)
        raw_id = payload.get("id") if isinstance(payload, dict) else None
        # A late message still gets a receipt, but no bid row is created.
        if late:
            receipt_id = self._new_receipt_id()
            detail = {
                "reason": "LATE",
                "tender": tender.id,
                "submitted_id": raw_id,
                "server_receipt_at": format_dt(receipt_at),
                "close_at": format_dt(tender.close_at),
            }
            self.store.execute(
                "INSERT INTO receipts (id, tender, vendor, bid, kind, outcome, at, detail, actor, created_at)"
                " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    tender.id,
                    actor.vendor,
                    RECEIPT_LATE,
                    RECEIPT_LATE,
                    format_dt(receipt_at),
                    json.dumps(detail, ensure_ascii=False),
                    actor.id,
                    format_dt(now),
                ),
            )
            audit(
                self.store,
                at=format_dt(now),
                actor=actor.id,
                action="bid.rejected_late",
                entity=receipt_id,
                detail=detail,
            )
            return {
                "receipt": receipt_id,
                "outcome": RECEIPT_REJECTED,
                "code": "LATE",
                "accepted": False,
                "late": True,
                "reason": "报文在关标时刻或之后送达，不能进入本轮",
                "server_receipt_at": format_dt(receipt_at),
            }
        try:
            zone_ok = self._ensure_credential_matches(actor, payload)
            bid = self._build_bid(actor, tender, payload, receipt_at)
        except AppError as exc:
            receipt_id = self._new_receipt_id()
            detail = {"reason": exc.code, "message": exc.message, **exc.details}
            self.store.execute(
                "INSERT INTO receipts (id, tender, vendor, bid, kind, outcome, at, detail, actor, created_at)"
                " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    tender.id,
                    actor.vendor,
                    RECEIPT_REJECTED,
                    RECEIPT_REJECTED,
                    format_dt(receipt_at),
                    json.dumps(detail, ensure_ascii=False),
                    actor.id,
                    format_dt(now),
                ),
            )
            return {
                "receipt": receipt_id,
                "outcome": RECEIPT_REJECTED,
                "code": exc.code,
                "accepted": False,
                "reason": exc.message,
            }
        # Determine revision and supersede previous revisions atomically.
        previous = self.store.query(
            "SELECT * FROM bids WHERE tender = ? AND vendor = ? ORDER BY revision DESC",
            (tender.id, actor.vendor),
        )
        revision = (previous[0]["revision"] + 1) if previous else 1
        submitted_id = bid.id
        # ``_ensure_credential_matches`` already refused unknown or borrowed
        # credentials, so zone coverage is the only remaining qualification
        # question here.
        status = BID_VALID if zone_ok else BID_INELIGIBLE
        reason = None if zone_ok else "报价的时段超出该资格凭证的核准范围"
        if submitted_id and self.store.one("SELECT id FROM bids WHERE id = ?", (submitted_id,)):
            # The wire id is a source identifier and a resend may repeat it.
            # Keep the first row addressable and give the revision its own
            # internal id; the receipt records both.
            bid_id = f"{submitted_id}-r{revision}"
            if self.store.one("SELECT id FROM bids WHERE id = ?", (bid_id,)):
                bid_id = self._new_bid_id(tender.id, actor.vendor or actor.id, revision)
        else:
            bid_id = submitted_id or self._new_bid_id(tender.id, actor.vendor or actor.id, revision)
        bid = Bid(
            id=bid_id,
            tender_id=tender.id,
            vendor=actor.vendor,
            credential=bid.credential,
            revision=revision,
            receipt_at=receipt_at,
            expires_at=bid.expires_at,
            packages=bid.packages,
            withdrawn=False,
            whole=bid.whole,
            status=status,
            status_reason=reason,
            actor_id=actor.id,
        )
        with self.store.transaction() as db:
            for row in previous:
                if row["withdrawn"]:
                    continue
                db.execute(
                    "UPDATE bids SET status = ? WHERE id = ?",
                    (BID_SUPERSEDED, row["id"]),
                )
            db.execute(
                "INSERT INTO bids (id, tender, vendor, credential, revision, receipt_at, expires_at,"
                " withdrawn, whole, status, status_reason, packages, actor, content_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bid.id,
                    bid.tender_id,
                    bid.vendor,
                    bid.credential,
                    bid.revision,
                    format_dt(bid.receipt_at),
                    format_dt(bid.expires_at),
                    0,
                    int(bid.whole),
                    bid.status,
                    bid.status_reason,
                    json.dumps([p.as_dict() for p in bid.packages], ensure_ascii=False),
                    actor.id,
                    bid.content_hash(),
                    format_dt(now),
                ),
            )
        receipt_id = self._new_receipt_id()
        kind = RECEIPT_REVISION if previous else RECEIPT_ACCEPTED
        detail = {
            "revision": revision,
            "source_id": submitted_id,
            "supersedes": [row["id"] for row in previous if not row["withdrawn"]],
            "content_hash": bid.content_hash(),
            "credential_status": status,
        }
        self.store.execute(
            "INSERT INTO receipts (id, tender, vendor, bid, kind, outcome, at, detail, actor, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                tender.id,
                actor.vendor,
                bid.id,
                kind,
                RECEIPT_ACCEPTED,
                format_dt(receipt_at),
                json.dumps(detail, ensure_ascii=False),
                actor.id,
                format_dt(now),
            ),
        )
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="bid.receive",
            entity=bid.id,
            detail=detail,
        )
        return {
            "receipt": receipt_id,
            "outcome": RECEIPT_ACCEPTED,
            "accepted": True,
            "bid": bid.id,
            "revision": revision,
            "valid_at_close": True,
            "credential_status": status,
            "server_receipt_at": format_dt(receipt_at),
            "supersedes": detail["supersedes"],
            "content_hash": detail["content_hash"],
        }

    def _ensure_credential_matches(self, actor: Identity, payload: Any) -> bool:
        """Reject borrowed qualifications; report out-of-zone lots.

        An out-of-zone lot is *not* rejected at receipt time: the material is
        received and later marked INELIGIBLE at qualification review, so the
        receipt trail can show what was submitted and why it did not qualify.
        """
        if not isinstance(payload, dict):
            raise ValidationError("报价单必须是对象")
        credential_id = payload.get("credential")
        if credential_id is None:
            raise ValidationError("credential 不能为空", field="credential")
        row = self.store.one("SELECT * FROM credentials WHERE id = ?", (str(credential_id),))
        if row is None or row["supplier"] != actor.vendor:
            raise Forbidden(
                "资格凭证不属于当前供应商",
                credential=str(credential_id),
            )
        zones = set(json.loads(row["zones"]))
        packages = payload.get("packages")
        if isinstance(packages, list):
            for package in packages:
                if not isinstance(package, dict):
                    continue
                lots = package.get("lots")
                if not isinstance(lots, dict):
                    continue
                for lot_id in lots:
                    if lot_id not in zones:
                        return False
        return True

    def _build_bid(
        self, actor: Identity, tender: Tender, payload: Any, receipt_at: datetime
    ) -> Bid:
        raw = dict(payload) if isinstance(payload, dict) else payload
        return Bid.parse(
            raw,
            tender=tender,
            receipt_at=receipt_at,
            vendor=actor.vendor or actor.id,
            actor_id=actor.id,
        )

    @_mutating
    def withdraw_bid(self, identity: Identity | None, tender_id: str, bid_id: str) -> dict:
        actor = self._require(identity, ROLE_SUPPLIER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        row = self.store.one(
            "SELECT * FROM bids WHERE id = ? AND tender = ?", (bid_id, tender.id)
        )
        if not row:
            raise NotFound("报价不存在", bid=bid_id)
        if row["vendor"] != actor.vendor:
            raise Forbidden("只能撤回自己的报价")
        if row["withdrawn"]:
            raise Conflict("报价已撤回", bid=bid_id)
        if not (now < tender.close_at):
            raise Conflict("关标后不能撤回或替换报价", bid=bid_id)
        self.store.execute(
            "UPDATE bids SET withdrawn = 1, status = ? WHERE id = ?", (BID_WITHDRAWN, bid_id)
        )
        receipt_id = self._new_receipt_id()
        detail = {"bid": bid_id, "revision": row["revision"]}
        self.store.execute(
            "INSERT INTO receipts (id, tender, vendor, bid, kind, outcome, at, detail, actor, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                tender.id,
                actor.vendor,
                bid_id,
                RECEIPT_WITHDRAWN,
                RECEIPT_ACCEPTED,
                format_dt(now),
                json.dumps(detail, ensure_ascii=False),
                actor.id,
                format_dt(now),
            ),
        )
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="bid.withdraw",
            entity=bid_id,
            detail=detail,
        )
        return {
            "receipt": receipt_id,
            "outcome": RECEIPT_WITHDRAWN,
            "bid": bid_id,
            "accepted": True,
        }

    def _new_receipt_id(self) -> str:
        return "R-" + secrets.token_hex(8).upper()

    def _new_bid_id(self, tender_id: str, vendor: str, revision: int) -> str:
        digest = hashlib.sha1(f"{tender_id}|{vendor}".encode("utf-8")).hexdigest()[:10]
        return f"B-{digest}-{revision}"

    # ------------------------------------------------------------------
    # Sealed reading
    # ------------------------------------------------------------------
    def my_bids(self, identity: Identity | None, tender_id: str) -> list[dict]:
        actor = self._require(identity, ROLE_SUPPLIER)
        tender = self.get_tender(tender_id)
        rows = self.store.query(
            "SELECT * FROM bids WHERE tender = ? AND vendor = ? ORDER BY revision DESC",
            (tender.id, actor.vendor),
        )
        return [self._row_to_bid(row).as_dict() for row in rows]

    def my_receipts(self, identity: Identity | None, tender_id: str) -> list[dict]:
        actor = self._require(identity, ROLE_SUPPLIER)
        tender = self.get_tender(tender_id)
        rows = self.store.query(
            "SELECT * FROM receipts WHERE tender = ? AND vendor = ? ORDER BY created_at, rowid",
            (tender.id, actor.vendor),
        )
        return [self._receipt_dict(row) for row in rows]

    def tender_public(self, identity: Identity | None, tender_id: str) -> dict:
        """The tender view, filtered by role and timetable.

        Before ``open_at`` even staff see only aggregate counters: the whole
        point of a sealed window is that nobody can read bids early.  After it
        opens, staff can read the materials; suppliers only ever see their own.
        """
        if identity is None:
            raise Forbidden("需要身份认证")
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        status = tender.status_at(now)
        data = {"tender": tender.as_dict(), "status": status}
        rows = self.store.query("SELECT * FROM bids WHERE tender = ?", (tender.id,))
        vendors = {row["vendor"] for row in rows if not row["withdrawn"]}
        if now < tender.open_at:
            data["bid_count"] = len(vendors)
            data["sealed"] = True
            if identity.role == ROLE_SUPPLIER:
                own = [row for row in rows if row["vendor"] == identity.vendor]
                data["own_bids"] = [self._row_to_bid(row).as_dict() for row in own]
            return data
        if identity.role == ROLE_SUPPLIER:
            own = [row for row in rows if row["vendor"] == identity.vendor]
            data["own_bids"] = [self._row_to_bid(row).as_dict() for row in own]
            data["bid_count"] = len(vendors)
            return data
        data["bid_count"] = len(vendors)
        data["bids"] = [
            self._row_to_bid(row).as_dict()
            for row in sorted(rows, key=lambda item: item["id"])
            if not row["withdrawn"]
        ]
        return data

    def read_bid_at_open(self, identity: Identity | None, tender_id: str, bid_id: str) -> dict:
        """Read a bid as it stood at the deadline.

        Evaluators (and the buyer) may read every bid in the round; suppliers
        only their own.  Before opening everything is sealed, including for
        staff.  The returned ``eligible_at_close`` reflects the qualification
        status of that version, not later dispute outcomes.
        """
        actor = self._require(identity, ROLE_SUPPLIER, ROLE_EVALUATOR, ROLE_BUYER)
        tender = self.get_tender(tender_id)
        if self.clock.now() < tender.open_at:
            raise Sealed("开标前不能查阅报价内容")
        row = self.store.one("SELECT * FROM bids WHERE id = ? AND tender = ?", (bid_id, tender.id))
        if not row:
            raise NotFound("报价不存在", bid=bid_id)
        if actor.role == ROLE_SUPPLIER and row["vendor"] != actor.vendor:
            raise Forbidden("供应商只能查看自己的凭证与报价")
        bid = self._row_to_bid(row)
        round_bid = self._round_bids(tender)
        in_round = any(item.id == bid.id for item in round_bid)
        return {
            "bid": bid.as_dict(),
            "as_of": "close",
            "in_round": in_round,
            "eligible": in_round
            and bid.status in (BID_VALID, BID_SUPERSEDED)
            and bid.receipt_at < tender.close_at,
            "status_reason": bid.status_reason,
        }

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------
    @_mutating
    def open_tender(self, identity: Identity | None, tender_id: str) -> dict:
        actor = self._require(identity, ROLE_EVALUATOR, ROLE_BUYER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        if now < tender.open_at:
            raise Conflict(
                "未到公告开标时刻",
                open_at=format_dt(tender.open_at),
                now=format_dt(now),
            )
        rows = self.store.query("SELECT * FROM bids WHERE tender = ? ORDER BY id", (tender.id,))
        valid, late, withdrawn = [], [], []
        for row in rows:
            if row["withdrawn"]:
                withdrawn.append(row["id"])
            elif parse_dt(row["receipt_at"]) < tender.close_at:
                if row["status"] in (BID_VALID, BID_SUPERSEDED):
                    continue
                valid.append(row["id"])
            else:
                late.append(row["id"])
        # Confirm receipt of a duplicate "open" is harmless: record a receipt
        # event only once per tender/opening window.
        already = self.store.one(
            "SELECT id FROM receipts WHERE tender = ? AND kind = ?", (tender.id, RECEIPT_OPEN)
        )
        receipt_id = already["id"] if already else self._new_receipt_id()
        detail = {
            "open_at": format_dt(tender.open_at),
            "close_at": format_dt(tender.close_at),
            "opened_at": format_dt(now),
            "late_receipts": len(late),
            "withdrawn": len(withdrawn),
        }
        if not already:
            self.store.execute(
                "INSERT INTO receipts (id, tender, vendor, bid, kind, outcome, at, detail, actor, created_at)"
                " VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    tender.id,
                    RECEIPT_OPEN,
                    RECEIPT_ACCEPTED,
                    format_dt(now),
                    json.dumps(detail, ensure_ascii=False),
                    actor.id,
                    format_dt(now),
                ),
            )
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="tender.open",
            entity=tender.id,
            detail=detail,
        )
        return {"tender": tender.id, "receipt": receipt_id, "opened_at": format_dt(now), **detail}

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    KINDS = ("INITIAL", "REEVALUATION", "STANDBY")

    def evaluate(self, identity: Identity | None, tender_id: str, *, kind: str = "INITIAL", dispute_id: str | None = None, notes: str | None = None) -> dict:
        actor = self._require(identity, ROLE_EVALUATOR, ROLE_BUYER)
        if kind not in self.KINDS:
            raise ValidationError("kind 不合法", kind=kind)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        if now < tender.open_at:
            raise Sealed("开标前不能评审")
        evaluation_id = self._record_reevaluation(
            tender,
            actor,
            now,
            kind=kind,
            dispute_id=dispute_id,
            notes=notes,
        )
        return self.get_evaluation(actor, evaluation_id)

    @_mutating
    def _record_reevaluation(
        self,
        tender: Tender,
        actor: Identity,
        now: datetime,
        *,
        kind: str = "INITIAL",
        dispute_id: str | None = None,
        notes: str | None = None,
        reserved_rows: Sequence[sqlite3.Row] | None = None,
        advisory: bool = False,
    ) -> int:
        """Compute and persist a plan.  ``reserved_rows`` are awards that are
        already binding (signed, and for standby also outstanding offers); the
        solver treats their scope as fixed rather than re-deciding it."""
        bids = self._round_bids(tender, now=now if kind in ("STANDBY",) else None)
        ineligible = {
            bid.id: bid.status
            for bid in bids
            if bid.status in (BID_INELIGIBLE, BID_DISQUALIFIED)
        }
        eligible = [bid for bid in bids if bid.id not in ineligible]
        if reserved_rows is None:
            # Signed contracts are binding for *any* later computation: a
            # repeated evaluation may fill remaining scope but can never revoke
            # a signature.
            fixed_rows = self.store.query(
                "SELECT * FROM awards WHERE tender = ? AND status = ?",
                (tender.id, AWARD_SIGNED),
            )
        else:
            fixed_rows = list(reserved_rows)
        solve_kwargs = self._fixed_scope(tender, fixed_rows, bids)
        solution = ev.solve(tender, eligible, **solve_kwargs)
        drafts = ev.draft_awards(tender, eligible, solution)
        drafts = [d for d in drafts if self._draft_not_fixed(d, fixed_rows)]
        # A standby computation is advisory scheduling: its candidates are not
        # published as decisions until a buyer formally offers one.
        publish_at = None if kind == "STANDBY" else format_dt(now)
        explanation = ev.explain_unselected(
            tender, eligible, solution, ineligible=ineligible
        ) + self._out_of_round_reasons(tender, bids)
        ranking = ev.ranked_bids(tender, eligible, solution, ineligible=ineligible)
        with self.store.transaction() as db:
            round_no = (db.execute(
                "SELECT COALESCE(MAX(round), 0) FROM evaluations WHERE tender = ?", (tender.id,)
            ).fetchone()[0]) + 1
            plan = {
                "kind": kind,
                "bid_ids": [bid.id for bid in eligible],
                "ineligible": ineligible,
                "fixed_awards": [row["id"] for row in fixed_rows],
                "solution": {
                    "volumes": solution.volumes,
                    "group_spend": solution.group_spend,
                    "total_cents": solution.total_cents,
                    "total_kwh": solution.total_kwh,
                    "selected_packages": solution.selected_packages,
                },
                "awards": [self._draft_dict(draft) for draft in drafts],
                "unselected": explanation,
                "ranking": ranking,
                "notes": notes,
            }
            cur = db.execute(
                "INSERT INTO evaluations (tender, round, kind, created_at, created_by, dispute, status, plan)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tender.id,
                    round_no,
                    kind,
                    format_dt(now),
                    actor.id,
                    dispute_id,
                    "DRAFT",
                    json.dumps(plan, ensure_ascii=False),
                ),
            )
            evaluation_id = cur.lastrowid
            for draft in drafts:
                key = self._award_key(draft)
                existing = db.execute(
                    "SELECT id, status FROM awards WHERE tender = ? AND key = ?",
                    (tender.id, key),
                ).fetchone()
                if existing:
                    if existing["status"] == AWARD_NOT_SELECTED:
                        db.execute(
                            "UPDATE awards SET status = ?, hold_reason = NULL, evaluation = ?,"
                            " published_at = COALESCE(published_at, ?), updated_at = ? WHERE id = ?",
                            (AWARD_DRAFT, evaluation_id, publish_at, format_dt(now), existing["id"]),
                        )
                    else:
                        # A held award keeps its hold until _refresh_holds has
                        # decided whether any pending claim still blocks it;
                        # demoting it here would lose the pre-hold offer.
                        db.execute(
                            "UPDATE awards SET evaluation = ?, updated_at = ? WHERE id = ?",
                            (evaluation_id, format_dt(now), existing["id"]),
                        )
                    continue
                db.execute(
                    "INSERT INTO awards (id, tender, key, evaluation, bid, vendor, kind, package_ids,"
                    " lots, cents, labels, groups, status, created_at, updated_at, published_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._new_award_id(tender.id, key),
                        tender.id,
                        key,
                        evaluation_id,
                        draft.bid_id,
                        draft.vendor,
                        draft.kind,
                        json.dumps(list(draft.package_ids)),
                        json.dumps(draft.lots),
                        draft.cents,
                        json.dumps(list(draft.labels)),
                        json.dumps(draft.groups, sort_keys=True),
                        AWARD_DRAFT,
                        format_dt(now),
                        format_dt(now),
                        publish_at,
                    ),
                )
        if advisory:
            preserve = {
                row["id"]
                for row in self.store.query(
                    "SELECT id FROM awards WHERE tender = ? AND status != ?", (tender.id, AWARD_DRAFT)
                )
            }
        else:
            preserve = set()
        self._sync_awards(tender, evaluation_id, drafts, now, preserve=preserve)
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="tender.evaluate",
            entity=tender.id,
            detail={"evaluation": evaluation_id, "round": round_no, "kind": kind},
        )
        return evaluation_id

    def _out_of_round_reasons(self, tender: Tender, round_bids: Sequence[Bid]) -> list[dict]:
        """Explain bids that never reached evaluation (late/withdrawn/lapsed).

        They are not silently absent: a supplier asking "why wasn't I
        considered" gets the timetable fact rather than a blank.
        """
        in_round = {bid.id for bid in round_bids}
        rows: list[dict] = []
        for row in self.store.query(
            "SELECT * FROM bids WHERE tender = ? ORDER BY id", (tender.id,)
        ):
            bid = self._row_to_bid(row)
            if bid.id in in_round:
                continue
            if bid.receipt_at >= tender.close_at:
                code = "LATE"
            elif bid.withdrawn:
                code = "WITHDRAWN"
            elif bid.status == BID_SUPERSEDED:
                code = "SUPERSEDED"
            else:
                continue
            rows.append(
                {
                    "bid": bid.id,
                    "vendor": bid.vendor,
                    "package": None,
                    "reason": code,
                    "detail": ev.REASON_TEXT.get(code, code),
                }
            )
        return rows

    def _fixed_scope(self, tender: Tender, fixed_rows: Sequence[sqlite3.Row], bids: Sequence[Bid]) -> dict:
        """Turn already-binding awards into solver preconditions."""
        if not fixed_rows:
            return {}
        volumes = {lot.id: 0 for lot in tender.lots}
        group_spend: dict[str, int] = {}
        total_cents = 0
        blocked_labels: set[str] = set()
        disabled: set[tuple[str, str]] = set()
        for row in fixed_rows:
            for lot_id, qty in json.loads(row["lots"]).items():
                volumes[lot_id] = volumes.get(lot_id, 0) + qty
            total_cents += row["cents"]
            for group, amount in json.loads(row["groups"]).items():
                group_spend[group] = group_spend.get(group, 0) + amount
            blocked_labels.update(json.loads(row["labels"]))
        by_id = {bid.id: bid for bid in bids}
        for row in fixed_rows:
            for package_id in json.loads(row["package_ids"]):
                disabled.add((row["bid"], package_id))
            bid = by_id.get(row["bid"])
            if bid is not None and bid.whole:
                # A whole bid was awarded as a unit; none of it can be
                # re-offered even if the recorded packages differ.
                for package in bid.packages:
                    disabled.add((bid.id, package.id))
        return {
            "initial_volumes": volumes,
            "initial_cents": total_cents,
            "initial_group_spend": group_spend,
            "blocked_labels": blocked_labels,
            "disabled_packages": disabled,
        }

    def _draft_not_fixed(self, draft: AwardDraft, fixed_rows: Sequence[sqlite3.Row]) -> bool:
        for row in fixed_rows:
            if row["bid"] == draft.bid_id and set(json.loads(row["package_ids"])) & set(
                draft.package_ids
            ):
                return False
        return True

    def _draft_dict(self, draft: AwardDraft) -> dict:
        return {
            "key": self._award_key(draft),
            "bid": draft.bid_id,
            "vendor": draft.vendor,
            "revision": draft.revision,
            "kind": draft.kind,
            "packages": list(draft.package_ids),
            "package_detail": list(draft.packages),
            "lots": draft.lots,
            "cents": draft.cents,
            "labels": list(draft.labels),
            "groups": dict(draft.groups),
        }

    def _award_key(self, draft: AwardDraft) -> str:
        return f"{draft.bid_id}:" + ",".join(draft.package_ids)

    def _sync_awards(
        self,
        tender: Tender,
        evaluation_id: int,
        drafts: Sequence[AwardDraft],
        now: datetime,
        *,
        preserve: set[str] | None = None,
    ) -> None:
        """Reconcile stored awards with a freshly computed plan.

        ``preserve`` names awards whose status must not change: an advisory
        standby computation must not cancel a live offer merely because that
        offer's scope was treated as pre-reserved for the computation.
        """
        keys = {self._award_key(draft): draft for draft in drafts}
        preserve = preserve or set()
        rows = self.store.query("SELECT * FROM awards WHERE tender = ?", (tender.id,))
        with self.store.transaction() as db:
            for row in rows:
                if row["status"] in (AWARD_SIGNED, AWARD_DECLINED, AWARD_EXPIRED, AWARD_SUPERSEDED):
                    continue
                if row["id"] in preserve:
                    continue
                if row["key"] in keys:
                    db.execute(
                        "UPDATE awards SET evaluation = ?, updated_at = ? WHERE id = ?",
                        (evaluation_id, format_dt(now), row["id"]),
                    )
                elif row["status"] == AWARD_OFFERED:
                    # An unsigned offer can still be withdrawn when a
                    # re-evaluation removes it from the plan.  The supplier
                    # keeps the published record but cannot sign it.
                    db.execute(
                        "UPDATE awards SET status = ?, hold_reason = ?, updated_at = ? WHERE id = ?",
                        (AWARD_SUPERSEDED, "重评后不再入选，送签作废", format_dt(now), row["id"]),
                    )
                elif row["status"] == AWARD_HELD:
                    # Held awards are decided by _refresh_holds, not here.
                    continue
                elif row["status"] in (AWARD_DRAFT, AWARD_NOT_SELECTED):
                    next_status = (
                        AWARD_SUPERSEDED if row["published_at"] else AWARD_NOT_SELECTED
                    )
                    db.execute(
                        "UPDATE awards SET status = ?, hold_reason = ?, updated_at = ? WHERE id = ?",
                        (next_status, "重评后不再入选，送签作废", format_dt(now), row["id"]),
                    )

    def get_evaluation(self, identity: Identity | None, evaluation_id: int) -> dict:
        actor = self._require(identity, ROLE_EVALUATOR, ROLE_BUYER)
        row = self.store.one("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,))
        if not row:
            raise NotFound("评审记录不存在", evaluation=evaluation_id)
        plan = json.loads(row["plan"])
        return {
            "id": row["id"],
            "tender": row["tender"],
            "round": row["round"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "dispute": row["dispute"],
            "plan": plan,
        }

    def list_evaluations(self, identity: Identity | None, tender_id: str) -> list[dict]:
        actor = self._require(identity, ROLE_EVALUATOR, ROLE_BUYER)
        rows = self.store.query(
            "SELECT * FROM evaluations WHERE tender = ? ORDER BY round", (tender_id,)
        )
        return [
            {
                "id": row["id"],
                "round": row["round"],
                "kind": row["kind"],
                "status": row["status"],
                "created_at": row["created_at"],
                "dispute": row["dispute"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Award lifecycle (draft -> offered -> signed/declined/expired)
    # ------------------------------------------------------------------
    def awards_view(self, identity: Identity | None, tender_id: str) -> list[dict]:
        actor = self._require(identity, ROLE_SUPPLIER, ROLE_EVALUATOR, ROLE_BUYER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        self._expire_awards(tender, now=now)
        if actor.role == ROLE_SUPPLIER:
            if now < tender.open_at:
                # Nothing has been published yet.
                return []
            # Suppliers see awards that were published to them and have not
            # since been withdrawn from publication by a re-evaluation.
            rows = self.store.query(
                "SELECT * FROM awards WHERE tender = ? AND vendor = ?"
                " AND published_at IS NOT NULL AND status IN (?, ?, ?, ?, ?, ?, ?) ORDER BY id",
                (
                    tender.id,
                    actor.vendor,
                    AWARD_DRAFT,
                    AWARD_OFFERED,
                    AWARD_HELD,
                    AWARD_SIGNED,
                    AWARD_DECLINED,
                    AWARD_EXPIRED,
                    AWARD_SUPERSEDED,
                ),
            )
            return [self._award_public(row, tender) for row in rows]
        if now < tender.open_at:
            raise Sealed("开标前不能查看拟授标安排")
        rows = self.store.query("SELECT * FROM awards WHERE tender = ? ORDER BY id", (tender.id,))
        return [self._award_full(row, tender) for row in rows]

    @_mutating
    def offer_award(self, identity: Identity | None, award_id: str) -> dict:
        actor = self._require(identity, ROLE_BUYER, ROLE_EVALUATOR)
        award = self._get_award(award_id)
        tender = self.get_tender(award["tender"])
        now = self.clock.now()
        self._expire_awards(tender, now=now)
        award = self._get_award(award_id)
        if award["status"] not in (AWARD_DRAFT, AWARD_HELD):
            raise Conflict("只有草稿或复核中的拟授标可以送签", award=award_id, status=award["status"])
        if now < tender.open_at:
            raise Sealed("开标前不能送签")
        blocking = self._blocking_disputes(tender, award)
        if blocking:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE awards SET status = ?, hold_reason = ?, updated_at = ? WHERE id = ?",
                    (
                        AWARD_HELD,
                        "存在可能改变本拟授标的未决异议",
                        format_dt(now),
                        award_id,
                    ),
                )
            raise Conflict(
                "存在可能影响本拟授标的未决异议，不能送签",
                award=award_id,
                disputes=[d.id for d in blocking],
            )
        deadline = self._signature_deadline(tender, award)
        if now >= deadline:
            # The validity window has already closed; sending the award would
            # invite an acceptance that can never be honoured.
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE awards SET status = ?, hold_reason = ?, signature_deadline = ?,"
                    " expired_at = ?, updated_at = ? WHERE id = ?",
                    (
                        AWARD_EXPIRED,
                        "报价有效期在送签前已届满",
                        format_dt(deadline),
                        format_dt(now),
                        format_dt(now),
                        award_id,
                    ),
                )
            audit(
                self.store,
                at=format_dt(now),
                actor=actor.id,
                action="award.offer_expired",
                entity=award_id,
                detail={"deadline": format_dt(deadline)},
            )
            return self._award_full(self._get_award(award_id), tender)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE awards SET status = ?, hold_reason = NULL, signature_deadline = ?,"
                " published_at = COALESCE(published_at, ?), updated_at = ? WHERE id = ?",
                (AWARD_OFFERED, format_dt(deadline), format_dt(now), format_dt(now), award_id),
            )
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="award.offer",
            entity=award_id,
            detail={"signature_deadline": format_dt(deadline)},
        )
        return self._award_full(self._get_award(award_id), tender)

    def _signature_deadline(self, tender: Tender, award: sqlite3.Row) -> datetime:
        """Earlier of the bid's validity and the tender's award validity."""
        bid = self.store.one("SELECT * FROM bids WHERE id = ?", (award["bid"],))
        if bid is None:
            raise NotFound("拟授标引用的报价不存在", award=award["id"])
        deadline = parse_dt(bid["expires_at"])
        if tender.valid_until is not None and tender.valid_until < deadline:
            deadline = tender.valid_until
        return deadline

    @_mutating
    def confirm_award(
        self,
        identity: Identity | None,
        award_id: str,
        *,
        action: str,
        idempotency_key: str | None,
    ) -> dict:
        actor = self._require(identity, ROLE_SUPPLIER)
        if action not in ("accept", "reject"):
            raise ValidationError("action 必须是 accept 或 reject", field="action")
        if not idempotency_key:
            raise ValidationError("必须提供幂等键 idempotency_key", field="idempotency_key")
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValidationError("幂等键长度不合法", field="idempotency_key")
        now = self.clock.now()
        award = self._get_award(award_id)
        if award["vendor"] != actor.vendor:
            raise Forbidden("只能确认发给自己的授标")
        tender = self.get_tender(award["tender"])
        prior = self.store.one(
            "SELECT * FROM award_attempts WHERE award = ? AND idempotency_key = ?",
            (award_id, idempotency_key),
        )
        if prior:
            if prior["action"] != action:
                raise Conflict(
                    "同一幂等键已用于不同动作",
                    award=award_id,
                    previous=prior["action"],
                )
            replay = json.loads(prior["response"])
            replay["idempotent_replay"] = True
            return replay
        self._expire_awards(tender, now=now)
        award = self._get_award(award_id)
        if award["status"] in (AWARD_DECLINED, AWARD_EXPIRED, AWARD_SIGNED):
            # Record the attempt (without creating a contract) so repeated
            # confirmations are explainable and idempotent.
            outcome = award["status"]
            response = {
                "award": award_id,
                "outcome": outcome,
                "code": "ALREADY_FINAL",
                "idempotent_replay": False,
                "message": "该授标已处于终态，不能再确认",
            }
            if outcome == AWARD_SIGNED:
                # Point at the existing contract: the caller must be able to
                # see the executable result, but no second one is created.
                contract = self.store.one(
                    "SELECT id FROM contracts WHERE award = ?", (award_id,)
                )
                if contract:
                    response["contract"] = contract["id"]
            self._record_attempt(award, actor, idempotency_key, action, outcome, response, now)
            return response
        if award["status"] == AWARD_HELD:
            response = {
                "award": award_id,
                "outcome": AWARD_HELD,
                "code": "HELD_FOR_REVIEW",
                "idempotent_replay": False,
                "message": "该授标处于复核状态，暂不能确认",
            }
            self._record_attempt(award, actor, idempotency_key, action, AWARD_HELD, response, now)
            return response
        if award["status"] != AWARD_OFFERED:
            response = {
                "award": award_id,
                "outcome": award["status"],
                "code": "NOT_OFFERED",
                "idempotent_replay": False,
                "message": "该授标尚未正式送签",
            }
            self._record_attempt(award, actor, idempotency_key, action, award["status"], response, now)
            return response
        blocking = self._blocking_disputes(tender, award)
        if blocking:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE awards SET status = ?, hold_reason = ?, updated_at = ? WHERE id = ?",
                    (AWARD_HELD, "异议复核导致送签暂停", format_dt(now), award_id),
                )
            response = {
                "award": award_id,
                "outcome": AWARD_HELD,
                "code": "HELD_FOR_REVIEW",
                "idempotent_replay": False,
                "message": "异议复核未完成，暂不能确认",
                "disputes": [d.id for d in blocking],
            }
            self._record_attempt(award, actor, idempotency_key, action, AWARD_HELD, response, now)
            return response
        if action == "reject":
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE awards SET status = ?, declined_at = ?, updated_at = ? WHERE id = ?",
                    (AWARD_DECLINED, format_dt(now), format_dt(now), award_id),
                )
            response = {
                "award": award_id,
                "outcome": AWARD_DECLINED,
                "code": "DECLINED",
                "idempotent_replay": False,
            }
            self._record_attempt(award, actor, idempotency_key, action, AWARD_DECLINED, response, now)
            audit(self.store, at=format_dt(now), actor=actor.id, action="award.decline", entity=award_id, detail={})
            return response
        # Accept: create exactly one contract.
        contract_id = self._new_contract_id(award)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE awards SET status = ?, signed_at = ?, updated_at = ? WHERE id = ?",
                (AWARD_SIGNED, format_dt(now), format_dt(now), award_id),
            )
            db.execute(
                "INSERT INTO contracts (id, tender, award, vendor, bid, package_ids, lots, cents, signed_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    contract_id,
                    award["tender"],
                    award_id,
                    award["vendor"],
                    award["bid"],
                    award["package_ids"],
                    award["lots"],
                    award["cents"],
                    format_dt(now),
                    "ACTIVE",
                ),
            )
        response = {
            "award": award_id,
            "outcome": AWARD_SIGNED,
            "code": "SIGNED",
            "contract": contract_id,
            "idempotent_replay": False,
            "signed_at": format_dt(now),
        }
        self._record_attempt(award, actor, idempotency_key, action, AWARD_SIGNED, response, now)
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="award.accept",
            entity=award_id,
            detail={"contract": contract_id},
        )
        return response

    def _record_attempt(self, award, actor, key, action, outcome, response, now) -> None:
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO award_attempts (id, award, vendor, idempotency_key, action, outcome, response, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "A-" + secrets.token_hex(8).upper(),
                    award["id"],
                    actor.vendor,
                    key,
                    action,
                    outcome,
                    json.dumps(response, ensure_ascii=False),
                    format_dt(now),
                ),
            )
            db.execute(
                "UPDATE awards SET attempt_count = attempt_count + 1 WHERE id = ?",
                (award["id"],),
            )

    def _expire_awards(self, tender: Tender, *, now: datetime) -> None:
        """Lapse every unsigned award whose signature window has closed.

        Holds are included: a paused award cannot be revived after its own
        validity or the tender's award validity has passed.
        """
        rows = self.store.query(
            "SELECT * FROM awards WHERE tender = ? AND status IN (?, ?)",
            (tender.id, AWARD_OFFERED, AWARD_HELD),
        )
        with self.store.transaction() as db:
            for row in rows:
                if row["published_at"] is None and row["status"] == AWARD_HELD:
                    continue
                deadline = row["signature_deadline"]
                if deadline and now >= parse_dt(deadline):
                    db.execute(
                        "UPDATE awards SET status = ?, hold_reason = NULL, expired_at = ?,"
                        " updated_at = ? WHERE id = ?",
                        (AWARD_EXPIRED, format_dt(now), format_dt(now), row["id"]),
                    )

    # ------------------------------------------------------------------
    # Disputes, re-evaluation and standby
    # ------------------------------------------------------------------
    @_mutating
    def file_dispute(self, identity: Identity | None, tender_id: str, payload: Any) -> Dispute:
        actor = self._require(identity, ROLE_SUPPLIER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        if now < tender.open_at:
            raise Sealed("开标前不能提出异议")
        if not isinstance(payload, dict):
            raise ValidationError("异议必须是对象")
        target_type = payload.get("target_type")
        target_id = payload.get("target_id")
        if target_type not in ("AWARD", "BID", "EVALUATION"):
            raise ValidationError("target_type 必须是 AWARD、BID 或 EVALUATION", field="target_type")
        target_id = require_id(target_id, "target_id")
        self._check_dispute_scope(actor, tender, target_type, target_id)
        claim = payload.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise ValidationError("必须说明异议理由", field="claim")
        orig = payload.get("orig")
        orig = require_id(orig, "orig") if orig is not None else None
        if orig is not None:
            credential = self.get_credential(orig)
            if credential is None:
                raise NotFound("被质疑的资格凭证不存在", orig=orig)
        scope = payload.get("affected_scope") or []
        if not isinstance(scope, list):
            raise ValidationError("affected_scope 必须是数组", field="affected_scope")
        scope = tuple(require_id(item, "affected_scope") for item in scope)
        unknown = [lot for lot in scope if lot not in tender.lot_map]
        if unknown:
            raise ValidationError("affected_scope 含未知时段", lots=unknown)
        dispute_id = payload.get("id")
        dispute_id = require_id(dispute_id, "id") if dispute_id else self._new_dispute_id(tender.id)
        if self.store.one("SELECT id FROM disputes WHERE id = ?", (dispute_id,)):
            raise Conflict("异议编号已存在", dispute=dispute_id)
        dispute = Dispute(
            id=dispute_id,
            tender_id=tender.id,
            who=actor.vendor or actor.id,
            target_type=target_type,
            target_id=target_id,
            orig=orig,
            affected_scope=scope,
            claim=claim.strip(),
            filed_at=now,
            status=DISPUTE_OPEN,
            actor_id=actor.id,
        )
        self.store.execute(
            "INSERT INTO disputes (id, tender, who, actor, target_type, target_id, orig,"
            " affected_scope, claim, filed_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dispute.id,
                tender.id,
                dispute.who,
                actor.id,
                target_type,
                target_id,
                orig,
                json.dumps(list(scope)),
                claim.strip(),
                format_dt(now),
                DISPUTE_OPEN,
            ),
        )
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="dispute.file",
            entity=dispute.id,
            detail={"target": target_id, "orig": orig, "scope": list(scope)},
        )
        # Publish the consequence immediately: awards this claim could change
        # move to review, while genuinely independent groups keep their status.
        self._refresh_holds(tender, now)
        return dispute

    def _check_dispute_scope(self, actor: Identity, tender: Tender, target_type: str, target_id: str) -> None:
        """A competitor may only contest published decisions or own material."""
        if target_type == "AWARD":
            row = self.store.one("SELECT * FROM awards WHERE id = ? AND tender = ?", (target_id, tender.id))
            if not row:
                raise NotFound("拟授标不存在", award=target_id)
            if row["published_at"] is None:
                raise Forbidden("异议只能针对已经公开的判定")
        elif target_type == "EVALUATION":
            row = self.store.one("SELECT * FROM evaluations WHERE id = ? AND tender = ?", (target_id, tender.id))
            if not row:
                raise NotFound("评审记录不存在", evaluation=target_id)
        else:  # BID
            row = self.store.one("SELECT * FROM bids WHERE id = ? AND tender = ?", (target_id, tender.id))
            if not row:
                raise NotFound("报价不存在", bid=target_id)
            if row["vendor"] != actor.vendor:
                # Competitor bid content is not public; only the vendor's own
                # submitted material may be contested by that vendor.
                raise Forbidden("不能针对未公开的竞争方材料提出异议")

    @_mutating
    def resolve_dispute(self, identity: Identity | None, dispute_id: str, payload: Any) -> dict:
        actor = self._require(identity, ROLE_BUYER)
        now = self.clock.now()
        row = self.store.one("SELECT * FROM disputes WHERE id = ?", (dispute_id,))
        if not row:
            raise NotFound("异议不存在", dispute=dispute_id)
        if row["status"] != DISPUTE_OPEN:
            raise Conflict("异议已处理", dispute=dispute_id, status=row["status"])
        if not isinstance(payload, dict):
            raise ValidationError("处理结果必须是对象")
        decision = payload.get("decision")
        if decision not in ("upheld", "rejected"):
            raise ValidationError("decision 必须是 upheld 或 rejected", field="decision")
        notes = payload.get("notes")
        tender = self.get_tender(row["tender"])
        status = DISPUTE_VALID if decision == "upheld" else DISPUTE_REJECTED
        affected_bid_id = None
        affected_scope = tuple(json.loads(row["affected_scope"]))
        affected_vendor = None
        evaluation_id = None
        if decision == "upheld":
            affected_bid_id, affected_vendor = self._disqualify_affected(
                tender, row, affected_scope, now
            )
            # Recompute immediately from the corrected eligibility: the
            # qualification failure must land before anyone can sign the old
            # plan.  This happens even when no bid used the credential, so the
            # dispute is recorded as settled rather than staying forever
            # "pending" and freezing unrelated awards.
            evaluation_id = self._record_reevaluation(
                tender,
                actor,
                now,
                kind="REEVALUATION",
                dispute_id=dispute_id,
                notes=f"异议 {dispute_id} 成立",
            )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE disputes SET status = ?, resolution = ?, resolved_at = ?,"
                " reevaluation_plan = ? WHERE id = ?",
                (
                    status,
                    (notes or decision).strip() if isinstance(notes, str) else decision,
                    format_dt(now),
                    str(evaluation_id) if evaluation_id else None,
                    dispute_id,
                ),
            )
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="dispute.resolve",
            entity=dispute_id,
            detail={
                "decision": decision,
                "affected_bid": affected_bid_id,
                "scope": list(affected_scope),
            },
        )
        result = self._dispute_dict(self.store.one("SELECT * FROM disputes WHERE id = ?", (dispute_id,)))
        result["affected_bid"] = affected_bid_id
        result["affected_vendor"] = affected_vendor
        # Every other group must not stay frozen just because it shares the
        # tender number: recompute and lift holds that the change no longer
        # touches, but keep holds if the change can still alter that award.
        self._refresh_holds(tender, now)
        return result

    def _disqualify_affected(
        self, tender: Tender, dispute_row: sqlite3.Row, scope: tuple[str, ...], now: datetime
    ) -> tuple[str | None, str | None]:
        """Mark the bid whose credential was contested as disqualified.

        A qualification failure makes the whole bid unavailable: packages are
        indivisible, so a defect on any declared lot cannot leave the rest of
        the bundle selectable.  The returned id/vendor let the caller record
        which exclusion group and which awards must be re-evaluated.
        """
        orig = dispute_row["orig"]
        bid = None
        if orig:
            rows = self.store.query(
                "SELECT * FROM bids WHERE tender = ? AND credential = ? AND withdrawn = 0"
                " ORDER BY revision DESC",
                (tender.id, orig),
            )
            if rows:
                bid = rows[0]
        elif dispute_row["target_type"] == "BID":
            bid = self.store.one(
                "SELECT * FROM bids WHERE id = ? AND tender = ?",
                (dispute_row["target_id"], tender.id),
            )
        elif dispute_row["target_type"] == "AWARD":
            award = self.store.one(
                "SELECT * FROM awards WHERE id = ? AND tender = ?",
                (dispute_row["target_id"], tender.id),
            )
            if award is not None:
                bid = self.store.one("SELECT * FROM bids WHERE id = ?", (award["bid"],))
        if bid is None:
            return None, None
        reason = f"异议 {dispute_row['id']} 成立"
        # Disqualify every revision of that vendor's bid: only the round
        # revision matters for selection, but the audit must be consistent.
        with self.store.transaction() as db:
            db.execute(
                "UPDATE bids SET status = ?, status_reason = ?"
                " WHERE tender = ? AND vendor = ?",
                (BID_DISQUALIFIED, reason, tender.id, bid["vendor"]),
            )
        return bid["id"], bid["vendor"]

    def reevaluate(
        self,
        identity: Identity | None,
        tender_id: str,
        *,
        dispute_id: str | None = None,
        reason: str | None = None,
    ) -> dict:
        actor = self._require(identity, ROLE_EVALUATOR, ROLE_BUYER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        if now < tender.open_at:
            raise Sealed("开标前不能评审")
        if dispute_id:
            dispute = self.store.one(
                "SELECT * FROM disputes WHERE id = ? AND tender = ?", (dispute_id, tender.id)
            )
            if not dispute:
                raise NotFound("异议不存在", dispute=dispute_id)
            if dispute["status"] == DISPUTE_OPEN:
                raise Conflict("异议尚未核实，不能据其重评", dispute=dispute_id)
            if dispute["reevaluation_plan"]:
                raise Conflict(
                    "该异议已记录过重评结果，请直接查看其评审记录",
                    dispute=dispute_id,
                    evaluation=dispute["reevaluation_plan"],
                )
        evaluation_id = self._record_reevaluation(
            tender,
            actor,
            now,
            kind="REEVALUATION",
            dispute_id=dispute_id,
            notes=reason,
        )
        self._refresh_holds(tender, now)
        if dispute_id:
            self.store.execute(
                "UPDATE disputes SET status = ?, reevaluation_plan = ? WHERE id = ?",
                (DISPUTE_VALID, str(evaluation_id), dispute_id),
            )
        result = self.get_evaluation(actor, evaluation_id)
        audit(
            self.store,
            at=format_dt(now),
            actor=actor.id,
            action="tender.reevaluate",
            entity=tender.id,
            detail={"evaluation": evaluation_id, "dispute": dispute_id, "reason": reason},
        )
        return result

    @_mutating
    def standby(self, identity: Identity | None, tender_id: str) -> dict:
        """Offer the next feasible scope without touching signed contracts.

        Signed awards and outstanding offers hold their scope (budget, volume,
        exclusion labels and packages).  Candidates must still be within the
        remaining budget, respect indivisible bundles and the vendors' mutual
        exclusions, and their bids must not have lapsed.
        """
        actor = self._require(identity, ROLE_BUYER, ROLE_EVALUATOR)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        if now < tender.open_at:
            raise Sealed("开标前不能安排候补")
        self._expire_awards(tender, now=now)
        signed_rows = self.store.query(
            "SELECT * FROM awards WHERE tender = ? AND status = ?", (tender.id, AWARD_SIGNED)
        )
        offered_rows = self.store.query(
            "SELECT * FROM awards WHERE tender = ? AND status = ?", (tender.id, AWARD_OFFERED)
        )
        reserved = list(signed_rows) + list(offered_rows)
        evaluation_id = self._record_reevaluation(
            tender,
            actor,
            now,
            kind="STANDBY",
            notes="候补安排",
            reserved_rows=reserved,
            advisory=True,
        )
        evaluation = self.get_evaluation(actor, evaluation_id)
        live_keys = {
            row["key"]
            for row in self.store.query(
                "SELECT key FROM awards WHERE tender = ? AND status IN (?, ?, ?)",
                (tender.id, AWARD_OFFERED, AWARD_HELD, AWARD_SIGNED),
            )
        }
        candidates = [
            row for row in evaluation["plan"]["awards"] if row["key"] not in live_keys
        ]
        expiring = [
            {
                "award": row["id"],
                "vendor": row["vendor"],
                "bid": row["bid"],
                "expires_at": (
                    self.store.one("SELECT expires_at FROM bids WHERE id = ?", (row["bid"],))
                    or {"expires_at": None}
                )["expires_at"],
                "signature_deadline": row["signature_deadline"],
            }
            for row in offered_rows
        ]
        reserved_volumes = {lot.id: 0 for lot in tender.lots}
        reserved_group: dict[str, int] = {}
        reserved_cents = 0
        for row in reserved:
            for lot_id, qty in json.loads(row["lots"]).items():
                reserved_volumes[lot_id] = reserved_volumes.get(lot_id, 0) + qty
            reserved_cents += row["cents"]
            for group, amount in json.loads(row["groups"]).items():
                reserved_group[group] = reserved_group.get(group, 0) + amount
        return {
            "tender": tender.id,
            "evaluation": evaluation_id,
            "reserved": {
                "volumes": reserved_volumes,
                "total_cents": reserved_cents,
                "group_spend": reserved_group,
                "signed_awards": [row["id"] for row in signed_rows],
                "offered_awards": [row["id"] for row in offered_rows],
            },
            "projected": evaluation["plan"]["solution"],
            "candidates": candidates,
            "unselected": evaluation["plan"]["unselected"],
            "expiring_offers": expiring,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _round_bids(self, tender: Tender, *, now: datetime | None = None) -> list[Bid]:
        """Bids in the round, one per vendor, as they stood at the deadline.

        The vendor's highest revision received *before* the deadline is the
        version in the round.  If that version was withdrawn, the vendor has no
        bid: falling back to an older superseded revision would let a
        withdrawal silently resurrect earlier material.

        ``now`` additionally drops bids whose validity has lapsed; standby
        rounds pass the current time because a lapsed bid can no longer be
        signed.  Opening itself never filters by expiry: it only replays the
        deadline state.
        """
        rows = self.store.query("SELECT * FROM bids WHERE tender = ? ORDER BY id", (tender.id,))
        latest: dict[str, Bid] = {}
        for row in rows:
            bid = self._row_to_bid(row)
            if bid.receipt_at >= tender.close_at:
                continue
            current = latest.get(bid.vendor)
            if current is None or bid.revision > current.revision:
                latest[bid.vendor] = bid
        kept = [bid for bid in latest.values() if not bid.withdrawn]
        if now is not None:
            kept = [bid for bid in kept if bid.expires_at > now]
        return sorted(kept, key=lambda bid: bid.id)

    def _get_award(self, award_id: str) -> sqlite3.Row:
        row = self.store.one("SELECT * FROM awards WHERE id = ?", (award_id,))
        if not row:
            raise NotFound("拟授标不存在", award=award_id)
        return row

    def _row_to_bid(self, row: sqlite3.Row) -> Bid:
        packages = tuple(Package(**item) for item in json.loads(row["packages"]))
        return Bid(
            id=row["id"],
            tender_id=row["tender"],
            vendor=row["vendor"],
            credential=row["credential"],
            revision=row["revision"],
            receipt_at=parse_dt(row["receipt_at"]),
            expires_at=parse_dt(row["expires_at"]),
            packages=packages,
            withdrawn=bool(row["withdrawn"]),
            whole=bool(row["whole"]),
            status=row["status"],
            status_reason=row["status_reason"],
            actor_id=row["actor"],
        )

    def _receipt_dict(self, row: sqlite3.Row) -> dict:
        return {
            "receipt": row["id"],
            "tender": row["tender"],
            "vendor": row["vendor"],
            "bid": row["bid"],
            "kind": row["kind"],
            "outcome": row["outcome"],
            "at": row["at"],
            "detail": json.loads(row["detail"]) if row["detail"] else None,
        }

    def _dispute_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "tender": row["tender"],
            "who": row["who"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "orig": row["orig"],
            "affected_scope": json.loads(row["affected_scope"]),
            "claim": row["claim"],
            "filed_at": row["filed_at"],
            "status": row["status"],
            "resolution": row["resolution"],
            "resolved_at": row["resolved_at"],
            "reevaluation_plan": row["reevaluation_plan"],
        }

    def _award_full(self, row: sqlite3.Row, tender: Tender) -> dict:
        return self._award_public(row, tender, staff=True)

    def _award_public(self, row: sqlite3.Row, tender: Tender, *, staff: bool = False) -> dict:
        """Award view.

        Suppliers receive only their own award and its own amounts.  Staff get
        the full calculation basis, holds and attempt count.
        """
        bid = self.store.one("SELECT * FROM bids WHERE id = ?", (row["bid"],))
        data = {
            "id": row["id"],
            "tender": row["tender"],
            "vendor": row["vendor"],
            "kind": row["kind"],
            "status": row["status"],
            "cents": row["cents"],
            "lots": json.loads(row["lots"]),
            "signature_deadline": row["signature_deadline"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "signed_at": row["signed_at"],
            "declined_at": row["declined_at"],
            "expired_at": row["expired_at"],
        }
        if bid is not None:
            data["bid"] = bid["id"]
            data["revision"] = bid["revision"]
            data["expires_at"] = bid["expires_at"]
        if staff:
            groups = json.loads(row["groups"])
            data["packages"] = json.loads(row["package_ids"])
            data["labels"] = json.loads(row["labels"])
            data["groups"] = groups if isinstance(groups, dict) else {}
            data["evaluation"] = row["evaluation"]
            data["hold_reason"] = row["hold_reason"]
            data["attempt_count"] = row["attempt_count"]
            data["published_at"] = row["published_at"]
        else:
            data["packages"] = json.loads(row["package_ids"])
            data["can_confirm"] = row["status"] == AWARD_OFFERED
        return data

    def _new_award_id(self, tender_id: str, key: str) -> str:
        digest = hashlib.sha1(f"{tender_id}|{key}".encode("utf-8")).hexdigest()[:10]
        return f"AW-{digest}"

    def _new_contract_id(self, award) -> str:
        digest = hashlib.sha1(f"{award['id']}|{award['vendor']}".encode("utf-8")).hexdigest()[:10]
        return f"C-{digest}"

    def _new_dispute_id(self, tender_id: str) -> str:
        return "D-" + secrets.token_hex(6).upper()

    # ------------------------------------------------------------------
    # Dispute impact analysis (which awards must be re-evaluated)
    # ------------------------------------------------------------------
    def _pending_disputes(self, tender_id: str) -> list[sqlite3.Row]:
        """Disputes that can still change an outcome.

        An *upheld* dispute remains pending until a re-evaluation records its
        effect: releasing awards immediately would let a qualification failure
        sign the old plan before it is recomputed.
        """
        return self.store.query(
            "SELECT * FROM disputes WHERE tender = ? AND (status = ? OR"
            " (status = ? AND reevaluation_plan IS NULL))",
            (tender_id, DISPUTE_OPEN, DISPUTE_VALID),
        )

    def _blocking_disputes(self, tender: Tender, award: sqlite3.Row) -> list[Dispute]:
        """Pending disputes whose outcome may still change this award."""
        rows = self._pending_disputes(tender.id)
        blocking: list[Dispute] = []
        for row in rows:
            if self._dispute_may_affect_award(tender, row, award):
                blocking.append(self._dispute_row(row))
        return blocking

    def _dispute_row(self, row: sqlite3.Row) -> Dispute:
        return Dispute(
            id=row["id"],
            tender_id=row["tender"],
            who=row["who"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            orig=row["orig"],
            affected_scope=tuple(json.loads(row["affected_scope"])),
            claim=row["claim"],
            filed_at=parse_dt(row["filed_at"]),
            status=row["status"],
        )

    def _dispute_may_affect_award(self, tender: Tender, dispute: sqlite3.Row, award: sqlite3.Row) -> bool:
        """Whether a still-pending claim could change *this* award.

        A qualification dispute identifies a credential (``orig``).  Its
        practical reach is: every bid using that credential, the vendors and
        exclusion groups those bids belong to, and the lots named in
        ``affected_scope``.  An award is affected when

        * it belongs to the same vendor or credential,
        * it shares an exclusion label with the contested bid,
        * it trades in a lot named in ``affected_scope`` (its quantity or its
          per-group budget could move), or
        * the disputed bid currently occupies the same budget group and its
          removal would free budget that could change selection.

        Awards in an entirely separate budget island with no shared lot, label
        or bid remain signable: merely sharing the tender number is not enough
        to freeze them.
        """
        scope = set(json.loads(dispute["affected_scope"]))
        if dispute["target_type"] == "EVALUATION":
            return True
        if dispute["orig"]:
            contested_bids = self.store.query(
                "SELECT * FROM bids WHERE tender = ? AND credential = ? AND withdrawn = 0",
                (tender.id, dispute["orig"]),
            )
        elif dispute["target_type"] == "BID":
            contested_bids = self.store.query(
                "SELECT * FROM bids WHERE tender = ? AND id = ? AND withdrawn = 0",
                (tender.id, dispute["target_id"]),
            )
        elif dispute["target_type"] == "AWARD":
            contested_bids = []
            target_award = self.store.one(
                "SELECT * FROM awards WHERE id = ? AND tender = ?",
                (dispute["target_id"], tender.id),
            )
            if target_award is not None:
                contested_bids = self.store.query(
                    "SELECT * FROM bids WHERE tender = ? AND id = ?",
                    (tender.id, target_award["bid"]),
                )
            if not contested_bids:
                return award["id"] == dispute["target_id"]
        else:
            contested_bids = []
        if not contested_bids:
            return False
        award_lots = set(json.loads(award["lots"]))
        award_labels = set(json.loads(award["labels"]))
        award_groups = set(json.loads(award["groups"]))
        award_bid = self.store.one("SELECT * FROM bids WHERE id = ?", (award["bid"],))
        award_credential = award_bid["credential"] if award_bid else None
        for bid in contested_bids:
            if award["vendor"] == bid["vendor"]:
                return True
            if award_credential and bid["credential"] == award_credential:
                return True
            bid_labels: set[str] = set()
            bid_lots: set[str] = set()
            bid_groups: set[str] = set()
            for package in json.loads(bid["packages"]):
                if package.get("exclusion"):
                    bid_labels.add(package["exclusion"])
                for lot_id in package["lots"]:
                    bid_lots.add(lot_id)
                    bid_groups.add(tender.lot_map[lot_id].group)
            if award_labels & bid_labels:
                return True
            if scope and award_lots & scope:
                return True
            if award_groups & bid_groups:
                # Same budget group: removing a competitor can free budget and
                # reorder this award, so a re-check is required.
                return True
        return False

    def _refresh_holds(self, tender: Tender, now: datetime) -> None:
        """Move awards in/out of review without losing their pre-hold state.

        A hold is a pause, not a demotion: once the blocking dispute is gone an
        award that had already been offered returns to *offered*, provided its
        signature window is still open.  Anything else returns to draft.
        """
        rows = self.store.query(
            "SELECT * FROM awards WHERE tender = ? AND status IN (?, ?, ?)",
            (tender.id, AWARD_DRAFT, AWARD_HELD, AWARD_OFFERED),
        )
        with self.store.transaction() as db:
            for row in rows:
                blocking = self._blocking_disputes(tender, row)
                if blocking:
                    db.execute(
                        "UPDATE awards SET status = ?, hold_reason = ?, updated_at = ? WHERE id = ?",
                        (AWARD_HELD, "存在可能影响本拟授标的未决异议", format_dt(now), row["id"]),
                    )
                elif row["status"] == AWARD_HELD:
                    deadline = row["signature_deadline"]
                    if row["published_at"] is not None and deadline is not None and parse_dt(
                        deadline
                    ) <= now:
                        # The pause outlasted the validity window: it can no
                        # longer be accepted, so record that rather than
                        # resurrecting an unusable offer.
                        db.execute(
                            "UPDATE awards SET status = ?, hold_reason = NULL, expired_at = ?,"
                            " updated_at = ? WHERE id = ?",
                            (AWARD_EXPIRED, format_dt(now), format_dt(now), row["id"]),
                        )
                        continue
                    reopen = row["published_at"] is not None and deadline is not None
                    db.execute(
                        "UPDATE awards SET status = ?, hold_reason = NULL, updated_at = ? WHERE id = ?",
                        (
                            AWARD_OFFERED if reopen else AWARD_DRAFT,
                            format_dt(now),
                            row["id"],
                        ),
                    )

    # ------------------------------------------------------------------
    # Read models: contracts, history, supplier status
    # ------------------------------------------------------------------
    def contracts(self, identity: Identity | None, tender_id: str, *, vendor: str | None = None) -> list[dict]:
        actor = self._require(identity, ROLE_SUPPLIER, ROLE_EVALUATOR, ROLE_BUYER)
        tender = self.get_tender(tender_id)
        if actor.role == ROLE_SUPPLIER:
            vendor = actor.vendor
        if self.clock.now() < tender.open_at:
            raise Sealed("开标前不能查看签约结果")
        if vendor:
            rows = self.store.query(
                "SELECT * FROM contracts WHERE tender = ? AND vendor = ? ORDER BY signed_at, id",
                (tender.id, vendor),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM contracts WHERE tender = ? ORDER BY signed_at, id", (tender.id,)
            )
        return [self._contract_dict(row) for row in rows]

    def _contract_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "tender": row["tender"],
            "award": row["award"],
            "vendor": row["vendor"],
            "bid": row["bid"],
            "packages": json.loads(row["package_ids"]),
            "lots": json.loads(row["lots"]),
            "cents": row["cents"],
            "signed_at": row["signed_at"],
            "status": row["status"],
            "superseded_by": row["superseded_by"] if "superseded_by" in row.keys() else None,
        }

    def supplier_status(self, identity: Identity | None, tender_id: str) -> dict:
        actor = self._require(identity, ROLE_SUPPLIER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        self._expire_awards(tender, now=now)
        bids = self.store.query(
            "SELECT * FROM bids WHERE tender = ? AND vendor = ? ORDER BY revision DESC",
            (tender.id, actor.vendor),
        )
        awards = self.store.query(
            "SELECT * FROM awards WHERE tender = ? AND vendor = ? ORDER BY id", (tender.id, actor.vendor)
        )
        contracts = self.store.query(
            "SELECT * FROM contracts WHERE tender = ? AND vendor = ?", (tender.id, actor.vendor)
        )
        receipts = self.store.query(
            "SELECT * FROM receipts WHERE tender = ? AND vendor = ? ORDER BY at, rowid",
            (tender.id, actor.vendor),
        )
        visible = [row for row in awards if row["published_at"] is not None]
        if now < tender.open_at:
            phase = "SEALED"
            headline = "报价已按凭证收存，等待开标"
        elif any(row["status"] == AWARD_OFFERED for row in visible):
            phase = "AWAITING_SIGNATURE"
            headline = "有拟授标待确认，请在有效期内答复"
        elif any(row["status"] == AWARD_HELD for row in visible):
            phase = "UNDER_REVIEW"
            headline = "相关拟授标仍在复核，暂不可签"
        elif any(row["status"] == AWARD_DRAFT for row in visible):
            phase = "PROPOSED"
            headline = "拟授标仍待采购组送签"
        elif any(row["status"] == AWARD_SIGNED for row in visible) or contracts:
            phase = "SIGNED"
            headline = "已签约"
        elif any(row["status"] == AWARD_DECLINED for row in visible):
            phase = "DECLINED"
            headline = "已拒绝拟授标"
        elif any(row["status"] in (AWARD_EXPIRED, AWARD_SUPERSEDED) for row in visible):
            phase = "NO_LONGER_SIGNABLE"
            headline = "拟授标已届满或被撤销，不再可签"
        else:
            phase = "NOT_SELECTED"
            headline = "本次未中选"
        return {
            "tender": tender.id,
            "phase": phase,
            "headline": headline,
            "bids": [self._row_to_bid(row).as_dict() for row in bids],
            "awards": [self._award_public(row, tender) for row in visible],
            "contracts": [self._contract_dict(row) for row in contracts],
            "receipts": [self._receipt_dict(row) for row in receipts],
            "disputes": [
                self._dispute_dict(row)
                for row in self.store.query(
                    "SELECT * FROM disputes WHERE tender = ? AND who = ? ORDER BY filed_at",
                    (tender.id, actor.vendor),
                )
            ],
        }

    def buyer_history(self, identity: Identity | None, tender_id: str) -> dict:
        actor = self._require(identity, ROLE_BUYER, ROLE_EVALUATOR)
        tender = self.get_tender(tender_id)
        if self.clock.now() < tender.open_at:
            raise Sealed("开标前不能查看完整报价历史")
        bids = self.store.query("SELECT * FROM bids WHERE tender = ? ORDER BY id", (tender.id,))
        awards = self.store.query("SELECT * FROM awards WHERE tender = ? ORDER BY id", (tender.id,))
        contracts = self.store.query("SELECT * FROM contracts WHERE tender = ? ORDER BY id", (tender.id,))
        receipts = self.store.query("SELECT * FROM receipts WHERE tender = ? ORDER BY at, rowid", (tender.id,))
        disputes = self.store.query("SELECT * FROM disputes WHERE tender = ? ORDER BY filed_at", (tender.id,))
        return {
            "tender": tender.id,
            "status": tender.status_at(self.clock.now()),
            "bids": [self._row_to_bid(row).as_dict() for row in bids],
            "receipts": [self._receipt_dict(row) for row in receipts],
            "evaluations": self.list_evaluations(actor, tender.id),
            "awards": [self._award_full(row, tender) for row in awards],
            "contracts": [self._contract_dict(row) for row in contracts],
            "disputes": [self._dispute_dict(row) for row in disputes],
            "audit": list_audit(self.store, limit=500),
        }

    def supplier_case(self, identity: Identity | None, tender_id: str) -> dict:
        """A supplier's own award reasons and a de-identified ranking sketch.

        The supplier learns why *its* packages were or were not selected and
        where its own material stood, but sees no competitor's quantities,
        prices or package identifiers: only the relative ordering and the
        published criteria that produced it.
        """
        actor = self._require(identity, ROLE_SUPPLIER)
        tender = self.get_tender(tender_id)
        now = self.clock.now()
        self._expire_awards(tender, now=now)
        if now < tender.open_at:
            raise Sealed("开标前不能查看评审与排序解释")
        latest_plan = self.store.one(
            "SELECT * FROM evaluations WHERE tender = ? ORDER BY id DESC", (tender.id,)
        )
        if latest_plan is None:
            return {"tender": tender.id, "evaluated": False, "bids": [], "awards": []}
        plan = json.loads(latest_plan["plan"])
        bids = {bid.id: bid for bid in self._round_bids(tender)}
        my_bids = [bid for bid in bids.values() if bid.vendor == actor.vendor]
        my_bid_ids = {bid.id for bid in my_bids}
        my_rows = [row for row in plan["ranking"] if row["bid"] in my_bid_ids]
        my_reasons = [row for row in plan["unselected"] if row["bid"] in my_bid_ids]
        awards = [
            self._award_public(row, tender)
            for row in self.store.query(
                "SELECT * FROM awards WHERE tender = ? AND vendor = ? AND published_at IS NOT NULL"
                " ORDER BY id",
                (tender.id, actor.vendor),
            )
        ]
        return {
            "tender": tender.id,
            "evaluated": True,
            "evaluation_round": latest_plan["round"],
            "my_bids": [
                {
                    "bid": row["bid"],
                    "revision": row["revision"],
                    "eligible": row["eligible"],
                    "status": row["status"],
                    "selected_packages": row["selected_packages"],
                    "volumes": row["volumes"],
                    "total_kwh": row["total_kwh"],
                    "cents": row["cents"],
                    "rank": plan["ranking"].index(row) + 1,
                    "ranked_bids": len(plan["ranking"]),
                }
                for row in my_rows
            ],
            "unselected_reasons": my_reasons,
            "awards": awards,
            "criteria": [
                "硬约束：时段上限、最低履约量、分组与整轮预算、exclusion 互斥、整包不可拆",
                "排序：最高优先时段进展，其次次级时段进展，再次可成交总量（多者优先）",
                "排序：总支出（低者优先），最后按报价编号字典序",
            ],
            "note": "解释仅覆盖本方材料与相对次序，不含其他供应商的报量、价格或方案。",
        }

    def dispute_impact(self, identity: Identity | None, dispute_id: str) -> dict:
        """Which exclusion group and which planned awards a dispute touches."""
        actor = self._require(identity, ROLE_BUYER, ROLE_EVALUATOR)
        row = self.store.one("SELECT * FROM disputes WHERE id = ?", (dispute_id,))
        if not row:
            raise NotFound("异议不存在", dispute=dispute_id)
        tender = self.get_tender(row["tender"])
        rows = self.store.query("SELECT * FROM awards WHERE tender = ?", (tender.id,))
        affected = []
        unaffected = []
        for award in rows:
            if award["status"] in (AWARD_DECLINED, AWARD_EXPIRED, AWARD_SUPERSEDED, AWARD_SIGNED):
                continue
            target = affected if self._dispute_may_affect_award(tender, row, award) else unaffected
            target.append(self._award_full(award, tender))
        contested = []
        if row["orig"]:
            contested = [
                {
                    "bid": bid["id"],
                    "vendor": bid["vendor"],
                    "revision": bid["revision"],
                    "labels": sorted(
                        {
                            package["exclusion"]
                            for package in json.loads(bid["packages"])
                            if package.get("exclusion")
                        }
                    ),
                    "lots": sorted(
                        {lot for package in json.loads(bid["packages"]) for lot in package["lots"]}
                    ),
                }
                for bid in self.store.query(
                    "SELECT * FROM bids WHERE tender = ? AND credential = ? AND withdrawn = 0",
                    (tender.id, row["orig"]),
                )
            ]
        return {
            "dispute": self._dispute_dict(row),
            "contested_bids": contested,
            "affected_awards": affected,
            "unaffected_awards": unaffected,
        }
