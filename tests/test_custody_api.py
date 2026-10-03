import unittest
from datetime import datetime, timezone

from creative_program_foundation.api import route
from creative_program_foundation.clock import ManualClock
from creative_program_foundation.custody import CustodyService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class CustodyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = ManualClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.custody = CustodyService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="组委会")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="wh", actor_id="a1", new_actor_id="wh1",
                                    display_name="仓管", role="warehouse_keeper", organization_id="o1")
        self.service.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="样品中心", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="wh1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor}, custody=self.custody)

    def _get(self, path, actor="wh1"):
        return route(self.service, "GET", path, None, {"X-Actor-Id": actor}, custody=self.custody)

    def test_reservation_create_and_replay(self):
        body = {"request_id": "r1", "site_id": "s1", "expected_at": "2026-10-02T09:00:00Z",
                "carrier_ref": "YTO-1", "expected_packages": 2}
        status, payload = self._post("/custody/reservations", body)
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self._post("/custody/reservations", body)
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = self._get("/custody/reservations?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual(1, payload["items"][0]["seq"])

    def test_permission_denied_maps_to_403(self):
        status, payload = self._post("/custody/reservations",
                                     {"request_id": "r1", "site_id": "s1",
                                      "expected_at": "2026-10-02T09:00:00Z",
                                      "carrier_ref": "YTO-1", "expected_packages": 1},
                                     actor="au1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_package_check_in_and_views(self):
        status, payload = self._post("/custody/packages", {
            "request_id": "p1", "site_id": "s1", "package_code": "PKG-1",
            "carrier_waybill": "YTO-1", "weight_grams": 800,
            "works": [{"work_key": "W1", "title": "茶器",
                       "components": [{"component_code": "C-01", "name": "茶壶",
                                       "seal_code": "S-01"}]}]})
        self.assertEqual(201, status)
        package_id = payload["resource_id"]
        status, payload = self._get(f"/custody/packages/detail?package_id={package_id}")
        self.assertEqual(200, status)
        self.assertEqual("PKG-1", payload["package_code"])
        status, payload = self._get("/custody/views/warehouse?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["components"]))
        status, payload = self._get("/custody/views/warehouse?site_id=s1", actor="au1")
        self.assertEqual(403, status)

    def test_location_at_requires_timestamp(self):
        self._post("/custody/packages", {
            "request_id": "p1", "site_id": "s1", "package_code": "PKG-1",
            "works": [{"work_key": "W1", "title": "茶器",
                       "components": [{"component_code": "C-01", "name": "茶壶"}]}]})
        status, payload = self._get("/custody/packages?site_id=s1")
        status, payload = self._get("/custody/components/location-at?component_id=x")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_custody_route_returns_404(self):
        status, payload = self._get("/custody/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_custody_route_without_service_returns_404(self):
        status, payload = route(self.service, "GET", "/custody/reservations?site_id=s1", None,
                                {"X-Actor-Id": "wh1"})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
