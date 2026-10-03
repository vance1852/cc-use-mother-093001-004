"""样品交接与保管服务。

在基础服务的组织、操作者、场所与哈希链审计之上，为组委会提供实物样品的
预约入库、包裹与组件登记、双人核验、独立异常流程、库位与设备原子占用、
连续保管链、组件级冻结和只增不改的补录证据。全部状态保存在 SQLite 中，
服务重启后预约顺序、资源占用与待确认交接保持不变。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService
from .storage import Database


CUSTODY_SCHEMA = """
CREATE TABLE IF NOT EXISTS custody_reservations (
    reservation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    seq INTEGER NOT NULL,
    expected_at TEXT NOT NULL,
    carrier_ref TEXT NOT NULL,
    expected_packages INTEGER NOT NULL CHECK(expected_packages >= 1),
    status TEXT NOT NULL CHECK(status IN ('pending','arrived','cancelled')),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, seq)
);
CREATE TABLE IF NOT EXISTS custody_packages (
    package_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    reservation_id TEXT REFERENCES custody_reservations(reservation_id),
    package_code TEXT NOT NULL,
    carrier_waybill TEXT,
    seal_code TEXT,
    weight_grams INTEGER CHECK(weight_grams IS NULL OR weight_grams >= 0),
    photo_digest TEXT,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('received','in_storage','sent_back')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, package_code)
);
CREATE TABLE IF NOT EXISTS custody_works (
    work_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL REFERENCES custody_packages(package_id),
    work_key TEXT NOT NULL,
    title TEXT NOT NULL,
    storage_condition_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('intake','reviewable','sent_back')),
    created_at TEXT NOT NULL,
    UNIQUE(package_id, work_key)
);
CREATE TABLE IF NOT EXISTS custody_components (
    component_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES custody_works(work_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    component_code TEXT NOT NULL,
    name TEXT NOT NULL,
    weight_grams INTEGER CHECK(weight_grams IS NULL OR weight_grams >= 0),
    seal_code TEXT,
    photo_digest TEXT,
    storage_condition_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('pending_verification','verified','on_loan','sent_back')),
    custodian TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, component_code)
);
CREATE TABLE IF NOT EXISTS custody_verifications (
    component_id TEXT NOT NULL REFERENCES custody_components(component_id),
    verifier_id TEXT NOT NULL REFERENCES actors(actor_id),
    result TEXT NOT NULL CHECK(result IN ('pass','fail')),
    note TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL,
    PRIMARY KEY(component_id, verifier_id)
);
CREATE TABLE IF NOT EXISTS custody_locations (
    location_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    condition_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_equipment (
    equipment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    total INTEGER NOT NULL CHECK(total >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_allocations (
    allocation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('location','equipment','loan_slot')),
    resource_id TEXT NOT NULL,
    component_id TEXT REFERENCES custody_components(component_id),
    start_at TEXT,
    end_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_allocations_resource
    ON custody_allocations(resource_type, resource_id, status);
CREATE INDEX IF NOT EXISTS idx_allocations_component
    ON custody_allocations(component_id, status);
CREATE TABLE IF NOT EXISTS custody_handovers (
    handover_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('check_in','loan','transfer','return','send_back')),
    from_party TEXT NOT NULL,
    to_party TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending_confirmation','confirmed')),
    due_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES actors(actor_id),
    confirmed_at TEXT,
    UNIQUE(site_id, seq)
);
CREATE TABLE IF NOT EXISTS custody_handover_items (
    handover_id TEXT NOT NULL REFERENCES custody_handovers(handover_id),
    component_id TEXT NOT NULL REFERENCES custody_components(component_id),
    seal_expected TEXT,
    seal_actual TEXT,
    seal_match INTEGER CHECK(seal_match IN (0, 1)),
    condition_note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(handover_id, component_id)
);
CREATE INDEX IF NOT EXISTS idx_handover_items_component
    ON custody_handover_items(component_id);
CREATE TABLE IF NOT EXISTS custody_exceptions (
    exception_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    kind TEXT NOT NULL CHECK(kind IN ('missing','misdelivered','damaged')),
    package_id TEXT REFERENCES custody_packages(package_id),
    work_id TEXT REFERENCES custody_works(work_id),
    component_id TEXT REFERENCES custody_components(component_id),
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    opened_by TEXT NOT NULL REFERENCES actors(actor_id),
    opened_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES actors(actor_id),
    resolved_at TEXT,
    resolution_note TEXT
);
CREATE TABLE IF NOT EXISTS custody_freezes (
    freeze_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES custody_components(component_id),
    reason TEXT NOT NULL CHECK(reason IN ('overdue','seal_mismatch','exception','manual')),
    detail TEXT NOT NULL DEFAULT '',
    frozen_by TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_freezes_component
    ON custody_freezes(component_id, released_at);
CREATE TABLE IF NOT EXISTS custody_evidence (
    evidence_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    target_type TEXT NOT NULL CHECK(target_type IN ('package','work','component','handover','exception','reservation')),
    target_id TEXT NOT NULL,
    note TEXT NOT NULL,
    attachment_digest TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
"""

HANDOVER_KINDS = ("loan", "transfer", "return", "send_back")
EXCEPTION_KINDS = ("missing", "misdelivered", "damaged")
FREEZE_REASONS = ("overdue", "seal_mismatch", "exception", "manual")
RESOURCE_TYPES = ("location", "equipment", "loan_slot")
EVIDENCE_TARGETS = ("package", "work", "component", "handover", "exception", "reservation")
CONDITION_KEYS = ("temperature_min_c", "temperature_max_c", "humidity_min_pct", "humidity_max_pct")
RANGE_KEYS = (("temperature_min_c", "temperature_max_c"), ("humidity_min_pct", "humidity_max_pct"))

ROLE_READ = ("warehouse_keeper", "review_secretary", "carrier_liaison", "auditor", "admin")
ROLE_WAREHOUSE = ("warehouse_keeper", "admin")
ROLE_VERIFY = ("warehouse_keeper", "review_secretary", "admin")
ROLE_HANDOVER = ("warehouse_keeper", "review_secretary", "admin")
ROLE_EXCEPTION_OPEN = ("warehouse_keeper", "review_secretary", "carrier_liaison", "admin")
ROLE_EXCEPTION_RESOLVE = ("warehouse_keeper", "review_secretary", "admin")
ROLE_EVIDENCE = ("warehouse_keeper", "review_secretary", "carrier_liaison", "admin")
ROLE_UNFREEZE = ("review_secretary", "admin")


class CustodyService:
    """协调样品保管的权限、幂等、事务、审计与保管链规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.foundation = DomainService(database, self.clock)
        self.database.connection.executescript(CUSTODY_SCHEMA)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _parse_time(self, value: Any, field: str) -> str:
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str):
        return self.foundation._actor(connection, actor_id)

    def _require(self, actor, roles: tuple[str, ...]) -> None:
        self.foundation._require(actor, *roles)

    def _identifier(self, value: str, field: str) -> str:
        return self.foundation._identifier(value, field)

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        return self.foundation._text(str(value), field, limit)

    def _optional_text(self, value: Any, field: str, limit: int = 120) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def _weight(self, value: Any, field: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValidationError(f"{field} 必须是非负数字")
        return int(value)

    def _condition(self, value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError(f"{field} 必须是对象")
        unknown = set(value) - set(CONDITION_KEYS) - {"orientation"}
        if unknown:
            raise ValidationError(f"{field} 包含不支持的保管条件项")
        result: dict[str, Any] = {}
        for key in CONDITION_KEYS:
            if key in value:
                number = value[key]
                if isinstance(number, bool) or not isinstance(number, (int, float)):
                    raise ValidationError(f"{field}.{key} 必须是数字")
                result[key] = number
        for low, high in RANGE_KEYS:
            if low in result and high in result and result[low] > result[high]:
                raise ValidationError(f"{field} 的范围下限不能高于上限")
        if "orientation" in value:
            result["orientation"] = self._text(value["orientation"], f"{field}.orientation", 40)
        return result

    def _capability(self, value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError(f"{field} 必须是对象")
        unknown = set(value) - set(CONDITION_KEYS) - {"orientations"}
        if unknown:
            raise ValidationError(f"{field} 包含不支持的环境能力项")
        result: dict[str, Any] = {}
        for key in CONDITION_KEYS:
            if key in value:
                number = value[key]
                if isinstance(number, bool) or not isinstance(number, (int, float)):
                    raise ValidationError(f"{field}.{key} 必须是数字")
                result[key] = number
        for low, high in RANGE_KEYS:
            if low in result and high in result and result[low] > result[high]:
                raise ValidationError(f"{field} 的范围下限不能高于上限")
        if "orientations" in value:
            orientations = value["orientations"]
            if not isinstance(orientations, list) or not orientations:
                raise ValidationError(f"{field}.orientations 必须是非空数组")
            result["orientations"] = [self._text(item, f"{field}.orientations", 40) for item in orientations]
        return result

    @staticmethod
    def _condition_supported(requirement: dict[str, Any], capability: dict[str, Any]) -> bool:
        """库位维持的环境范围必须完整落在作品可接受的范围内。"""

        for low, high in RANGE_KEYS:
            req_low, req_high = requirement.get(low), requirement.get(high)
            if req_low is None and req_high is None:
                continue
            cap_low, cap_high = capability.get(low), capability.get(high)
            if req_low is not None and (cap_low is None or cap_low < req_low):
                return False
            if req_high is not None and (cap_high is None or cap_high > req_high):
                return False
        orientation = requirement.get("orientation")
        if orientation:
            supported = capability.get("orientations") or []
            if orientation not in supported and "any" not in supported:
                return False
        return True

    def _next_seq(self, connection, table: str, site_id: str) -> int:
        row = connection.execute(
            f"SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM {table} WHERE site_id=?", (site_id,)
        ).fetchone()
        return int(row["next_seq"])

    def _get_site(self, connection, site_id: str) -> None:
        if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
            raise NotFoundError("场所不存在")

    def _get_component(self, connection, component_id: str):
        row = connection.execute(
            "SELECT * FROM custody_components WHERE component_id=?", (component_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("组件不存在")
        return row

    def _get_work(self, connection, work_id: str):
        row = connection.execute("SELECT * FROM custody_works WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return row

    def _get_package(self, connection, package_id: str):
        row = connection.execute("SELECT * FROM custody_packages WHERE package_id=?", (package_id,)).fetchone()
        if row is None:
            raise NotFoundError("包裹不存在")
        return row

    def _get_handover(self, connection, handover_id: str):
        row = connection.execute("SELECT * FROM custody_handovers WHERE handover_id=?", (handover_id,)).fetchone()
        if row is None:
            raise NotFoundError("交接单不存在")
        return row

    def _is_frozen(self, connection, component_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM custody_freezes WHERE component_id=? AND released_at IS NULL", (component_id,)
        ).fetchone() is not None

    def _open_exceptions(self, connection, component_id: str) -> list:
        return connection.execute(
            "SELECT * FROM custody_exceptions WHERE component_id=? AND status='open'", (component_id,)
        ).fetchall()

    def _has_open_exception(self, connection, component_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM custody_exceptions WHERE component_id=? AND status='open'", (component_id,)
        ).fetchone() is not None

    def _freeze(self, connection, *, component_id: str, reason: str, detail: str, actor_id: str) -> tuple[str, bool]:
        existing = connection.execute(
            "SELECT freeze_id FROM custody_freezes WHERE component_id=? AND released_at IS NULL", (component_id,)
        ).fetchone()
        if existing:
            return existing["freeze_id"], False
        freeze_id = uuid.uuid4().hex
        now = self._now()
        connection.execute(
            "INSERT INTO custody_freezes(freeze_id,component_id,reason,detail,frozen_by,frozen_at) VALUES(?,?,?,?,?,?)",
            (freeze_id, component_id, reason, detail, actor_id, now),
        )
        append_event(connection, actor_id=actor_id, action="custody.component.frozen",
                     resource_type="custody_component", resource_id=component_id,
                     detail={"reason": reason, "detail": detail}, occurred_at=now)
        return freeze_id, True

    def _open_exception_row(self, connection, *, site_id: str, kind: str, description: str,
                            actor_id: str, package_id: str | None = None, work_id: str | None = None,
                            component_id: str | None = None) -> str:
        exception_id = uuid.uuid4().hex
        now = self._now()
        connection.execute(
            "INSERT INTO custody_exceptions(exception_id,site_id,kind,package_id,work_id,component_id,"
            "description,status,opened_by,opened_at) VALUES(?,?,?,?,?,?,?,'open',?,?)",
            (exception_id, site_id, kind, package_id, work_id, component_id, description, actor_id, now),
        )
        append_event(connection, actor_id=actor_id, action="custody.exception.opened",
                     resource_type="custody_exception", resource_id=exception_id,
                     detail={"site_id": site_id, "kind": kind, "component_id": component_id,
                             "description": description}, occurred_at=now)
        return exception_id

    def _release_allocations(self, connection, *, component_id: str, actor_id: str, now: str) -> None:
        connection.execute(
            "UPDATE custody_allocations SET status='released', released_by=?, released_at=? "
            "WHERE component_id=? AND status='active' AND resource_type IN ('location','equipment')",
            (actor_id, now, component_id),
        )

    # ------------------------------------------------------------------
    # 预约入库
    # ------------------------------------------------------------------

    def create_reservation(self, *, request_id: str, actor_id: str, site_id: str, expected_at: str,
                           carrier_ref: str, expected_packages: int, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "expected_at": expected_at,
                   "carrier_ref": carrier_ref, "expected_packages": expected_packages, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            self._get_site(connection, site_id)
            expected_at = self._parse_time(expected_at, "expected_at")
            carrier_ref = self._text(carrier_ref, "carrier_ref", 120)
            if isinstance(expected_packages, bool) or not isinstance(expected_packages, int) or expected_packages < 1:
                raise ValidationError("expected_packages 必须是不小于 1 的整数")
            note = self._optional_text(note, "note", 400) or ""

            def create() -> tuple[str, str, dict[str, Any]]:
                reservation_id = uuid.uuid4().hex
                seq = self._next_seq(connection, "custody_reservations", site_id)
                now = self._now()
                connection.execute(
                    "INSERT INTO custody_reservations(reservation_id,site_id,seq,expected_at,carrier_ref,"
                    "expected_packages,status,note,created_by,created_at) VALUES(?,?,?,?,?,?,'pending',?,?,?)",
                    (reservation_id, site_id, seq, expected_at, carrier_ref, expected_packages, note, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="custody.reservation.created",
                             resource_type="custody_reservation", resource_id=reservation_id,
                             detail={"site_id": site_id, "seq": seq, "expected_at": expected_at,
                                     "carrier_ref": carrier_ref, "expected_packages": expected_packages},
                             occurred_at=now)
                return "custody_reservation", reservation_id, {"reservation_id": reservation_id, "seq": seq}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.reservation.create", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 包裹、作品与组件登记（扫码入库）
    # ------------------------------------------------------------------

    def _parse_component(self, component: Any, work_index: int, index: int,
                         inherited_condition: dict[str, Any]) -> dict[str, Any]:
        field = f"works[{work_index}].components[{index}]"
        if not isinstance(component, dict):
            raise ValidationError(f"{field} 必须是对象")
        condition = component.get("storage_condition")
        return {
            "component_code": self._text(component.get("component_code", ""), f"{field}.component_code", 64),
            "name": self._text(component.get("name", ""), f"{field}.name", 120),
            "weight_grams": self._weight(component.get("weight_grams"), f"{field}.weight_grams"),
            "seal_code": self._optional_text(component.get("seal_code"), f"{field}.seal_code", 64),
            "photo_digest": self._optional_text(component.get("photo_digest"), f"{field}.photo_digest", 128),
            "storage_condition": inherited_condition if condition is None else self._condition(condition, f"{field}.storage_condition"),
        }

    def _parse_work(self, work: Any, index: int) -> dict[str, Any]:
        field = f"works[{index}]"
        if not isinstance(work, dict):
            raise ValidationError(f"{field} 必须是对象")
        condition = self._condition(work.get("storage_condition"), f"{field}.storage_condition")
        components = work.get("components")
        if not isinstance(components, list) or not components:
            raise ValidationError(f"{field}.components 必须是非空数组")
        parsed = [self._parse_component(item, index, position, condition)
                  for position, item in enumerate(components)]
        codes = [item["component_code"] for item in parsed]
        if len(set(codes)) != len(codes):
            raise ValidationError(f"{field} 内组件编码重复")
        return {
            "work_key": self._text(work.get("work_key", ""), f"{field}.work_key", 64),
            "title": self._text(work.get("title", ""), f"{field}.title", 120),
            "storage_condition": condition,
            "components": parsed,
        }

    def check_in_package(self, *, request_id: str, actor_id: str, site_id: str, package_code: str,
                         reservation_id: str | None = None, carrier_waybill: str | None = None,
                         seal_code: str | None = None, weight_grams: Any = None,
                         photo_digest: str | None = None, works: Any = None) -> WriteReceipt:
        if not isinstance(works, list) or not works:
            raise ValidationError("works 必须是非空数组")
        parsed_works = [self._parse_work(work, index) for index, work in enumerate(works)]
        payload = {"actor_id": actor_id, "site_id": site_id, "package_code": package_code,
                   "reservation_id": reservation_id, "carrier_waybill": carrier_waybill,
                   "seal_code": seal_code, "weight_grams": weight_grams,
                   "photo_digest": photo_digest, "works": parsed_works}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            self._get_site(connection, site_id)
            package_code = self._text(package_code, "package_code", 64)
            carrier_waybill = self._optional_text(carrier_waybill, "carrier_waybill", 64)
            seal_code = self._optional_text(seal_code, "seal_code", 64)
            weight_grams = self._weight(weight_grams, "weight_grams")
            photo_digest = self._optional_text(photo_digest, "photo_digest", 128)
            reservation = None
            if reservation_id is not None:
                reservation = connection.execute(
                    "SELECT * FROM custody_reservations WHERE reservation_id=?", (reservation_id,)
                ).fetchone()
                if reservation is None:
                    raise NotFoundError("预约不存在")
                if reservation["site_id"] != site_id:
                    raise ValidationError("预约不属于该场所")
            content_hash = digest({"package_code": package_code, "carrier_waybill": carrier_waybill,
                                   "seal_code": seal_code, "weight_grams": weight_grams,
                                   "photo_digest": photo_digest, "works": parsed_works})

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM custody_packages WHERE site_id=? AND package_code=?",
                    (site_id, package_code),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] == content_hash:
                        return "custody_package", existing["package_id"], {"package_id": existing["package_id"]}
                    raise ConflictError("包裹编码已登记不同内容，疑似重复扫码")
                now = self._now()
                package_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_packages(package_id,site_id,reservation_id,package_code,carrier_waybill,"
                    "seal_code,weight_grams,photo_digest,content_hash,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'received',?,?)",
                    (package_id, site_id, reservation_id, package_code, carrier_waybill, seal_code,
                     weight_grams, photo_digest, content_hash, actor_id, now),
                )
                handover_id = uuid.uuid4().hex
                seq = self._next_seq(connection, "custody_handovers", site_id)
                from_party = f"carrier:{carrier_waybill}" if carrier_waybill else "carrier:unknown"
                to_party = f"warehouse:{site_id}"
                connection.execute(
                    "INSERT INTO custody_handovers(handover_id,site_id,seq,kind,from_party,to_party,status,"
                    "due_at,note,created_by,created_at,confirmed_by,confirmed_at) "
                    "VALUES(?,?,?,'check_in',?,?,'confirmed',NULL,'',?,?,?,?)",
                    (handover_id, site_id, seq, from_party, to_party, actor_id, now, actor_id, now),
                )
                component_total = 0
                for work in parsed_works:
                    work_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO custody_works(work_id,package_id,work_key,title,storage_condition_json,"
                        "status,created_at) VALUES(?,?,?,?,?,'intake',?)",
                        (work_id, package_id, work["work_key"], work["title"],
                         canonical_json(work["storage_condition"]), now),
                    )
                    for component in work["components"]:
                        component_id = uuid.uuid4().hex
                        try:
                            connection.execute(
                                "INSERT INTO custody_components(component_id,work_id,site_id,component_code,name,"
                                "weight_grams,seal_code,photo_digest,storage_condition_json,status,custodian,created_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,'pending_verification',?,?)",
                                (component_id, work_id, site_id, component["component_code"], component["name"],
                                 component["weight_grams"], component["seal_code"], component["photo_digest"],
                                 canonical_json(component["storage_condition"]), to_party, now),
                            )
                        except Exception as exc:
                            raise ConflictError(f"组件编码 {component['component_code']} 已存在，疑似重复扫码") from exc
                        connection.execute(
                            "INSERT INTO custody_handover_items(handover_id,component_id,seal_expected,seal_actual,"
                            "seal_match,condition_note) VALUES(?,?,?,?,1,'')",
                            (handover_id, component_id, component["seal_code"], component["seal_code"]),
                        )
                        component_total += 1
                if reservation is not None and reservation["status"] == "pending":
                    connection.execute(
                        "UPDATE custody_reservations SET status='arrived' WHERE reservation_id=?",
                        (reservation_id,),
                    )
                append_event(connection, actor_id=actor_id, action="custody.package.checked_in",
                             resource_type="custody_package", resource_id=package_id,
                             detail={"site_id": site_id, "package_code": package_code,
                                     "works": len(parsed_works), "components": component_total,
                                     "responsible": actor_id}, occurred_at=now)
                return "custody_package", package_id, {"package_id": package_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.package.check_in", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 双人核验与可评审样品
    # ------------------------------------------------------------------

    def verify_component(self, *, request_id: str, actor_id: str, component_id: str,
                         result: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "component_id": component_id, "result": result, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_VERIFY)
            component = self._get_component(connection, component_id)
            if component["status"] == "sent_back":
                raise ValidationError("组件已退件，不能核验")
            if result not in ("pass", "fail"):
                raise ValidationError("result 必须是 pass 或 fail")
            note = self._optional_text(note, "note", 400) or ""

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO custody_verifications(component_id,verifier_id,result,note,verified_at) "
                        "VALUES(?,?,?,?,?)",
                        (component_id, actor_id, result, note, now),
                    )
                except Exception as exc:
                    raise ConflictError("同一核验人不能对同一组件重复核验") from exc
                append_event(connection, actor_id=actor_id, action="custody.component.verification_recorded",
                             resource_type="custody_component", resource_id=component_id,
                             detail={"result": result, "note": note}, occurred_at=now)
                verified = False
                if result == "fail":
                    self._open_exception_row(connection, site_id=component["site_id"], kind="damaged",
                                             description=f"双人核验未通过：{note or '未说明'}",
                                             actor_id=actor_id, work_id=component["work_id"],
                                             component_id=component_id)
                else:
                    passes = connection.execute(
                        "SELECT COUNT(DISTINCT verifier_id) AS count FROM custody_verifications "
                        "WHERE component_id=? AND result='pass'", (component_id,)
                    ).fetchone()["count"]
                    if passes >= 2 and component["status"] == "pending_verification":
                        connection.execute(
                            "UPDATE custody_components SET status='verified' WHERE component_id=?",
                            (component_id,),
                        )
                        append_event(connection, actor_id=actor_id, action="custody.component.verified",
                                     resource_type="custody_component", resource_id=component_id,
                                     detail={"verifiers": passes}, occurred_at=now)
                        verified = True
                return "custody_component", component_id, {"component_id": component_id, "verified": verified}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.component.verify", payload=payload, create=create)

    def assemble_sample(self, *, request_id: str, actor_id: str, work_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_HANDOVER)
            work = self._get_work(connection, work_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if work["status"] == "reviewable":
                    return "custody_work", work_id, {"work_id": work_id}
                if work["status"] != "intake":
                    raise ConflictError("作品当前状态不能组成样品")
                components = connection.execute(
                    "SELECT * FROM custody_components WHERE work_id=? ORDER BY component_code", (work_id,)
                ).fetchall()
                blockers = []
                for component in components:
                    if component["status"] != "verified":
                        blockers.append(f"{component['component_code']}:未完成双人核验")
                    elif self._is_frozen(connection, component["component_id"]):
                        blockers.append(f"{component['component_code']}:已冻结")
                    elif self._has_open_exception(connection, component["component_id"]):
                        blockers.append(f"{component['component_code']}:存在未结异常")
                if blockers:
                    raise ConflictError("存在不能组成样品的组件：" + "；".join(blockers))
                now = self._now()
                connection.execute("UPDATE custody_works SET status='reviewable' WHERE work_id=?", (work_id,))
                append_event(connection, actor_id=actor_id, action="custody.sample.assembled",
                             resource_type="custody_work", resource_id=work_id,
                             detail={"components": len(components)}, occurred_at=now)
                return "custody_work", work_id, {"work_id": work_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.sample.assemble", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 库位、特殊设备与外借时段的原子占用
    # ------------------------------------------------------------------

    def register_location(self, *, request_id: str, actor_id: str, site_id: str, location_id: str,
                          capacity: int, conditions: Any = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "location_id": location_id,
                   "capacity": capacity, "conditions": conditions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            self._get_site(connection, site_id)
            location_id = self._identifier(location_id, "location_id")
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
                raise ValidationError("capacity 必须是不小于 1 的整数")
            capability = self._capability(conditions, "conditions")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO custody_locations(location_id,site_id,capacity,condition_json,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (location_id, site_id, capacity, canonical_json(capability), now),
                    )
                except Exception as exc:
                    raise ConflictError("库位编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="custody.location.registered",
                             resource_type="custody_location", resource_id=location_id,
                             detail={"site_id": site_id, "capacity": capacity, "conditions": capability},
                             occurred_at=now)
                return "custody_location", location_id, {"location_id": location_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.location.register", payload=payload, create=create)

    def register_equipment(self, *, request_id: str, actor_id: str, site_id: str, equipment_id: str,
                           name: str, total: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "equipment_id": equipment_id,
                   "name": name, "total": total}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            self._get_site(connection, site_id)
            equipment_id = self._identifier(equipment_id, "equipment_id")
            name = self._text(name, "name", 120)
            if isinstance(total, bool) or not isinstance(total, int) or total < 1:
                raise ValidationError("total 必须是不小于 1 的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO custody_equipment(equipment_id,site_id,name,total,created_at) VALUES(?,?,?,?,?)",
                        (equipment_id, site_id, name, total, now),
                    )
                except Exception as exc:
                    raise ConflictError("设备编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="custody.equipment.registered",
                             resource_type="custody_equipment", resource_id=equipment_id,
                             detail={"site_id": site_id, "name": name, "total": total}, occurred_at=now)
                return "custody_equipment", equipment_id, {"equipment_id": equipment_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.equipment.register", payload=payload, create=create)

    def allocate_resource(self, *, request_id: str, actor_id: str, site_id: str, resource_type: str,
                          resource_id: str, component_id: str | None = None,
                          start_at: str | None = None, end_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "resource_type": resource_type,
                   "resource_id": resource_id, "component_id": component_id,
                   "start_at": start_at, "end_at": end_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            self._get_site(connection, site_id)
            if resource_type not in RESOURCE_TYPES:
                raise ValidationError("resource_type 不在允许范围内")
            resource_id = self._text(resource_id, "resource_id", 64)
            component = None
            if component_id is not None:
                component = self._get_component(connection, component_id)
                if component["status"] != "verified":
                    raise ConflictError("只有双人核验通过且在库的组件才能占用资源")
                if self._is_frozen(connection, component_id):
                    raise ConflictError("组件已冻结，不能占用资源")
                if self._has_open_exception(connection, component_id):
                    raise ConflictError("组件存在未结异常，不能占用资源")
            if resource_type == "loan_slot":
                if start_at is None or end_at is None:
                    raise ValidationError("外借时段必须提供 start_at 和 end_at")
                start_at = self._parse_time(start_at, "start_at")
                end_at = self._parse_time(end_at, "end_at")
                if start_at >= end_at:
                    raise ValidationError("外借时段的开始必须早于结束")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                if resource_type == "location":
                    location = connection.execute(
                        "SELECT * FROM custody_locations WHERE location_id=? AND site_id=?",
                        (resource_id, site_id),
                    ).fetchone()
                    if location is None:
                        raise NotFoundError("库位不存在")
                    if component is None:
                        raise ValidationError("占用库位必须指定组件")
                    requirement = json.loads(component["storage_condition_json"])
                    capability = json.loads(location["condition_json"])
                    if not self._condition_supported(requirement, capability):
                        raise ConflictError("库位环境不满足作品保管条件")
                    active = connection.execute(
                        "SELECT COUNT(*) AS count FROM custody_allocations "
                        "WHERE resource_type='location' AND resource_id=? AND status='active'",
                        (resource_id,),
                    ).fetchone()["count"]
                    if active >= location["capacity"]:
                        raise ConflictError("库位容量不足")
                    duplicate = connection.execute(
                        "SELECT 1 FROM custody_allocations WHERE component_id=? AND resource_type='location' "
                        "AND status='active'", (component_id,)
                    ).fetchone()
                    if duplicate is not None:
                        raise ConflictError("组件已有生效库位，请先释放")
                elif resource_type == "equipment":
                    equipment = connection.execute(
                        "SELECT * FROM custody_equipment WHERE equipment_id=? AND site_id=?",
                        (resource_id, site_id),
                    ).fetchone()
                    if equipment is None:
                        raise NotFoundError("特殊设备不存在")
                    if component is None:
                        raise ValidationError("占用设备必须指定组件")
                    active = connection.execute(
                        "SELECT COUNT(*) AS count FROM custody_allocations "
                        "WHERE resource_type='equipment' AND resource_id=? AND status='active'",
                        (resource_id,),
                    ).fetchone()["count"]
                    if active >= equipment["total"]:
                        raise ConflictError("特殊设备占用已满")
                else:
                    overlap = connection.execute(
                        "SELECT 1 FROM custody_allocations WHERE resource_type='loan_slot' AND resource_id=? "
                        "AND status='active' AND start_at < ? AND end_at > ?",
                        (resource_id, end_at, start_at),
                    ).fetchone()
                    if overlap is not None:
                        raise ConflictError("外借时段与既有占用冲突")
                allocation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_allocations(allocation_id,site_id,resource_type,resource_id,component_id,"
                    "start_at,end_at,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
                    (allocation_id, site_id, resource_type, resource_id, component_id,
                     start_at, end_at, actor_id, now),
                )
                if resource_type == "location" and component is not None:
                    connection.execute(
                        "UPDATE custody_packages SET status='in_storage' WHERE package_id=(SELECT package_id "
                        "FROM custody_works WHERE work_id=?) AND status='received'",
                        (component["work_id"],),
                    )
                append_event(connection, actor_id=actor_id, action="custody.resource.allocated",
                             resource_type="custody_allocation", resource_id=allocation_id,
                             detail={"site_id": site_id, "resource_type": resource_type,
                                     "resource_id": resource_id, "component_id": component_id,
                                     "start_at": start_at, "end_at": end_at}, occurred_at=now)
                return "custody_allocation", allocation_id, {"allocation_id": allocation_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.resource.allocate", payload=payload, create=create)

    def release_resource(self, *, request_id: str, actor_id: str, allocation_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "allocation_id": allocation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_WAREHOUSE)
            allocation = connection.execute(
                "SELECT * FROM custody_allocations WHERE allocation_id=?", (allocation_id,)
            ).fetchone()
            if allocation is None:
                raise NotFoundError("资源占用不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if allocation["status"] != "active":
                    raise ConflictError("资源占用已经释放")
                now = self._now()
                connection.execute(
                    "UPDATE custody_allocations SET status='released', released_by=?, released_at=? "
                    "WHERE allocation_id=?",
                    (actor_id, now, allocation_id),
                )
                append_event(connection, actor_id=actor_id, action="custody.resource.released",
                             resource_type="custody_allocation", resource_id=allocation_id,
                             detail={"resource_type": allocation["resource_type"],
                                     "resource_id": allocation["resource_id"]}, occurred_at=now)
                return "custody_allocation", allocation_id, {"allocation_id": allocation_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.resource.release", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 交接与连续保管链
    # ------------------------------------------------------------------

    def create_handover(self, *, request_id: str, actor_id: str, site_id: str, kind: str,
                        to_party: str, component_ids: Any, due_at: str | None = None,
                        note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind, "to_party": to_party,
                   "component_ids": component_ids, "due_at": due_at, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_HANDOVER)
            self._get_site(connection, site_id)
            if kind not in HANDOVER_KINDS:
                raise ValidationError("kind 不在允许范围内")
            to_party = self._text(to_party, "to_party", 120)
            note = self._optional_text(note, "note", 400) or ""
            if not isinstance(component_ids, list) or not component_ids:
                raise ValidationError("component_ids 必须是非空数组")
            component_ids = [self._text(item, "component_ids", 64) for item in component_ids]
            if len(set(component_ids)) != len(component_ids):
                raise ValidationError("component_ids 存在重复")
            if due_at is not None:
                due_at = self._parse_time(due_at, "due_at")
            if kind == "loan" and due_at is None:
                raise ValidationError("借出交接必须约定归还期限 due_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                components = []
                custodians = set()
                for component_id in component_ids:
                    component = self._get_component(connection, component_id)
                    if component["site_id"] != site_id:
                        raise ValidationError("组件不属于该场所")
                    if self._is_frozen(connection, component_id):
                        raise ConflictError(f"组件 {component['component_code']} 已冻结")
                    if self._has_open_exception(connection, component_id):
                        raise ConflictError(f"组件 {component['component_code']} 存在未结异常")
                    pending = connection.execute(
                        "SELECT 1 FROM custody_handover_items i JOIN custody_handovers h "
                        "ON h.handover_id=i.handover_id WHERE i.component_id=? "
                        "AND h.status='pending_confirmation'", (component_id,)
                    ).fetchone()
                    if pending is not None:
                        raise ConflictError(f"组件 {component['component_code']} 存在待确认交接")
                    work = self._get_work(connection, component["work_id"])
                    if kind == "loan":
                        if work["status"] != "reviewable":
                            raise ConflictError("作品尚未组成可评审样品，不能借出")
                        if component["status"] != "verified":
                            raise ConflictError(f"组件 {component['component_code']} 不在库可借")
                    elif kind == "return":
                        if component["status"] != "on_loan":
                            raise ConflictError(f"组件 {component['component_code']} 不在借出状态")
                    elif kind == "transfer":
                        if component["status"] != "verified":
                            raise ConflictError(f"组件 {component['component_code']} 不在库，不能转场")
                    elif kind == "send_back":
                        if component["status"] not in ("verified", "on_loan"):
                            raise ConflictError(f"组件 {component['component_code']} 当前状态不能退件")
                    custodians.add(component["custodian"])
                    components.append(component)
                if len(custodians) != 1:
                    raise ValidationError("同一交接单的组件必须来自同一保管人")
                from_party = custodians.pop()
                if from_party == to_party:
                    raise ValidationError("交接双方不能相同")
                now = self._now()
                handover_id = uuid.uuid4().hex
                seq = self._next_seq(connection, "custody_handovers", site_id)
                connection.execute(
                    "INSERT INTO custody_handovers(handover_id,site_id,seq,kind,from_party,to_party,status,"
                    "due_at,note,created_by,created_at) VALUES(?,?,?,?,?,?,'pending_confirmation',?,?,?,?)",
                    (handover_id, site_id, seq, kind, from_party, to_party, due_at, note, actor_id, now),
                )
                for component in components:
                    connection.execute(
                        "INSERT INTO custody_handover_items(handover_id,component_id,seal_expected) VALUES(?,?,?)",
                        (handover_id, component["component_id"], component["seal_code"]),
                    )
                append_event(connection, actor_id=actor_id, action="custody.handover.created",
                             resource_type="custody_handover", resource_id=handover_id,
                             detail={"site_id": site_id, "kind": kind, "from_party": from_party,
                                     "to_party": to_party, "components": component_ids, "due_at": due_at},
                             occurred_at=now)
                return "custody_handover", handover_id, {"handover_id": handover_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.handover.create", payload=payload, create=create)

    def confirm_handover(self, *, request_id: str, actor_id: str, handover_id: str,
                         items: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "handover_id": handover_id, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_HANDOVER)
            handover = self._get_handover(connection, handover_id)
            if handover["status"] != "pending_confirmation":
                raise ConflictError("交接已确认，不能重复操作")
            if handover["created_by"] == actor_id:
                raise PermissionDenied("交接双方不能是同一人")
            if not isinstance(items, list) or not items:
                raise ValidationError("items 必须是非空数组")
            expected_items = {
                row["component_id"]: row
                for row in connection.execute(
                    "SELECT * FROM custody_handover_items WHERE handover_id=?", (handover_id,)
                ).fetchall()
            }
            provided: dict[str, dict[str, Any]] = {}
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValidationError(f"items[{index}] 必须是对象")
                component_id = item.get("component_id")
                if component_id not in expected_items:
                    raise ValidationError("交接明细与待确认组件不符")
                provided[component_id] = {
                    "seal_code_actual": self._optional_text(item.get("seal_code_actual"),
                                                            f"items[{index}].seal_code_actual", 64),
                    "condition_note": self._optional_text(item.get("condition_note"),
                                                          f"items[{index}].condition_note", 400) or "",
                }
            if set(provided) != set(expected_items):
                raise ValidationError("交接明细必须覆盖全部待确认组件")
            kind = handover["kind"]

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                handover_overdue = bool(handover["due_at"]) and now > handover["due_at"]
                overdue = handover_overdue
                mismatched: list[str] = []
                for component_id, detail in provided.items():
                    item_row = expected_items[component_id]
                    expected_seal = item_row["seal_expected"]
                    actual_seal = detail["seal_code_actual"]
                    match = 1 if (expected_seal or None) == (actual_seal or None) else 0
                    connection.execute(
                        "UPDATE custody_handover_items SET seal_actual=?, seal_match=?, condition_note=? "
                        "WHERE handover_id=? AND component_id=?",
                        (actual_seal, match, detail["condition_note"], handover_id, component_id),
                    )
                    component = self._get_component(connection, component_id)
                    if not match:
                        mismatched.append(component_id)
                        self._freeze(connection, component_id=component_id, reason="seal_mismatch",
                                     detail=f"handover:{handover_id}", actor_id=actor_id)
                        self._open_exception_row(
                            connection, site_id=handover["site_id"], kind="damaged",
                            description=f"交接封签不符：期望 {expected_seal or '无'}，实到 {actual_seal or '无'}",
                            actor_id=actor_id, work_id=component["work_id"], component_id=component_id)
                        continue
                    new_status = {"loan": "on_loan", "transfer": "verified",
                                  "return": "verified", "send_back": "sent_back"}[kind]
                    connection.execute(
                        "UPDATE custody_components SET custodian=?, status=? WHERE component_id=?",
                        (handover["to_party"], new_status, component_id),
                    )
                    if kind in ("loan", "transfer", "send_back"):
                        self._release_allocations(connection, component_id=component_id,
                                                  actor_id=actor_id, now=now)
                    if kind == "return":
                        loan = connection.execute(
                            "SELECT h.due_at FROM custody_handover_items i JOIN custody_handovers h "
                            "ON h.handover_id=i.handover_id WHERE i.component_id=? AND h.kind='loan' "
                            "AND h.status='confirmed' ORDER BY h.seq DESC LIMIT 1", (component_id,)
                        ).fetchone()
                        if loan and loan["due_at"] and now > loan["due_at"]:
                            self._freeze(connection, component_id=component_id, reason="overdue",
                                         detail=f"handover:{handover_id}", actor_id=actor_id)
                            overdue = True
                if handover_overdue:
                    for component_id in provided:
                        self._freeze(connection, component_id=component_id, reason="overdue",
                                     detail=f"handover:{handover_id}", actor_id=actor_id)
                connection.execute(
                    "UPDATE custody_handovers SET status='confirmed', confirmed_by=?, confirmed_at=? "
                    "WHERE handover_id=?",
                    (actor_id, now, handover_id),
                )
                if kind == "send_back":
                    self._refresh_send_back_status(connection, provided.keys())
                append_event(connection, actor_id=actor_id, action="custody.handover.confirmed",
                             resource_type="custody_handover", resource_id=handover_id,
                             detail={"kind": kind, "to_party": handover["to_party"], "overdue": overdue,
                                     "mismatched": mismatched}, occurred_at=now)
                return "custody_handover", handover_id, {
                    "handover_id": handover_id, "overdue": overdue, "mismatched": mismatched}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.handover.confirm", payload=payload, create=create)

    def _refresh_send_back_status(self, connection, component_ids) -> None:
        work_ids = {row["work_id"] for row in connection.execute(
            f"SELECT DISTINCT work_id FROM custody_components WHERE component_id IN "
            f"({','.join('?' for _ in component_ids)})", tuple(component_ids)).fetchall()}
        package_ids = set()
        for work_id in work_ids:
            remaining = connection.execute(
                "SELECT COUNT(*) AS count FROM custody_components WHERE work_id=? AND status!='sent_back'",
                (work_id,),
            ).fetchone()["count"]
            if remaining == 0:
                connection.execute("UPDATE custody_works SET status='sent_back' WHERE work_id=?", (work_id,))
                package_ids.add(connection.execute(
                    "SELECT package_id FROM custody_works WHERE work_id=?", (work_id,)).fetchone()["package_id"])
        for package_id in package_ids:
            remaining = connection.execute(
                "SELECT COUNT(*) AS count FROM custody_works WHERE package_id=? AND status!='sent_back'",
                (package_id,),
            ).fetchone()["count"]
            if remaining == 0:
                connection.execute(
                    "UPDATE custody_packages SET status='sent_back' WHERE package_id=?", (package_id,))

    # ------------------------------------------------------------------
    # 独立异常流程
    # ------------------------------------------------------------------

    def open_exception(self, *, request_id: str, actor_id: str, site_id: str, kind: str,
                       description: str, package_id: str | None = None, work_id: str | None = None,
                       component_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind, "description": description,
                   "package_id": package_id, "work_id": work_id, "component_id": component_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_EXCEPTION_OPEN)
            self._get_site(connection, site_id)
            if kind not in EXCEPTION_KINDS:
                raise ValidationError("kind 不在允许范围内")
            description = self._text(description, "description", 400)
            if package_id is None and work_id is None and component_id is None:
                raise ValidationError("异常必须关联包裹、作品或组件")
            if package_id is not None:
                self._get_package(connection, package_id)
            if work_id is not None:
                self._get_work(connection, work_id)
            if component_id is not None:
                self._get_component(connection, component_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                exception_id = self._open_exception_row(
                    connection, site_id=site_id, kind=kind, description=description, actor_id=actor_id,
                    package_id=package_id, work_id=work_id, component_id=component_id)
                return "custody_exception", exception_id, {"exception_id": exception_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.exception.open", payload=payload, create=create)

    def resolve_exception(self, *, request_id: str, actor_id: str, exception_id: str,
                          resolution_note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "exception_id": exception_id, "resolution_note": resolution_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_EXCEPTION_RESOLVE)
            exception = connection.execute(
                "SELECT * FROM custody_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if exception is None:
                raise NotFoundError("异常不存在")
            resolution_note = self._text(resolution_note, "resolution_note", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                if exception["status"] != "open":
                    raise ConflictError("异常已经结案")
                now = self._now()
                connection.execute(
                    "UPDATE custody_exceptions SET status='resolved', resolved_by=?, resolved_at=?, "
                    "resolution_note=? WHERE exception_id=?",
                    (actor_id, now, resolution_note, exception_id),
                )
                append_event(connection, actor_id=actor_id, action="custody.exception.resolved",
                             resource_type="custody_exception", resource_id=exception_id,
                             detail={"resolution_note": resolution_note}, occurred_at=now)
                return "custody_exception", exception_id, {"exception_id": exception_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.exception.resolve", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 冻结与解冻
    # ------------------------------------------------------------------

    def freeze_component(self, *, request_id: str, actor_id: str, component_id: str,
                         reason: str = "manual", detail: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "component_id": component_id, "reason": reason, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_HANDOVER)
            self._get_component(connection, component_id)
            if reason not in FREEZE_REASONS:
                raise ValidationError("reason 不在允许范围内")
            detail = self._optional_text(detail, "detail", 400) or ""

            def create() -> tuple[str, str, dict[str, Any]]:
                freeze_id, created = self._freeze(connection, component_id=component_id, reason=reason,
                                                  detail=detail, actor_id=actor_id)
                if not created:
                    raise ConflictError("组件已处于冻结状态")
                return "custody_freeze", freeze_id, {"freeze_id": freeze_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.component.freeze", payload=payload, create=create)

    def unfreeze_component(self, *, request_id: str, actor_id: str, component_id: str,
                           note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "component_id": component_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_UNFREEZE)
            self._get_component(connection, component_id)
            note = self._optional_text(note, "note", 400) or ""

            def create() -> tuple[str, str, dict[str, Any]]:
                freeze = connection.execute(
                    "SELECT * FROM custody_freezes WHERE component_id=? AND released_at IS NULL",
                    (component_id,),
                ).fetchone()
                if freeze is None:
                    raise ConflictError("组件未处于冻结状态")
                if self._has_open_exception(connection, component_id):
                    raise ConflictError("组件存在未结异常，不能解冻")
                now = self._now()
                connection.execute(
                    "UPDATE custody_freezes SET released_by=?, released_at=? WHERE freeze_id=?",
                    (actor_id, now, freeze["freeze_id"]),
                )
                append_event(connection, actor_id=actor_id, action="custody.component.unfrozen",
                             resource_type="custody_component", resource_id=component_id,
                             detail={"freeze_id": freeze["freeze_id"], "note": note}, occurred_at=now)
                return "custody_freeze", freeze["freeze_id"], {"freeze_id": freeze["freeze_id"]}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.component.unfreeze", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 补录证据（只增不改）
    # ------------------------------------------------------------------

    def append_evidence(self, *, request_id: str, actor_id: str, site_id: str, target_type: str,
                        target_id: str, note: str, attachment_digest: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "target_type": target_type,
                   "target_id": target_id, "note": note, "attachment_digest": attachment_digest}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_EVIDENCE)
            self._get_site(connection, site_id)
            if target_type not in EVIDENCE_TARGETS:
                raise ValidationError("target_type 不在允许范围内")
            target_id = self._text(target_id, "target_id", 64)
            note = self._text(note, "note", 400)
            attachment_digest = self._optional_text(attachment_digest, "attachment_digest", 128)
            self._evidence_target(connection, target_type, target_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO custody_evidence(evidence_id,site_id,target_type,target_id,note,"
                    "attachment_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (evidence_id, site_id, target_type, target_id, note, attachment_digest, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="custody.evidence.appended",
                             resource_type="custody_evidence", resource_id=evidence_id,
                             detail={"site_id": site_id, "target_type": target_type, "target_id": target_id},
                             occurred_at=now)
                return "custody_evidence", evidence_id, {"evidence_id": evidence_id}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.evidence.append", payload=payload, create=create)

    def _evidence_target(self, connection, target_type: str, target_id: str) -> None:
        table = {"package": "custody_packages", "work": "custody_works", "component": "custody_components",
                 "handover": "custody_handovers", "exception": "custody_exceptions",
                 "reservation": "custody_reservations"}[target_type]
        key = {"package": "package_id", "work": "work_id", "component": "component_id",
               "handover": "handover_id", "exception": "exception_id",
               "reservation": "reservation_id"}[target_type]
        if connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (target_id,)).fetchone() is None:
            raise NotFoundError("补录目标不存在")

    # ------------------------------------------------------------------
    # 逾期巡检
    # ------------------------------------------------------------------

    def sweep_overdue(self, *, request_id: str, actor_id: str, site_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, ROLE_HANDOVER)
            self._get_site(connection, site_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                frozen: list[str] = []
                pending = connection.execute(
                    "SELECT handover_id FROM custody_handovers WHERE site_id=? AND status='pending_confirmation' "
                    "AND due_at IS NOT NULL AND due_at < ?", (site_id, now)
                ).fetchall()
                targets: list[tuple[str, str]] = []
                for row in pending:
                    for item in connection.execute(
                        "SELECT component_id FROM custody_handover_items WHERE handover_id=?",
                        (row["handover_id"],),
                    ).fetchall():
                        targets.append((item["component_id"], row["handover_id"]))
                on_loan = connection.execute(
                    "SELECT component_id FROM custody_components WHERE site_id=? AND status='on_loan'",
                    (site_id,),
                ).fetchall()
                for row in on_loan:
                    handover = connection.execute(
                        "SELECT h.due_at FROM custody_handover_items i JOIN custody_handovers h "
                        "ON h.handover_id=i.handover_id WHERE i.component_id=? AND h.kind='loan' "
                        "AND h.status='confirmed' ORDER BY h.seq DESC LIMIT 1", (row["component_id"],)
                    ).fetchone()
                    if handover and handover["due_at"] and handover["due_at"] < now:
                        targets.append((row["component_id"], "loan"))
                for component_id, source in targets:
                    _, created = self._freeze(connection, component_id=component_id, reason="overdue",
                                              detail=f"sweep:{source}", actor_id=actor_id)
                    if created:
                        frozen.append(component_id)
                if frozen:
                    append_event(connection, actor_id=actor_id, action="custody.overdue.swept",
                                 resource_type="site", resource_id=site_id,
                                 detail={"frozen_components": frozen}, occurred_at=now)
                return "site", site_id, {"site_id": site_id, "frozen_components": frozen}

            return self.foundation._idempotent(connection, request_id=request_id,
                                               action="custody.overdue.sweep", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：保管链、责任区间与历史时点
    # ------------------------------------------------------------------

    def _component_dict(self, connection, row) -> dict[str, Any]:
        component_id = row["component_id"]
        location = connection.execute(
            "SELECT resource_id FROM custody_allocations WHERE component_id=? AND resource_type='location' "
            "AND status='active' ORDER BY created_at DESC LIMIT 1", (component_id,)
        ).fetchone()
        passes = connection.execute(
            "SELECT COUNT(*) AS count FROM custody_verifications WHERE component_id=? AND result='pass'",
            (component_id,),
        ).fetchone()["count"]
        return {
            "component_id": component_id,
            "work_id": row["work_id"],
            "site_id": row["site_id"],
            "component_code": row["component_code"],
            "name": row["name"],
            "weight_grams": row["weight_grams"],
            "seal_code": row["seal_code"],
            "photo_digest": row["photo_digest"],
            "storage_condition": json.loads(row["storage_condition_json"]),
            "status": row["status"],
            "custodian": row["custodian"],
            "frozen": self._is_frozen(connection, component_id),
            "open_exceptions": len(self._open_exceptions(connection, component_id)),
            "verifications_passed": passes,
            "location_id": location["resource_id"] if location else None,
            "created_at": row["created_at"],
        }

    def custody_chain(self, *, actor_id: str, component_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_READ)
        component = self._get_component(connection, component_id)
        rows = connection.execute(
            "SELECT h.handover_id, h.seq, h.kind, h.from_party, h.to_party, h.status, h.due_at, "
            "h.created_by, h.created_at, h.confirmed_by, h.confirmed_at, "
            "i.seal_expected, i.seal_actual, i.seal_match, i.condition_note "
            "FROM custody_handover_items i JOIN custody_handovers h ON h.handover_id=i.handover_id "
            "WHERE i.component_id=? ORDER BY h.seq", (component_id,)
        ).fetchall()
        return {"component": self._component_dict(connection, component),
                "handovers": [dict(row) for row in rows]}

    def damage_responsibility(self, *, actor_id: str, component_id: str,
                              handover_id: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_READ)
        self._get_component(connection, component_id)
        rows = connection.execute(
            "SELECT h.handover_id, h.seq, h.kind, h.from_party, h.to_party, h.confirmed_at, "
            "i.seal_expected, i.seal_actual, i.seal_match, i.condition_note "
            "FROM custody_handover_items i JOIN custody_handovers h ON h.handover_id=i.handover_id "
            "WHERE i.component_id=? AND h.status='confirmed' ORDER BY h.seq", (component_id,)
        ).fetchall()
        intervals = [dict(row) for row in rows]
        detection = None
        target = None
        if handover_id is not None:
            target = next((row for row in rows if row["handover_id"] == handover_id), None)
            if target is None:
                raise NotFoundError("该组件的保管链中不存在此交接")
        else:
            target = next((row for row in reversed(rows) if row["seal_match"] == 0), None)
        if target is not None:
            index = rows.index(target)
            detection = {
                "handover_id": target["handover_id"],
                "interval_start": rows[index - 1]["confirmed_at"] if index else None,
                "interval_end": target["confirmed_at"],
                "responsible_party": target["from_party"],
                "seal_expected": target["seal_expected"],
                "seal_actual": target["seal_actual"],
                "condition_note": target["condition_note"],
            }
        return {"component_id": component_id, "intervals": intervals, "detection": detection}

    def location_at(self, *, actor_id: str, component_id: str, at: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_READ)
        self._get_component(connection, component_id)
        at = self._parse_time(at, "at")
        allocation = connection.execute(
            "SELECT resource_id FROM custody_allocations WHERE component_id=? AND resource_type='location' "
            "AND created_at<=? AND (released_at IS NULL OR released_at>?) "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (component_id, at, at)
        ).fetchone()
        handover = connection.execute(
            "SELECT h.to_party, h.kind, h.confirmed_at FROM custody_handover_items i "
            "JOIN custody_handovers h ON h.handover_id=i.handover_id "
            "WHERE i.component_id=? AND h.status='confirmed' AND h.confirmed_at<=? "
            "ORDER BY h.seq DESC LIMIT 1", (component_id, at)
        ).fetchone()
        return {"component_id": component_id, "at": at,
                "location_id": allocation["resource_id"] if allocation else None,
                "custodian": handover["to_party"] if handover else None,
                "last_handover_kind": handover["kind"] if handover else None}

    # ------------------------------------------------------------------
    # 列表与角色视图
    # ------------------------------------------------------------------

    def _list_reservations(self, connection, site_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM custody_packages p WHERE p.reservation_id=r.reservation_id) "
            "AS arrived_packages FROM custody_reservations r WHERE r.site_id=? ORDER BY r.seq", (site_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_reservations(self, *, actor_id: str, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_READ)
        self._get_site(connection, site_id)
        return self._list_reservations(connection, site_id)

    def _list_packages(self, connection, site_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM custody_works w WHERE w.package_id=p.package_id) AS works, "
            "(SELECT COUNT(*) FROM custody_components c JOIN custody_works w ON c.work_id=w.work_id "
            "WHERE w.package_id=p.package_id) AS components "
            "FROM custody_packages p WHERE p.site_id=? ORDER BY p.created_at, p.package_code", (site_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_packages(self, *, actor_id: str, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_READ)
        self._get_site(connection, site_id)
        return self._list_packages(connection, site_id)

    def package_detail(self, *, actor_id: str, package_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_READ)
        package = self._get_package(connection, package_id)
        works = []
        for work in connection.execute(
            "SELECT * FROM custody_works WHERE package_id=? ORDER BY work_key", (package_id,)
        ).fetchall():
            components = [self._component_dict(connection, row) for row in connection.execute(
                "SELECT * FROM custody_components WHERE work_id=? ORDER BY component_code", (work["work_id"],)
            ).fetchall()]
            works.append({"work_id": work["work_id"], "work_key": work["work_key"], "title": work["title"],
                          "status": work["status"],
                          "storage_condition": json.loads(work["storage_condition_json"]),
                          "components": components})
        return {"package_id": package["package_id"], "site_id": package["site_id"],
                "package_code": package["package_code"], "reservation_id": package["reservation_id"],
                "carrier_waybill": package["carrier_waybill"], "seal_code": package["seal_code"],
                "weight_grams": package["weight_grams"], "photo_digest": package["photo_digest"],
                "status": package["status"], "created_by": package["created_by"],
                "created_at": package["created_at"], "works": works}

    def _pending_handovers(self, connection, site_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM custody_handovers WHERE site_id=? AND status='pending_confirmation' ORDER BY seq",
            (site_id,),
        ).fetchall()
        result = []
        for row in rows:
            components = [item["component_id"] for item in connection.execute(
                "SELECT component_id FROM custody_handover_items WHERE handover_id=?", (row["handover_id"],)
            ).fetchall()]
            result.append({"handover_id": row["handover_id"], "kind": row["kind"],
                           "from_party": row["from_party"], "to_party": row["to_party"],
                           "due_at": row["due_at"], "note": row["note"],
                           "created_by": row["created_by"], "created_at": row["created_at"],
                           "components": components})
        return result

    def pending_handovers(self, *, actor_id: str, site_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_READ)
        self._get_site(connection, site_id)
        return self._pending_handovers(connection, site_id)

    def _open_exception_dicts(self, connection, site_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM custody_exceptions WHERE site_id=? AND status='open' ORDER BY opened_at", (site_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def warehouse_view(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ROLE_WAREHOUSE)
        self._get_site(connection, site_id)
        components = [self._component_dict(connection, row) for row in connection.execute(
            "SELECT * FROM custody_components WHERE site_id=? ORDER BY component_code", (site_id,)
        ).fetchall()]
        locations = []
        for row in connection.execute(
            "SELECT * FROM custody_locations WHERE site_id=? ORDER BY location_id", (site_id,)
        ).fetchall():
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM custody_allocations WHERE resource_type='location' "
                "AND resource_id=? AND status='active'", (row["location_id"],)
            ).fetchone()["count"]
            locations.append({"location_id": row["location_id"], "capacity": row["capacity"],
                              "active": active, "free": row["capacity"] - active,
                              "conditions": json.loads(row["condition_json"])})
        equipment = []
        for row in connection.execute(
            "SELECT * FROM custody_equipment WHERE site_id=? ORDER BY equipment_id", (site_id,)
        ).fetchall():
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM custody_allocations WHERE resource_type='equipment' "
                "AND resource_id=? AND status='active'", (row["equipment_id"],)
            ).fetchone()["count"]
            equipment.append({"equipment_id": row["equipment_id"], "name": row["name"],
                              "total": row["total"], "active": active, "free": row["total"] - active})
        return {"site_id": site_id, "packages": self._list_packages(connection, site_id),
                "components": components, "locations": locations, "equipment": equipment,
                "pending_handovers": self._pending_handovers(connection, site_id),
                "open_exceptions": self._open_exception_dicts(connection, site_id)}

    def secretary_view(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ("review_secretary", "admin"))
        self._get_site(connection, site_id)
        now = self._now()
        reviewable = [dict(row) for row in connection.execute(
            "SELECT w.work_id, w.work_key, w.title, p.package_code FROM custody_works w "
            "JOIN custody_packages p ON p.package_id=w.package_id "
            "WHERE p.site_id=? AND w.status='reviewable' ORDER BY w.work_key", (site_id,)
        ).fetchall()]
        loans = []
        for row in connection.execute(
            "SELECT * FROM custody_components WHERE site_id=? AND status='on_loan' ORDER BY component_code",
            (site_id,),
        ).fetchall():
            handover = connection.execute(
                "SELECT h.due_at, h.confirmed_at FROM custody_handover_items i JOIN custody_handovers h "
                "ON h.handover_id=i.handover_id WHERE i.component_id=? AND h.kind='loan' "
                "AND h.status='confirmed' ORDER BY h.seq DESC LIMIT 1", (row["component_id"],)
            ).fetchone()
            due_at = handover["due_at"] if handover else None
            loans.append({"component_id": row["component_id"], "component_code": row["component_code"],
                          "name": row["name"], "borrower": row["custodian"], "due_at": due_at,
                          "overdue": bool(due_at) and now > due_at})
        frozen = [dict(row) for row in connection.execute(
            "SELECT f.freeze_id, f.component_id, f.reason, f.detail, f.frozen_by, f.frozen_at, "
            "c.component_code FROM custody_freezes f JOIN custody_components c "
            "ON c.component_id=f.component_id WHERE c.site_id=? AND f.released_at IS NULL "
            "ORDER BY f.frozen_at", (site_id,)
        ).fetchall()]
        return {"site_id": site_id, "reviewable_works": reviewable, "loans": loans,
                "frozen_components": frozen,
                "pending_handovers": self._pending_handovers(connection, site_id)}

    def carrier_view(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ("carrier_liaison", "admin"))
        self._get_site(connection, site_id)
        packages = []
        for row in self._list_packages(connection, site_id):
            evidence = connection.execute(
                "SELECT COUNT(*) AS count FROM custody_evidence WHERE target_type='package' AND target_id=?",
                (row["package_id"],),
            ).fetchone()["count"]
            packages.append({"package_id": row["package_id"], "package_code": row["package_code"],
                             "carrier_waybill": row["carrier_waybill"], "seal_code": row["seal_code"],
                             "status": row["status"], "created_at": row["created_at"],
                             "evidence_notes": evidence})
        misdeliveries = [dict(row) for row in connection.execute(
            "SELECT * FROM custody_exceptions WHERE site_id=? AND kind='misdelivered' ORDER BY opened_at",
            (site_id,),
        ).fetchall()]
        return {"site_id": site_id, "reservations": self._list_reservations(connection, site_id),
                "packages": packages, "misdeliveries": misdeliveries}

    def auditor_view(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, ("auditor", "admin"))
        self._get_site(connection, site_id)
        valid, count = self.foundation.verify_audit()
        events = [event for event in self.foundation.audit_events()
                  if event["action"].startswith("custody.")]
        evidence = [dict(row) for row in connection.execute(
            "SELECT * FROM custody_evidence WHERE site_id=? ORDER BY created_at, evidence_id", (site_id,)
        ).fetchall()]
        freezes = [dict(row) for row in connection.execute(
            "SELECT f.* FROM custody_freezes f JOIN custody_components c ON c.component_id=f.component_id "
            "WHERE c.site_id=? ORDER BY f.frozen_at", (site_id,)
        ).fetchall()]
        exceptions = [dict(row) for row in connection.execute(
            "SELECT * FROM custody_exceptions WHERE site_id=? ORDER BY opened_at", (site_id,)
        ).fetchall()]
        return {"site_id": site_id, "audit_valid": valid, "audit_events_total": count,
                "custody_events": events, "evidence": evidence, "freezes": freezes,
                "exceptions": exceptions}
