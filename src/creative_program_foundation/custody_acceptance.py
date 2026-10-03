"""运行样品交接与保管服务的离线端到端验收。

覆盖完整业务链：建档、库位与设备、预约入库、扫码登记茶器套装、双人核验、
组成可评审样品、原子占用、评委借阅与返还、封签差异冻结、独立异常流程、
补录证据、责任区间与历史时点查询，以及服务重启后的状态保持。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import ManualClock
from .custody import CustodyService
from .service import DomainService
from .storage import Database

FRAGILE = {"temperature_min_c": 18, "temperature_max_c": 24,
           "humidity_min_pct": 45, "humidity_max_pct": 55, "orientation": "upright"}


def _bootstrap(service: DomainService) -> None:
    service.register_organization(request_id="acc-org", actor_id="bootstrap",
                                  organization_id="org-acc", name="赛事组委会")
    service.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="系统管理员", role="admin", organization_id="org-acc")
    for request_id, actor_id, name, role in (
        ("acc-wh1", "wh1", "仓管甲", "warehouse_keeper"),
        ("acc-wh2", "wh2", "仓管乙", "warehouse_keeper"),
        ("acc-sec", "sec1", "评审秘书", "review_secretary"),
        ("acc-car", "car1", "承运联络人", "carrier_liaison"),
        ("acc-au", "au1", "审计员", "auditor"),
    ):
        service.register_actor(request_id=request_id, actor_id="admin", new_actor_id=actor_id,
                               display_name=name, role=role, organization_id="org-acc")
    service.register_site(request_id="acc-site", actor_id="admin", site_id="s1",
                          organization_id="org-acc", name="样品中心", timezone_name="Asia/Shanghai")


def _check_in_tea_set(custody: CustodyService, reservation_id: str) -> str:
    receipt = custody.check_in_package(
        request_id="acc-pkg", actor_id="wh1", site_id="s1", package_code="PKG-001",
        reservation_id=reservation_id, seal_code="SEAL-PKG-1", weight_grams=3200,
        photo_digest="sha256:pkg-photo",
        works=[{"work_key": "W1", "title": "青瓷茶器套装", "storage_condition": FRAGILE,
                "components": [
                    {"component_code": "C-01", "name": "茶壶", "weight_grams": 820,
                     "seal_code": "S-01", "photo_digest": "sha256:c01"},
                    {"component_code": "C-02", "name": "茶壶盖", "weight_grams": 160,
                     "seal_code": "S-02", "photo_digest": "sha256:c02"},
                    {"component_code": "C-03", "name": "茶杯", "weight_grams": 240,
                     "seal_code": "S-03", "photo_digest": "sha256:c03"},
                    {"component_code": "C-04", "name": "茶盘", "weight_grams": 1980,
                     "seal_code": "S-04", "photo_digest": "sha256:c04"},
                ]}])
    return receipt.resource_id


def _verify_all(custody: CustodyService, component_ids: list[str]) -> None:
    for index, component_id in enumerate(component_ids):
        custody.verify_component(request_id=f"acc-v1-{index}", actor_id="wh1",
                                 component_id=component_id, result="pass")
        custody.verify_component(request_id=f"acc-v2-{index}", actor_id="sec1",
                                 component_id=component_id, result="pass")


def run() -> dict[str, object]:
    """执行完整保管链并返回验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "custody.sqlite3"
        clock = ManualClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        database = Database(path)
        service = DomainService(database, clock)
        custody = CustodyService(database, clock)
        _bootstrap(service)

        custody.register_location(request_id="acc-loc", actor_id="wh1", site_id="s1",
                                  location_id="L1", capacity=6,
                                  conditions={"temperature_min_c": 20, "temperature_max_c": 22,
                                              "humidity_min_pct": 48, "humidity_max_pct": 52,
                                              "orientations": ["upright", "any"]})
        custody.register_equipment(request_id="acc-eq", actor_id="wh1", site_id="s1",
                                   equipment_id="E1", name="恒温恒湿柜", total=2)
        reservation = custody.create_reservation(
            request_id="acc-res", actor_id="wh1", site_id="s1",
            expected_at="2026-10-02T09:00:00+08:00", carrier_ref="YTO-88",
            expected_packages=1, note="茶器套装预计一箱")
        package_id = _check_in_tea_set(custody, reservation.resource_id)
        detail = custody.package_detail(actor_id="wh1", package_id=package_id)
        work_id = detail["works"][0]["work_id"]
        components = {item["component_code"]: item["component_id"]
                      for item in detail["works"][0]["components"]}
        _verify_all(custody, list(components.values()))
        custody.assemble_sample(request_id="acc-sample", actor_id="sec1", work_id=work_id)

        # 原子占用：库位、恒温柜与外借时段。
        for index, code in enumerate(("C-01", "C-02", "C-03", "C-04")):
            custody.allocate_resource(request_id=f"acc-al-{code}", actor_id="wh1", site_id="s1",
                                      resource_type="location", resource_id="L1",
                                      component_id=components[code])
        custody.allocate_resource(request_id="acc-al-e1", actor_id="wh1", site_id="s1",
                                  resource_type="equipment", resource_id="E1",
                                  component_id=components["C-01"])
        custody.allocate_resource(request_id="acc-slot", actor_id="wh1", site_id="s1",
                                  resource_type="loan_slot", resource_id="review-room-1",
                                  component_id=components["C-01"],
                                  start_at="2026-10-03T10:00:00Z", end_at="2026-10-03T11:00:00Z")

        # 评委借阅与按时返还（交接双方必须是不同操作者）。
        clock.advance(hours=2)
        loan = custody.create_handover(request_id="acc-loan", actor_id="wh1", site_id="s1",
                                       kind="loan", to_party="reviewer:jv1",
                                       component_ids=[components["C-01"], components["C-02"]],
                                       due_at="2026-10-05T10:00:00Z")
        custody.confirm_handover(request_id="acc-loan-cf", actor_id="sec1",
                                 handover_id=loan.resource_id,
                                 items=[{"component_id": components["C-01"], "seal_code_actual": "S-01"},
                                        {"component_id": components["C-02"], "seal_code_actual": "S-02"}])
        clock.advance(hours=2)
        hand_back = custody.create_handover(request_id="acc-ret", actor_id="sec1", site_id="s1",
                                            kind="return", to_party="warehouse:s1",
                                            component_ids=[components["C-01"], components["C-02"]])
        custody.confirm_handover(request_id="acc-ret-cf", actor_id="wh1",
                                 handover_id=hand_back.resource_id,
                                 items=[{"component_id": components["C-01"], "seal_code_actual": "S-01"},
                                        {"component_id": components["C-02"], "seal_code_actual": "S-02"}])
        custody.allocate_resource(request_id="acc-al-c01b", actor_id="wh1", site_id="s1",
                                  resource_type="location", resource_id="L1",
                                  component_id=components["C-01"])

        # 封签差异：C-03 返还时封签不符，只冻结 C-03，同包裹的 C-04 不受影响。
        clock.advance(hours=1)
        loan2 = custody.create_handover(request_id="acc-loan2", actor_id="wh1", site_id="s1",
                                        kind="loan", to_party="reviewer:jv2",
                                        component_ids=[components["C-03"]],
                                        due_at="2026-10-06T10:00:00Z")
        custody.confirm_handover(request_id="acc-loan2-cf", actor_id="sec1",
                                 handover_id=loan2.resource_id,
                                 items=[{"component_id": components["C-03"], "seal_code_actual": "S-03"}])
        clock.advance(hours=1)
        back2 = custody.create_handover(request_id="acc-ret2", actor_id="sec1", site_id="s1",
                                        kind="return", to_party="warehouse:s1",
                                        component_ids=[components["C-03"]])
        custody.confirm_handover(request_id="acc-ret2-cf", actor_id="wh1",
                                 handover_id=back2.resource_id,
                                 items=[{"component_id": components["C-03"],
                                         "seal_code_actual": "S-XX",
                                         "condition_note": "封签与出库记录不一致"}])
        responsibility = custody.damage_responsibility(actor_id="au1", component_id=components["C-03"])
        c04_before_sweep = custody.package_detail(actor_id="wh1", package_id=package_id)
        c04_state = {item["component_code"]: item for item in c04_before_sweep["works"][0]["components"]}["C-04"]

        # 补录证据：晚到的承运单与现场说明，只追加不覆盖。
        custody.append_evidence(request_id="acc-ev1", actor_id="car1", site_id="s1",
                                target_type="package", target_id=package_id,
                                note="承运单 YTO-88 晚到，补录", attachment_digest="sha256:waybill")
        custody.append_evidence(request_id="acc-ev2", actor_id="wh1", site_id="s1",
                                target_type="handover", target_id=back2.resource_id,
                                note="现场照片显示封签在返还前已破损")

        # 逾期巡检：C-04 借出后逾期未还，被冻结后由评审秘书解冻。
        loan3 = custody.create_handover(request_id="acc-loan3", actor_id="wh1", site_id="s1",
                                        kind="loan", to_party="reviewer:jv3",
                                        component_ids=[components["C-04"]],
                                        due_at="2026-10-01T18:00:00Z")
        custody.confirm_handover(request_id="acc-loan3-cf", actor_id="sec1",
                                 handover_id=loan3.resource_id,
                                 items=[{"component_id": components["C-04"], "seal_code_actual": "S-04"}])
        clock.advance(days=2)
        custody.sweep_overdue(request_id="acc-sweep", actor_id="sec1", site_id="s1")
        c04_after_sweep = custody.package_detail(actor_id="wh1", package_id=package_id)
        c04_frozen = {item["component_code"]: item for item in c04_after_sweep["works"][0]["components"]}["C-04"]["frozen"]

        # 历史时点还原：借阅途中 C-01 不在任何库位，保管人是评委。
        during_loan = custody.location_at(actor_id="au1", component_id=components["C-01"],
                                          at="2026-10-01T11:00:00Z")

        # 重启前留下一条待确认交接。
        custody.create_handover(request_id="acc-pending", actor_id="wh1", site_id="s1",
                                kind="transfer", to_party="warehouse:s2",
                                component_ids=[components["C-02"]], note="复核转场待对方确认")
        database.close()

        # 服务重启：预约顺序、资源占用与待确认交接保持不变。
        reopened = Database(path)
        custody2 = CustodyService(reopened, clock)
        reservations = custody2.list_reservations(actor_id="wh1", site_id="s1")
        warehouse = custody2.warehouse_view(actor_id="wh1", site_id="s1")
        pending = custody2.pending_handovers(actor_id="sec1", site_id="s1")
        auditor = custody2.auditor_view(actor_id="au1", site_id="s1")
        valid, event_count = custody2.foundation.verify_audit()
        reopened.close()

        result = {
            "status": "ok",
            "audit_valid": valid and auditor["audit_valid"],
            "audit_events": event_count,
            "components": len(components),
            "reservation_seq": reservations[0]["seq"],
            "reservation_status": reservations[0]["status"],
            "responsibility": responsibility["detection"]["responsible_party"],
            "sibling_unaffected": not c04_state["frozen"],
            "frozen_after_sweep": c04_frozen,
            "pending_after_restart": len(pending),
            "location_occupied": sum(item["active"] for item in warehouse["locations"]),
            "during_loan_custodian": during_loan["custodian"],
            "during_loan_location": during_loan["location_id"],
            "evidence_notes": len(auditor["evidence"]),
        }
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = (result["status"] == "ok" and result["audit_valid"]
                and result["responsibility"] == "reviewer:jv2"
                and result["sibling_unaffected"] and result["frozen_after_sweep"]
                and result["pending_after_restart"] == 1
                and result["during_loan_custodian"] == "reviewer:jv1"
                and result["during_loan_location"] is None)
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
