"""Domain records and validation for the joint procurement service.

The wire format is documented in ``docs/exchange.md``.  Parsing is strict:
a malformed submitted document is rejected as a whole so that a receipt never
refers to half-understood material.  ``None`` is accepted for optional fields
(the exchange convention says JSON null means "reported but not yet
confirmed"), while required fields must be present and non-null.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .errors import ValidationError
from .timeutil import format_dt, parse_dt

ROLE_BUYER = "buyer"
ROLE_EVALUATOR = "evaluator"
ROLE_SUPPLIER = "supplier"

#: Tender lifecycle as seen by an authenticated caller.
TENDER_SEALED = "SEALED"
TENDER_CLOSED_SEALED = "CLOSED_SEALED"
TENDER_OPEN = "OPEN"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def require_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError(
            f"{field_name} 必须是由字母、数字开头且仅含字母、数字和 ._:- 的标识",
            field=field_name,
        )
    return value


def require_text(value: Any, field_name: str, *, max_length: int = 128) -> str:
    """Free-form business name (supplier names are not technical ids)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} 必须是非空文本", field=field_name)
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field_name} 过长", field=field_name)
    return value


def require_int(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if value is None:
        raise ValidationError(f"{field_name} 尚未确认，不能用于本轮", field=field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field_name} 必须是整数", field=field_name)
    if value < minimum:
        raise ValidationError(f"{field_name} 不能小于 {minimum}", field=field_name)
    return value


def opt_text(value: Any, field_name: str, *, max_length: int = 500) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} 必须是非空文本", field=field_name)
    if len(value) > max_length:
        raise ValidationError(f"{field_name} 过长", field=field_name)
    return value.strip()


def require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field_name} 必须是布尔值", field=field_name)
    return value


@dataclass(frozen=True)
class Lot:
    id: str
    group: str
    min_kwh: int
    max_kwh: int
    priority: int

    @classmethod
    def parse(cls, raw: Any, index: int) -> "Lot":
        if not isinstance(raw, dict):
            raise ValidationError(f"lots[{index}] 必须是对象")
        lot_id = require_id(raw.get("id"), f"lots[{index}].id")
        group = require_id(raw.get("group"), f"lots[{index}].group")
        minimum = require_int(raw.get("min_kwh"), f"lots[{index}].min_kwh", minimum=0)
        maximum = require_int(raw.get("max_kwh"), f"lots[{index}].max_kwh", minimum=1)
        if minimum > maximum:
            raise ValidationError(
                f"lot {lot_id} 的最低履约量不能高于可接受最大电量",
                lot=lot_id,
            )
        priority = require_int(raw.get("priority"), f"lots[{index}].priority", minimum=1)
        return cls(lot_id, group, minimum, maximum, priority)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "group": self.group,
            "min_kwh": self.min_kwh,
            "max_kwh": self.max_kwh,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class Tender:
    id: str
    close_at: datetime
    open_at: datetime
    partial: bool
    limits: dict[str, int]
    lots: tuple[Lot, ...]
    valid_until: datetime | None
    created_at: datetime | None = None
    created_by: str | None = None

    @classmethod
    def parse(cls, raw: Any, *, tender_id: str | None = None) -> "Tender":
        if not isinstance(raw, dict):
            raise ValidationError("采购公告必须是对象")
        tid = require_id(tender_id or raw.get("id"), "id")
        close_at = parse_dt(raw.get("close_at"), field="close_at")
        open_at = parse_dt(raw.get("open_at"), field="open_at")
        if close_at >= open_at:
            raise ValidationError("关标时刻必须早于开标时刻", close_at=format_dt(close_at))
        partial = require_bool(raw.get("partial"), "partial")
        lot_rows = raw.get("lots")
        if not isinstance(lot_rows, list) or not lot_rows:
            raise ValidationError("lots 必须是非空数组", field="lots")
        lots = tuple(Lot.parse(row, i) for i, row in enumerate(lot_rows))
        lot_ids = [lot.id for lot in lots]
        if len(set(lot_ids)) != len(lot_ids):
            raise ValidationError("lots 的 id 不能重复", field="lots")
        raw_limits = raw.get("limits")
        if not isinstance(raw_limits, dict) or not raw_limits:
            raise ValidationError("limits 必须是非空对象", field="limits")
        limits: dict[str, int] = {}
        for key, value in raw_limits.items():
            key = require_id(key, "limits 的键")
            limits[key] = require_int(value, f"limits.{key}", minimum=0)
        if "total" not in limits:
            raise ValidationError("limits.total 必须提供", field="limits.total")
        for lot in lots:
            if lot.group not in limits:
                raise ValidationError(
                    f"lot {lot.id} 的分组 {lot.group} 缺少预算上限",
                    group=lot.group,
                )
        valid_until = None
        if raw.get("valid_until") is not None:
            valid_until = parse_dt(raw.get("valid_until"), field="valid_until")
            if valid_until <= close_at:
                raise ValidationError("采购授标有效期必须晚于关标时刻", field="valid_until")
        return cls(
            id=tid,
            close_at=close_at,
            open_at=open_at,
            partial=partial,
            limits=limits,
            lots=lots,
            valid_until=valid_until,
        )

    @property
    def lot_ids(self) -> tuple[str, ...]:
        return tuple(lot.id for lot in self.lots)

    @property
    def lot_map(self) -> dict[str, Lot]:
        return {lot.id: lot for lot in self.lots}

    def ordered_lots(self) -> tuple[Lot, ...]:
        return tuple(sorted(self.lots, key=lambda lot: (lot.priority, lot.id)))

    def status_at(self, now: datetime) -> str:
        if now >= self.open_at:
            return TENDER_OPEN
        if now >= self.close_at:
            return TENDER_CLOSED_SEALED
        return TENDER_SEALED

    def as_dict(self) -> dict:
        data = {
            "id": self.id,
            "close_at": format_dt(self.close_at),
            "open_at": format_dt(self.open_at),
            "partial": self.partial,
            "limits": dict(self.limits),
            "lots": [lot.as_dict() for lot in self.lots],
        }
        if self.valid_until is not None:
            data["valid_until"] = format_dt(self.valid_until)
        return data


