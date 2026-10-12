"""Additional final acceptance checks; fake data and API only."""
import copy
import sqlite3
import threading
from types import SimpleNamespace

from backup_safety import BackupGuard, BackupError, parse_backup
from test_backup_safety import IsolatedTest, FakeRemote, fixture, schema, load_app_functions, NOW


class FinalAcceptanceTests(IsolatedTest):
    def test_invalid_uploaded_bytes_leave_all_tables_and_remote_unchanged(self):
        remote = self.ready()
        before = self.snapshot()
        remote_before = copy.deepcopy(remote.data)
        fake_st = SimpleNamespace(session_state={})
        env = load_app_functions(["import_order_data", "current_backup_result"],
                                 st=fake_st, backup_guard=self.guard,
                                 github_is_configured=lambda: True,
                                 github_get_backup=remote.get, github_put_backup=remote.put)
        for raw in (b"{}", b"{", b"\xff\xfe", b'{"members":[],"orders":[]}'):
            with self.subTest(input_prefix=raw[:1]):
                with self.assertRaises(BackupError):
                    env["import_order_data"](parse_backup(raw))
                self.assertEqual(self.snapshot(), before)  # all four tables, IDs included
                self.assertEqual(remote.data, remote_before)
                self.assertEqual(remote.put_calls, 0)

    def test_os_lock_remains_held_after_commit_and_rollback_until_close(self):
        for commit in (False, True):
            with self.subTest(commit=commit):
                self.ready()
                conn = self.guard.connect(write=True)
                try:
                    conn.execute("UPDATE orders SET item_name='transaction-test' WHERE id=1")
                    conn.commit() if commit else conn.rollback()
                    outcomes = []
                    def contender():
                        other = BackupGuard(self.db, lambda: NOW, timeout=0.15)
                        try:
                            candidate = other.connect(write=True)
                            candidate.close()
                            outcomes.append("unexpectedly-acquired")
                        except sqlite3.OperationalError:
                            outcomes.append("locked")
                    thread = threading.Thread(target=contender)
                    thread.start(); thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(outcomes, ["locked"])
                finally:
                    conn.close()
                self.assertEqual(self.snapshot()["orders"][0]["item_name"],
                                 "transaction-test" if commit else "測試便當")
                # The coordination lock is released after close, not leaked.
                candidate = self.guard.connect(write=True)
                candidate.close()

    def test_two_independent_databases_use_sha_conflict_without_merging_or_overwriting(self):
        remote = self.ready()
        second = BackupGuard(self.db.parent / "second-fake.db", lambda: NOW, timeout=3)
        second.prepare(schema)
        self.assertEqual(second.startup(True, remote.get), "restored")
        self.write_item("instance-A")
        conn = second.connect(write=True)
        try:
            conn.execute("UPDATE orders SET item_name='instance-B' WHERE id=1")
            conn.commit()
        finally:
            conn.close()
        barrier = threading.Barrier(2)
        def simultaneous_get():
            snapshot = remote.get()
            barrier.wait(timeout=3)  # both instances observed the same old SHA
            return snapshot
        results, errors = {}, []
        def sync(label, guard):
            try:
                results[label] = guard.transfer(True, simultaneous_get, remote.put)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=sync, args=("A", self.guard)),
                   threading.Thread(target=sync, args=("B", second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results.values()), [False, True])
        winner = next(label for label, success in results.items() if success)
        loser = second if winner == "A" else self.guard
        self.assertEqual(remote.data["orders"][0]["item_name"], "instance-" + winner)
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "instance-A")
        self.assertEqual(second.export()["orders"][0]["item_name"], "instance-B")
        self.assertEqual(loser.status()["kind"], "error")
        put_calls = remote.put_calls
        remote_before = copy.deepcopy(remote.data)
        self.assertFalse(loser.transfer(True, remote.get, remote.put))
        self.assertEqual(remote.put_calls, put_calls)  # retry does not adopt a new SHA
        self.assertEqual(remote.data, remote_before)
