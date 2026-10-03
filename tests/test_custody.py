import unittest
from datetime import datetime, timezone

from creative_program_foundation.clock import ManualClock
from creative_program_foundation.custody import CustodyService
from creative_program_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

FRAGILE = {"temperature_min_c": 18, "temperature_max_c": 24,
           "humidity_min_pct": 45, "humidity_max_pct": 55, "orientation": "upright"}


class CustodyTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.custody = CustodyService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="组委会")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        for request_id, actor_id, name, role in (
            ("wh1", "wh1", "仓管甲", "warehouse_keeper"),
            ("wh2", "wh2", "仓管乙", "warehouse_keeper"),
            ("sec", "sec1", "评审秘书", "review_secretary"),
            ("car", "car1", "承运联络", "carrier_liaison"),
            ("au", "au1", "审计员", "auditor"),
        ):
            self.service.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                        display_name=name, role=role, organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="样品中心", timezone_name="Asia/Shanghai")
        self.custody.register_location(request_id="loc1", actor_id="wh1", site_id="s1",
                                       location_id="L1", capacity=2,
                                       conditions={"temperature_min_c": 20, "temperature_max_c": 22,
                                                   "humidity_min_pct": 48, "humidity_max_pct": 52,
                                                   "orientations": ["upright", "any"]})
        self.custody.register_location(request_id="loc2", actor_id="wh1", site_id="s1",
                                       location_id="L2", capacity=1)
        self.custody.register_equipment(request_id="eq1", actor_id="wh1", site_id="s1",
                                        equipment_id="E1", name="恒温恒湿柜", total=1)

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------

    def _check_in(self, request_id="pkg", package_code="PKG-1", works=None, **kwargs):
        works = works if works is not None else [
            {"work_key": "W1", "title": "青瓷茶器套装", "storage_condition": FRAGILE,
             "components": [
                 {"component_code": "C-01", "name": "茶壶", "weight_grams": 820,
                  "seal_code": "S-01", "photo_digest": "sha256:c01"},
                 {"component_code": "C-02", "name": "茶杯", "weight_grams": 240,
                  "seal_code": "S-02", "photo_digest": "sha256:c02",
                  "storage_condition": {}},
             ]}
        ]
        defaults = {"actor_id": "wh1", "site_id": "s1", "package_code": package_code,
                    "carrier_waybill": "YTO-1", "seal_code": "SEAL-P", "weight_grams": 1200,
                    "photo_digest": "sha256:pkg", "works": works}
        defaults.update(kwargs)
        return self.custody.check_in_package(request_id=request_id, **defaults)

    def _components(self, package_id):
        detail = self.custody.package_detail(actor_id="wh1", package_id=package_id)
        return {item["component_code"]: item for item in detail["works"][0]["components"]}

    def _work_id(self, package_id):
        return self.custody.package_detail(actor_id="wh1", package_id=package_id)["works"][0]["work_id"]

    def _verify_pair(self, component_id, tag=""):
        self.custody.verify_component(request_id=f"v1{tag}{component_id[:6]}", actor_id="wh1",
                                      component_id=component_id, result="pass")
        self.custody.verify_component(request_id=f"v2{tag}{component_id[:6]}", actor_id="sec1",
                                      component_id=component_id, result="pass")

    def _reviewable(self):
        receipt = self._check_in()
        package_id = receipt.resource_id
        components = self._components(package_id)
        for component in components.values():
            self._verify_pair(component["component_id"])
        work_id = self._work_id(package_id)
        self.custody.assemble_sample(request_id="assemble", actor_id="sec1", work_id=work_id)
        return package_id, work_id, components

    def _plain_components(self, request_id="pkg-plain", package_code="PKG-PLAIN"):
        """登记一对没有保管条件要求的组件并通过双人核验。"""

        receipt = self._check_in(request_id=request_id, package_code=package_code, works=[
            {"work_key": "W9", "title": "普通作品", "components": [
                {"component_code": "P-01", "name": "部件一"},
                {"component_code": "P-02", "name": "部件二"},
            ]}])
        components = self._components(receipt.resource_id)
        for component in components.values():
            self._verify_pair(component["component_id"])
        return components

    # ------------------------------------------------------------------
    # 入库登记与防重复扫码
    # ------------------------------------------------------------------

    def test_check_in_records_full_profile(self):
        receipt = self._check_in()
        detail = self.custody.package_detail(actor_id="wh1", package_id=receipt.resource_id)
        self.assertEqual("PKG-1", detail["package_code"])
        self.assertEqual("YTO-1", detail["carrier_waybill"])
        self.assertEqual("SEAL-P", detail["seal_code"])
        self.assertEqual(1200, detail["weight_grams"])
        self.assertEqual("sha256:pkg", detail["photo_digest"])
        self.assertEqual("wh1", detail["created_by"])
        components = detail["works"][0]["components"]
        self.assertEqual(2, len(components))
        teapot = next(item for item in components if item["component_code"] == "C-01")
        self.assertEqual("S-01", teapot["seal_code"])
        self.assertEqual(820, teapot["weight_grams"])
        self.assertEqual(FRAGILE, teapot["storage_condition"])
        self.assertEqual("pending_verification", teapot["status"])
        self.assertEqual("warehouse:s1", teapot["custodian"])
        chain = self.custody.custody_chain(actor_id="wh1", component_id=teapot["component_id"])
        self.assertEqual(1, len(chain["handovers"]))
        self.assertEqual("check_in", chain["handovers"][0]["kind"])
        self.assertEqual("carrier:YTO-1", chain["handovers"][0]["from_party"])
        self.assertEqual("warehouse:s1", chain["handovers"][0]["to_party"])

    def test_check_in_links_reservation_and_marks_arrived(self):
        reservation = self.custody.create_reservation(
            request_id="res", actor_id="wh1", site_id="s1", expected_at="2026-10-02T09:00:00Z",
            carrier_ref="YTO-1", expected_packages=1)
        self._check_in(reservation_id=reservation.resource_id)
        reservations = self.custody.list_reservations(actor_id="wh1", site_id="s1")
        self.assertEqual("arrived", reservations[0]["status"])
        self.assertEqual(1, reservations[0]["arrived_packages"])

    def test_duplicate_scan_same_content_does_not_duplicate_inventory(self):
        first = self._check_in(request_id="scan-1")
        second = self._check_in(request_id="scan-2")
        self.assertEqual(first.resource_id, second.resource_id)
        packages = self.custody.list_packages(actor_id="wh1", site_id="s1")
        self.assertEqual(1, len(packages))
        self.assertEqual(2, packages[0]["components"])

    def test_duplicate_scan_different_content_conflicts(self):
        self._check_in(request_id="scan-1")
        with self.assertRaises(ConflictError):
            self._check_in(request_id="scan-2", weight_grams=9999)

    def test_duplicate_component_code_conflicts(self):
        self._check_in(request_id="scan-1")
        with self.assertRaises(ConflictError):
            self._check_in(request_id="scan-2", package_code="PKG-2")

    def test_check_in_rejects_non_warehouse_role(self):
        with self.assertRaises(PermissionDenied):
            self.custody.check_in_package(request_id="pkg-x", actor_id="car1", site_id="s1",
                                          package_code="PKG-X",
                                          works=[{"work_key": "W", "title": "t",
                                                  "components": [{"component_code": "C-X", "name": "n"}]}])

    # ------------------------------------------------------------------
    # 双人核验与可评审样品
    # ------------------------------------------------------------------

    def test_double_verification_requires_two_distinct_actors(self):
        package_id = self._check_in().resource_id
        component = self._components(package_id)["C-01"]
        cid = component["component_id"]
        self.custody.verify_component(request_id="v1", actor_id="wh1", component_id=cid, result="pass")
        with self.assertRaises(ConflictError):
            self.custody.verify_component(request_id="v2", actor_id="wh1", component_id=cid, result="pass")
        state = self._components(package_id)["C-01"]
        self.assertEqual("pending_verification", state["status"])
        self.custody.verify_component(request_id="v3", actor_id="sec1", component_id=cid, result="pass")
        self.assertEqual("verified", self._components(package_id)["C-01"]["status"])

    def test_assemble_requires_all_components_verified(self):
        package_id = self._check_in().resource_id
        components = self._components(package_id)
        self._verify_pair(components["C-01"]["component_id"])
        with self.assertRaises(ConflictError):
            self.custody.assemble_sample(request_id="asm", actor_id="sec1",
                                         work_id=self._work_id(package_id))
        self._verify_pair(components["C-02"]["component_id"])
        self.custody.assemble_sample(request_id="asm", actor_id="sec1",
                                     work_id=self._work_id(package_id))
        detail = self.custody.package_detail(actor_id="wh1", package_id=package_id)
        self.assertEqual("reviewable", detail["works"][0]["status"])

    def test_verification_fail_opens_independent_exception(self):
        package_id = self._check_in().resource_id
        cid = self._components(package_id)["C-01"]["component_id"]
        self.custody.verify_component(request_id="v1", actor_id="wh1", component_id=cid,
                                      result="fail", note="壶嘴有裂纹")
        view = self.custody.warehouse_view(actor_id="wh1", site_id="s1")
        self.assertEqual(1, len(view["open_exceptions"]))
        self.assertEqual("damaged", view["open_exceptions"][0]["kind"])
        self.custody.verify_component(request_id="v-re-1", actor_id="wh2", component_id=cid, result="pass")
        self.custody.verify_component(request_id="v-re-2", actor_id="sec1", component_id=cid, result="pass")
        self.assertEqual("verified", self._components(package_id)["C-01"]["status"])
        with self.assertRaises(ConflictError):
            self.custody.assemble_sample(request_id="asm", actor_id="sec1",
                                         work_id=self._work_id(package_id))

    # ------------------------------------------------------------------
    # 资源原子占用
    # ------------------------------------------------------------------

    def test_location_capacity_is_enforced_atomically(self):
        components = self._plain_components()
        p01, p02 = components["P-01"]["component_id"], components["P-02"]["component_id"]
        self.custody.allocate_resource(request_id="a1", actor_id="wh1", site_id="s1",
                                       resource_type="location", resource_id="L2", component_id=p01)
        with self.assertRaises(ConflictError):
            self.custody.allocate_resource(request_id="a2", actor_id="wh1", site_id="s1",
                                           resource_type="location", resource_id="L2", component_id=p02)
        view = self.custody.warehouse_view(actor_id="wh1", site_id="s1")
        l2 = next(item for item in view["locations"] if item["location_id"] == "L2")
        self.assertEqual(1, l2["active"])
        self.assertEqual(0, l2["free"])

    def test_location_must_satisfy_storage_condition(self):
        _, _, components = self._reviewable()
        fragile = components["C-01"]["component_id"]
        with self.assertRaises(ConflictError):
            self.custody.allocate_resource(request_id="a1", actor_id="wh1", site_id="s1",
                                           resource_type="location", resource_id="L2",
                                           component_id=fragile)
        self.custody.allocate_resource(request_id="a2", actor_id="wh1", site_id="s1",
                                       resource_type="location", resource_id="L1", component_id=fragile)

    def test_component_has_single_active_location(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        self.custody.allocate_resource(request_id="a1", actor_id="wh1", site_id="s1",
                                       resource_type="location", resource_id="L1", component_id=c01)
        with self.assertRaises(ConflictError):
            self.custody.allocate_resource(request_id="a2", actor_id="wh1", site_id="s1",
                                           resource_type="location", resource_id="L1", component_id=c01)

    def test_equipment_capacity_is_enforced(self):
        components = self._plain_components()
        p01, p02 = components["P-01"]["component_id"], components["P-02"]["component_id"]
        self.custody.allocate_resource(request_id="e1", actor_id="wh1", site_id="s1",
                                       resource_type="equipment", resource_id="E1", component_id=p01)
        with self.assertRaises(ConflictError):
            self.custody.allocate_resource(request_id="e2", actor_id="wh1", site_id="s1",
                                           resource_type="equipment", resource_id="E1", component_id=p02)

    def test_loan_slot_overlap_is_rejected(self):
        _, _, components = self._reviewable()
        c01, c02 = components["C-01"]["component_id"], components["C-02"]["component_id"]
        self.custody.allocate_resource(request_id="s1", actor_id="wh1", site_id="s1",
                                       resource_type="loan_slot", resource_id="room-1",
                                       component_id=c01,
                                       start_at="2026-10-03T10:00:00Z", end_at="2026-10-03T11:00:00Z")
        with self.assertRaises(ConflictError):
            self.custody.allocate_resource(request_id="s2", actor_id="wh1", site_id="s1",
                                           resource_type="loan_slot", resource_id="room-1",
                                           component_id=c02,
                                           start_at="2026-10-03T10:30:00Z", end_at="2026-10-03T12:00:00Z")
        self.custody.allocate_resource(request_id="s3", actor_id="wh1", site_id="s1",
                                       resource_type="loan_slot", resource_id="room-1",
                                       component_id=c02,
                                       start_at="2026-10-03T11:00:00Z", end_at="2026-10-03T12:00:00Z")

    def test_release_resource_frees_capacity(self):
        components = self._plain_components()
        p01, p02 = components["P-01"]["component_id"], components["P-02"]["component_id"]
        allocation = self.custody.allocate_resource(
            request_id="a1", actor_id="wh1", site_id="s1",
            resource_type="location", resource_id="L2", component_id=p01)
        self.custody.release_resource(request_id="r1", actor_id="wh1",
                                      allocation_id=allocation.resource_id)
        self.custody.allocate_resource(request_id="a2", actor_id="wh1", site_id="s1",
                                       resource_type="location", resource_id="L2", component_id=p02)

    # ------------------------------------------------------------------
    # 交接与连续保管链
    # ------------------------------------------------------------------

    def test_loan_moves_custody_and_releases_location(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        self.custody.allocate_resource(request_id="a1", actor_id="wh1", site_id="s1",
                                       resource_type="location", resource_id="L1", component_id=c01)
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01], due_at="2026-10-05T10:00:00Z")
        self.custody.confirm_handover(request_id="h1-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        chain = self.custody.custody_chain(actor_id="wh1", component_id=c01)
        self.assertEqual("reviewer:jv1", chain["component"]["custodian"])
        self.assertEqual("on_loan", chain["component"]["status"])
        self.assertIsNone(chain["component"]["location_id"])
        view = self.custody.warehouse_view(actor_id="wh1", site_id="s1")
        l1 = next(item for item in view["locations"] if item["location_id"] == "L1")
        self.assertEqual(0, l1["active"])

    def test_confirm_requires_different_actor(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01], due_at="2026-10-05T10:00:00Z")
        with self.assertRaises(PermissionDenied):
            self.custody.confirm_handover(request_id="h1-cf", actor_id="wh1",
                                          handover_id=loan.resource_id,
                                          items=[{"component_id": c01, "seal_code_actual": "S-01"}])

    def test_loan_requires_reviewable_sample(self):
        package_id = self._check_in().resource_id
        c01 = self._components(package_id)["C-01"]["component_id"]
        with self.assertRaises(ConflictError):
            self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                         kind="loan", to_party="reviewer:jv1",
                                         component_ids=[c01], due_at="2026-10-05T10:00:00Z")

    def test_pending_handover_blocks_second_handover(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                     kind="loan", to_party="reviewer:jv1",
                                     component_ids=[c01], due_at="2026-10-05T10:00:00Z")
        with self.assertRaises(ConflictError):
            self.custody.create_handover(request_id="h2", actor_id="wh1", site_id="s1",
                                         kind="transfer", to_party="warehouse:s2",
                                         component_ids=[c01])

    def test_seal_mismatch_freezes_only_affected_component(self):
        _, _, components = self._reviewable()
        c01, c02 = components["C-01"]["component_id"], components["C-02"]["component_id"]
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01, c02], due_at="2026-10-05T10:00:00Z")
        self.custody.confirm_handover(request_id="h1-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"},
                                             {"component_id": c02, "seal_code_actual": "S-02"}])
        back = self.custody.create_handover(request_id="h2", actor_id="sec1", site_id="s1",
                                            kind="return", to_party="warehouse:s1",
                                            component_ids=[c01, c02])
        self.custody.confirm_handover(request_id="h2-cf", actor_id="wh1",
                                      handover_id=back.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-TAMPERED"},
                                             {"component_id": c02, "seal_code_actual": "S-02"}])
        chain01 = self.custody.custody_chain(actor_id="wh1", component_id=c01)
        chain02 = self.custody.custody_chain(actor_id="wh1", component_id=c02)
        self.assertTrue(chain01["component"]["frozen"])
        self.assertEqual("reviewer:jv1", chain01["component"]["custodian"])
        self.assertEqual(1, chain01["component"]["open_exceptions"])
        self.assertFalse(chain02["component"]["frozen"])
        self.assertEqual("warehouse:s1", chain02["component"]["custodian"])
        self.assertEqual("verified", chain02["component"]["status"])
        # 完好的 C-02 仍可继续借出，不被同包裹的异常阻断。
        again = self.custody.create_handover(request_id="h3", actor_id="wh1", site_id="s1",
                                             kind="loan", to_party="reviewer:jv2",
                                             component_ids=[c02], due_at="2026-10-06T10:00:00Z")
        self.custody.confirm_handover(request_id="h3-cf", actor_id="sec1",
                                      handover_id=again.resource_id,
                                      items=[{"component_id": c02, "seal_code_actual": "S-02"}])

    def test_overdue_confirmation_freezes_component(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01], due_at="2026-10-02T10:00:00Z")
        self.custody.confirm_handover(request_id="h1-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        back = self.custody.create_handover(request_id="h2", actor_id="sec1", site_id="s1",
                                            kind="return", to_party="warehouse:s1",
                                            component_ids=[c01])
        self.clock.advance(days=3)
        self.custody.confirm_handover(request_id="h2-cf", actor_id="wh1",
                                      handover_id=back.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        chain = self.custody.custody_chain(actor_id="wh1", component_id=c01)
        self.assertTrue(chain["component"]["frozen"])

    def test_sweep_freezes_overdue_loan_and_pending_handover(self):
        _, _, components = self._reviewable()
        c01, c02 = components["C-01"]["component_id"], components["C-02"]["component_id"]
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01], due_at="2026-10-02T10:00:00Z")
        self.custody.confirm_handover(request_id="h1-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        self.custody.create_handover(request_id="h2", actor_id="wh1", site_id="s1",
                                     kind="transfer", to_party="warehouse:s2",
                                     component_ids=[c02], due_at="2026-10-02T12:00:00Z")
        self.clock.advance(days=2)
        self.custody.sweep_overdue(request_id="sweep", actor_id="sec1", site_id="s1")
        self.assertTrue(self.custody.custody_chain(actor_id="wh1", component_id=c01)["component"]["frozen"])
        self.assertTrue(self.custody.custody_chain(actor_id="wh1", component_id=c02)["component"]["frozen"])

    def test_unfreeze_requires_resolved_exceptions_and_privileged_role(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01], due_at="2026-10-05T10:00:00Z")
        self.custody.confirm_handover(request_id="h1-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        back = self.custody.create_handover(request_id="h2", actor_id="sec1", site_id="s1",
                                            kind="return", to_party="warehouse:s1",
                                            component_ids=[c01])
        self.custody.confirm_handover(request_id="h2-cf", actor_id="wh1",
                                      handover_id=back.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "BAD"}])
        with self.assertRaises(ConflictError):
            self.custody.unfreeze_component(request_id="u1", actor_id="sec1", component_id=c01)
        view = self.custody.warehouse_view(actor_id="wh1", site_id="s1")
        exception_id = view["open_exceptions"][0]["exception_id"]
        self.custody.resolve_exception(request_id="res1", actor_id="sec1", exception_id=exception_id,
                                       resolution_note="承运商确认运输途中换封，作品无损")
        with self.assertRaises(PermissionDenied):
            self.custody.unfreeze_component(request_id="u2", actor_id="wh1", component_id=c01)
        self.custody.unfreeze_component(request_id="u3", actor_id="sec1", component_id=c01,
                                        note="异常已结案，解除冻结")
        self.assertFalse(self.custody.custody_chain(actor_id="wh1", component_id=c01)["component"]["frozen"])

    # ------------------------------------------------------------------
    # 异常流程与补录证据
    # ------------------------------------------------------------------

    def test_independent_exception_flow(self):
        package_id = self._check_in().resource_id
        work_id = self._work_id(package_id)
        missing = self.custody.open_exception(request_id="ex1", actor_id="wh1", site_id="s1",
                                              kind="missing", work_id=work_id,
                                              description="茶盘缺失，包裹内未见")
        wrong = self.custody.open_exception(request_id="ex2", actor_id="car1", site_id="s1",
                                            kind="misdelivered", package_id=package_id,
                                            description="包裹错投至二号样品中心")
        view = self.custody.carrier_view(actor_id="car1", site_id="s1")
        self.assertEqual(1, len(view["misdeliveries"]))
        self.custody.resolve_exception(request_id="ex1-r", actor_id="sec1",
                                       exception_id=missing.resource_id,
                                       resolution_note="承运方补送到位")
        auditor = self.custody.auditor_view(actor_id="au1", site_id="s1")
        states = {item["exception_id"]: item["status"] for item in auditor["exceptions"]}
        self.assertEqual("resolved", states[missing.resource_id])
        self.assertEqual("open", states[wrong.resource_id])

    def test_evidence_is_append_only_and_does_not_rewrite_handover(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01], due_at="2026-10-05T10:00:00Z")
        self.custody.confirm_handover(request_id="h1-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        self.custody.append_evidence(request_id="ev1", actor_id="car1", site_id="s1",
                                     target_type="handover", target_id=loan.resource_id,
                                     note="承运单晚到，补录编号 YTO-88")
        self.custody.append_evidence(request_id="ev2", actor_id="wh1", site_id="s1",
                                     target_type="handover", target_id=loan.resource_id,
                                     note="现场照片补充", attachment_digest="sha256:photo")
        auditor = self.custody.auditor_view(actor_id="au1", site_id="s1")
        notes = [item["note"] for item in auditor["evidence"]]
        self.assertEqual(2, len(notes))
        chain = self.custody.custody_chain(actor_id="wh1", component_id=c01)
        loan_row = next(item for item in chain["handovers"] if item["handover_id"] == loan.resource_id)
        self.assertEqual("S-01", loan_row["seal_actual"])
        with self.assertRaises(NotFoundError):
            self.custody.append_evidence(request_id="ev3", actor_id="wh1", site_id="s1",
                                         target_type="handover", target_id="missing", note="x")

    # ------------------------------------------------------------------
    # 责任区间与历史时点
    # ------------------------------------------------------------------

    def _loan_and_return(self, c01, tag, borrower, return_seal="S-01"):
        loan = self.custody.create_handover(request_id=f"loan-{tag}", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party=borrower,
                                            component_ids=[c01], due_at="2026-10-20T10:00:00Z")
        self.custody.confirm_handover(request_id=f"loan-{tag}-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        self.clock.advance(hours=1)
        back = self.custody.create_handover(request_id=f"ret-{tag}", actor_id="sec1", site_id="s1",
                                            kind="return", to_party="warehouse:s1",
                                            component_ids=[c01])
        self.custody.confirm_handover(request_id=f"ret-{tag}-cf", actor_id="wh1",
                                      handover_id=back.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": return_seal}])
        return loan.resource_id, back.resource_id

    def test_damage_responsibility_interval(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        self.clock.advance(hours=1)
        self._loan_and_return(c01, "a", "reviewer:jv1")
        self.clock.advance(hours=1)
        _, back2 = self._loan_and_return(c01, "b", "reviewer:jv2", return_seal="S-TAMPERED")
        result = self.custody.damage_responsibility(actor_id="au1", component_id=c01,
                                                    handover_id=back2)
        detection = result["detection"]
        self.assertEqual("reviewer:jv2", detection["responsible_party"])
        self.assertIsNotNone(detection["interval_start"])
        self.assertEqual(back2, detection["handover_id"])
        self.assertEqual(0, next(item for item in result["intervals"]
                                 if item["handover_id"] == back2)["seal_match"])
        # 不指定交接时自动定位最近一次封签差异。
        auto = self.custody.damage_responsibility(actor_id="au1", component_id=c01)
        self.assertEqual(back2, auto["detection"]["handover_id"])

    def test_location_at_historical_moments(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        self.clock.advance(hours=1)
        self.custody.allocate_resource(request_id="a1", actor_id="wh1", site_id="s1",
                                       resource_type="location", resource_id="L1", component_id=c01)
        self.clock.advance(hours=1)
        loan = self.custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                            kind="loan", to_party="reviewer:jv1",
                                            component_ids=[c01], due_at="2026-10-10T10:00:00Z")
        self.custody.confirm_handover(request_id="h1-cf", actor_id="sec1",
                                      handover_id=loan.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        self.clock.advance(hours=1)
        back = self.custody.create_handover(request_id="h2", actor_id="sec1", site_id="s1",
                                            kind="return", to_party="warehouse:s1",
                                            component_ids=[c01])
        self.custody.confirm_handover(request_id="h2-cf", actor_id="wh1",
                                      handover_id=back.resource_id,
                                      items=[{"component_id": c01, "seal_code_actual": "S-01"}])
        self.custody.allocate_resource(request_id="a2", actor_id="wh1", site_id="s1",
                                       resource_type="location", resource_id="L1", component_id=c01)
        base = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        in_storage = self.custody.location_at(actor_id="au1", component_id=c01,
                                              at=base.replace(hour=9, minute=30).isoformat())
        self.assertEqual("L1", in_storage["location_id"])
        self.assertEqual("warehouse:s1", in_storage["custodian"])
        during_loan = self.custody.location_at(actor_id="au1", component_id=c01,
                                               at=base.replace(hour=10, minute=30).isoformat())
        self.assertIsNone(during_loan["location_id"])
        self.assertEqual("reviewer:jv1", during_loan["custodian"])
        after_return = self.custody.location_at(actor_id="au1", component_id=c01,
                                                at=base.replace(hour=12, minute=30).isoformat())
        self.assertEqual("L1", after_return["location_id"])
        self.assertEqual("warehouse:s1", after_return["custodian"])

    # ------------------------------------------------------------------
    # 退件、视图与重启保持
    # ------------------------------------------------------------------

    def test_send_back_completes_chain(self):
        package_id, work_id, components = self._reviewable()
        ids = [item["component_id"] for item in components.values()]
        send = self.custody.create_handover(request_id="sb", actor_id="wh1", site_id="s1",
                                            kind="send_back", to_party="carrier:return",
                                            component_ids=ids)
        self.custody.confirm_handover(request_id="sb-cf", actor_id="sec1",
                                      handover_id=send.resource_id,
                                      items=[{"component_id": cid,
                                              "seal_code_actual": code}
                                             for cid, code in zip(ids, ("S-01", "S-02"))])
        detail = self.custody.package_detail(actor_id="wh1", package_id=package_id)
        self.assertEqual("sent_back", detail["status"])
        self.assertEqual("sent_back", detail["works"][0]["status"])
        for item in detail["works"][0]["components"]:
            self.assertEqual("sent_back", item["status"])

    def test_role_views_are_scoped(self):
        self._reviewable()
        with self.assertRaises(PermissionDenied):
            self.custody.warehouse_view(actor_id="car1", site_id="s1")
        with self.assertRaises(PermissionDenied):
            self.custody.secretary_view(actor_id="wh1", site_id="s1")
        with self.assertRaises(PermissionDenied):
            self.custody.carrier_view(actor_id="au1", site_id="s1")
        with self.assertRaises(PermissionDenied):
            self.custody.auditor_view(actor_id="wh1", site_id="s1")
        secretary = self.custody.secretary_view(actor_id="sec1", site_id="s1")
        self.assertEqual(1, len(secretary["reviewable_works"]))
        carrier = self.custody.carrier_view(actor_id="car1", site_id="s1")
        self.assertEqual(1, len(carrier["packages"]))
        auditor = self.custody.auditor_view(actor_id="au1", site_id="s1")
        self.assertTrue(auditor["audit_valid"])
        self.assertGreater(len(auditor["custody_events"]), 0)

    def test_restart_preserves_reservations_allocations_and_pending_handovers(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custody.sqlite3"
            database = Database(path)
            clock = ManualClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
            service = DomainService(database, clock)
            custody = CustodyService(database, clock)
            service.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="组委会")
            service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_actor(request_id="wh", actor_id="a1", new_actor_id="wh1",
                                   display_name="仓管", role="warehouse_keeper", organization_id="o1")
            service.register_actor(request_id="sec", actor_id="a1", new_actor_id="sec1",
                                   display_name="秘书", role="review_secretary", organization_id="o1")
            service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                  organization_id="o1", name="样品中心", timezone_name="Asia/Shanghai")
            custody.register_location(request_id="loc", actor_id="wh1", site_id="s1",
                                      location_id="L1", capacity=2)
            for index in range(2):
                custody.create_reservation(request_id=f"res-{index}", actor_id="wh1", site_id="s1",
                                           expected_at="2026-10-02T09:00:00Z",
                                           carrier_ref=f"YTO-{index}", expected_packages=1)
            receipt = custody.check_in_package(
                request_id="pkg", actor_id="wh1", site_id="s1", package_code="PKG-1",
                works=[{"work_key": "W1", "title": "茶器",
                        "components": [{"component_code": "C-01", "name": "茶壶",
                                        "seal_code": "S-01"}]}])
            cid = custody.package_detail(actor_id="wh1", package_id=receipt.resource_id)[
                "works"][0]["components"][0]["component_id"]
            custody.verify_component(request_id="v1", actor_id="wh1", component_id=cid, result="pass")
            custody.verify_component(request_id="v2", actor_id="sec1", component_id=cid, result="pass")
            custody.allocate_resource(request_id="a1", actor_id="wh1", site_id="s1",
                                      resource_type="location", resource_id="L1", component_id=cid)
            work_id = custody.package_detail(actor_id="wh1", package_id=receipt.resource_id)[
                "works"][0]["work_id"]
            custody.assemble_sample(request_id="asm", actor_id="sec1", work_id=work_id)
            custody.create_handover(request_id="h1", actor_id="wh1", site_id="s1",
                                    kind="transfer", to_party="warehouse:s2", component_ids=[cid])
            database.close()

            reopened = Database(path)
            custody2 = CustodyService(reopened, clock)
            reservations = custody2.list_reservations(actor_id="wh1", site_id="s1")
            self.assertEqual([1, 2], [item["seq"] for item in reservations])
            self.assertEqual(["YTO-0", "YTO-1"], [item["carrier_ref"] for item in reservations])
            view = custody2.warehouse_view(actor_id="wh1", site_id="s1")
            self.assertEqual(1, view["locations"][0]["active"])
            pending = custody2.pending_handovers(actor_id="sec1", site_id="s1")
            self.assertEqual(1, len(pending))
            self.assertEqual("transfer", pending[0]["kind"])
            self.assertEqual([cid], pending[0]["components"])
            valid, _ = custody2.foundation.verify_audit()
            self.assertTrue(valid)
            reopened.close()

    def test_audit_chain_remains_valid_after_full_flow(self):
        _, _, components = self._reviewable()
        c01 = components["C-01"]["component_id"]
        self._loan_and_return(c01, "a", "reviewer:jv1")
        valid, count = self.custody.foundation.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
