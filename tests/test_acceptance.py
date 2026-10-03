import unittest

from creative_program_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        self.assertEqual(1, result["custody_reservation_sequence"])
        self.assertTrue(result["custody_duplicate_scan"])
        self.assertEqual(1, result["custody_reviewable_works"])


if __name__ == "__main__":
    unittest.main()
