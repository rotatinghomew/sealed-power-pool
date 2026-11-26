"""JSON HTTP API for the joint procurement service.

Routing is explicit and role enforcement stays in the service.  The layer only
translates identities (``Authorization: Bearer <token>``), JSON bodies and
domain errors into HTTP.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import AppError, NotFound, ValidationError
from .models import ROLE_SUPPLIER, Identity
from .service import ProcurementService

Route = tuple[str, re.Pattern, Callable]


def _routes() -> list[tuple[str, re.Pattern, str]]:
    # (method, pattern, handler-name)
    return [
        ("GET", re.compile(r"^/health$"), "health"),
        ("GET", re.compile(r"^/tenders$"), "list_tenders"),
        ("POST", re.compile(r"^/tenders$"), "create_tender"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)$"), "get_tender"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/open$"), "open_tender"),
        ("POST", re.compile(r"^/tenders/(?P<tender>[^/]+)/open$"), "open_tender"),
        ("POST", re.compile(r"^/tenders/(?P<tender>[^/]+)/bids$"), "submit"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/bids$"), "list_bids"),
        ("POST", re.compile(r"^/tenders/(?P<tender>[^/]+)/bids/(?P<bid>[^/]+)/withdraw$"), "withdraw"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/bids/(?P<bid>[^/]+)$"), "read_bid"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/receipts$"), "receipts"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/evaluations$"), "list_evaluations"),
        ("POST", re.compile(r"^/tenders/(?P<tender>[^/]+)/evaluations$"), "evaluate"),
        ("POST", re.compile(r"^/tenders/(?P<tender>[^/]+)/reevaluate$"), "reevaluate"),
        ("GET", re.compile(r"^/evaluations/(?P<evaluation>\d+)$"), "get_evaluation"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/awards$"), "list_awards"),
        ("POST", re.compile(r"^/awards/(?P<award>[^/]+)/offer$"), "offer_award"),
        ("POST", re.compile(r"^/awards/(?P<award>[^/]+)/(?P<action>accept|reject)$"), "confirm_award"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/contracts$"), "contracts"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/standby$"), "standby"),
        ("POST", re.compile(r"^/tenders/(?P<tender>[^/]+)/standby$"), "standby"),
        ("POST", re.compile(r"^/tenders/(?P<tender>[^/]+)/disputes$"), "file_dispute"),
        ("GET", re.compile(r"^/disputes/(?P<dispute>[^/]+)$"), "get_dispute_impact"),
        ("POST", re.compile(r"^/disputes/(?P<dispute>[^/]+)/resolve$"), "resolve_dispute"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/history$"), "history"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/status$"), "status"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/my-case$"), "my_case"),
        ("GET", re.compile(r"^/tenders/(?P<tender>[^/]+)/impact/(?P<dispute>[^/]+)$"), "get_dispute_impact"),
    ]


class Api:
    def __init__(self, service: ProcurementService):
        self.service = service
        self.routes = [(m, p, getattr(self, h)) for (m, p, h) in _routes()]

    def dispatch(self, method: str, path: str, body: Any, identity: Identity | None) -> tuple[int, Any]:
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                result = handler(identity, body, **match.groupdict())
                status = 200
                if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
                    status, result = result
                return status, result
        raise NotFound("接口不存在", path=path)

    # -- handlers ------------------------------------------------------
    def health(self, identity, body, **params):
        return {"status": "ok"}

    def list_tenders(self, identity, body, **params):
        return {"tenders": self.service.list_tenders()}

    def create_tender(self, identity, body, **params):
        tender = self.service.create_tender(identity, body)
        return 201, tender.as_dict()

    def get_tender(self, identity, body, **params):
        return self.service.tender_public(identity, params["tender"])

    def open_tender(self, identity, body, **params):
        return self.service.open_tender(identity, params["tender"])

    def submit(self, identity, body, **params):
        return self.service.submit_bid(identity, params["tender"], body)

    def list_bids(self, identity, body, **params):
        if identity is None or identity.role != ROLE_SUPPLIER:
            return self.service.tender_public(identity, params["tender"])
        return {
            "bids": self.service.my_bids(identity, params["tender"]),
            "tender": self.service.tender_public(identity, params["tender"]),
        }

    def withdraw(self, identity, body, **params):
        return self.service.withdraw_bid(identity, params["tender"], params["bid"])

    def read_bid(self, identity, body, **params):
        return self.service.read_bid_at_open(identity, params["tender"], params["bid"])

    def receipts(self, identity, body, **params):
        return {"receipts": self.service.my_receipts(identity, params["tender"])}

    def list_evaluations(self, identity, body, **params):
        return {"evaluations": self.service.list_evaluations(identity, params["tender"])}

    def evaluate(self, identity, body, **params):
        body = body or {}
        return self.service.evaluate(
            identity,
            params["tender"],
            kind=body.get("kind", "INITIAL"),
            dispute_id=body.get("dispute"),
            notes=body.get("notes"),
        )

    def reevaluate(self, identity, body, **params):
        body = body or {}
        return self.service.reevaluate(
            identity,
            params["tender"],
            dispute_id=body.get("dispute"),
            reason=body.get("reason"),
        )

    def get_evaluation(self, identity, body, **params):
        return self.service.get_evaluation(identity, int(params["evaluation"]))

    def list_awards(self, identity, body, **params):
        return {"awards": self.service.awards_view(identity, params["tender"])}

    def offer_award(self, identity, body, **params):
        return self.service.offer_award(identity, params["award"])

    def confirm_award(self, identity, body, **params):
        body = body or {}
        key = body.get("idempotency_key")
        return self.service.confirm_award(
            identity, params["award"], action=params["action"], idempotency_key=key
        )

    def contracts(self, identity, body, **params):
        return {"contracts": self.service.contracts(identity, params["tender"])}

    def standby(self, identity, body, **params):
        return self.service.standby(identity, params["tender"])

    def file_dispute(self, identity, body, **params):
        dispute = self.service.file_dispute(identity, params["tender"], body)
        return 201, dispute.as_dict()

    def resolve_dispute(self, identity, body, **params):
        return self.service.resolve_dispute(identity, params["dispute"], body)

    def history(self, identity, body, **params):
        return self.service.buyer_history(identity, params["tender"])

    def my_case(self, identity, body, **params):
        return self.service.supplier_case(identity, params["tender"])

    def status(self, identity, body, **params):
        if identity is not None and identity.role == ROLE_SUPPLIER:
            return self.service.supplier_status(identity, params["tender"])
        return self.service.tender_public(identity, params["tender"])

    def get_dispute_impact(self, identity, body, **params):
        return self.service.dispute_impact(identity, params["dispute"])


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "liancai/1.0"

        def log_message(self, fmt, *args):  # keep test output clean
            pass

        def _send(self, status: int, body: Any) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _identity(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return None
            return api.service.resolve_token(header[len("Bearer ") :])

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise ValidationError("Content-Length 不合法") from exc
            if length < 0 or length > 8 * 1024 * 1024:
                raise ValidationError("报文过大")
            if length == 0:
                return None
            raw = self.rfile.read(length)
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValidationError("报文必须是 UTF-8") from exc
            except json.JSONDecodeError as exc:
                raise ValidationError("报文不是合法 JSON", offset=exc.pos) from exc

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            try:
                identity = self._identity()
                body = self._read_body() if method in ("POST", "PUT", "PATCH") else None
                status, result = api.dispatch(method, parsed.path, body, identity)
                self._send(status, result)
            except AppError as exc:
                self._send(exc.http_status, exc.as_dict())
            except Exception as exc:  # pragma: no cover - defensive
                self._send(500, {"error": "INTERNAL", "message": str(exc)})

        def do_GET(self):  # noqa: N802
            self._handle("GET")



        def do_POST(self):  # noqa: N802
            self._handle("POST")

    return Handler


def serve(service: ProcurementService, host: str = "127.0.0.1", port: int = 8412) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(Api(service)))
    return server
