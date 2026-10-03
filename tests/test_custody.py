import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creative_program_foundation.custody import CustodyService
from creative_program_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class MutableClock:
    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value

    def advance(self, **kwargs):
        self._value = self._value + timedelta(**kwargs)


class CustodyTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.custody = CustodyService(self.database, self.clock)
        self._request = 0
        self.domain.register_organization(request_id="boot-org", actor_id="bootstrap",
                                          organization_id="o1", name="组委会")
        self.domain.register_actor(request_id="boot-admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_organization(request_id="boot-carrier-org", actor_id="a1",
                                          organization_id="carrier-org", name="承运商")
        for request_id, actor_id, name, role, org in (
                ("boot-w1", "w1", "仓管甲", "warehouse", "o1"),
                ("boot-w2", "w2", "仓管乙", "warehouse", "o1"),
                ("boot-sec", "sec1", "评审秘书", "secretary", "o1"),
                ("boot-rev", "rev1", "评委一", "reviewer", "o1"),
                ("boot-rev2", "rev2", "评委二", "reviewer", "o1"),
                ("boot-au", "au1", "审计员", "auditor", "o1"),
                ("boot-car", "car1", "承运联络", "carrier", "carrier-org")):
            self.domain.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                       display_name=name, role=role, organization_id=org)
        self.domain.register_site(request_id="boot-site", actor_id="a1", site_id="s1",
                                  organization_id="o1", name="样品中心", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def req(self):
        self._request += 1
        return f"req-{self._request}"

    def make_reservation(self, carrier_org="carrier-org", waybill_no="WB-1"):
        return self.custody.create_reservation(
            request_id=self.req(), actor_id="sec1", site_id="s1", carrier_org=carrier_org,
            expected_packages=2, slot_start="2026-10-01T09:00:00Z",
            slot_end="2026-10-01T18:00:00Z", waybill_no=waybill_no)

    def make_package(self, reservation_id, tracking_no="TRK-1", seal_no="SEAL-1", weight=3200):
        return self.custody.scan_package(
            request_id=self.req(), actor_id="w1", site_id="s1", reservation_id=reservation_id,
            tracking_no=tracking_no, seal_no=seal_no, weight_grams=weight,
            photo_digest="sha256:pkg", storage_condition="恒温")

    def make_work(self, package_id, declared=2, entry_no="E-1"):
        return self.custody.register_work(
            request_id=self.req(), actor_id="w1", package_id=package_id, entry_no=entry_no,
            title="茶器套装", declared_components=declared,
            storage_condition="恒温恒湿", orientation="竖直向上")

    def make_component(self, work_id, component_no="C-01", name="壶身", weight=1800, fragile=False):
        return self.custody.register_component(
            request_id=self.req(), actor_id="w1", work_id=work_id, component_no=component_no,
            name=name, weight_grams=weight, fragile=fragile)

    def make_verified_component(self, work_id, component_no="C-01", name="壶身", fragile=False):
        component = self.make_component(work_id, component_no, name, fragile=fragile)
        self.custody.verify_component(request_id=self.req(), actor_id="w1",
                                      component_id=component["component_id"], seal_no="SEAL-1")
        self.custody.verify_component(request_id=self.req(), actor_id="w2",
                                      component_id=component["component_id"], seal_no="SEAL-1")
        return component

    def make_location(self, code="L-01", capacity=1, condition_class="normal"):
        return self.custody.register_location(
            request_id=self.req(), actor_id="w1", site_id="s1", code=code,
            capacity=capacity, condition_class=condition_class)

    def intake_to_reviewable(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"])
        first = self.make_verified_component(work["work_id"], "C-01", "壶身")
        second = self.make_verified_component(work["work_id"], "C-02", "壶盖")
        return reservation, package, work, first, second

    # ---- 预约入库与扫码 ---------------------------------------------------

    def test_intake_flow_makes_work_reviewable(self):
        reservation, package, work, first, second = self.intake_to_reviewable()
        self.assertEqual(1, reservation["sequence"])
        self.assertEqual("registered", package["result"])
        view = self.custody.secretary_view(actor_id="sec1", site_id="s1")
        self.assertEqual(1, len(view["works"]))
        self.assertTrue(view["works"][0]["reviewable"])
        self.assertEqual(2, view["works"][0]["verified_components"])

    def test_reservation_sequence_is_stable_per_site(self):
        first = self.make_reservation()
        second = self.make_reservation(waybill_no="WB-2")
        self.assertEqual((1, 2), (first["sequence"], second["sequence"]))

    def test_late_waybill_can_be_registered_but_not_overwritten(self):
        reservation = self.custody.create_reservation(
            request_id=self.req(), actor_id="sec1", site_id="s1", carrier_org="carrier-org",
            expected_packages=1, slot_start="2026-10-01T09:00:00Z", slot_end="2026-10-01T18:00:00Z")
        updated = self.custody.update_reservation_waybill(
            request_id=self.req(), actor_id="car1",
            reservation_id=reservation["reservation_id"], waybill_no="WB-LATE")
        self.assertEqual("WB-LATE", updated["waybill_no"])
        with self.assertRaises(ConflictError):
            self.custody.update_reservation_waybill(
                request_id=self.req(), actor_id="car1",
                reservation_id=reservation["reservation_id"], waybill_no="WB-OTHER")

    def test_duplicate_scan_does_not_create_duplicate_inventory(self):
        reservation = self.make_reservation()
        first = self.make_package(reservation["reservation_id"])
        second = self.custody.scan_package(
            request_id=self.req(), actor_id="w2", site_id="s1",
            reservation_id=reservation["reservation_id"], tracking_no="TRK-1", seal_no="SEAL-1",
            weight_grams=3200)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["package_id"], second["package_id"])
        view = self.custody.warehouse_view(actor_id="w1", site_id="s1")
        self.assertEqual(1, len(view["packages"]))
        scans = self.database.connection.execute(
            "SELECT result, COUNT(*) AS count FROM custody_scan_events GROUP BY result"
        ).fetchall()
        self.assertEqual({"registered": 1, "duplicate": 1},
                         {row["result"]: row["count"] for row in scans})

    def test_misdelivered_scan_is_rejected_and_recorded(self):
        reservation = self.make_reservation(waybill_no="WB-1")
        result = self.custody.scan_package(
            request_id=self.req(), actor_id="w1", site_id="s1",
            reservation_id=reservation["reservation_id"], tracking_no="TRK-X",
            seal_no="SEAL-X", weight_grams=100, waybill_no="WB-OTHER")
        self.assertEqual("rejected_misdelivered", result["result"])
        view = self.custody.warehouse_view(actor_id="w1", site_id="s1")
        self.assertEqual(0, len(view["packages"]))
        self.assertEqual("misdelivered", view["open_exceptions"][0]["exception_type"])

    # ---- 双人核验 ---------------------------------------------------------

    def test_same_actor_cannot_verify_twice(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"])
        component = self.make_component(work["work_id"])
        self.custody.verify_component(request_id=self.req(), actor_id="w1",
                                      component_id=component["component_id"])
        with self.assertRaises(ConflictError):
            self.custody.verify_component(request_id=self.req(), actor_id="w1",
                                          component_id=component["component_id"])
        chain = self.custody.custody_chain(actor_id="w1", component_id=component["component_id"])
        self.assertEqual(1, chain["verify_count"])

    def test_single_verification_is_not_reviewable(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"])
        component = self.make_component(work["work_id"])
        self.make_component(work["work_id"], "C-02", "壶盖")
        self.custody.verify_component(request_id=self.req(), actor_id="w1",
                                      component_id=component["component_id"])
        view = self.custody.secretary_view(actor_id="sec1", site_id="s1")
        self.assertFalse(view["works"][0]["reviewable"])
        self.assertEqual(2, len(view["pending_verification"]))

    def test_component_count_cannot_exceed_declaration(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"], declared=1)
        self.make_component(work["work_id"])
        with self.assertRaises(ConflictError):
            self.make_component(work["work_id"], "C-02", "壶盖")

    # ---- 原子占用 ---------------------------------------------------------

    def test_location_capacity_is_atomic(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"])
        first = self.make_component(work["work_id"])
        second = self.make_component(work["work_id"], "C-02", "壶盖")
        location = self.make_location(capacity=1)
        self.custody.assign_location(request_id=self.req(), actor_id="w1",
                                     component_id=first["component_id"],
                                     location_id=location["location_id"])
        with self.assertRaises(ConflictError):
            self.custody.assign_location(request_id=self.req(), actor_id="w1",
                                         component_id=second["component_id"],
                                         location_id=location["location_id"])
        view = self.custody.warehouse_view(actor_id="w1", site_id="s1")
        self.assertEqual(1, view["locations"][0]["occupied"])
        other = self.make_location(code="L-02", capacity=1)
        self.custody.assign_location(request_id=self.req(), actor_id="w1",
                                     component_id=first["component_id"],
                                     location_id=other["location_id"])
        view = self.custody.warehouse_view(actor_id="w1", site_id="s1")
        occupancy = {row["code"]: row["occupied"] for row in view["locations"]}
        self.assertEqual({"L-01": 0, "L-02": 1}, occupancy)

    def test_equipment_capacity_is_atomic(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"])
        first = self.make_component(work["work_id"], fragile=True)
        second = self.make_component(work["work_id"], "C-02", "壶盖", fragile=True)
        equipment = self.custody.register_equipment(
            request_id=self.req(), actor_id="w1", site_id="s1", name="恒温柜-1",
            equipment_type="climate", capacity=1)
        self.custody.assign_equipment(request_id=self.req(), actor_id="w1",
                                      component_id=first["component_id"],
                                      equipment_id=equipment["equipment_id"])
        with self.assertRaises(ConflictError):
            self.custody.assign_equipment(request_id=self.req(), actor_id="w1",
                                          component_id=second["component_id"],
                                          equipment_id=equipment["equipment_id"])

    def test_fragile_component_requires_special_condition(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"])
        fragile = self.make_component(work["work_id"], fragile=True)
        normal = self.make_location(code="L-N", capacity=2, condition_class="normal")
        with self.assertRaises(ValidationError):
            self.custody.assign_location(request_id=self.req(), actor_id="w1",
                                         component_id=fragile["component_id"],
                                         location_id=normal["location_id"])
        climate = self.make_location(code="L-C", capacity=2, condition_class="climate")
        assigned = self.custody.assign_location(request_id=self.req(), actor_id="w1",
                                                component_id=fragile["component_id"],
                                                location_id=climate["location_id"])
        self.assertTrue(assigned["changed"])

    def test_loan_slots_are_atomic(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                 component_id=first["component_id"], borrower_id="rev1",
                                 slot_start="2026-10-02T09:00:00Z", slot_end="2026-10-03T18:00:00Z")
        with self.assertRaises(ConflictError):
            self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                     component_id=first["component_id"], borrower_id="rev2",
                                     slot_start="2026-10-03T09:00:00Z", slot_end="2026-10-04T18:00:00Z")
        follow_up = self.custody.create_loan(
            request_id=self.req(), actor_id="sec1", component_id=first["component_id"],
            borrower_id="rev2", slot_start="2026-10-03T18:00:00Z", slot_end="2026-10-05T18:00:00Z")
        self.assertEqual("booked", follow_up["status"])

    def test_unverified_component_cannot_be_loaned(self):
        reservation = self.make_reservation()
        package = self.make_package(reservation["reservation_id"])
        work = self.make_work(package["package_id"])
        component = self.make_component(work["work_id"])
        with self.assertRaises(ConflictError):
            self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                     component_id=component["component_id"], borrower_id="rev1",
                                     slot_start="2026-10-02T09:00:00Z",
                                     slot_end="2026-10-03T18:00:00Z")

    # ---- 连续保管链 -------------------------------------------------------

    def test_handover_requires_dual_confirmation_and_keeps_chain(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        loan = self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                        component_id=first["component_id"], borrower_id="rev1",
                                        slot_start="2026-10-01T10:00:00Z",
                                        slot_end="2026-10-02T18:00:00Z")
        handover = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="loan_out", to_holder="reviewer:rev1", seal_no="SEAL-1",
            weight_grams=1800)
        self.assertEqual(f"warehouse:s1", handover["from_holder"])
        self.assertEqual(loan["loan_id"], handover["loan_id"])
        with self.assertRaises(ConflictError):
            self.custody.initiate_handover(
                request_id=self.req(), actor_id="w1", component_id=first["component_id"],
                handover_type="transfer", to_holder="warehouse:s1")
        with self.assertRaises(PermissionDenied):
            self.custody.confirm_handover(request_id=self.req(), actor_id="w1",
                                          handover_id=handover["handover_id"], seal_no="SEAL-1")
        with self.assertRaises(PermissionDenied):
            self.custody.confirm_handover(request_id=self.req(), actor_id="rev2",
                                          handover_id=handover["handover_id"], seal_no="SEAL-1")
        confirmed = self.custody.confirm_handover(request_id=self.req(), actor_id="rev1",
                                                  handover_id=handover["handover_id"],
                                                  seal_no="SEAL-1", weight_grams=1800)
        self.assertEqual("confirmed", confirmed["status"])
        chain = self.custody.custody_chain(actor_id="au1", component_id=first["component_id"])
        self.assertEqual("reviewer:rev1", chain["current_holder"])
        self.assertEqual(["intake", "loan_out"],
                         [row["handover_type"] for row in chain["handovers"]])

    def test_return_out_completes_chain_and_releases_resources(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        location = self.make_location(capacity=2)
        self.custody.assign_location(request_id=self.req(), actor_id="w1",
                                     component_id=first["component_id"],
                                     location_id=location["location_id"])
        handover = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="return_out", to_holder="carrier:carrier-org", seal_no="SEAL-1")
        self.custody.confirm_handover(request_id=self.req(), actor_id="car1",
                                      handover_id=handover["handover_id"], seal_no="SEAL-1")
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        self.assertEqual("returned", chain["status"])
        self.assertEqual("carrier:carrier-org", chain["current_holder"])
        view = self.custody.warehouse_view(actor_id="w1", site_id="s1")
        self.assertEqual(0, view["locations"][0]["occupied"])

    # ---- 冻结与异常 -------------------------------------------------------

    def test_seal_mismatch_freezes_only_that_component(self):
        _, _, _, first, second = self.intake_to_reviewable()
        self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                 component_id=first["component_id"], borrower_id="rev1",
                                 slot_start="2026-10-01T10:00:00Z", slot_end="2026-10-02T18:00:00Z")
        handover = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="loan_out", to_holder="reviewer:rev1", seal_no="SEAL-1")
        confirmed = self.custody.confirm_handover(request_id=self.req(), actor_id="rev1",
                                                  handover_id=handover["handover_id"],
                                                  seal_no="SEAL-TAMPERED")
        self.assertTrue(confirmed["frozen"])
        self.assertEqual(1, len(confirmed["exception_ids"]))
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        self.assertTrue(chain["frozen"])
        self.assertEqual("seal_mismatch", chain["freeze_reason"])
        sibling = self.custody.custody_chain(actor_id="w1", component_id=second["component_id"])
        self.assertFalse(sibling["frozen"])
        loan = self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                        component_id=second["component_id"], borrower_id="rev2",
                                        slot_start="2026-10-01T10:00:00Z",
                                        slot_end="2026-10-02T18:00:00Z")
        self.assertEqual("booked", loan["status"])

    def test_frozen_component_blocks_outbound_but_allows_return(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                 component_id=first["component_id"], borrower_id="rev1",
                                 slot_start="2026-10-01T09:00:00Z", slot_end="2026-10-01T12:00:00Z")
        handover = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="loan_out", to_holder="reviewer:rev1", seal_no="SEAL-1")
        self.custody.confirm_handover(request_id=self.req(), actor_id="rev1",
                                      handover_id=handover["handover_id"], seal_no="SEAL-1")
        self.clock.advance(hours=13)
        swept = self.custody.sweep_overdue(actor_id="w1", site_id="s1")
        self.assertEqual(1, swept["count"])
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        self.assertTrue(chain["frozen"])
        self.assertEqual("overdue", chain["freeze_reason"])
        with self.assertRaises(ConflictError):
            self.custody.initiate_handover(
                request_id=self.req(), actor_id="w1", component_id=first["component_id"],
                handover_type="transfer", to_holder="warehouse:s1")
        returning = self.custody.initiate_handover(
            request_id=self.req(), actor_id="rev1", component_id=first["component_id"],
            handover_type="loan_return", to_holder="warehouse:s1", seal_no="SEAL-1")
        self.custody.confirm_handover(request_id=self.req(), actor_id="w2",
                                      handover_id=returning["handover_id"], seal_no="SEAL-1")
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        self.assertEqual("warehouse:s1", chain["current_holder"])
        self.assertTrue(chain["frozen"])
        view = self.custody.warehouse_view(actor_id="w1", site_id="s1")
        exception_id = view["open_exceptions"][0]["exception_id"]
        self.custody.resolve_exception(request_id=self.req(), actor_id="w1",
                                       exception_id=exception_id, resolution="已归还并复核无误")
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        self.assertFalse(chain["frozen"])

    def test_weight_mismatch_opens_exception(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                 component_id=first["component_id"], borrower_id="rev1",
                                 slot_start="2026-10-01T10:00:00Z", slot_end="2026-10-02T18:00:00Z")
        handover = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="loan_out", to_holder="reviewer:rev1",
            seal_no="SEAL-1", weight_grams=1800)
        confirmed = self.custody.confirm_handover(request_id=self.req(), actor_id="rev1",
                                                  handover_id=handover["handover_id"],
                                                  seal_no="SEAL-1", weight_grams=1200)
        self.assertTrue(confirmed["frozen"])
        view = self.custody.warehouse_view(actor_id="w1", site_id="s1")
        self.assertEqual("weight_mismatch", view["open_exceptions"][0]["exception_type"])

    def test_missing_component_exception_and_restore(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        opened = self.custody.open_exception(request_id=self.req(), actor_id="w1", site_id="s1",
                                             exception_type="missing",
                                             component_id=first["component_id"],
                                             detail="开箱复核未发现壶身")
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        self.assertEqual("missing", chain["status"])
        self.assertTrue(chain["frozen"])
        resolved = self.custody.resolve_exception(request_id=self.req(), actor_id="w1",
                                                  exception_id=opened["exception_id"],
                                                  resolution="在错放箱中找回",
                                                  restore_component=True)
        self.assertEqual("verified", resolved["component_status"])
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        self.assertFalse(chain["frozen"])
        self.assertEqual("verified", chain["status"])

    def test_auditor_cannot_open_exception(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        with self.assertRaises(PermissionDenied):
            self.custody.open_exception(request_id=self.req(), actor_id="au1", site_id="s1",
                                        exception_type="damaged",
                                        component_id=first["component_id"])

    # ---- 补录证据 ---------------------------------------------------------

    def test_evidence_is_append_only(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                 component_id=first["component_id"], borrower_id="rev1",
                                 slot_start="2026-10-01T10:00:00Z", slot_end="2026-10-02T18:00:00Z")
        handover = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="loan_out", to_holder="reviewer:rev1", seal_no="SEAL-1")
        before = self.database.connection.execute(
            "SELECT * FROM custody_handovers WHERE handover_id=?",
            (handover["handover_id"],)).fetchone()
        before = dict(before)
        self.custody.supplement_evidence(request_id=self.req(), actor_id="car1",
                                         handover_id=handover["handover_id"],
                                         evidence_type="waybill", content="承运单 WB-1 晚到补录")
        self.clock.advance(hours=1)
        self.custody.supplement_evidence(request_id=self.req(), actor_id="w1",
                                         handover_id=handover["handover_id"],
                                         evidence_type="photo", content="sha256:after-photo")
        after = dict(self.database.connection.execute(
            "SELECT * FROM custody_handovers WHERE handover_id=?",
            (handover["handover_id"],)).fetchone())
        self.assertEqual(before, after)
        chain = self.custody.custody_chain(actor_id="w1", component_id=first["component_id"])
        evidence = chain["handovers"][-1]["evidence"]
        self.assertEqual(["waybill", "photo"], [row["evidence_type"] for row in evidence])

    # ---- 损伤责任区间与历史还原 -------------------------------------------

    def test_damage_interval_lists_responsible_holders(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                 component_id=first["component_id"], borrower_id="rev1",
                                 slot_start="2026-10-01T09:00:00Z", slot_end="2026-10-02T18:00:00Z")
        loan_out = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="loan_out", to_holder="reviewer:rev1", seal_no="SEAL-1")
        self.clock.advance(hours=1)
        self.custody.confirm_handover(request_id=self.req(), actor_id="rev1",
                                      handover_id=loan_out["handover_id"], seal_no="SEAL-1")
        self.clock.advance(hours=5)
        loan_return = self.custody.initiate_handover(
            request_id=self.req(), actor_id="rev1", component_id=first["component_id"],
            handover_type="loan_return", to_holder="warehouse:s1", seal_no="SEAL-1")
        self.clock.advance(hours=1)
        self.custody.confirm_handover(request_id=self.req(), actor_id="w2",
                                      handover_id=loan_return["handover_id"],
                                      seal_no="SEAL-1", condition_state="damaged",
                                      condition_note="壶身出现裂纹")
        interval = self.custody.damage_interval(actor_id="au1",
                                                component_id=first["component_id"])
        self.assertEqual("damaged", interval["exception_type"])
        self.assertIsNotNone(interval["last_good_at"])
        # 最后一次完好观测是评委签收时，因此责任区间覆盖评委保管与归还转场两段
        self.assertEqual(["reviewer:rev1", "warehouse:s1"],
                         interval["responsible_holders"])
        self.assertEqual(["loan_return"],
                         [row["handover_type"] for row in interval["handovers"]])

    def test_position_at_historical_time(self):
        _, _, _, first, _ = self.intake_to_reviewable()
        location = self.make_location(capacity=2)
        self.clock.advance(hours=1)
        shelve_at = self.clock.now()
        self.custody.assign_location(request_id=self.req(), actor_id="w1",
                                     component_id=first["component_id"],
                                     location_id=location["location_id"])
        self.custody.create_loan(request_id=self.req(), actor_id="sec1",
                                 component_id=first["component_id"], borrower_id="rev1",
                                 slot_start="2026-10-01T12:00:00Z", slot_end="2026-10-02T18:00:00Z")
        self.clock.advance(hours=2)
        handover = self.custody.initiate_handover(
            request_id=self.req(), actor_id="w1", component_id=first["component_id"],
            handover_type="loan_out", to_holder="reviewer:rev1", seal_no="SEAL-1")
        self.custody.confirm_handover(request_id=self.req(), actor_id="rev1",
                                      handover_id=handover["handover_id"], seal_no="SEAL-1")
        early = self.custody.position_at(actor_id="au1", component_id=first["component_id"],
                                         at="2026-10-01T08:30:00Z")
        self.assertEqual("warehouse:s1", early["holder"])
        self.assertIsNone(early["location_id"])
        shelved = self.custody.position_at(actor_id="au1", component_id=first["component_id"],
                                           at=shelve_at.isoformat().replace("+00:00", "Z"))
        self.assertEqual("warehouse:s1", shelved["holder"])
        self.assertEqual(location["location_id"], shelved["location_id"])
        late = self.custody.position_at(actor_id="au1", component_id=first["component_id"],
                                        at="2026-10-01T23:00:00Z")
        self.assertEqual("reviewer:rev1", late["holder"])

    # ---- 角色视图 ---------------------------------------------------------

    def test_views_are_role_separated(self):
        self.intake_to_reviewable()
        carrier = self.custody.carrier_view(actor_id="car1", site_id="s1", carrier_org="carrier-org")
        self.assertEqual(1, len(carrier["packages"]))
        self.assertNotIn("title", carrier["packages"][0])
        with self.assertRaises(PermissionDenied):
            self.custody.carrier_view(actor_id="car1", site_id="s1", carrier_org="other-carrier")
        with self.assertRaises(PermissionDenied):
            self.custody.warehouse_view(actor_id="sec1", site_id="s1")
        with self.assertRaises(PermissionDenied):
            self.custody.secretary_view(actor_id="w1", site_id="s1")
        auditor = self.custody.auditor_view(actor_id="au1", site_id="s1")
        self.assertGreater(len(auditor["custody_audit_events"]), 0)
        secretary = self.custody.secretary_view(actor_id="sec1", site_id="s1")
        self.assertEqual([1], [row["sequence"] for row in secretary["reservations"]])

    # ---- 幂等与重启 -------------------------------------------------------

    def test_request_replay_returns_stored_result(self):
        reservation = self.make_reservation()
        request_id = self.req()
        first = self.custody.scan_package(
            request_id=request_id, actor_id="w1", site_id="s1",
            reservation_id=reservation["reservation_id"], tracking_no="TRK-9",
            seal_no="SEAL-9", weight_grams=900)
        replay = self.custody.scan_package(
            request_id=request_id, actor_id="w1", site_id="s1",
            reservation_id=reservation["reservation_id"], tracking_no="TRK-9",
            seal_no="SEAL-9", weight_grams=900)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["package_id"], replay["package_id"])
        with self.assertRaises(ConflictError):
            self.custody.scan_package(
                request_id=request_id, actor_id="w1", site_id="s1",
                reservation_id=reservation["reservation_id"], tracking_no="TRK-OTHER",
                seal_no="SEAL-9", weight_grams=900)

    def test_restart_preserves_reservations_occupancy_and_pending_handovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custody.sqlite3"
            database = Database(path)
            clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
            domain = DomainService(database, clock)
            custody = CustodyService(database, clock)
            domain.register_organization(request_id="boot-org", actor_id="bootstrap",
                                         organization_id="o1", name="组委会")
            domain.register_actor(request_id="boot-admin", actor_id="bootstrap", new_actor_id="a1",
                                  display_name="管理员", role="admin", organization_id="o1")
            domain.register_actor(request_id="boot-w1", actor_id="a1", new_actor_id="w1",
                                  display_name="仓管甲", role="warehouse", organization_id="o1")
            domain.register_actor(request_id="boot-w2", actor_id="a1", new_actor_id="w2",
                                  display_name="仓管乙", role="warehouse", organization_id="o1")
            domain.register_actor(request_id="boot-sec", actor_id="a1", new_actor_id="sec1",
                                  display_name="评审秘书", role="secretary", organization_id="o1")
            domain.register_actor(request_id="boot-rev", actor_id="a1", new_actor_id="rev1",
                                  display_name="评委", role="reviewer", organization_id="o1")
            domain.register_site(request_id="boot-site", actor_id="a1", site_id="s1",
                                 organization_id="o1", name="样品中心", timezone_name="Asia/Shanghai")
            reservation = custody.create_reservation(
                request_id="r-1", actor_id="sec1", site_id="s1", carrier_org="carrier-org",
                expected_packages=1, slot_start="2026-10-01T09:00:00Z",
                slot_end="2026-10-01T18:00:00Z")
            package = custody.scan_package(request_id="r-2", actor_id="w1", site_id="s1",
                                           reservation_id=reservation["reservation_id"],
                                           tracking_no="TRK-1", seal_no="SEAL-1", weight_grams=100)
            work = custody.register_work(request_id="r-3", actor_id="w1",
                                         package_id=package["package_id"], entry_no="E-1",
                                         title="茶器套装", declared_components=1)
            component = custody.register_component(request_id="r-4", actor_id="w1",
                                                   work_id=work["work_id"], component_no="C-01",
                                                   name="壶身")
            custody.verify_component(request_id="r-5", actor_id="w1",
                                     component_id=component["component_id"])
            custody.verify_component(request_id="r-6", actor_id="w2",
                                     component_id=component["component_id"])
            location = custody.register_location(request_id="r-7", actor_id="w1", site_id="s1",
                                                 code="L-01", capacity=1)
            custody.assign_location(request_id="r-8", actor_id="w1",
                                    component_id=component["component_id"],
                                    location_id=location["location_id"])
            custody.create_loan(request_id="r-9", actor_id="sec1",
                                component_id=component["component_id"], borrower_id="rev1",
                                slot_start="2026-10-01T10:00:00Z", slot_end="2026-10-02T18:00:00Z")
            handover = custody.initiate_handover(
                request_id="r-10", actor_id="w1", component_id=component["component_id"],
                handover_type="loan_out", to_holder="reviewer:rev1", seal_no="SEAL-1")
            database.close()

            reopened = Database(path)
            custody2 = CustodyService(reopened, clock)
            view = custody2.secretary_view(actor_id="sec1", site_id="s1")
            self.assertEqual([1], [row["sequence"] for row in view["reservations"]])
            follow_up = custody2.create_reservation(
                request_id="r-11", actor_id="sec1", site_id="s1", carrier_org="carrier-org",
                expected_packages=1, slot_start="2026-10-02T09:00:00Z",
                slot_end="2026-10-02T18:00:00Z")
            self.assertEqual(2, follow_up["sequence"])
            warehouse = custody2.warehouse_view(actor_id="w1", site_id="s1")
            self.assertEqual(1, warehouse["locations"][0]["occupied"])
            self.assertEqual(1, len(warehouse["pending_handovers"]))
            confirmed = custody2.confirm_handover(request_id="r-12", actor_id="rev1",
                                                  handover_id=handover["handover_id"],
                                                  seal_no="SEAL-1")
            self.assertEqual("confirmed", confirmed["status"])
            chain = custody2.custody_chain(actor_id="w1", component_id=component["component_id"])
            self.assertEqual("reviewer:rev1", chain["current_holder"])
            reopened.close()


if __name__ == "__main__":
    unittest.main()
