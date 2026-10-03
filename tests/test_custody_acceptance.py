import unittest

from creative_program_foundation.custody_acceptance import run


class CustodyAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(4, result["components"])
        self.assertEqual("reviewer:jv2", result["responsibility"])
        self.assertTrue(result["sibling_unaffected"])
        self.assertTrue(result["frozen_after_sweep"])
        self.assertEqual(1, result["pending_after_restart"])
        self.assertEqual("reviewer:jv1", result["during_loan_custodian"])
        self.assertIsNone(result["during_loan_location"])
        self.assertEqual(2, result["evidence_notes"])


if __name__ == "__main__":
    unittest.main()
