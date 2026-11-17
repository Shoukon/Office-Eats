"""save_shop_names: one guarded transaction; fake SQLite and fake GitHub only."""
import sqlite3
import unittest

from test_backup_safety import IsolatedTest, load_app_functions


class ShopNameTests(IsolatedTest):
    def shops(self):
        return {row["category"]: row["shop_name"] for row in self.guard.export()["config_shop"]}

    def load(self, **extra):
        env = dict(db_connect=self.guard.connect)
        env.update(extra)
        return load_app_functions(["save_shop_names"], **env)["save_shop_names"]

    def test_saves_both_names_trimmed(self):
        self.ready()
        self.assertEqual(self.load()("  姑姑園 ", " 清心 "), (True, ""))
        self.assertEqual(self.shops(), {"main": "姑姑園", "drink": "清心"})

    def test_blank_names_are_allowed(self):
        self.ready()
        self.assertEqual(self.load()("", ""), (True, ""))
        self.assertEqual(self.shops(), {"main": "", "drink": ""})

    def test_failure_keeps_old_names_and_reports_error(self):
        self.ready()
        before = self.shops()

        def broken(write=False):
            conn = self.guard.connect(write=write)
            class Failing:
                def execute(inner, query, params=()):
                    if query.startswith("INSERT INTO config_shop"):
                        raise sqlite3.OperationalError("fake write failure")
                    return conn.execute(query, params)
                def __getattr__(inner, name): return getattr(conn, name)
            return Failing()

        ok, message = self.load(db_connect=broken)("新店", "新飲料")
        self.assertFalse(ok)
        self.assertIn("儲存失敗", message)
        self.assertEqual(self.shops(), before)


if __name__ == "__main__":
    unittest.main()
