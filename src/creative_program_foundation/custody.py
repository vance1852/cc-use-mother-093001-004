"""实物复核阶段的样品交接与保管领域服务。

在基础服务（组织、操作者、场所、幂等回执、哈希审计链）之上实现：

- 预约入库：预约顺序按场所持久化，包裹、作品、组件、封签、重量、
  照片摘要、保管条件与责任人全程登记，承运单允许晚于实物补登；
- 双人核验：组件须两名不同操作者核验通过，作品才成为可评审样品；
- 独立异常流程：缺件、错投、破损、封签差异、逾期、重量差异都生成异常单；
- 原子占用：库位容量、特殊设备容量与外借时段在 IMMEDIATE 事务内原子占用；
- 连续保管链：评委借阅、复核转场、返还与退件都由交接单串联，
  交出方由服务端从最近已确认交接推导，接收方确认后生效；
- 组件级冻结：逾期或封签差异只冻结相关组件，不阻断同包裹中的完好作品；
- 补录证据：证据只能追加，原交接记录确认后不可覆盖；
- 历史还原：按任意时点重放交接链还原位置，损伤责任区间可查询；
- 重启保持：预约顺序、资源占用与待确认交接全部落在 SQLite。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor
from .service import IDENTIFIER
from .storage import Database


CUSTODY_SCHEMA = """
CREATE TABLE IF NOT EXISTS custody_reservations (
    reservation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sequence INTEGER NOT NULL,
    carrier_org TEXT NOT NULL,
    waybill_no TEXT NOT NULL DEFAULT '',
    expected_packages INTEGER NOT NULL CHECK(expected_packages >= 1),
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scheduled','receiving','closed','cancelled')),
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, sequence)
);
CREATE TABLE IF NOT EXISTS custody_packages (
    package_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES custody_reservations(reservation_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    tracking_no TEXT NOT NULL,
    seal_no TEXT NOT NULL DEFAULT '',
    weight_grams INTEGER NOT NULL DEFAULT 0,
    photo_digest TEXT NOT NULL DEFAULT '',
    storage_condition TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('received','opened','returned')),
    received_by TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(site_id, tracking_no)
);
CREATE TABLE IF NOT EXISTS custody_scan_events (
    scan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    code TEXT NOT NULL,
    result TEXT NOT NULL,
    package_id TEXT,
    actor_id TEXT NOT NULL,
    scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_works (
    work_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL REFERENCES custody_packages(package_id),
    site_id TEXT NOT NULL,
    entry_no TEXT NOT NULL,
    title TEXT NOT NULL,
    declared_components INTEGER NOT NULL CHECK(declared_components >= 1),
    storage_condition TEXT NOT NULL DEFAULT '',
    orientation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(package_id, entry_no)
);
CREATE TABLE IF NOT EXISTS custody_components (
    component_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES custody_works(work_id),
    site_id TEXT NOT NULL,
    component_no TEXT NOT NULL,
    name TEXT NOT NULL,
    weight_grams INTEGER NOT NULL DEFAULT 0,
    fragile INTEGER NOT NULL CHECK(fragile IN (0, 1)) DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN
        ('pending_verify','verified','missing','damaged','misdelivered','returned')),
    verify_count INTEGER NOT NULL DEFAULT 0,
    location_id TEXT,
    equipment_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, component_no)
);
CREATE TABLE IF NOT EXISTS custody_verifications (
    verification_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES custody_components(component_id),
    actor_id TEXT NOT NULL,
    weight_grams INTEGER,
    seal_no TEXT NOT NULL DEFAULT '',
    photo_digest TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL,
    UNIQUE(component_id, actor_id)
);
CREATE TABLE IF NOT EXISTS custody_locations (
    location_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    code TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    occupied INTEGER NOT NULL DEFAULT 0 CHECK(occupied >= 0),
    condition_class TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL,
    UNIQUE(site_id, code)
);
CREATE TABLE IF NOT EXISTS custody_equipment (
    equipment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    name TEXT NOT NULL,
    equipment_type TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    occupied INTEGER NOT NULL DEFAULT 0 CHECK(occupied >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, name)
);
CREATE TABLE IF NOT EXISTS custody_loans (
    loan_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES custody_components(component_id),
    site_id TEXT NOT NULL,
    borrower_id TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('booked','active','returned','overdue','cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    returned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_custody_loans_component ON custody_loans(component_id, status);
CREATE TABLE IF NOT EXISTS custody_handovers (
    handover_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES custody_components(component_id),
    site_id TEXT NOT NULL,
    handover_type TEXT NOT NULL CHECK(handover_type IN
        ('intake','shelve','loan_out','loan_return','transfer','return_out')),
    from_holder TEXT NOT NULL,
    to_holder TEXT NOT NULL,
    seal_no TEXT NOT NULL DEFAULT '',
    confirmed_seal_no TEXT NOT NULL DEFAULT '',
    weight_grams INTEGER,
    confirmed_weight_grams INTEGER,
    photo_digest TEXT NOT NULL DEFAULT '',
    condition_note TEXT NOT NULL DEFAULT '',
    condition_state TEXT NOT NULL CHECK(condition_state IN ('good','damaged','unknown')) DEFAULT 'good',
    confirmed_condition_note TEXT NOT NULL DEFAULT '',
    confirmed_condition_state TEXT NOT NULL DEFAULT '',
    location_id TEXT,
    loan_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed')),
    initiated_by TEXT NOT NULL,
    confirmed_by TEXT,
    occurred_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_custody_handover_pending
    ON custody_handovers(component_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_custody_handover_component
    ON custody_handovers(component_id, occurred_at);
CREATE TABLE IF NOT EXISTS custody_evidence (
    evidence_id TEXT PRIMARY KEY,
    handover_id TEXT NOT NULL REFERENCES custody_handovers(handover_id),
    actor_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    content TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_exceptions (
    exception_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    exception_type TEXT NOT NULL CHECK(exception_type IN
        ('missing','misdelivered','damaged','seal_mismatch','overdue','weight_mismatch')),
    component_id TEXT,
    package_id TEXT,
    work_id TEXT,
    handover_id TEXT,
    loan_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('open','investigating','resolved')),
    detail TEXT NOT NULL DEFAULT '',
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS custody_freezes (
    freeze_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES custody_components(component_id),
    site_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    exception_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    frozen_by TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_custody_freeze_active
    ON custody_freezes(component_id) WHERE status='active';
"""

HOLDER_PATTERN = re.compile(r"^(warehouse|reviewer|carrier|secretary):[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
EXCEPTION_TYPES = frozenset({"missing", "misdelivered", "damaged", "seal_mismatch", "overdue", "weight_mismatch"})
CONDITION_EXCEPTION_STATUS = {"missing": "missing", "misdelivered": "misdelivered", "damaged": "damaged"}
API_HANDOVER_TYPES = frozenset({"loan_out", "loan_return", "transfer", "return_out"})
DAMAGE_EXCEPTION_TYPES = ("damaged", "weight_mismatch")


def _iso(value: datetime) -> str:
    """生成固定宽度、可按字符串排序的 UTC 时间文本。"""

    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: Any, field: str) -> str:
    """把输入规范化为带时区的 UTC 时间文本。"""

    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return _iso(parsed)


class CustodyService:
    """协调样品保管的权限、幂等、事务、冻结与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.database.connection.executescript(CUSTODY_SCHEMA)

    # ---- 基础工具 -------------------------------------------------------

    def _now(self) -> str:
        return _iso(self.clock.now())

    def _identifier(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200, allow_empty: bool = False) -> str:
        value = str(value).strip()
        if not value:
            if allow_empty:
                return ""
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        if len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _weight(self, value: Any, field: str) -> int | None:
        if value is None:
            return None
        try:
            weight = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是非负整数") from exc
        if weight < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return weight

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _check_site_scope(self, actor: Actor, site) -> None:
        if actor.role in ("admin", "carrier", "reviewer"):
            return
        if actor.organization_id != site["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")

    def _component(self, connection, component_id: str):
        row = connection.execute(
            "SELECT * FROM custody_components WHERE component_id=?", (component_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("组件不存在")
        return row

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """与基础服务共享 request_receipts 表；重放时返回首次存储的结果。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            stored = json.loads(row["response_json"])
            stored["replayed"] = True
            return stored
        resource_type, resource_id, response = create()
        result = {"request_id": request_id, "replayed": False, **response}
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(result), self._now()),
        )
        return result

    # ---- 保管链与冻结工具 -------------------------------------------------

    def _current_holder(self, connection, component_id: str, site_id: str) -> str:
        row = connection.execute(
            "SELECT to_holder FROM custody_handovers WHERE component_id=? AND status='confirmed' "
            "ORDER BY COALESCE(confirmed_at, occurred_at) DESC, rowid DESC LIMIT 1",
            (component_id,),
        ).fetchone()
        return row["to_holder"] if row else f"warehouse:{site_id}"

    def _pending_handover(self, connection, component_id: str):
        return connection.execute(
            "SELECT * FROM custody_handovers WHERE component_id=? AND status='pending'", (component_id,)
        ).fetchone()

    def _active_freeze(self, connection, component_id: str):
        return connection.execute(
            "SELECT * FROM custody_freezes WHERE component_id=? AND status='active'", (component_id,)
        ).fetchone()

    def _freeze(self, connection, *, component_id: str, site_id: str, reason: str,
                exception_id: str | None, actor_id: str) -> str | None:
        if self._active_freeze(connection, component_id):
            return None
        freeze_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO custody_freezes(freeze_id,component_id,site_id,reason,exception_id,status,frozen_by,frozen_at) "
            "VALUES(?,?,?,?,?,'active',?,?)",
            (freeze_id, component_id, site_id, reason, exception_id, actor_id, self._now()),
        )
        return freeze_id

    def _open_exception(self, connection, *, site_id: str, exception_type: str, actor_id: str,
                        component_id: str | None = None, package_id: str | None = None,
                        work_id: str | None = None, handover_id: str | None = None,
                        loan_id: str | None = None, detail: str = "") -> str:
        """在独立异常流程中登记异常；组件级异常同步冻结组件。"""

        exception_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO custody_exceptions(exception_id,site_id,exception_type,component_id,package_id,work_id,"
            "handover_id,loan_id,status,detail,opened_by,opened_at) VALUES(?,?,?,?,?,?,?,?,'open',?,?,?)",
            (exception_id, site_id, exception_type, component_id, package_id, work_id,
             handover_id, loan_id, detail, actor_id, self._now()),
        )
        if component_id:
            new_status = CONDITION_EXCEPTION_STATUS.get(exception_type)
            if new_status:
                connection.execute(
                    "UPDATE custody_components SET status=? WHERE component_id=?", (new_status, component_id)
                )
            self._freeze(connection, component_id=component_id, site_id=site_id,
                         reason=exception_type, exception_id=exception_id, actor_id=actor_id)
        append_event(connection, actor_id=actor_id, action="custody.exception.opened",
                     resource_type="exception", resource_id=exception_id,
                     detail={"site_id": site_id, "exception_type": exception_type, "component_id": component_id,
                             "package_id": package_id, "work_id": work_id, "handover_id": handover_id,
                             "loan_id": loan_id, "detail": detail},
                     occurred_at=self._now())
        return exception_id

    def _release_occupancy(self, connection, component) -> None:
        if component["location_id"]:
            connection.execute(
                "UPDATE custody_locations SET occupied=occupied-1 WHERE location_id=? AND occupied>0",
                (component["location_id"],),
            )
        if component["equipment_id"]:
            connection.execute(
                "UPDATE custody_equipment SET occupied=occupied-1 WHERE equipment_id=? AND occupied>0",
                (component["equipment_id"],),
            )
        connection.execute(
            "UPDATE custody_components SET location_id=NULL, equipment_id=NULL WHERE component_id=?",
            (component["component_id"],),
        )

    def _move_location(self, connection, component, location_id: str) -> None:
        """在事务内原子地占用新库位并释放旧库位。"""

        if component["location_id"] == location_id:
            return
        cursor = connection.execute(
            "UPDATE custody_locations SET occupied=occupied+1 WHERE location_id=? AND occupied<capacity",
            (location_id,),
        )
        if cursor.rowcount != 1:
            raise ConflictError("库位容量不足")
        if component["location_id"]:
            connection.execute(
                "UPDATE custody_locations SET occupied=occupied-1 WHERE location_id=? AND occupied>0",
                (component["location_id"],),
            )
        connection.execute(
            "UPDATE custody_components SET location_id=? WHERE component_id=?",
            (location_id, component["component_id"]),
        )

    def _work_reviewable(self, connection, work_id: str) -> dict[str, Any]:
        work = connection.execute("SELECT * FROM custody_works WHERE work_id=?", (work_id,)).fetchone()
        components = connection.execute(
            "SELECT * FROM custody_components WHERE work_id=? ORDER BY component_no", (work_id,)
        ).fetchall()
        verified = 0
        frozen = 0
        for component in components:
            if component["status"] == "verified" and component["verify_count"] >= 2 \
                    and not self._active_freeze(connection, component["component_id"]):
                verified += 1
            if self._active_freeze(connection, component["component_id"]):
                frozen += 1
        reviewable = bool(components) and len(components) >= work["declared_components"] \
            and verified == len(components)
        return {"work_id": work_id, "declared_components": work["declared_components"],
                "registered_components": len(components), "verified_components": verified,
                "frozen_components": frozen, "reviewable": reviewable}

    # ---- 预约入库 ---------------------------------------------------------

    def create_reservation(self, *, request_id: str, actor_id: str, site_id: str, carrier_org: str,
                           expected_packages: int, slot_start: str, slot_end: str,
                           waybill_no: str | None = None, notes: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "carrier_org": carrier_org,
                   "expected_packages": expected_packages, "slot_start": slot_start,
                   "slot_end": slot_end, "waybill_no": waybill_no, "notes": notes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "secretary", "warehouse")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            carrier_org = self._text(carrier_org, "carrier_org")
            try:
                expected = int(expected_packages)
            except (TypeError, ValueError) as exc:
                raise ValidationError("expected_packages 必须是正整数") from exc
            if expected < 1:
                raise ValidationError("expected_packages 必须是正整数")
            start = _parse_time(slot_start, "slot_start")
            end = _parse_time(slot_end, "slot_end")
            if end <= start:
                raise ValidationError("slot_end 必须晚于 slot_start")
            waybill = self._identifier(waybill_no, "waybill_no") if waybill_no else ""
            notes = self._text(notes, "notes", 500, allow_empty=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS next FROM custody_reservations WHERE site_id=?",
                    (site_id,),
                ).fetchone()["next"]
                reservation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_reservations(reservation_id,site_id,sequence,carrier_org,waybill_no,"
                    "expected_packages,slot_start,slot_end,status,notes,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?, 'scheduled',?,?,?)",
                    (reservation_id, site_id, sequence, carrier_org, waybill, expected,
                     start, end, notes, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="custody.reservation.created",
                             resource_type="reservation", resource_id=reservation_id,
                             detail={"site_id": site_id, "sequence": sequence, "carrier_org": carrier_org,
                                     "expected_packages": expected, "slot_start": start, "slot_end": end},
                             occurred_at=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id, "sequence": sequence,
                                                       "status": "scheduled"}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.reservation.create", payload=payload, create=create)

    def update_reservation_waybill(self, *, request_id: str, actor_id: str, reservation_id: str,
                                   waybill_no: str) -> dict[str, Any]:
        """承运单晚于实物到达时补登；已登记的不同单号会被拒绝而不是覆盖。"""

        payload = {"actor_id": actor_id, "reservation_id": reservation_id, "waybill_no": waybill_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "secretary", "warehouse", "carrier")
            row = connection.execute(
                "SELECT * FROM custody_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("预约不存在")
            waybill_no = self._identifier(waybill_no, "waybill_no")

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["waybill_no"] and row["waybill_no"] != waybill_no:
                    raise ConflictError("承运单号已登记且不一致，不能覆盖")
                if row["waybill_no"] != waybill_no:
                    connection.execute(
                        "UPDATE custody_reservations SET waybill_no=? WHERE reservation_id=?",
                        (waybill_no, reservation_id),
                    )
                    append_event(connection, actor_id=actor_id, action="custody.reservation.waybill_updated",
                                 resource_type="reservation", resource_id=reservation_id,
                                 detail={"waybill_no": waybill_no}, occurred_at=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id,
                                                       "waybill_no": waybill_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.reservation.waybill", payload=payload, create=create)

    def close_reservation(self, *, request_id: str, actor_id: str, reservation_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "reservation_id": reservation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "secretary", "warehouse")
            row = connection.execute(
                "SELECT * FROM custody_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("预约不存在")
            site = self._site(connection, row["site_id"])
            self._check_site_scope(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] in ("closed", "cancelled"):
                    raise ConflictError("预约已关闭")
                connection.execute(
                    "UPDATE custody_reservations SET status='closed' WHERE reservation_id=?", (reservation_id,)
                )
                append_event(connection, actor_id=actor_id, action="custody.reservation.closed",
                             resource_type="reservation", resource_id=reservation_id,
                             detail={"site_id": row["site_id"]}, occurred_at=self._now())
                return "reservation", reservation_id, {"reservation_id": reservation_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.reservation.close", payload=payload, create=create)

    def scan_package(self, *, request_id: str, actor_id: str, site_id: str, reservation_id: str,
                     tracking_no: str, seal_no: str = "", weight_grams: int = 0,
                     photo_digest: str = "", storage_condition: str = "",
                     waybill_no: str | None = None) -> dict[str, Any]:
        """包裹扫码入库；重复扫码只记录事件，不产生重复库存。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "reservation_id": reservation_id,
                   "tracking_no": tracking_no, "seal_no": seal_no, "weight_grams": weight_grams,
                   "photo_digest": photo_digest, "storage_condition": storage_condition,
                   "waybill_no": waybill_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            tracking_no = self._identifier(tracking_no, "tracking_no")
            seal_no = self._text(seal_no, "seal_no", 120, allow_empty=True)
            weight = self._weight(weight_grams, "weight_grams") or 0
            photo_digest = self._text(photo_digest, "photo_digest", 200, allow_empty=True)
            storage_condition = self._text(storage_condition, "storage_condition", 200, allow_empty=True)
            reservation = connection.execute(
                "SELECT * FROM custody_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if reservation is None or reservation["site_id"] != site_id:
                raise NotFoundError("预约不存在")
            if reservation["status"] in ("closed", "cancelled"):
                raise ConflictError("预约已关闭，不能继续收货")

            def record_scan(result: str, package_id: str | None) -> None:
                connection.execute(
                    "INSERT INTO custody_scan_events(scan_id,site_id,code,result,package_id,actor_id,scanned_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, site_id, tracking_no, result, package_id, actor_id, self._now()),
                )

            def create() -> tuple[str, str, dict[str, Any]]:
                if waybill_no and reservation["waybill_no"] and waybill_no != reservation["waybill_no"]:
                    exception_id = self._open_exception(
                        connection, site_id=site_id, exception_type="misdelivered", actor_id=actor_id,
                        detail=f"扫码运单 {waybill_no} 与预约承运单 {reservation['waybill_no']} 不符")
                    record_scan("rejected", None)
                    return "exception", exception_id, {"result": "rejected_misdelivered",
                                                       "exception_id": exception_id,
                                                       "package_id": None, "duplicate": False}
                existing = connection.execute(
                    "SELECT * FROM custody_packages WHERE site_id=? AND tracking_no=?",
                    (site_id, tracking_no),
                ).fetchone()
                if existing:
                    record_scan("duplicate", existing["package_id"])
                    append_event(connection, actor_id=actor_id, action="custody.package.scan_duplicate",
                                 resource_type="package", resource_id=existing["package_id"],
                                 detail={"site_id": site_id, "tracking_no": tracking_no},
                                 occurred_at=self._now())
                    return "package", existing["package_id"], {"result": "duplicate", "duplicate": True,
                                                               "package_id": existing["package_id"]}
                package_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_packages(package_id,reservation_id,site_id,tracking_no,seal_no,"
                    "weight_grams,photo_digest,storage_condition,status,received_by,received_at) "
                    "VALUES(?,?,?,?,?,?,?,?, 'received',?,?)",
                    (package_id, reservation_id, site_id, tracking_no, seal_no, weight,
                     photo_digest, storage_condition, actor_id, self._now()),
                )
                record_scan("registered", package_id)
                if reservation["status"] == "scheduled":
                    connection.execute(
                        "UPDATE custody_reservations SET status='receiving' WHERE reservation_id=?",
                        (reservation_id,),
                    )
                append_event(connection, actor_id=actor_id, action="custody.package.scanned",
                             resource_type="package", resource_id=package_id,
                             detail={"site_id": site_id, "reservation_id": reservation_id,
                                     "tracking_no": tracking_no, "seal_no": seal_no,
                                     "weight_grams": weight, "photo_digest": photo_digest},
                             occurred_at=self._now())
                return "package", package_id, {"result": "registered", "duplicate": False,
                                               "package_id": package_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.package.scan", payload=payload, create=create)

    # ---- 作品与组件 -------------------------------------------------------

    def register_work(self, *, request_id: str, actor_id: str, package_id: str, entry_no: str,
                      title: str, declared_components: int, storage_condition: str = "",
                      orientation: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "package_id": package_id, "entry_no": entry_no, "title": title,
                   "declared_components": declared_components, "storage_condition": storage_condition,
                   "orientation": orientation}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse")
            package = connection.execute(
                "SELECT * FROM custody_packages WHERE package_id=?", (package_id,)
            ).fetchone()
            if package is None:
                raise NotFoundError("包裹不存在")
            site = self._site(connection, package["site_id"])
            self._check_site_scope(actor, site)
            if package["status"] == "returned":
                raise ConflictError("包裹已退件")
            entry_no = self._identifier(entry_no, "entry_no")
            title = self._text(title, "title")
            try:
                declared = int(declared_components)
            except (TypeError, ValueError) as exc:
                raise ValidationError("declared_components 必须是正整数") from exc
            if declared < 1:
                raise ValidationError("declared_components 必须是正整数")
            storage_condition = self._text(storage_condition, "storage_condition", 200, allow_empty=True)
            orientation = self._text(orientation, "orientation", 120, allow_empty=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                work_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO custody_works(work_id,package_id,site_id,entry_no,title,declared_components,"
                        "storage_condition,orientation,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (work_id, package_id, package["site_id"], entry_no, title, declared,
                         storage_condition, orientation, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一包裹内作品编号已经存在") from exc
                connection.execute(
                    "UPDATE custody_packages SET status='opened' WHERE package_id=? AND status='received'",
                    (package_id,),
                )
                append_event(connection, actor_id=actor_id, action="custody.work.registered",
                             resource_type="work", resource_id=work_id,
                             detail={"package_id": package_id, "entry_no": entry_no, "title": title,
                                     "declared_components": declared,
                                     "storage_condition": storage_condition, "orientation": orientation},
                             occurred_at=self._now())
                return "work", work_id, {"work_id": work_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.work.register", payload=payload, create=create)

    def register_component(self, *, request_id: str, actor_id: str, work_id: str, component_no: str,
                           name: str, weight_grams: int = 0, fragile: bool = False) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "work_id": work_id, "component_no": component_no, "name": name,
                   "weight_grams": weight_grams, "fragile": bool(fragile)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse")
            work = connection.execute("SELECT * FROM custody_works WHERE work_id=?", (work_id,)).fetchone()
            if work is None:
                raise NotFoundError("作品不存在")
            site = self._site(connection, work["site_id"])
            self._check_site_scope(actor, site)
            component_no = self._identifier(component_no, "component_no")
            name = self._text(name, "name")
            weight = self._weight(weight_grams, "weight_grams") or 0
            package = connection.execute(
                "SELECT * FROM custody_packages WHERE package_id=?", (work["package_id"],)
            ).fetchone()
            reservation = connection.execute(
                "SELECT * FROM custody_reservations WHERE reservation_id=?", (package["reservation_id"],)
            ).fetchone()

            def create() -> tuple[str, str, dict[str, Any]]:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM custody_components WHERE work_id=?", (work_id,)
                ).fetchone()["count"]
                if count >= work["declared_components"]:
                    raise ConflictError("组件数量超出作品申报数")
                component_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO custody_components(component_id,work_id,site_id,component_no,name,"
                        "weight_grams,fragile,status,verify_count,created_at) "
                        "VALUES(?,?,?,?,?,?,?,'pending_verify',0,?)",
                        (component_id, work_id, work["site_id"], component_no, name, weight,
                         1 if fragile else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一作品内组件编号已经存在") from exc
                handover_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_handovers(handover_id,component_id,site_id,handover_type,from_holder,"
                    "to_holder,status,initiated_by,confirmed_by,occurred_at,confirmed_at) "
                    "VALUES(?,?,?,'intake',?,?, 'confirmed',?,?,?,?)",
                    (handover_id, component_id, work["site_id"], f"carrier:{reservation['carrier_org']}",
                     f"warehouse:{work['site_id']}", actor_id, actor_id, self._now(), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="custody.component.registered",
                             resource_type="component", resource_id=component_id,
                             detail={"work_id": work_id, "component_no": component_no, "name": name,
                                     "weight_grams": weight, "fragile": bool(fragile)},
                             occurred_at=self._now())
                return "component", component_id, {"component_id": component_id, "status": "pending_verify"}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.component.register", payload=payload, create=create)

    def verify_component(self, *, request_id: str, actor_id: str, component_id: str,
                         weight_grams: int | None = None, seal_no: str = "",
                         photo_digest: str = "", notes: str = "") -> dict[str, Any]:
        """双人核验：两名不同操作者各自登记一次，第二次通过后组件生效。"""

        payload = {"actor_id": actor_id, "component_id": component_id, "weight_grams": weight_grams,
                   "seal_no": seal_no, "photo_digest": photo_digest, "notes": notes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse", "secretary")
            component = self._component(connection, component_id)
            site = self._site(connection, component["site_id"])
            self._check_site_scope(actor, site)
            weight = self._weight(weight_grams, "weight_grams")
            seal_no = self._text(seal_no, "seal_no", 120, allow_empty=True)
            photo_digest = self._text(photo_digest, "photo_digest", 200, allow_empty=True)
            notes = self._text(notes, "notes", 500, allow_empty=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                current = self._component(connection, component_id)
                if current["status"] == "verified":
                    raise ConflictError("组件已完成双人核验")
                if current["status"] != "pending_verify":
                    raise ConflictError("组件当前状态不允许核验")
                if self._active_freeze(connection, component_id):
                    raise ConflictError("组件已冻结，等待异常处理")
                if self._pending_handover(connection, component_id):
                    raise ConflictError("组件存在待确认交接，不能核验")
                if connection.execute(
                        "SELECT 1 FROM custody_verifications WHERE component_id=? AND actor_id=?",
                        (component_id, actor_id)).fetchone():
                    raise ConflictError("同一操作者不能重复核验同一组件")
                connection.execute(
                    "INSERT INTO custody_verifications(verification_id,component_id,actor_id,weight_grams,"
                    "seal_no,photo_digest,notes,verified_at) VALUES(?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, component_id, actor_id, weight, seal_no, photo_digest,
                     notes, self._now()),
                )
                verify_count = current["verify_count"] + 1
                status = "verified" if verify_count >= 2 else "pending_verify"
                connection.execute(
                    "UPDATE custody_components SET verify_count=?, status=? WHERE component_id=?",
                    (verify_count, status, component_id),
                )
                append_event(connection, actor_id=actor_id, action="custody.component.verified",
                             resource_type="component", resource_id=component_id,
                             detail={"verify_count": verify_count, "status": status,
                                     "weight_grams": weight, "seal_no": seal_no},
                             occurred_at=self._now())
                return "component", component_id, {"component_id": component_id,
                                                   "verify_count": verify_count, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.component.verify", payload=payload, create=create)

    # ---- 库位与设备 -------------------------------------------------------

    def register_location(self, *, request_id: str, actor_id: str, site_id: str, code: str,
                          capacity: int, condition_class: str = "normal") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "code": code,
                   "capacity": capacity, "condition_class": condition_class}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            code = self._identifier(code, "code")
            try:
                capacity = int(capacity)
            except (TypeError, ValueError) as exc:
                raise ValidationError("capacity 必须是正整数") from exc
            if capacity < 1:
                raise ValidationError("capacity 必须是正整数")
            if condition_class not in ("normal", "climate", "orientation"):
                raise ValidationError("condition_class 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                location_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO custody_locations(location_id,site_id,code,capacity,occupied,"
                        "condition_class,created_at) VALUES(?,?,?,?,0,?,?)",
                        (location_id, site_id, code, capacity, condition_class, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("库位编码已经存在") from exc
                append_event(connection, actor_id=actor_id, action="custody.location.registered",
                             resource_type="location", resource_id=location_id,
                             detail={"site_id": site_id, "code": code, "capacity": capacity,
                                     "condition_class": condition_class},
                             occurred_at=self._now())
                return "location", location_id, {"location_id": location_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.location.register", payload=payload, create=create)

    def register_equipment(self, *, request_id: str, actor_id: str, site_id: str, name: str,
                           equipment_type: str, capacity: int = 1) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "name": name,
                   "equipment_type": equipment_type, "capacity": capacity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            name = self._text(name, "name")
            equipment_type = self._text(equipment_type, "equipment_type", 80)
            try:
                capacity = int(capacity)
            except (TypeError, ValueError) as exc:
                raise ValidationError("capacity 必须是正整数") from exc
            if capacity < 1:
                raise ValidationError("capacity 必须是正整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                equipment_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO custody_equipment(equipment_id,site_id,name,equipment_type,capacity,"
                        "occupied,created_at) VALUES(?,?,?,?,?,0,?)",
                        (equipment_id, site_id, name, equipment_type, capacity, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备名称已经存在") from exc
                append_event(connection, actor_id=actor_id, action="custody.equipment.registered",
                             resource_type="equipment", resource_id=equipment_id,
                             detail={"site_id": site_id, "name": name,
                                     "equipment_type": equipment_type, "capacity": capacity},
                             occurred_at=self._now())
                return "equipment", equipment_id, {"equipment_id": equipment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.equipment.register", payload=payload, create=create)

    def assign_location(self, *, request_id: str, actor_id: str, component_id: str,
                        location_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "component_id": component_id, "location_id": location_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse")
            component = self._component(connection, component_id)
            site = self._site(connection, component["site_id"])
            self._check_site_scope(actor, site)
            location = connection.execute(
                "SELECT * FROM custody_locations WHERE location_id=?", (location_id,)
            ).fetchone()
            if location is None or location["site_id"] != component["site_id"]:
                raise NotFoundError("库位不存在")
            if component["status"] in ("missing", "misdelivered", "returned"):
                raise ConflictError("组件当前状态不允许分配库位")
            if self._active_freeze(connection, component_id):
                raise ConflictError("组件已冻结，等待异常处理")
            if self._pending_handover(connection, component_id):
                raise ConflictError("组件存在待确认交接")
            if component["fragile"] and location["condition_class"] == "normal" \
                    and not component["equipment_id"]:
                raise ValidationError("易损组件需要恒温恒湿设备或专用库位")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = self._component(connection, component_id)
                if current["location_id"] == location_id:
                    return "location", location_id, {"component_id": component_id,
                                                     "location_id": location_id, "changed": False}
                self._move_location(connection, current, location_id)
                holder = self._current_holder(connection, component_id, component["site_id"])
                handover_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_handovers(handover_id,component_id,site_id,handover_type,from_holder,"
                    "to_holder,location_id,status,initiated_by,confirmed_by,occurred_at,confirmed_at) "
                    "VALUES(?,?,?,'shelve',?,?,?, 'confirmed',?,?,?,?)",
                    (handover_id, component_id, component["site_id"], holder, holder, location_id,
                     actor_id, actor_id, self._now(), self._now()),
                )
                occupied = connection.execute(
                    "SELECT occupied FROM custody_locations WHERE location_id=?", (location_id,)
                ).fetchone()["occupied"]
                append_event(connection, actor_id=actor_id, action="custody.location.assigned",
                             resource_type="component", resource_id=component_id,
                             detail={"location_id": location_id, "occupied": occupied},
                             occurred_at=self._now())
                return "location", location_id, {"component_id": component_id,
                                                 "location_id": location_id, "changed": True,
                                                 "occupied": occupied}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.component.assign_location", payload=payload, create=create)

    def assign_equipment(self, *, request_id: str, actor_id: str, component_id: str,
                         equipment_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "component_id": component_id, "equipment_id": equipment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse")
            component = self._component(connection, component_id)
            site = self._site(connection, component["site_id"])
            self._check_site_scope(actor, site)
            equipment = connection.execute(
                "SELECT * FROM custody_equipment WHERE equipment_id=?", (equipment_id,)
            ).fetchone()
            if equipment is None or equipment["site_id"] != component["site_id"]:
                raise NotFoundError("设备不存在")
            if component["status"] in ("missing", "misdelivered", "returned"):
                raise ConflictError("组件当前状态不允许分配设备")
            if self._active_freeze(connection, component_id):
                raise ConflictError("组件已冻结，等待异常处理")
            if self._pending_handover(connection, component_id):
                raise ConflictError("组件存在待确认交接")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = self._component(connection, component_id)
                if current["equipment_id"] == equipment_id:
                    return "equipment", equipment_id, {"component_id": component_id,
                                                       "equipment_id": equipment_id, "changed": False}
                cursor = connection.execute(
                    "UPDATE custody_equipment SET occupied=occupied+1 WHERE equipment_id=? AND occupied<capacity",
                    (equipment_id,),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("设备容量不足")
                if current["equipment_id"]:
                    connection.execute(
                        "UPDATE custody_equipment SET occupied=occupied-1 WHERE equipment_id=? AND occupied>0",
                        (current["equipment_id"],),
                    )
                connection.execute(
                    "UPDATE custody_components SET equipment_id=? WHERE component_id=?",
                    (equipment_id, component_id),
                )
                occupied = connection.execute(
                    "SELECT occupied FROM custody_equipment WHERE equipment_id=?", (equipment_id,)
                ).fetchone()["occupied"]
                append_event(connection, actor_id=actor_id, action="custody.equipment.assigned",
                             resource_type="component", resource_id=component_id,
                             detail={"equipment_id": equipment_id, "occupied": occupied},
                             occurred_at=self._now())
                return "equipment", equipment_id, {"component_id": component_id,
                                                   "equipment_id": equipment_id, "changed": True,
                                                   "occupied": occupied}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.component.assign_equipment", payload=payload, create=create)

    # ---- 外借时段 ---------------------------------------------------------

    def create_loan(self, *, request_id: str, actor_id: str, component_id: str, borrower_id: str,
                    slot_start: str, slot_end: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "component_id": component_id, "borrower_id": borrower_id,
                   "slot_start": slot_start, "slot_end": slot_end}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "secretary")
            component = self._component(connection, component_id)
            site = self._site(connection, component["site_id"])
            self._check_site_scope(actor, site)
            borrower = self._actor(connection, borrower_id)
            self._require(borrower, "reviewer")
            start = _parse_time(slot_start, "slot_start")
            end = _parse_time(slot_end, "slot_end")
            if end <= start:
                raise ValidationError("slot_end 必须晚于 slot_start")
            if end <= self._now():
                raise ValidationError("外借时段必须尚未结束")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = self._component(connection, component_id)
                if current["status"] != "verified":
                    raise ConflictError("组件未通过双人核验，不能外借")
                if self._active_freeze(connection, component_id):
                    raise ConflictError("组件已冻结，等待异常处理")
                overlap = connection.execute(
                    "SELECT loan_id FROM custody_loans WHERE component_id=? "
                    "AND status IN ('booked','active','overdue') AND slot_start<? AND slot_end>?",
                    (component_id, end, start),
                ).fetchone()
                if overlap:
                    raise ConflictError("外借时段与既有借阅冲突")
                loan_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_loans(loan_id,component_id,site_id,borrower_id,slot_start,slot_end,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?, 'booked',?,?)",
                    (loan_id, component_id, component["site_id"], borrower_id, start, end,
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="custody.loan.created",
                             resource_type="loan", resource_id=loan_id,
                             detail={"component_id": component_id, "borrower_id": borrower_id,
                                     "slot_start": start, "slot_end": end},
                             occurred_at=self._now())
                return "loan", loan_id, {"loan_id": loan_id, "status": "booked"}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.loan.create", payload=payload, create=create)

    def cancel_loan(self, *, request_id: str, actor_id: str, loan_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "loan_id": loan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "secretary")
            loan = connection.execute("SELECT * FROM custody_loans WHERE loan_id=?", (loan_id,)).fetchone()
            if loan is None:
                raise NotFoundError("借阅不存在")
            site = self._site(connection, loan["site_id"])
            self._check_site_scope(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                if loan["status"] != "booked":
                    raise ConflictError("只有未借出的预约可以取消")
                connection.execute(
                    "UPDATE custody_loans SET status='cancelled' WHERE loan_id=?", (loan_id,)
                )
                append_event(connection, actor_id=actor_id, action="custody.loan.cancelled",
                             resource_type="loan", resource_id=loan_id,
                             detail={"component_id": loan["component_id"]}, occurred_at=self._now())
                return "loan", loan_id, {"loan_id": loan_id, "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.loan.cancel", payload=payload, create=create)

    def sweep_overdue(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        """把超过外借时段仍未归还的借阅标记逾期，冻结组件并进入异常流程。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "secretary", "warehouse")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            rows = connection.execute(
                "SELECT * FROM custody_loans WHERE site_id=? AND status IN ('booked','active') AND slot_end<?",
                (site_id, self._now()),
            ).fetchall()
            overdue_ids = []
            for loan in rows:
                connection.execute(
                    "UPDATE custody_loans SET status='overdue' WHERE loan_id=?", (loan["loan_id"],)
                )
                self._open_exception(connection, site_id=site_id, exception_type="overdue",
                                     actor_id=actor_id, component_id=loan["component_id"],
                                     loan_id=loan["loan_id"],
                                     detail=f"借阅 {loan['loan_id']} 超过应还时间 {loan['slot_end']}")
                append_event(connection, actor_id=actor_id, action="custody.loan.overdue",
                             resource_type="loan", resource_id=loan["loan_id"],
                             detail={"component_id": loan["component_id"], "slot_end": loan["slot_end"]},
                             occurred_at=self._now())
                overdue_ids.append(loan["loan_id"])
            return {"site_id": site_id, "overdue_loan_ids": overdue_ids, "count": len(overdue_ids)}

    # ---- 交接链 -----------------------------------------------------------

    def initiate_handover(self, *, request_id: str, actor_id: str, component_id: str,
                          handover_type: str, to_holder: str, seal_no: str = "",
                          weight_grams: int | None = None, photo_digest: str = "",
                          condition_note: str = "", condition_state: str = "good",
                          location_id: str | None = None) -> dict[str, Any]:
        """发起一次交接；交出方由保管链推导，接收方确认后生效。"""

        payload = {"actor_id": actor_id, "component_id": component_id, "handover_type": handover_type,
                   "to_holder": to_holder, "seal_no": seal_no, "weight_grams": weight_grams,
                   "photo_digest": photo_digest, "condition_note": condition_note,
                   "condition_state": condition_state, "location_id": location_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            component = self._component(connection, component_id)
            site = self._site(connection, component["site_id"])
            self._check_site_scope(actor, site)
            if handover_type not in API_HANDOVER_TYPES:
                raise ValidationError("handover_type 不在允许范围内")
            if not HOLDER_PATTERN.fullmatch(str(to_holder).strip()):
                raise ValidationError("to_holder 格式无效")
            to_holder = str(to_holder).strip()
            seal_no = self._text(seal_no, "seal_no", 120, allow_empty=True)
            weight = self._weight(weight_grams, "weight_grams")
            photo_digest = self._text(photo_digest, "photo_digest", 200, allow_empty=True)
            condition_note = self._text(condition_note, "condition_note", 500, allow_empty=True)
            if condition_state not in ("good", "damaged", "unknown"):
                raise ValidationError("condition_state 不在允许范围内")
            if component["status"] in ("missing", "misdelivered"):
                raise ConflictError("组件处于异常状态，不能交接")
            if component["status"] == "returned":
                raise ConflictError("组件已退件")
            if component["status"] == "damaged" and handover_type != "return_out":
                raise ConflictError("破损组件只能办理退件")
            freeze = self._active_freeze(connection, component_id)
            if freeze and handover_type != "loan_return":
                raise ConflictError("组件已冻结，等待异常处理")
            if self._pending_handover(connection, component_id):
                raise ConflictError("组件存在待确认交接")

            loan_id = None
            if handover_type == "loan_out":
                self._require(actor, "admin", "warehouse")
                if component["status"] != "verified":
                    raise ConflictError("组件未通过双人核验，不能外借")
                loan = connection.execute(
                    "SELECT * FROM custody_loans WHERE component_id=? AND status='booked' "
                    "ORDER BY slot_start LIMIT 1", (component_id,),
                ).fetchone()
                if loan is None:
                    raise ConflictError("没有可借出的借阅预约")
                if to_holder != f"reviewer:{loan['borrower_id']}":
                    raise ValidationError("to_holder 必须是借阅人")
                loan_id = loan["loan_id"]
            elif handover_type == "loan_return":
                self._require(actor, "admin", "warehouse", "reviewer")
                loan = connection.execute(
                    "SELECT * FROM custody_loans WHERE component_id=? AND status IN ('active','overdue') "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1", (component_id,),
                ).fetchone()
                if loan is None:
                    raise ConflictError("没有待归还的借阅")
                if to_holder != f"warehouse:{component['site_id']}":
                    raise ValidationError("归还接收方必须是样品中心")
                holder = self._current_holder(connection, component_id, component["site_id"])
                if actor.role == "reviewer" and holder != f"reviewer:{actor.actor_id}":
                    raise PermissionDenied("只有当前保管人可以发起归还")
                loan_id = loan["loan_id"]
            elif handover_type == "transfer":
                self._require(actor, "admin", "warehouse", "secretary")
            else:
                self._require(actor, "admin", "warehouse")
                if not to_holder.startswith("carrier:"):
                    raise ValidationError("退件接收方必须是承运方")
            if location_id is not None:
                location = connection.execute(
                    "SELECT * FROM custody_locations WHERE location_id=?", (location_id,)
                ).fetchone()
                if location is None or location["site_id"] != component["site_id"]:
                    raise NotFoundError("库位不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                from_holder = self._current_holder(connection, component_id, component["site_id"])
                handover_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_handovers(handover_id,component_id,site_id,handover_type,from_holder,"
                    "to_holder,seal_no,weight_grams,photo_digest,condition_note,condition_state,location_id,"
                    "loan_id,status,initiated_by,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (handover_id, component_id, component["site_id"], handover_type, from_holder, to_holder,
                     seal_no, weight, photo_digest, condition_note, condition_state, location_id,
                     loan_id, actor_id, self._now()),
                )
                exception_ids = []
                if condition_state == "damaged":
                    exception_ids.append(self._open_exception(
                        connection, site_id=component["site_id"], exception_type="damaged",
                        actor_id=actor_id, component_id=component_id, handover_id=handover_id,
                        detail=condition_note or "交接发起时发现破损"))
                append_event(connection, actor_id=actor_id, action="custody.handover.initiated",
                             resource_type="handover", resource_id=handover_id,
                             detail={"component_id": component_id, "handover_type": handover_type,
                                     "from_holder": from_holder, "to_holder": to_holder,
                                     "loan_id": loan_id, "condition_state": condition_state},
                             occurred_at=self._now())
                return "handover", handover_id, {"handover_id": handover_id, "from_holder": from_holder,
                                                 "to_holder": to_holder, "status": "pending",
                                                 "loan_id": loan_id, "exception_ids": exception_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.handover.initiate", payload=payload, create=create)

    def _authorize_confirmer(self, actor: Actor, handover) -> None:
        if actor.actor_id == handover["initiated_by"]:
            raise PermissionDenied("交接必须由接收方另一名操作者确认")
        if actor.role == "admin":
            return
        party, _, party_id = handover["to_holder"].partition(":")
        if party == "warehouse" and actor.role == "warehouse":
            return
        if party == "reviewer" and actor.role == "reviewer" and actor.actor_id == party_id:
            return
        if party == "carrier" and actor.role == "carrier":
            return
        if party == "secretary" and actor.role == "secretary":
            return
        raise PermissionDenied("当前角色不能确认该交接")

    def confirm_handover(self, *, request_id: str, actor_id: str, handover_id: str,
                         seal_no: str = "", weight_grams: int | None = None,
                         condition_note: str = "", condition_state: str = "") -> dict[str, Any]:
        """接收方确认交接；封签或重量差异只冻结相关组件并进入异常流程。"""

        payload = {"actor_id": actor_id, "handover_id": handover_id, "seal_no": seal_no,
                   "weight_grams": weight_grams, "condition_note": condition_note,
                   "condition_state": condition_state}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            handover = connection.execute(
                "SELECT * FROM custody_handovers WHERE handover_id=?", (handover_id,)
            ).fetchone()
            if handover is None:
                raise NotFoundError("交接不存在")
            self._authorize_confirmer(actor, handover)
            seal_no = self._text(seal_no, "seal_no", 120, allow_empty=True)
            weight = self._weight(weight_grams, "weight_grams")
            condition_note = self._text(condition_note, "condition_note", 500, allow_empty=True)
            if condition_state and condition_state not in ("good", "damaged", "unknown"):
                raise ValidationError("condition_state 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT * FROM custody_handovers WHERE handover_id=?", (handover_id,)
                ).fetchone()
                if current["status"] != "pending":
                    raise ConflictError("交接已确认，不能重复确认")
                component = self._component(connection, current["component_id"])
                connection.execute(
                    "UPDATE custody_handovers SET status='confirmed', confirmed_by=?, confirmed_at=?, "
                    "confirmed_seal_no=?, confirmed_weight_grams=?, confirmed_condition_note=?, "
                    "confirmed_condition_state=? WHERE handover_id=?",
                    (actor.actor_id, self._now(), seal_no, weight, condition_note,
                     condition_state, handover_id),
                )
                if current["handover_type"] == "loan_out" and current["loan_id"]:
                    connection.execute(
                        "UPDATE custody_loans SET status='active' WHERE loan_id=? AND status='booked'",
                        (current["loan_id"],),
                    )
                elif current["handover_type"] == "loan_return" and current["loan_id"]:
                    connection.execute(
                        "UPDATE custody_loans SET status='returned', returned_at=? WHERE loan_id=?",
                        (self._now(), current["loan_id"]),
                    )
                elif current["handover_type"] == "return_out":
                    self._release_occupancy(connection, component)
                    connection.execute(
                        "UPDATE custody_components SET status='returned' WHERE component_id=?",
                        (component["component_id"],),
                    )
                if current["location_id"] and current["handover_type"] in ("transfer", "loan_return"):
                    fresh = self._component(connection, component["component_id"])
                    self._move_location(connection, fresh, current["location_id"])
                exception_ids = []
                if current["seal_no"] and seal_no and current["seal_no"] != seal_no:
                    exception_ids.append(self._open_exception(
                        connection, site_id=current["site_id"], exception_type="seal_mismatch",
                        actor_id=actor.actor_id, component_id=component["component_id"],
                        handover_id=handover_id,
                        detail=f"封签应为 {current['seal_no']}，实收为 {seal_no}"))
                expected_weight = current["weight_grams"]
                if expected_weight is not None and weight is not None:
                    tolerance = max(10, int(expected_weight * 0.05))
                    if abs(weight - expected_weight) > tolerance:
                        exception_ids.append(self._open_exception(
                            connection, site_id=current["site_id"], exception_type="weight_mismatch",
                            actor_id=actor.actor_id, component_id=component["component_id"],
                            handover_id=handover_id,
                            detail=f"重量应为 {expected_weight} 克，实收为 {weight} 克"))
                if condition_state == "damaged":
                    exception_ids.append(self._open_exception(
                        connection, site_id=current["site_id"], exception_type="damaged",
                        actor_id=actor.actor_id, component_id=component["component_id"],
                        handover_id=handover_id,
                        detail=condition_note or "接收确认时发现破损"))
                append_event(connection, actor_id=actor.actor_id, action="custody.handover.confirmed",
                             resource_type="handover", resource_id=handover_id,
                             detail={"component_id": current["component_id"],
                                     "handover_type": current["handover_type"],
                                     "to_holder": current["to_holder"],
                                     "exception_ids": exception_ids},
                             occurred_at=self._now())
                frozen = bool(self._active_freeze(connection, current["component_id"]))
                return "handover", handover_id, {"handover_id": handover_id, "status": "confirmed",
                                                 "exception_ids": exception_ids, "frozen": frozen}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.handover.confirm", payload=payload, create=create)

    def supplement_evidence(self, *, request_id: str, actor_id: str, handover_id: str,
                            evidence_type: str, content: str) -> dict[str, Any]:
        """补录证据只追加新行，原交接记录保持不可变。"""

        payload = {"actor_id": actor_id, "handover_id": handover_id,
                   "evidence_type": evidence_type, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "warehouse", "secretary", "carrier", "reviewer")
            handover = connection.execute(
                "SELECT * FROM custody_handovers WHERE handover_id=?", (handover_id,)
            ).fetchone()
            if handover is None:
                raise NotFoundError("交接不存在")
            if evidence_type not in ("photo", "note", "weight", "seal", "waybill"):
                raise ValidationError("evidence_type 不在允许范围内")
            content = self._text(content, "content", 2000)

            def create() -> tuple[str, str, dict[str, Any]]:
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO custody_evidence(evidence_id,handover_id,actor_id,evidence_type,content,"
                    "added_at) VALUES(?,?,?,?,?,?)",
                    (evidence_id, handover_id, actor_id, evidence_type, content, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="custody.handover.evidence_added",
                             resource_type="handover", resource_id=handover_id,
                             detail={"evidence_id": evidence_id, "evidence_type": evidence_type},
                             occurred_at=self._now())
                return "evidence", evidence_id, {"evidence_id": evidence_id, "handover_id": handover_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.handover.evidence", payload=payload, create=create)

    # ---- 异常流程 ---------------------------------------------------------

    def open_exception(self, *, request_id: str, actor_id: str, site_id: str, exception_type: str,
                       component_id: str | None = None, package_id: str | None = None,
                       work_id: str | None = None, detail: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "exception_type": exception_type,
                   "component_id": component_id, "package_id": package_id,
                   "work_id": work_id, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse", "secretary", "carrier")
            site = self._site(connection, site_id)
            self._check_site_scope(actor, site)
            if exception_type not in EXCEPTION_TYPES:
                raise ValidationError("exception_type 不在允许范围内")
            if not any([component_id, package_id, work_id]):
                raise ValidationError("至少需要一个关联对象")
            detail = self._text(detail, "detail", 500, allow_empty=True)
            if component_id:
                component = self._component(connection, component_id)
                if component["site_id"] != site_id:
                    raise ValidationError("组件不属于该场所")
            if package_id and not connection.execute(
                    "SELECT 1 FROM custody_packages WHERE package_id=? AND site_id=?",
                    (package_id, site_id)).fetchone():
                raise NotFoundError("包裹不存在")
            if work_id and not connection.execute(
                    "SELECT 1 FROM custody_works WHERE work_id=? AND site_id=?",
                    (work_id, site_id)).fetchone():
                raise NotFoundError("作品不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                exception_id = self._open_exception(
                    connection, site_id=site_id, exception_type=exception_type, actor_id=actor_id,
                    component_id=component_id, package_id=package_id, work_id=work_id, detail=detail)
                return "exception", exception_id, {"exception_id": exception_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.exception.open", payload=payload, create=create)

    def resolve_exception(self, *, request_id: str, actor_id: str, exception_id: str,
                          resolution: str, restore_component: bool = False) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "exception_id": exception_id, "resolution": resolution,
                   "restore_component": bool(restore_component)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "warehouse", "secretary")
            exception = connection.execute(
                "SELECT * FROM custody_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if exception is None:
                raise NotFoundError("异常不存在")
            site = self._site(connection, exception["site_id"])
            self._check_site_scope(actor, site)
            resolution = self._text(resolution, "resolution", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if exception["status"] == "resolved":
                    raise ConflictError("异常已关闭")
                connection.execute(
                    "UPDATE custody_exceptions SET status='resolved', resolved_by=?, resolved_at=?, "
                    "resolution=? WHERE exception_id=?",
                    (actor_id, self._now(), resolution, exception_id),
                )
                connection.execute(
                    "UPDATE custody_freezes SET status='released', released_by=?, released_at=? "
                    "WHERE exception_id=? AND status='active'",
                    (actor_id, self._now(), exception_id),
                )
                component_status = None
                if restore_component and exception["component_id"]:
                    component = self._component(connection, exception["component_id"])
                    component_status = "verified" if component["verify_count"] >= 2 else "pending_verify"
                    connection.execute(
                        "UPDATE custody_components SET status=? WHERE component_id=?",
                        (component_status, exception["component_id"]),
                    )
                append_event(connection, actor_id=actor_id, action="custody.exception.resolved",
                             resource_type="exception", resource_id=exception_id,
                             detail={"resolution": resolution, "restore_component": bool(restore_component),
                                     "component_id": exception["component_id"]},
                             occurred_at=self._now())
                return "exception", exception_id, {"exception_id": exception_id, "status": "resolved",
                                                   "component_status": component_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="custody.exception.resolve", payload=payload, create=create)

    # ---- 查询与视图 -------------------------------------------------------

    def custody_chain(self, *, actor_id: str, component_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "warehouse", "secretary", "auditor")
        component = self._component(connection, component_id)
        handovers = []
        rows = connection.execute(
            "SELECT * FROM custody_handovers WHERE component_id=? "
            "ORDER BY COALESCE(confirmed_at, occurred_at), rowid",
            (component_id,),
        ).fetchall()
        for row in rows:
            evidence = connection.execute(
                "SELECT evidence_id, actor_id, evidence_type, content, added_at FROM custody_evidence "
                "WHERE handover_id=? ORDER BY added_at, rowid", (row["handover_id"],),
            ).fetchall()
            handovers.append({
                "handover_id": row["handover_id"], "handover_type": row["handover_type"],
                "from_holder": row["from_holder"], "to_holder": row["to_holder"],
                "seal_no": row["seal_no"], "confirmed_seal_no": row["confirmed_seal_no"],
                "weight_grams": row["weight_grams"], "confirmed_weight_grams": row["confirmed_weight_grams"],
                "condition_state": row["condition_state"],
                "confirmed_condition_state": row["confirmed_condition_state"],
                "location_id": row["location_id"], "loan_id": row["loan_id"],
                "status": row["status"], "initiated_by": row["initiated_by"],
                "confirmed_by": row["confirmed_by"], "occurred_at": row["occurred_at"],
                "confirmed_at": row["confirmed_at"],
                "evidence": [dict(item) for item in evidence],
            })
        freeze = self._active_freeze(connection, component_id)
        return {"component_id": component_id, "status": component["status"],
                "verify_count": component["verify_count"],
                "location_id": component["location_id"], "equipment_id": component["equipment_id"],
                "current_holder": self._current_holder(connection, component_id, component["site_id"]),
                "frozen": bool(freeze), "freeze_reason": freeze["reason"] if freeze else None,
                "handovers": handovers}

    def position_at(self, *, actor_id: str, component_id: str, at: str) -> dict[str, Any]:
        """按历史时点还原组件的保管人与最近登记的库位。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "warehouse", "secretary", "auditor")
        component = self._component(connection, component_id)
        moment = _parse_time(at, "at")
        holder_row = connection.execute(
            "SELECT to_holder, COALESCE(confirmed_at, occurred_at) AS effective_at "
            "FROM custody_handovers WHERE component_id=? AND status='confirmed' "
            "AND COALESCE(confirmed_at, occurred_at)<=? "
            "ORDER BY COALESCE(confirmed_at, occurred_at) DESC, rowid DESC LIMIT 1",
            (component_id, moment),
        ).fetchone()
        location_row = connection.execute(
            "SELECT location_id FROM custody_handovers WHERE component_id=? AND status='confirmed' "
            "AND COALESCE(confirmed_at, occurred_at)<=? AND location_id IS NOT NULL "
            "ORDER BY COALESCE(confirmed_at, occurred_at) DESC, rowid DESC LIMIT 1",
            (component_id, moment),
        ).fetchone()
        return {"component_id": component_id, "at": moment,
                "holder": holder_row["to_holder"] if holder_row else None,
                "location_id": location_row["location_id"] if location_row else None,
                "found": bool(holder_row)}

    def damage_interval(self, *, actor_id: str, component_id: str,
                        exception_id: str | None = None) -> dict[str, Any]:
        """定位损伤责任区间：最后一次完好观测到损伤检出之间的保管人序列。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "warehouse", "secretary", "auditor")
        self._component(connection, component_id)
        if exception_id:
            exception = connection.execute(
                "SELECT * FROM custody_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if exception is None or exception["component_id"] != component_id:
                raise NotFoundError("异常不存在")
        else:
            exception = connection.execute(
                f"SELECT * FROM custody_exceptions WHERE component_id=? "
                f"AND exception_type IN ({','.join('?' * len(DAMAGE_EXCEPTION_TYPES))}) "
                "ORDER BY opened_at DESC, rowid DESC LIMIT 1",
                (component_id, *DAMAGE_EXCEPTION_TYPES),
            ).fetchone()
            if exception is None:
                raise NotFoundError("该组件没有损伤类异常记录")
        detected_at = exception["opened_at"]
        good_marks: list[str] = []
        row = connection.execute(
            "SELECT MAX(verified_at) AS mark FROM custody_verifications "
            "WHERE component_id=? AND verified_at<?",
            (component_id, detected_at),
        ).fetchone()
        if row["mark"]:
            good_marks.append(row["mark"])
        row = connection.execute(
            "SELECT MAX(COALESCE(confirmed_at, occurred_at)) AS mark FROM custody_handovers "
            "WHERE component_id=? AND status='confirmed' AND condition_state='good' "
            "AND confirmed_condition_state!='damaged' AND COALESCE(confirmed_at, occurred_at)<?",
            (component_id, detected_at),
        ).fetchone()
        if row["mark"]:
            good_marks.append(row["mark"])
        last_good_at = max(good_marks) if good_marks else None
        if last_good_at:
            rows = connection.execute(
                "SELECT * FROM custody_handovers WHERE component_id=? AND status='confirmed' "
                "AND COALESCE(confirmed_at, occurred_at)>? AND COALESCE(confirmed_at, occurred_at)<=? "
                "ORDER BY COALESCE(confirmed_at, occurred_at), rowid",
                (component_id, last_good_at, detected_at),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM custody_handovers WHERE component_id=? AND status='confirmed' "
                "AND COALESCE(confirmed_at, occurred_at)<=? "
                "ORDER BY COALESCE(confirmed_at, occurred_at), rowid",
                (component_id, detected_at),
            ).fetchall()
        responsible: list[str] = []
        if last_good_at:
            start = connection.execute(
                "SELECT to_holder FROM custody_handovers WHERE component_id=? AND status='confirmed' "
                "AND COALESCE(confirmed_at, occurred_at)<=? "
                "ORDER BY COALESCE(confirmed_at, occurred_at) DESC, rowid DESC LIMIT 1",
                (component_id, last_good_at),
            ).fetchone()
            if start:
                responsible.append(start["to_holder"])
        for handover in rows:
            if not responsible or responsible[-1] != handover["to_holder"]:
                responsible.append(handover["to_holder"])
        return {"component_id": component_id, "exception_id": exception["exception_id"],
                "exception_type": exception["exception_type"], "detected_at": detected_at,
                "last_good_at": last_good_at, "responsible_holders": responsible,
                "handovers": [{"handover_id": row["handover_id"],
                               "handover_type": row["handover_type"],
                               "from_holder": row["from_holder"], "to_holder": row["to_holder"],
                               "occurred_at": row["occurred_at"]} for row in rows]}

    def warehouse_view(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "warehouse")
        site = self._site(connection, site_id)
        self._check_site_scope(actor, site)
        packages = [dict(row) for row in connection.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM custody_works w WHERE w.package_id=p.package_id) AS work_count,"
            " (SELECT COUNT(*) FROM custody_components c JOIN custody_works w2 ON c.work_id=w2.work_id "
            "  WHERE w2.package_id=p.package_id) AS component_count "
            "FROM custody_packages p WHERE p.site_id=? ORDER BY p.received_at, p.package_id", (site_id,))]
        locations = [dict(row) for row in connection.execute(
            "SELECT location_id, code, capacity, occupied, condition_class FROM custody_locations "
            "WHERE site_id=? ORDER BY code", (site_id,))]
        equipment = [dict(row) for row in connection.execute(
            "SELECT equipment_id, name, equipment_type, capacity, occupied FROM custody_equipment "
            "WHERE site_id=? ORDER BY name", (site_id,))]
        pending = [dict(row) for row in connection.execute(
            "SELECT h.handover_id, h.component_id, h.handover_type, h.from_holder, h.to_holder, "
            "h.initiated_by, h.occurred_at, c.component_no, w.entry_no "
            "FROM custody_handovers h JOIN custody_components c ON c.component_id=h.component_id "
            "JOIN custody_works w ON w.work_id=c.work_id "
            "WHERE h.site_id=? AND h.status='pending' ORDER BY h.occurred_at, h.handover_id", (site_id,))]
        exceptions = [dict(row) for row in connection.execute(
            "SELECT * FROM custody_exceptions WHERE site_id=? AND status!='resolved' "
            "ORDER BY opened_at, exception_id", (site_id,))]
        freezes = [dict(row) for row in connection.execute(
            "SELECT f.*, c.component_no FROM custody_freezes f "
            "JOIN custody_components c ON c.component_id=f.component_id "
            "WHERE f.site_id=? AND f.status='active' ORDER BY f.frozen_at", (site_id,))]
        return {"site_id": site_id, "packages": packages, "locations": locations,
                "equipment": equipment, "pending_handovers": pending,
                "open_exceptions": exceptions, "active_freezes": freezes}

    def secretary_view(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "secretary")
        site = self._site(connection, site_id)
        self._check_site_scope(actor, site)
        reservations = [dict(row) for row in connection.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM custody_packages p WHERE p.reservation_id=r.reservation_id) "
            "AS arrived_packages FROM custody_reservations r WHERE r.site_id=? "
            "ORDER BY r.sequence", (site_id,))]
        works = []
        for work in connection.execute(
                "SELECT w.*, p.tracking_no FROM custody_works w "
                "JOIN custody_packages p ON p.package_id=w.package_id "
                "WHERE w.site_id=? ORDER BY w.created_at, w.work_id", (site_id,)):
            progress = self._work_reviewable(connection, work["work_id"])
            works.append({"work_id": work["work_id"], "entry_no": work["entry_no"],
                          "title": work["title"], "tracking_no": work["tracking_no"],
                          "storage_condition": work["storage_condition"],
                          "orientation": work["orientation"], **progress})
        loans = [dict(row) for row in connection.execute(
            "SELECT l.*, c.component_no, w.entry_no FROM custody_loans l "
            "JOIN custody_components c ON c.component_id=l.component_id "
            "JOIN custody_works w ON w.work_id=c.work_id "
            "WHERE l.site_id=? AND l.status IN ('booked','active','overdue') "
            "ORDER BY l.slot_start, l.loan_id", (site_id,))]
        pending_verification = [dict(row) for row in connection.execute(
            "SELECT c.component_id, c.component_no, c.verify_count, w.entry_no FROM custody_components c "
            "JOIN custody_works w ON w.work_id=c.work_id "
            "WHERE c.site_id=? AND c.status='pending_verify' ORDER BY c.created_at, c.component_id",
            (site_id,))]
        return {"site_id": site_id, "reservations": reservations, "works": works,
                "loans": loans, "pending_verification": pending_verification}

    def carrier_view(self, *, actor_id: str, site_id: str, carrier_org: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "warehouse", "secretary", "carrier")
        site = self._site(connection, site_id)
        if actor.role == "carrier":
            if actor.organization_id != carrier_org:
                raise PermissionDenied("承运联络人只能查看本承运方的包裹")
        else:
            self._check_site_scope(actor, site)
        carrier_org = self._text(carrier_org, "carrier_org")
        reservations = [dict(row) for row in connection.execute(
            "SELECT reservation_id, sequence, waybill_no, expected_packages, slot_start, slot_end, status "
            "FROM custody_reservations WHERE site_id=? AND carrier_org=? ORDER BY sequence",
            (site_id, carrier_org))]
        packages = [dict(row) for row in connection.execute(
            "SELECT p.package_id, p.tracking_no, p.seal_no, p.weight_grams, p.status, p.received_at, "
            "(SELECT COUNT(*) FROM custody_works w WHERE w.package_id=p.package_id) AS work_count "
            "FROM custody_packages p JOIN custody_reservations r ON r.reservation_id=p.reservation_id "
            "WHERE p.site_id=? AND r.carrier_org=? ORDER BY p.received_at, p.package_id",
            (site_id, carrier_org))]
        exceptions = [dict(row) for row in connection.execute(
            "SELECT e.exception_id, e.exception_type, e.status, e.package_id, e.opened_at "
            "FROM custody_exceptions e WHERE e.site_id=? AND e.package_id IN "
            "(SELECT p.package_id FROM custody_packages p JOIN custody_reservations r "
            " ON r.reservation_id=p.reservation_id WHERE r.carrier_org=?) "
            "ORDER BY e.opened_at", (site_id, carrier_org))]
        return {"site_id": site_id, "carrier_org": carrier_org, "reservations": reservations,
                "packages": packages, "exceptions": exceptions}

    def auditor_view(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "auditor")
        site = self._site(connection, site_id)
        self._check_site_scope(actor, site)
        exceptions = [dict(row) for row in connection.execute(
            "SELECT * FROM custody_exceptions WHERE site_id=? ORDER BY opened_at, exception_id", (site_id,))]
        freezes = [dict(row) for row in connection.execute(
            "SELECT * FROM custody_freezes WHERE site_id=? ORDER BY frozen_at, freeze_id", (site_id,))]
        handovers = [dict(row) for row in connection.execute(
            "SELECT * FROM custody_handovers WHERE site_id=? "
            "ORDER BY occurred_at DESC, handover_id DESC LIMIT 50", (site_id,))]
        events = []
        for row in connection.execute(
                "SELECT sequence, event_id, actor_id, action, resource_type, resource_id, detail_json, "
                "event_hash, occurred_at FROM audit_events WHERE action LIKE 'custody.%' "
                "ORDER BY sequence DESC LIMIT 100"):
            events.append({"sequence": row["sequence"], "event_id": row["event_id"],
                           "actor_id": row["actor_id"], "action": row["action"],
                           "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                           "detail": json.loads(row["detail_json"]), "event_hash": row["event_hash"],
                           "occurred_at": row["occurred_at"]})
        return {"site_id": site_id, "exceptions": exceptions, "freezes": freezes,
                "recent_handovers": handovers, "custody_audit_events": events}
