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


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          custody: CustodyService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if parsed.path.startswith("/custody/"):
            if custody is None:
                return 404, {"error": "route_not_found", "message": "接口不存在"}
            return _custody_route(custody, method, parsed, body, actor_id)
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _custody_route(custody: CustodyService, method: str, parsed, body: dict[str, Any],
                   actor_id: str) -> tuple[int, dict[str, Any]]:
    """分派样品交接与保管服务的接口。"""

    path = parsed.path
    query = parse_qs(parsed.query)

    def param(name: str) -> str:
        value = query.get(name, [""])[0]
        if not value:
            raise ValidationError(f"{name} 不能为空")
        return value

    if method == "POST" and path == "/custody/reservations":
        return _receipt(custody.create_reservation(actor_id=actor_id, **body))
    if method == "GET" and path == "/custody/reservations":
        return 200, {"items": custody.list_reservations(actor_id=actor_id, site_id=param("site_id"))}
    if method == "POST" and path == "/custody/packages":
        return _receipt(custody.check_in_package(actor_id=actor_id, **body))
    if method == "GET" and path == "/custody/packages":
        return 200, {"items": custody.list_packages(actor_id=actor_id, site_id=param("site_id"))}
    if method == "GET" and path == "/custody/packages/detail":
        return 200, custody.package_detail(actor_id=actor_id, package_id=param("package_id"))
    if method == "POST" and path == "/custody/locations":
        return _receipt(custody.register_location(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/equipment":
        return _receipt(custody.register_equipment(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/verifications":
        return _receipt(custody.verify_component(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/samples":
        return _receipt(custody.assemble_sample(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/allocations":
        return _receipt(custody.allocate_resource(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/allocations/release":
        return _receipt(custody.release_resource(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/handovers":
        return _receipt(custody.create_handover(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/handovers/confirm":
        return _receipt(custody.confirm_handover(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/exceptions":
        return _receipt(custody.open_exception(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/exceptions/resolve":
        return _receipt(custody.resolve_exception(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/components/freeze":
        return _receipt(custody.freeze_component(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/components/unfreeze":
        return _receipt(custody.unfreeze_component(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/evidence":
        return _receipt(custody.append_evidence(actor_id=actor_id, **body))
    if method == "POST" and path == "/custody/overdue/sweep":
        return _receipt(custody.sweep_overdue(actor_id=actor_id, **body))
    if method == "GET" and path == "/custody/components/chain":
        return 200, custody.custody_chain(actor_id=actor_id, component_id=param("component_id"))
    if method == "GET" and path == "/custody/components/responsibility":
        return 200, custody.damage_responsibility(actor_id=actor_id, component_id=param("component_id"),
                                                  handover_id=query.get("handover_id", [None])[0])
    if method == "GET" and path == "/custody/components/location-at":
        return 200, custody.location_at(actor_id=actor_id, component_id=param("component_id"),
                                        at=param("at"))
    if method == "GET" and path == "/custody/handovers/pending":
        return 200, {"items": custody.pending_handovers(actor_id=actor_id, site_id=param("site_id"))}
    if method == "GET" and path == "/custody/views/warehouse":
        return 200, custody.warehouse_view(actor_id=actor_id, site_id=param("site_id"))
    if method == "GET" and path == "/custody/views/secretary":
        return 200, custody.secretary_view(actor_id=actor_id, site_id=param("site_id"))
    if method == "GET" and path == "/custody/views/carrier":
        return 200, custody.carrier_view(actor_id=actor_id, site_id=param("site_id"))
    if method == "GET" and path == "/custody/views/auditor":
        return 200, custody.auditor_view(actor_id=actor_id, site_id=param("site_id"))
    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    custody: CustodyService | None = None

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                custody=self.custody)
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
    Handler.service = DomainService(database)
    Handler.custody = CustodyService(database)
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
