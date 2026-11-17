"""Admin password check: constant-time compare and lockout; fake secrets only."""
import hmac
import unittest

from test_backup_safety import load_app_functions


class AdminLoginTests(unittest.TestCase):
    def setUp(self):
        env = load_app_functions(["verify_admin_password"], hmac=hmac,
                                 ADMIN_MAX_FAILURES=3, ADMIN_LOCK_SECONDS=60)
        self.verify = env["verify_admin_password"]
        self.throttle = {"failures": 0, "locked_until": 0.0}

    def check(self, candidate, now=0.0, expected="s3cret"):
        return self.verify(candidate, expected, self.throttle, now)

    def test_correct_password_succeeds_and_resets_failures(self):
        self.check("bad"); self.check("bad")
        self.assertEqual(self.check("s3cret"), (True, ""))
        self.assertEqual(self.throttle["failures"], 0)

    def test_wrong_empty_and_unconfigured_passwords_fail(self):
        self.assertFalse(self.check("nope")[0])
        self.assertFalse(self.check("")[0])
        self.assertFalse(self.check("", expected="")[0])
        self.assertFalse(self.check("anything", expected="")[0])

    def test_non_ascii_password(self):
        self.assertTrue(self.check("密碼✓", expected="密碼✓")[0])
        self.assertFalse(self.check("密碼", expected="密碼✓")[0])

    def test_lockout_blocks_even_the_correct_password_until_expiry(self):
        for _ in range(3):
            self.assertFalse(self.check("bad", now=10.0)[0])
        ok, message = self.check("s3cret", now=11.0)
        self.assertFalse(ok)
        self.assertIn("秒後再試", message)
        self.assertTrue(self.check("s3cret", now=71.0)[0])


if __name__ == "__main__":
    unittest.main()
