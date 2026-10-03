"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .custody import CustodyService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


class ServiceHub:
    """把基础服务与样品保管服务组合为 HTTP 边界使用的整体。"""

    def __init__(self, database: Database, clock=None) -> None:
        self.domain = DomainService(database, clock)
        self.custody = CustodyService(database, clock)


CUSTODY_POST_ROUTES = {
    "/custody/reservations": "create_reservation",
    "/custody/reservations/waybill": "update_reservation_waybill",
    "/custody/reservations/close": "close_reservation",
    "/custody/packages/scan": "scan_package",
    "/custody/works": "register_work",
    "/custody/components": "register_component",
    "/custody/components/verify": "verify_component",
    "/custody/components/assign-location": "assign_location",
    "/custody/components/assign-equipment": "assign_equipment",
    "/custody/locations": "register_location",
    "/custody/equipment": "register_equipment",
    "/custody/loans": "create_loan",
    "/custody/loans/cancel": "cancel_loan",
    "/custody/loans/sweep": "sweep_overdue",
    "/custody/handovers": "initiate_handover",
    "/custody/handovers/confirm": "confirm_handover",
    "/custody/handovers/evidence": "supplement_evidence",
    "/custody/exceptions": "open_exception",
    "/custody/exceptions/resolve": "resolve_exception",
}


def _route_custody(custody: CustodyService, method: str, parsed, body: dict[str, Any],
                   actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """分派样品保管接口；未命中时返回 None。"""

    if method == "POST" and parsed.path in CUSTODY_POST_ROUTES:
        result = getattr(custody, CUSTODY_POST_ROUTES[parsed.path])(actor_id=actor_id, **body)
        status = 200 if result.get("replayed", True) else 201
        return status, result
    if method == "GET":
        query = parse_qs(parsed.query)

        def arg(name: str, default: str = "") -> str:
            return query.get(name, [default])[0]

        if parsed.path == "/custody/views/warehouse":
            return 200, custody.warehouse_view(actor_id=actor_id, site_id=arg("site_id"))
        if parsed.path == "/custody/views/secretary":
            return 200, custody.secretary_view(actor_id=actor_id, site_id=arg("site_id"))
        if parsed.path == "/custody/views/carrier":
            return 200, custody.carrier_view(actor_id=actor_id, site_id=arg("site_id"),
                                             carrier_org=arg("carrier_org"))
        if parsed.path == "/custody/views/auditor":
            return 200, custody.auditor_view(actor_id=actor_id, site_id=arg("site_id"))
        if parsed.path == "/custody/components/chain":
            return 200, custody.custody_chain(actor_id=actor_id, component_id=arg("component_id"))
        if parsed.path == "/custody/components/position":
            return 200, custody.position_at(actor_id=actor_id, component_id=arg("component_id"),
                                            at=arg("at"))
        if parsed.path == "/custody/components/damage-interval":
            return 200, custody.damage_interval(actor_id=actor_id, component_id=arg("component_id"),
                                                exception_id=arg("exception_id") or None)
    return None


def route(service, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    domain = getattr(service, "domain", service)
    custody = getattr(service, "custody", None)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = domain.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = domain.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = domain.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = domain.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = domain.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in domain.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": domain.audit_events(after)}
        if custody is not None:
            handled = _route_custody(custody, method, parsed, body, actor_id)
            if handled is not None:
                return handled
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = ServiceHub(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
