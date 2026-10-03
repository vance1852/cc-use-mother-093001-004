"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .custody import CustodyService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范项目机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号项目节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="program_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="program_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        custody = _run_custody_flow(database, clock, service)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, **custody}
        database.close()
        return result


def _run_custody_flow(database: Database, clock, service: DomainService) -> dict[str, object]:
    """在验收库上跑通预约入库到双人核验的最小保管链。"""

    service.register_actor(request_id="req-warehouse", actor_id="admin-001", new_actor_id="warehouse-001",
                           display_name="仓管员甲", role="warehouse", organization_id="org-001")
    service.register_actor(request_id="req-warehouse-2", actor_id="admin-001", new_actor_id="warehouse-002",
                           display_name="仓管员乙", role="warehouse", organization_id="org-001")
    service.register_actor(request_id="req-secretary", actor_id="admin-001", new_actor_id="secretary-001",
                           display_name="评审秘书", role="secretary", organization_id="org-001")
    custody = CustodyService(database, clock)
    reservation = custody.create_reservation(
        request_id="req-reservation", actor_id="secretary-001", site_id="site-001",
        carrier_org="carrier-org", expected_packages=1,
        slot_start="2026-09-26T09:00:00Z", slot_end="2026-09-26T18:00:00Z", waybill_no="WB-001")
    package = custody.scan_package(
        request_id="req-package", actor_id="warehouse-001", site_id="site-001",
        reservation_id=reservation["reservation_id"], tracking_no="TRK-001", seal_no="SEAL-001",
        weight_grams=3200, photo_digest="sha256:package", storage_condition="恒温")
    duplicate = custody.scan_package(
        request_id="req-package-dup", actor_id="warehouse-001", site_id="site-001",
        reservation_id=reservation["reservation_id"], tracking_no="TRK-001", seal_no="SEAL-001",
        weight_grams=3200)
    work = custody.register_work(
        request_id="req-work", actor_id="warehouse-001", package_id=package["package_id"],
        entry_no="E-001", title="茶器套装", declared_components=2,
        storage_condition="恒温恒湿", orientation="竖直向上")
    component_a = custody.register_component(
        request_id="req-component-a", actor_id="warehouse-001", work_id=work["work_id"],
        component_no="C-01", name="壶身", weight_grams=1800, fragile=True)
    component_b = custody.register_component(
        request_id="req-component-b", actor_id="warehouse-001", work_id=work["work_id"],
        component_no="C-02", name="壶盖", weight_grams=320, fragile=True)
    for component in (component_a, component_b):
        custody.verify_component(request_id=f"req-verify-1-{component['component_id'][:8]}",
                                 actor_id="warehouse-001", component_id=component["component_id"],
                                 seal_no="SEAL-001")
        custody.verify_component(request_id=f"req-verify-2-{component['component_id'][:8]}",
                                 actor_id="warehouse-002", component_id=component["component_id"],
                                 seal_no="SEAL-001")
    view = custody.secretary_view(actor_id="secretary-001", site_id="site-001")
    reviewable = [item for item in view["works"] if item["reviewable"]]
    return {"custody_reservation_sequence": reservation["sequence"],
            "custody_duplicate_scan": duplicate["duplicate"],
            "custody_reviewable_works": len(reviewable)}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