@dataclass(frozen=True)
class Package:
    id: str
    lots: dict[str, int]
    cents: int
    exclusion: str | None = None

    @classmethod
    def parse(cls, raw: Any, index: int) -> "Package":
        if not isinstance(raw, dict):
            raise ValidationError(f"packages[{index}] 必须是对象")
        pid = require_id(raw.get("id"), f"packages[{index}].id")
        lots_raw = raw.get("lots")
        if not isinstance(lots_raw, dict) or not lots_raw:
            raise ValidationError(f"包 {pid} 必须声明非空的时段电量", package=pid)
        lots: dict[str, int] = {}
        for lot_id, qty in lots_raw.items():
            lot_id = require_id(lot_id, f"packages[{index}].lots 的键")
            if lot_id in lots:
                raise ValidationError(f"包 {pid} 的时段重复", package=pid)
            lots[lot_id] = require_int(qty, f"packages[{index}].lots.{lot_id}", minimum=1)
        cents = require_int(raw.get("cents"), f"packages[{index}].cents", minimum=0)
        exclusion = opt_text(raw.get("exclusion"), f"packages[{index}].exclusion", max_length=64)
        return cls(pid, lots, cents, exclusion)

    def as_dict(self) -> dict:
        data = {"id": self.id, "lots": dict(self.lots), "cents": self.cents}
        if self.exclusion is not None:
            data["exclusion"] = self.exclusion
        return data

    @property
    def total_kwh(self) -> int:
        return sum(self.lots.values())


