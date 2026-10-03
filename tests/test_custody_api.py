import unittest
from datetime import datetime, timezone

from creative_program_foundation.api import ServiceHub, route
from creative_program_foundation.clock import FixedClock
from creative_program_foundation.storage import Database


class CustodyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.hub = ServiceHub(self.database, clock)
        route(self.hub, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "组委会"},
              {"X-Actor-Id": "bootstrap"})
        route(self.hub, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.hub, "POST", "/actors",
              {"request_id": "w1", "new_actor_id": "w1", "display_name": "仓管",
               "role": "warehouse", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.hub, "POST", "/actors",
              {"request_id": "sec1", "new_actor_id": "sec1", "display_name": "秘书",
               "role": "secretary", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.hub, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "样品中心", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "a1"})

    def tearDown(self):
        self.database.close()

    def test_custody_reservation_round_trip(self):
        status, payload = route(self.hub, "POST", "/custody/reservations",
                                {"request_id": "r1", "site_id": "s1", "carrier_org": "carrier-org",
                                 "expected_packages": 2, "slot_start": "2026-10-01T09:00:00Z",
                                 "slot_end": "2026-10-01T18:00:00Z"},
                                {"X-Actor-Id": "sec1"})
        self.assertEqual(201, status)
        self.assertEqual(1, payload["sequence"])
        status, payload = route(self.hub, "POST", "/custody/reservations",
                                {"request_id": "r1", "site_id": "s1", "carrier_org": "carrier-org",
                                 "expected_packages": 2, "slot_start": "2026-10-01T09:00:00Z",
                                 "slot_end": "2026-10-01T18:00:00Z"},
                                {"X-Actor-Id": "sec1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = route(self.hub, "GET", "/custody/views/secretary?site_id=s1",
                                None, {"X-Actor-Id": "sec1"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["reservations"]))

    def test_custody_scan_and_chain_over_http(self):
        route(self.hub, "POST", "/custody/reservations",
              {"request_id": "r1", "site_id": "s1", "carrier_org": "carrier-org",
               "expected_packages": 1, "slot_start": "2026-10-01T09:00:00Z",
               "slot_end": "2026-10-01T18:00:00Z"}, {"X-Actor-Id": "sec1"})
        status, package = route(self.hub, "POST", "/custody/packages/scan",
                                {"request_id": "r2", "site_id": "s1", "reservation_id": self._reservation(),
                                 "tracking_no": "TRK-1", "seal_no": "SEAL-1", "weight_grams": 100},
                                {"X-Actor-Id": "w1"})
        self.assertEqual(201, status)
        status, duplicate = route(self.hub, "POST", "/custody/packages/scan",
                                  {"request_id": "r3", "site_id": "s1", "reservation_id": self._reservation(),
                                   "tracking_no": "TRK-1", "seal_no": "SEAL-1", "weight_grams": 100},
                                  {"X-Actor-Id": "w1"})
        self.assertEqual(201, status)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(package["package_id"], duplicate["package_id"])

    def _reservation(self):
        row = self.database.connection.execute(
            "SELECT reservation_id FROM custody_reservations LIMIT 1").fetchone()
        return row["reservation_id"]

    def test_custody_route_requires_known_role(self):
        status, payload = route(self.hub, "POST", "/custody/reservations",
                                {"request_id": "r1", "site_id": "s1", "carrier_org": "carrier-org",
                                 "expected_packages": 1, "slot_start": "2026-10-01T09:00:00Z",
                                 "slot_end": "2026-10-01T18:00:00Z"},
                                {"X-Actor-Id": "ghost"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_unknown_custody_route_returns_404(self):
        status, payload = route(self.hub, "GET", "/custody/unknown", None,
                                {"X-Actor-Id": "a1"})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
