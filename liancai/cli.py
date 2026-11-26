"""Command line entry point.

Useful for local operation and for scripted verification without an HTTP
client: ``python3 -m liancai <command> ...``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .api import serve
from .errors import AppError
from .service import ProcurementService
from .storage import Store
from .timeutil import FixedClock


def _store(args) -> Store:
    return Store(args.db)


def _service(args) -> ProcurementService:
    clock = FixedClock(args.now) if getattr(args, "now", None) else None
    if clock is not None:
        return ProcurementService(_store(args), clock=clock)
    return ProcurementService(_store(args))


def _identity(service: ProcurementService, args):
    if not getattr(args, "token", None):
        return None
    return service.resolve_token(args.token)


def _load_json(value: str | None) -> Any:
    if value is None:
        return None
    if value == "-":
        return json.load(sys.stdin)
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="liancai", description="联采密封报价与授标签约服务")
    parser.add_argument("--db", default=os.environ.get("LIANCAI_DB", "liancai.sqlite3"))
    parser.add_argument("--now", help="固定时钟（ISO-8601，带时区），用于复现")
    parser.add_argument("--token", help="调用身份令牌")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库、身份、资格凭证与采购公告")
    seed = sub.add_parser("seed-demo", help="按样例时间线重放报价并开标评审")
    seed.add_argument("--evaluate", action="store_true", help="开标后立即评审")

    user = sub.add_parser("register-user")
    user.add_argument("--id", required=True)
    user.add_argument("--role", required=True, choices=["buyer", "evaluator", "supplier"])
    user.add_argument("--display")
    user.add_argument("--vendor")

    credential = sub.add_parser("import-credential")
    credential.add_argument("file", help="资格凭证 JSON 文件（数组或单个对象）")

    tender = sub.add_parser("create-tender")
    tender.add_argument("file", help="采购公告 JSON 文件")

    show = sub.add_parser("show-tender")
    show.add_argument("tender")

    submit = sub.add_parser("submit-bid")
    submit.add_argument("tender")
    submit.add_argument("file")

    withdraw = sub.add_parser("withdraw-bid")
    withdraw.add_argument("tender")
    withdraw.add_argument("bid")

    receipts = sub.add_parser("receipts")
    receipts.add_argument("tender")

    open_tender = sub.add_parser("open")
    open_tender.add_argument("tender")

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("tender")
    evaluate.add_argument("--dispute")
    evaluate.add_argument("--reason")

    reevaluate = sub.add_parser("reevaluate")
    reevaluate.add_argument("tender")
    reevaluate.add_argument("--dispute", required=True)
    reevaluate.add_argument("--reason")

    awards = sub.add_parser("awards")
    awards.add_argument("tender")

    offer = sub.add_parser("offer")
    offer.add_argument("award")

    confirm = sub.add_parser("confirm")
    confirm.add_argument("award")
    confirm.add_argument("action", choices=["accept", "reject"])
    confirm.add_argument("--key", required=True)

    contracts = sub.add_parser("contracts")
    contracts.add_argument("tender")

    standby = sub.add_parser("standby")
    standby.add_argument("tender")

    dispute = sub.add_parser("file-dispute")
    dispute.add_argument("tender")
    dispute.add_argument("file")

    resolve = sub.add_parser("resolve-dispute")
    resolve.add_argument("dispute")
    resolve.add_argument("--decision", required=True, choices=["upheld", "rejected"])
    resolve.add_argument("--notes")

    impact = sub.add_parser("impact")
    impact.add_argument("dispute")

    history = sub.add_parser("history")
    history.add_argument("tender")

    case = sub.add_parser("my-case")
    case.add_argument("tender")

    status = sub.add_parser("status")
    status.add_argument("tender")

    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8412)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = _service(args)
    try:
        if args.command == "init":
            _init(service)
            return 0
        if args.command == "seed-demo":
            _seed_demo(service, evaluate=args.evaluate)
            return 0
        if args.command == "serve":
            httpd = serve(service, args.host, args.port)
            print(f"listening on http://{args.host}:{args.port}", file=sys.stderr)
            httpd.serve_forever()
            return 0
        identity = _identity(service, args)
        command = args.command
        if command == "register-user":
            result = service.register_user(
                user_id=args.id,
                role=args.role,
                display=args.display,
                vendor=args.vendor,
            )
            _print(result)
        elif command == "import-credential":
            data = _load_json(args.file)
            rows = data if isinstance(data, list) else [data]
            _print([service.import_credential(identity, row) for row in rows])
        elif command == "create-tender":
            _print(service.create_tender(identity, _load_json(args.file)).as_dict())
        elif command == "show-tender":
            _print(service.tender_public(identity, args.tender))
        elif command == "submit-bid":
            _print(service.submit_bid(identity, args.tender, _load_json(args.file)))
        elif command == "withdraw-bid":
            _print(service.withdraw_bid(identity, args.tender, args.bid))
        elif command == "receipts":
            _print({"receipts": service.my_receipts(identity, args.tender)})
        elif command == "open":
            _print(service.open_tender(identity, args.tender))
        elif command == "evaluate":
            _print(service.evaluate(identity, args.tender, dispute_id=args.dispute))
        elif command == "reevaluate":
            _print(
                service.reevaluate(
                    identity, args.tender, dispute_id=args.dispute, reason=args.reason
                )
            )
        elif command == "awards":
            _print({"awards": service.awards_view(identity, args.tender)})
        elif command == "offer":
            _print(service.offer_award(identity, args.award))
        elif command == "confirm":
            _print(
                service.confirm_award(
                    identity, args.award, action=args.action, idempotency_key=args.key
                )
            )
        elif command == "contracts":
            _print({"contracts": service.contracts(identity, args.tender)})
        elif command == "standby":
            _print(service.standby(identity, args.tender))
        elif command == "file-dispute":
            _print(service.file_dispute(identity, args.tender, _load_json(args.file)).as_dict())
        elif command == "resolve-dispute":
            _print(
                service.resolve_dispute(
                    identity, args.dispute, {"decision": args.decision, "notes": args.notes}
                )
            )
        elif command == "impact":
            _print(service.dispute_impact(identity, args.dispute))
        elif command == "history":
            _print(service.buyer_history(identity, args.tender))
        elif command == "my-case":
            _print(service.supplier_case(identity, args.tender))
        elif command == "status":
            if identity and identity.role == "supplier":
                _print(service.supplier_status(identity, args.tender))
            else:
                _print(service.tender_public(identity, args.tender))
        else:
            raise SystemExit(f"unknown command {command}")
        return 0
    except AppError as exc:
        _print(exc.as_dict())
        return 2
    finally:
        service.store.close()


def _data(name: str):
    root = Path(__file__).resolve().parents[1] / "data"
    return json.loads((root / f"{name}.json").read_text(encoding="utf-8"))


def _init(service: ProcurementService) -> None:
    """Idempotent bootstrap: identities, credentials and the published tender."""
    buyer = service.register_user(user_id="buyer-1", role="buyer", display="采购组")
    evaluator = service.register_user(user_id="eval-1", role="evaluator", display="评审组")
    buyer_identity = service.resolve_token(buyer["token"])
    for row in _data("credentials"):
        service.import_credential(buyer_identity, row)
    for row in _data("tender"):
        try:
            service.create_tender(buyer_identity, row)
        except AppError as exc:
            if exc.code != "CONFLICT":
                raise
    tokens: dict[str, str] = {}
    vendors = sorted({row["vendor"] for row in _data("bids")})
    for index, vendor in enumerate(vendors, start=1):
        created = service.register_user(
            user_id=f"supplier-{index}", role="supplier", display=vendor, vendor=vendor
        )
        tokens[vendor] = created["token"]
    _print(
        {
            "buyer": buyer["token"],
            "evaluator": evaluator["token"],
            "suppliers": tokens,
            "note": "令牌只在创建时返回；seed-demo 需要固定时钟重放样例报价。",
        }
    )


def _seed_demo(service: ProcurementService, *, evaluate: bool = False) -> None:
    """Replay the sample bids at their recorded arrival times.

    The receipt timestamp is the *server* clock, so this walks a fixed clock
    through the sample timeline.  It is a local demonstration aid, never an API
    surface: production submissions always use the real clock.
    """
    from .timeutil import FixedClock

    for row in _data("bids"):
        service.clock = FixedClock(row["receipt_at"])
        identity = _identity_for_vendor(service, row["vendor"])
        payload = {
            key: row[key]
            for key in ("id", "credential", "expires_at", "packages", "whole")
            if key in row
        }
        service.submit_bid(identity, "AW-72", payload)
    service.clock = FixedClock("2027-02-01T10:15:00+08:00")
    evaluator = _identity_for_role(service, "evaluator")
    service.open_tender(evaluator, "AW-72")
    if evaluate:
        service.evaluate(evaluator, "AW-72")
    _print({"tender": "AW-72", "replayed": len(_data("bids")), "evaluated": evaluate})


def _identity_for_vendor(service: ProcurementService, vendor: str):
    from .models import Identity

    row = service.store.one(
        "SELECT * FROM users WHERE vendor = ? AND role = 'supplier'", (vendor,)
    )
    if not row:
        raise SystemExit(f"未找到供应商身份 {vendor}")
    return Identity(id=row["id"], role=row["role"], display=row["display"], vendor=row["vendor"])


def _identity_for_role(service: ProcurementService, role: str):
    from .models import Identity

    row = service.store.one("SELECT * FROM users WHERE role = ? ORDER BY id", (role,))
    if not row:
        raise SystemExit(f"未找到 {role} 身份")
    return Identity(id=row["id"], role=row["role"], display=row["display"], vendor=row["vendor"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