@dataclass(frozen=True)
class Bid:
    id: str
    tender_id: str
    vendor: str
    credential: str
    revision: int
    receipt_at: datetime
    expires_at: datetime
    packages: tuple[Package, ...]
    withdrawn: bool = False
    whole: bool = False
    status: str = "PENDING_CLOSE"
    status_reason: str | None = None
    actor_id: str | None = None

    @classmethod
    def parse(
        cls,
        raw: Any,
        *,
        tender: Tender,
        receipt_at: datetime,
        vendor: str,
        bid_id: str | None = None,
        revision: int | None = None,
        actor_id: str | None = None,
    ) -> "Bid":
        if not isinstance(raw, dict):
            raise ValidationError("报价单必须是对象")
        credential = require_id(raw.get("credential"), "credential")
        expires_at = parse_dt(raw.get("expires_at"), field="expires_at")
        if expires_at <= receipt_at:
            raise ValidationError("报价有效期必须晚于接收时间", field="expires_at")
        package_rows = raw.get("packages")
        if not isinstance(package_rows, list) or not package_rows:
            raise ValidationError("packages 必须是非空数组", field="packages")
        if len(package_rows) > 200:
            raise ValidationError("packages 数量过多", field="packages")
        packages = tuple(Package.parse(row, i) for i, row in enumerate(package_rows))
        package_ids = [package.id for package in packages]
        if len(set(package_ids)) != len(package_ids):
            raise ValidationError("同一报价内 packages 的 id 不能重复", field="packages")
        lot_map = tender.lot_map
        for package in packages:
            for lot_id in package.lots:
                if lot_id not in lot_map:
                    raise ValidationError(
                        f"包 {package.id} 引用了采购公告中不存在的时段 {lot_id}",
                        package=package.id,
                        lot=lot_id,
                    )
        whole_raw = raw.get("whole", False)
        whole = require_bool(whole_raw, "whole") if whole_raw is not None else False
        rev = require_int(
            revision if revision is not None else raw.get("revision", 1),
            "revision",
            minimum=1,
        )
        identifier = bid_id if bid_id is not None else raw.get("id")
        identifier = require_id(identifier, "id") if identifier else None
        return cls(
            id=identifier,
            tender_id=tender.id,
            vendor=vendor,
            credential=credential,
            revision=rev,
            receipt_at=receipt_at,
            expires_at=expires_at,
            packages=packages,
            withdrawn=False,
            whole=whole,
            status="PENDING_CLOSE",
            actor_id=actor_id,
        )

    def package_map(self) -> dict[str, Package]:
        return {package.id: package for package in self.packages}

    def lots_for(self, package_ids: Iterable[str]) -> dict[str, int]:
        wanted = set(package_ids)
        totals: dict[str, int] = {}
        for package in self.packages:
            if package.id in wanted:
                for lot_id, qty in package.lots.items():
                    totals[lot_id] = totals.get(lot_id, 0) + qty
        return totals

    def cents_for(self, package_ids: Iterable[str]) -> int:
        wanted = set(package_ids)
        return sum(p.cents for p in self.packages if p.id in wanted)

    def as_dict(self, *, include_amounts: bool = True) -> dict:
        data = {
            "id": self.id,
            "tender": self.tender_id,
            "vendor": self.vendor,
            "credential": self.credential,
            "revision": self.revision,
            "receipt_at": format_dt(self.receipt_at),
            "expires_at": format_dt(self.expires_at),
            "withdrawn": self.withdrawn,
            "status": self.status,
        }
        if include_amounts:
            data["whole"] = self.whole
            data["packages"] = [package.as_dict() for package in self.packages]
        if self.status_reason:
            data["status_reason"] = self.status_reason
        return data

    def content_hash(self) -> str:
        payload = {
            "id": self.id,
            "tender": self.tender_id,
            "vendor": self.vendor,
            "credential": self.credential,
            "revision": self.revision,
            "receipt_at": format_dt(self.receipt_at),
            "expires_at": format_dt(self.expires_at),
            "whole": self.whole,
            "packages": [package.as_dict() for package in self.packages],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class Credential:
    id: str
    supplier: str
    zones: tuple[str, ...]

    @classmethod
    def parse(cls, raw: Any) -> "Credential":
        if not isinstance(raw, dict):
            raise ValidationError("资格凭证必须是对象")
        cid = require_id(raw.get("id"), "credential.id")
        supplier = opt_text(raw.get("supplier"), "credential.supplier")
        if not supplier:
            raise ValidationError("资格凭证缺少供应商", credential=cid)
        zones_raw = raw.get("zones")
        if not isinstance(zones_raw, list):
            raise ValidationError("资格凭证的 zones 必须是数组", credential=cid)
        zones = tuple(require_id(zone, "zones") for zone in zones_raw)
        return cls(cid, supplier, zones)

    def as_dict(self) -> dict:
        return {"id": self.id, "supplier": self.supplier, "zones": list(self.zones)}


@dataclass(frozen=True)
class Identity:
    id: str
    role: str
    display: str
    vendor: str | None = None
    token: str | None = field(default=None, repr=False)

    @property
    def is_supplier(self) -> bool:
        return self.role == ROLE_SUPPLIER

    def as_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "display": self.display, "vendor": self.vendor}


@dataclass(frozen=True)
class Dispute:
    id: str
    tender_id: str
    who: str
    target_type: str
    target_id: str | None
    orig: str | None
    affected_scope: tuple[str, ...]
    claim: str
    filed_at: datetime
    status: str
    actor_id: str | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    reevaluation_plan: str | None = None

    def as_dict(self) -> dict:
        data = {
            "id": self.id,
            "tender": self.tender_id,
            "who": self.who,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "orig": self.orig,
            "affected_scope": list(self.affected_scope),
            "claim": self.claim,
            "filed_at": format_dt(self.filed_at),
            "status": self.status,
        }
        if self.resolution:
            data["resolution"] = self.resolution
        if self.resolved_at:
            data["resolved_at"] = format_dt(self.resolved_at)
        if self.reevaluation_plan:
            data["reevaluation_plan"] = self.reevaluation_plan
        return data
