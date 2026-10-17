"""Run with `python -B -m unittest discover -s tests -v`.

Only TemporaryDirectory SQLite files, invented data, generated test encryption
keys and mocked GitHub requests are used. No application boot or Secrets reads.
"""
import ast
import base64
import copy
import json
import multiprocessing
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from backup_safety import (BackupGuard, BackupError, RemoteRejected, SafetyBlocked,
                           content_digest, parse_backup, replace_snapshot, validate_backup)


NOW = "2026-10-12 12:00:00"
SOURCE = Path(__file__).resolve().parents[1] / "order.py"


def fixture(item="測試便當", price=100):
    return {"format": "office-order-backup", "version": "4.1", "exported_at": NOW,
            "members": [{"id": 1, "name": "測試甲", "sort_order": 0}],
            "options": {"main_size": [{"value": "無", "is_default": 1}],
                        "tags": [{"value": "測試不要蔥", "is_default": 0}]},
            "config_shop": [{"category": "main", "shop_name": "測試店"}, {"category": "drink", "shop_name": ""}],
            "orders": [{"id": 1, "name": "測試甲", "category": "主餐", "item_name": item,
                        "price": price, "custom": "", "quantity": 1, "order_time": NOW,
                        "is_paid": 0, "unit_price": price}]}


def load_app_functions(names, **env):
    # Extract ONLY named definitions. Never execute order.py's app bootstrap,
    # top-level database operations, UI, or get_github_settings/get_admin_password.
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != set(names):
        raise AssertionError("missing app function")
    scope = {"sqlite3": sqlite3, "json": json, "base64": base64, "urllib": __import__("urllib"),
             "time": time, "BackupError": BackupError, "RemoteRejected": RemoteRejected,
             "SafetyBlocked": SafetyBlocked, "validate_backup": validate_backup, "parse_backup": parse_backup}
    scope.update(env)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope


def schema(conn):
    load_app_functions(["_init_schema"])["_init_schema"](conn)


class FakeRemote:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.sha = "fake-sha-0" if data is not None else None
        self.get_calls = 0
        self.put_calls = 0
        self.mode = "normal"
        self.fail_get = False

    def get(self):
        self.get_calls += 1
        if self.fail_get:
            raise TimeoutError("simulated GET timeout")
        return copy.deepcopy(self.data), self.sha

    def put(self, data, sha):
        self.put_calls += 1
        if self.mode == "reject" or sha != self.sha:
            raise RemoteRejected("simulated PUT conflict")
        if self.mode == "timeout_before":
            raise TimeoutError("simulated unconfirmed PUT")
        self.data = copy.deepcopy(data)
        self.sha = f"fake-sha-{self.put_calls}"
        if self.mode == "lost_response":
            raise TimeoutError("simulated response lost AFTER successful write")
        if self.mode == "lost_response_get_fail":
            self.fail_get = True
            raise TimeoutError("simulated response lost AFTER successful write")
        return self.sha


def child_write(db_file, queue):
    guard = BackupGuard(db_file, lambda: NOW, timeout=3)
    try:
        with guard.connect(write=True) as conn:
            conn.execute("UPDATE orders SET item_name='process-test' WHERE id=1")
            conn.commit()
        conn.close()
        queue.put("ok")
    except Exception as exc:
        queue.put(type(exc).__name__)


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        root = SOURCE.parent / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.tmp = TemporaryDirectory(prefix="office-eats-safety-test-", dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "fake.db"
        self.guard = BackupGuard(self.db, lambda: NOW, timeout=3)
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("LIVE NETWORK FORBIDDEN"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def ready(self, data=None):
        if not self.guard.prepare(schema):
            # Reset only this test's invented fixture between subcases.
            with self.guard.exclusive():
                state = self.guard.load_state()
                state.update(restore="pending", baseline_known=False, remote_sha=None, attempt=None)
                self.guard.save_state(state)
        remote = FakeRemote(data or fixture())
        self.assertEqual(self.guard.startup(True, remote.get), "restored")
        return remote

    def snapshot(self):
        return self.guard.export()

    def write_item(self, name):
        conn = self.guard.connect(write=True)
        try:
            conn.execute("UPDATE orders SET item_name=? WHERE id=1", (name,))
            conn.commit()
        finally:
            conn.close()


class RestoreTests(IsolatedTest):
    def existing_without_journal(self):
        conn = sqlite3.connect(self.db)
        schema(conn)
        replace_snapshot(conn, validate_backup(fixture()))
        conn.commit(); conn.close()
        self.guard.prepare(schema)

    def test_existing_database_first_adoption_requires_equal_content(self):
        self.existing_without_journal()
        remote = FakeRemote(fixture())
        self.assertEqual(self.guard.startup(True, remote.get), "adopted")
        self.assertEqual(remote.put_calls, 0)
        self.write_item("allowed-after-adoption")

    def test_existing_database_different_remote_is_preserved_and_blocked(self):
        self.existing_without_journal()
        before = self.snapshot()
        remote = FakeRemote(fixture("remote-newer"))
        self.assertEqual(self.guard.startup(True, remote.get), "adoption_conflict")
        with self.assertRaises(SafetyBlocked):
            self.write_item("must-not-write")
        with self.assertRaises(SafetyBlocked):
            self.guard.transfer(True, remote.get, remote.put)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(remote.data["orders"][0]["item_name"], "remote-newer")
        self.assertEqual(remote.put_calls, 0)

    def test_existing_adoption_get_failure_can_retry_without_losing_local_data(self):
        self.existing_without_journal()
        before = self.snapshot(); remote = FakeRemote(fixture()); remote.fail_get = True
        self.assertEqual(self.guard.startup(True, remote.get), "adoption_failed")
        self.assertEqual(self.snapshot(), before)
        remote.fail_get = False
        self.assertEqual(self.guard.startup(True, remote.get), "adopted")
        self.assertEqual(remote.put_calls, 0)

    def test_existing_database_verified_missing_remote_can_establish_baseline(self):
        self.existing_without_journal()
        remote = FakeRemote()
        self.assertEqual(self.guard.startup(True, remote.get), "missing")
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put))
        self.assertEqual(content_digest(remote.data), content_digest(self.snapshot()))

    def test_existing_remote_restored_and_normal_startup_does_not_restore_again(self):
        remote = self.ready()
        self.assertEqual(content_digest(self.snapshot()), content_digest(fixture()))
        calls = remote.get_calls
        self.assertFalse(self.guard.prepare(schema))
        self.assertEqual(self.guard.startup(True, remote.get), "ready")
        self.assertEqual(remote.get_calls, calls)
        self.assertEqual(remote.put_calls, 0)

    def test_verified_missing_remote_can_bootstrap_once(self):
        self.assertTrue(self.guard.prepare(schema))
        remote = FakeRemote()
        self.assertEqual(self.guard.startup(True, remote.get), "missing")
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put))
        self.assertEqual(remote.put_calls, 1)
        self.assertEqual(self.guard.startup(True, remote.get), "ready")

    def test_read_failure_keeps_retry_state_across_guard_recreation(self):
        self.guard.prepare(schema)
        remote = FakeRemote(fixture())
        remote.fail_get = True
        self.assertEqual(self.guard.startup(True, remote.get), "failed")
        with self.assertRaises(SafetyBlocked):
            self.guard.transfer(True, remote.get, remote.put)
        with self.assertRaises(SafetyBlocked):
            self.guard.connect(write=True)
        self.assertEqual(remote.put_calls, 0)
        restarted = BackupGuard(self.db, lambda: NOW)
        self.assertTrue(restarted.prepare(schema))
        remote.fail_get = False
        self.assertEqual(restarted.startup(True, remote.get), "restored")

    def test_invalid_remote_format_never_uploads_defaults(self):
        for data in ({}, {**fixture(), "version": "unknown"}):
            with self.subTest(data_version=data.get("version")):
                self.guard.prepare(schema)
                remote = FakeRemote(data)
                self.assertEqual(self.guard.startup(True, remote.get), "failed")
                self.assertEqual(remote.put_calls, 0)
                self.assertEqual(self.snapshot()["orders"], [])

    def test_restore_transaction_failure_rolls_back_and_retries(self):
        self.guard.prepare(schema)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TRIGGER fail_restore BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT,'injected'); END")
        conn.commit()
        conn.close()
        remote = FakeRemote(fixture())
        self.assertEqual(self.guard.startup(True, remote.get), "failed")
        self.assertEqual(self.snapshot()["members"], [])
        self.assertEqual(remote.put_calls, 0)

    def test_local_only_startup_has_explicit_unbacked_status(self):
        self.guard.prepare(schema)
        self.assertEqual(self.guard.startup(False, lambda: self.fail("must not GET")), "local_only")
        self.assertEqual(self.guard.status()["kind"], "error")

    def test_partial_schema_and_corrupt_sidecar_fail_closed(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE orders(id INTEGER)")
        conn.close()
        with self.assertRaises(SafetyBlocked):
            self.guard.prepare(schema)
        self.guard.state_path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(SafetyBlocked):
            self.guard.connect(write=True)


class ValidationTests(IsolatedTest):
    def test_current_format_roundtrip_business_data(self):
        self.ready()
        source = self.snapshot()
        imported = parse_backup(json.dumps(source, ensure_ascii=False).encode("utf-8"))
        remote = FakeRemote(source)
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=imported))
        self.assertEqual(content_digest(source), content_digest(self.snapshot()))
        self.assertEqual(source["orders"], self.snapshot()["orders"])
        self.assertEqual(source["options"], self.snapshot()["options"])

    def test_approved_unversioned_legacy_strings_and_missing_optional_fields(self):
        old = fixture()
        for key in ("format", "version", "exported_at"):
            old.pop(key)
        old["members"] = [{"name": "測試甲"}]
        old["options"] = {"tags": ["測試不要蔥"]}
        for key in ("custom", "is_paid", "unit_price"):
            old["orders"][0].pop(key)
        clean = validate_backup(old)
        self.assertEqual(clean["orders"][0]["unit_price"], None)
        self.assertEqual(clean["orders"][0]["is_paid"], 0)
        self.ready()
        remote = FakeRemote(fixture())
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=old))
        self.assertEqual(self.snapshot()["options"]["tags"][0]["is_default"], 0)
        self.assertEqual(self.snapshot()["orders"][0]["unit_price"], None)

    def test_deleted_members_historical_orders_empty_shops_zero_price_are_legal(self):
        data = fixture(price=0)
        data["members"] = []
        data["config_shop"][0]["shop_name"] = ""
        self.assertEqual(validate_backup(data)["orders"][0]["price"], 0)

    def test_invalid_inputs_leave_original_data_and_remote_unchanged(self):
        remote = self.ready()
        original = content_digest(self.snapshot())
        cases = [{}, [], {**fixture(), "version": "4.0"}, {**fixture(), "format": "other"}]
        for field in ("members", "options", "config_shop", "orders", "exported_at"):
            data = fixture(); data.pop(field); cases.append(data)
        for field in ("members", "options", "config_shop", "orders"):
            data = fixture(); data[field] = "wrong-type"; cases.append(data)
        for key, val in (("id", True), ("id", 0), ("quantity", 0), ("quantity", -1),
                         ("quantity", "1"), ("price", -1), ("price", 1.5), ("is_paid", 2),
                         ("is_paid", "0"), ("category", "unknown"), ("custom", None),
                         ("order_time", "bad-time"), ("unit_price", 90), ("item_name", "")):
            data = fixture(); data["orders"][0][key] = val; cases.append(data)
        data = fixture(); data["orders"].append(copy.deepcopy(data["orders"][0])); cases.append(data)
        data = fixture(); data["members"].append(copy.deepcopy(data["members"][0])); cases.append(data)
        data = fixture(); data["options"]["tags"] *= 2; cases.append(data)
        data = fixture(); data["options"]["tags"][0]["is_default"] = 2; cases.append(data)
        data = fixture(); data["options"]["main_size"].append({"value": "大", "is_default": 1}); cases.append(data)
        data = fixture(); data["config_shop"] = data["config_shop"][:1]; cases.append(data)
        data = fixture(); data["config_shop"].append(copy.deepcopy(data["config_shop"][0])); cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(BackupError):
                    self.guard.transfer(True, remote.get, remote.put, imported=data)
                self.assertEqual(content_digest(self.snapshot()), original)
                self.assertEqual(remote.put_calls, 0)

    def test_bad_json_encoding_duplicate_keys_and_empty_objects(self):
        for raw in (b"{}", b"{", b"\xff\xfe", b'{"members":[],"members":[]}'):
            with self.subTest(raw_kind=raw[:1]):
                with self.assertRaises(BackupError):
                    parse_backup(raw)

    def test_import_transaction_failure_rolls_back_every_table(self):
        remote = self.ready()
        before = self.snapshot()
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TRIGGER fail_import BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT,'injected'); END")
        conn.commit(); conn.close()
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new")))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(remote.put_calls, 0)


class SyncTests(IsolatedTest):
    def test_unconfigured_import_never_deletes_original(self):
        remote = self.ready(); before = self.snapshot()
        self.assertFalse(self.guard.transfer(False, remote.get, remote.put, imported=fixture("new")))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(remote.put_calls, 0)

    def test_uncertain_previous_transfer_cannot_claim_a_different_new_import_succeeded(self):
        remote = self.ready(); remote.mode = "timeout_before"
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("first")))
        with self.assertRaises(SafetyBlocked):
            self.guard.transfer(True, remote.get, remote.put, imported=fixture("second"))
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "測試便當")
        self.assertEqual(remote.put_calls, 1)

    def test_commit_failure_after_remote_success_reconciles_from_original(self):
        remote = self.ready()
        from contextlib import contextmanager
        original_exclusive = self.guard.exclusive
        class FailCommit:
            def __init__(self, conn):
                object.__setattr__(self, "conn", conn)
            def __getattr__(self, name):
                return getattr(self.conn, name)
            def __setattr__(self, name, value):
                setattr(self.conn, name, value)
            def commit(self):
                raise sqlite3.OperationalError("injected commit failure")
        @contextmanager
        def failing_exclusive():
            with original_exclusive() as conn:
                yield FailCommit(conn)
        self.guard.exclusive = failing_exclusive
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-import")))
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "測試便當")
        self.assertIsNotNone(self.guard.load_state()["attempt"])
        self.guard.exclusive = original_exclusive
        self.assertEqual(self.guard.startup(True, remote.get), "reconciled")
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "new-import")
        self.assertEqual(remote.put_calls, 1)

    def test_status_detects_other_session_changes_instead_of_stale_success(self):
        self.ready()
        other = BackupGuard(self.db, lambda: NOW)
        self.assertEqual(other.status()["kind"], "success")
        self.write_item("changed-by-another-session")
        self.assertEqual(other.status()["kind"], "pending")

    def test_get_failure_and_put_conflict_keep_local_changes_and_error_status(self):
        remote = self.ready()
        self.write_item("new-local")
        before = self.snapshot()
        remote.fail_get = True
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put))
        self.assertEqual(remote.put_calls, 0)
        self.assertEqual(self.guard.status()["kind"], "error")
        remote.fail_get = False; remote.mode = "reject"
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put))
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.guard.load_state()["attempt"])
        remote.mode = "normal"
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put))
        self.assertEqual(self.guard.status()["kind"], "success")

    def test_external_new_sha_is_not_adopted_to_overwrite_newer_backup(self):
        remote = self.ready()
        remote.sha = "external-newer-sha"; remote.data = fixture("external-data")
        self.write_item("local-data")
        for _ in range(2):
            self.assertFalse(self.guard.transfer(True, remote.get, remote.put))
        self.assertEqual(remote.put_calls, 0)
        self.assertEqual(remote.data["orders"][0]["item_name"], "external-data")

    def test_lost_response_reconciles_without_second_put_for_sync_and_import(self):
        for importing in (False, True):
            with self.subTest(importing=importing):
                remote = self.ready()
                if not importing:
                    self.write_item("new-sync")
                remote.mode = "lost_response"
                self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-import") if importing else None))
                self.assertEqual(remote.put_calls, 1)
                self.assertEqual(content_digest(self.snapshot()), content_digest(remote.data))
                self.assertIsNone(self.guard.load_state()["attempt"])

    def test_unconfirmed_put_retains_journal_and_blocks_writes_without_resend(self):
        remote = self.ready(); remote.mode = "timeout_before"
        before = self.snapshot()
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-import")))
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(SafetyBlocked):
            self.write_item("must-not-write")
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put))
        self.assertEqual(remote.put_calls, 1)
        self.assertEqual(self.guard.startup(True, remote.get), "uncertain")
        self.assertEqual(remote.put_calls, 1)

    def test_uncertain_write_can_be_reconciled_after_restart(self):
        remote = self.ready(); remote.mode = "lost_response_get_fail"
        before = self.snapshot()
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-import")))
        self.assertEqual(self.snapshot(), before)
        remote.fail_get = False
        restarted = BackupGuard(self.db, lambda: NOW)
        restarted.prepare(schema)
        self.assertEqual(restarted.startup(True, remote.get), "reconciled")
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "new-import")
        self.assertEqual(remote.put_calls, 1)

    def test_reconcile_never_overwrites_external_local_changes(self):
        remote = self.ready(); remote.mode = "lost_response_get_fail"
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-import")))
        # Deliberately emulate an OLD/uncoordinated external process.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE orders SET item_name='external-local' WHERE id=1")
        conn.commit(); conn.close()
        remote.fail_get = False
        self.assertEqual(self.guard.startup(True, remote.get), "uncertain")
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "external-local")

    def test_ui_wrapper_replaces_stale_success_after_failure_and_unconfigured(self):
        remote = self.ready()
        fake_st = SimpleNamespace(session_state={"github_sync_result": ("success", "old-success")}, error=lambda msg: None)
        env = load_app_functions(["sync_github_backup", "current_backup_result"],
                                 st=fake_st, backup_guard=self.guard, github_is_configured=lambda: True,
                                 github_get_backup=remote.get, github_put_backup=remote.put)
        remote.fail_get = True
        self.assertFalse(env["sync_github_backup"](True))
        self.assertEqual(fake_st.session_state["github_sync_result"][0], "error")
        env["github_is_configured"] = lambda: False
        self.assertFalse(env["sync_github_backup"]())
        self.assertEqual(fake_st.session_state["github_sync_result"][0], "error")


class ConcurrencyTests(IsolatedTest):
    def test_import_queued_addition_survives_success_and_rollback(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                remote = self.ready()
                entered, release, writer_started = threading.Event(), threading.Event(), threading.Event()
                errors, outcomes = [], []
                original_put = remote.put
                def delayed_put(data, sha):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("test release timeout")
                    if fail:
                        raise RemoteRejected("simulated conflict")
                    return original_put(data, sha)
                def importer():
                    try:
                        outcomes.append(self.guard.transfer(True, remote.get, delayed_put, imported=fixture("imported")))
                    except Exception as exc:
                        errors.append(exc)
                def writer():
                    writer_started.set()
                    try:
                        other = BackupGuard(self.db, lambda: NOW, timeout=3)
                        conn = other.connect(write=True)
                        try:
                            conn.execute("INSERT INTO orders(name,category,item_name,price,custom,quantity,order_time,is_paid,unit_price) VALUES(?,?,?,?,?,?,?,?,?)",
                                         ("測試乙", "主餐", "queued-addition", 20, "", 1, NOW, 0, 20))
                            conn.commit()
                        finally:
                            conn.close()
                    except Exception as exc:
                        errors.append(exc)
                first = threading.Thread(target=importer); first.start()
                self.assertTrue(entered.wait(3))
                second = threading.Thread(target=writer); second.start()
                self.assertTrue(writer_started.wait(3))
                release.set(); first.join(4); second.join(4)
                self.assertFalse(first.is_alive() or second.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(outcomes, [not fail])
                self.assertEqual([r["item_name"] for r in self.snapshot()["orders"]],
                                 ["測試便當" if fail else "imported", "queued-addition"])

    def test_two_requested_syncs_build_snapshots_only_after_lock_and_latest_write(self):
        remote = self.ready()
        self.write_item("first-change")
        entered, release, latest_written = threading.Event(), threading.Event(), threading.Event()
        outputs, results, errors = [], [], []
        original_put = remote.put
        def slow_put(data, sha):
            outputs.append(data["orders"][0]["item_name"])
            entered.set()
            if not release.wait(3):
                raise AssertionError("test timeout")
            return original_put(data, sha)
        other = BackupGuard(self.db, lambda: NOW, timeout=3)
        original_lock = other.file_lock
        from contextlib import contextmanager
        @contextmanager
        def wait_for_latest_lock():
            if not latest_written.wait(3):
                raise AssertionError("test latest write timeout")
            with original_lock():
                yield
        other.file_lock = wait_for_latest_lock
        def run(guard, put):
            try:
                results.append(guard.transfer(True, remote.get, put))
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run, args=(self.guard, slow_put)); first.start()
        self.assertTrue(entered.wait(3))
        second = threading.Thread(target=run, args=(other, remote.put)); second.start()
        release.set(); first.join(4)
        self.write_item("latest-change"); latest_written.set(); second.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(results, [True, True])
        self.assertEqual(outputs, ["first-change"])
        self.assertEqual(remote.data["orders"][0]["item_name"], "latest-change")
        self.assertEqual(content_digest(self.snapshot()), content_digest(remote.data))

    def test_same_file_os_lock_coordinates_a_second_process(self):
        self.ready()
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        with self.guard.exclusive():
            process = context.Process(target=child_write, args=(str(self.db), queue))
            process.start()
            # The child cannot finish while this transaction/OS lock is held.
            with self.assertRaises(__import__("queue").Empty):
                queue.get(timeout=0.3)
        process.join(5)
        self.assertFalse(process.is_alive())
        self.assertEqual(queue.get(timeout=1), "ok")
        queue.close()
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "process-test")

    def test_four_table_export_is_one_read_snapshot(self):
        self.ready()
        # WAL allows a committed second writer between export SELECTs, so this
        # verifies read-snapshot consistency rather than merely writer blocking.
        conn = sqlite3.connect(self.db); conn.execute("PRAGMA journal_mode=WAL"); conn.close()
        from backup_safety import export_snapshot
        conn = sqlite3.connect(self.db); conn.execute("BEGIN")
        fired = False
        def interleave(sql):
            nonlocal fired
            if not fired and sql.startswith("SELECT category,option_value"):
                fired = True
                writer = sqlite3.connect(self.db)
                writer.execute("UPDATE order_members SET name='new-member'")
                writer.execute("UPDATE orders SET name='new-member'")
                writer.commit(); writer.close()
        conn.set_trace_callback(interleave)
        snapshot = export_snapshot(conn, NOW); conn.close()
        self.assertTrue(fired)
        self.assertEqual(snapshot["members"][0]["name"], "測試甲")
        self.assertEqual(snapshot["orders"][0]["name"], "測試甲")
        self.assertEqual(self.snapshot()["orders"][0]["name"], "new-member")


class GitHubAdapterTests(IsolatedTest):
    def client(self, request):
        from cryptography.fernet import Fernet, InvalidToken
        test_key = Fernet.generate_key().decode("ascii")
        return load_app_functions(["github_get_backup", "github_put_backup", "encrypt_github_backup", "decrypt_github_backup"],
                                  Fernet=Fernet, InvalidToken=InvalidToken,
                                  get_github_settings=lambda: ("FAKE_TEST_TOKEN", "test-owner", "test-repo", "test-branch", "test-data.enc", test_key),
                                  github_request=request, taiwan_now_str=lambda: NOW)

    def http_error(self, status):
        return urllib.error.HTTPError("https://example.invalid/mock", status, "simulated", {}, None)

    def test_404_only_counts_as_missing_when_repository_and_ref_contents_are_accessible(self):
        calls = []
        def request(method, url, token, payload=None):
            calls.append(url)
            if "test-data.enc" in url:
                raise self.http_error(404)
            if "/contents?" in url:
                return []
            return {"name": "test-repo"}
        env = self.client(request)
        self.assertEqual(env["github_get_backup"](), (None, None))
        self.assertEqual(len(calls), 3)
        for code in (401, 403, 404):
            def inaccessible(method, url, token, payload=None):
                raise self.http_error(code)
            with self.subTest(code=code):
                with self.assertRaises(BackupError):
                    self.client(inaccessible)["github_get_backup"]()

    def test_contents_read_or_branch_failure_does_not_confirm_absence(self):
        def request(method, url, token, payload=None):
            if "/contents" in url:
                raise self.http_error(404)
            return {"name": "test-repo"}
        with self.assertRaises(BackupError):
            self.client(request)["github_get_backup"]()

    def test_get_timeout_and_empty_or_damaged_content_are_errors(self):
        def timeout(*args, **kwargs):
            raise TimeoutError("simulated")
        with self.assertRaises(TimeoutError):
            self.client(timeout)["github_get_backup"]()
        for response in ({"sha": "fake", "encoding": "base64", "content": ""},
                         {"sha": "fake", "encoding": "base64", "content": "!!!"},
                         {"sha": "fake", "encoding": "base64", "content": base64.b64encode(b"bad-fernet").decode()},
                         {"encoding": "base64", "content": "abc"}):
            with self.subTest(response_kind=response.get("content", "")[:3]):
                with self.assertRaises(ValueError):
                    self.client(lambda *args, **kwargs: response)["github_get_backup"]()

    def test_encrypted_backup_roundtrip_wrong_key_damage_and_envelope(self):
        env = self.client(lambda *args: None)
        encrypted = env["encrypt_github_backup"](fixture())
        result = env["decrypt_github_backup"](encrypted)
        self.assertEqual(content_digest(result), content_digest(fixture()))
        other = self.client(lambda *args: None)
        with self.assertRaises(ValueError):
            other["decrypt_github_backup"](encrypted)
        with self.assertRaises(ValueError):
            env["decrypt_github_backup"](encrypted[:-5] + "xxxxx")
        key = env["get_github_settings"]()[-1]
        from cryptography.fernet import Fernet
        for data in ([], {**fixture(), "backup_format": "wrong"}, {"backup_format": "office-order-encrypted-v1"}):
            token = Fernet(key.encode()).encrypt(json.dumps(data).encode()).decode()
            with self.assertRaises(ValueError):
                env["decrypt_github_backup"](token)

    def test_put_uses_supplied_sha_without_get_and_classifies_definite_rejections(self):
        calls = []
        def request(method, url, token, payload=None):
            calls.append((method, payload))
            return {"content": {"sha": "new-fake-sha"}}
        env = self.client(request)
        self.assertEqual(env["github_put_backup"](fixture(), "expected-fake-sha"), "new-fake-sha")
        self.assertEqual([row[0] for row in calls], ["PUT"])
        self.assertEqual(calls[0][1]["sha"], "expected-fake-sha")
        for status in (401, 403, 409, 422):
            def rejected(*args, **kwargs):
                raise self.http_error(status)
            with self.assertRaises(RemoteRejected):
                self.client(rejected)["github_put_backup"](fixture(), "fake")
        for status in (408, 500, 503):
            def ambiguous(*args, **kwargs):
                raise self.http_error(status)
            with self.assertRaises(urllib.error.HTTPError):
                self.client(ambiguous)["github_put_backup"](fixture(), "fake")


if __name__ == "__main__":
    unittest.main()


class PendingStateRegression(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.remote=self.ready()

    def old_pending(self):
        with self.guard.exclusive():
            state=self.guard.load_state()
            state['sync']={'kind':'pending','message':'old no-change commit'}
            self.guard.save_state(state)

    def test_repeated_default_checks_preserve_confirmed_success(self):
        initialize=load_app_functions(['ensure_single_select_defaults'],db_connect=self.guard.connect)['ensure_single_select_defaults']
        initialize()
        self.assertTrue(self.guard.transfer(True,self.remote.get,self.remote.put))
        before=self.guard.load_state(); calls=self.remote.get_calls
        for _ in range(3):
            initialize()
            self.guard.startup(True,self.remote.get)
            self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual(self.guard.load_state(),before)
        self.assertEqual(self.remote.get_calls,calls)

    def test_same_value_update_and_reverted_update_are_not_dirty(self):
        before=self.guard.load_state()
        for revert in (False,True):
            conn=self.guard.connect(write=True)
            try:
                if revert: conn.execute("UPDATE orders SET item_name='temporary' WHERE id=1")
                conn.execute('UPDATE orders SET item_name=? WHERE id=1',(fixture()['orders'][0]['item_name'],))
                conn.commit()
            finally: conn.close()
            self.assertEqual(self.guard.load_state(),before)

    def test_real_change_marks_pending_before_commit_and_sync_recovers(self):
        original=self.guard.save_state; observed=[]
        def save(state):
            if state['sync']['kind']=='pending':
                observed.append(self.snapshot()['orders'][0]['item_name'])
            original(state)
        with patch.object(self.guard,'save_state',save): self.write_item('changed')
        self.assertEqual(observed,[fixture()['orders'][0]['item_name']])
        self.assertEqual(self.guard.load_state()['sync']['kind'],'pending')
        self.assertTrue(self.guard.transfer(True,self.remote.get,self.remote.put))
        self.assertEqual(self.guard.status()['kind'],'success')
        conn=self.guard.connect(write=True)
        try: conn.commit()
        finally: conn.close()
        self.assertEqual(self.guard.status()['kind'],'success')

    def test_actual_sqlite_commit_failure_keeps_marker_and_rolls_back(self):
        before=self.snapshot(); conn=self.guard.connect(write=True)
        try:
            conn.execute("UPDATE orders SET item_name='uncommitted' WHERE id=1")
            conn.set_authorizer(lambda action,arg,*rest:sqlite3.SQLITE_DENY
                if action==sqlite3.SQLITE_TRANSACTION and arg=='COMMIT' else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError): conn.commit()
            self.assertEqual(self.guard.load_state()['sync']['kind'],'pending')
            conn.set_authorizer(None); conn.rollback()
        finally: conn.close()
        self.assertEqual(self.snapshot(),before)
        self.guard.startup(True,self.remote.get)
        self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual(self.remote.put_calls,0)

    def test_connection_context_cannot_bypass_dirty_marker(self):
        conn=self.guard.connect(write=True)
        try:
            with conn: conn.execute("UPDATE orders SET item_name='context changed' WHERE id=1")
        finally: conn.close()
        self.assertEqual(self.guard.load_state()['sync']['kind'],'pending')

    def test_old_pending_equal_remote_recovers_once_without_put(self):
        self.old_pending(); before=self.snapshot(); calls=self.remote.get_calls
        self.guard.startup(True,self.remote.get)
        self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual(self.snapshot(),before); self.assertEqual(self.remote.put_calls,0)
        self.assertEqual(self.remote.get_calls,calls+1)
        for _ in range(3): self.guard.startup(True,self.remote.get)
        self.assertEqual(self.remote.get_calls,calls+1)

    def test_changed_local_or_changed_remote_does_not_clear_warning(self):
        for local_change in (True,False):
            with self.subTest(local_change=local_change):
                if local_change: self.write_item('local only')
                else: self.remote.data=fixture('remote only')
                before=self.snapshot(); remote=copy.deepcopy(self.remote.data)
                self.guard.startup(True,self.remote.get)
                self.assertNotEqual(self.guard.status()['kind'],'success')
                self.assertEqual(self.snapshot(),before); self.assertEqual(self.remote.data,remote)
                self.assertEqual(self.remote.put_calls,0)

    def test_failed_read_checks_once_then_explicit_read_only_retry(self):
        self.old_pending(); self.remote.fail_get=True; calls=self.remote.get_calls
        for _ in range(3): self.guard.startup(True,self.remote.get)
        self.assertEqual(self.remote.get_calls,calls+1)
        self.assertNotEqual(self.guard.status()['kind'],'success')
        self.remote.fail_get=False
        self.assertTrue(self.guard.check_pending(True,self.remote.get,retry=True))
        self.assertEqual(self.guard.status()['kind'],'success'); self.assertEqual(self.remote.put_calls,0)

    def test_invalid_remote_or_missing_sha_cannot_confirm(self):
        for remote in (({},'fake'),(fixture(),None),(None,None)):
            self.old_pending(); before=self.snapshot()
            self.assertFalse(self.guard.check_pending(True,lambda:remote,retry=True))
            self.assertNotEqual(self.guard.status()['kind'],'success')
            self.assertEqual(self.snapshot(),before); self.assertEqual(self.remote.put_calls,0)

    def test_uncertain_put_is_not_recovered_as_plain_pending(self):
        self.write_item('target'); self.remote.mode='lost_response_get_fail'
        self.assertFalse(self.guard.transfer(True,self.remote.get,self.remote.put))
        state=self.guard.load_state(); before=self.snapshot(); calls=self.remote.get_calls
        self.assertFalse(self.guard.check_pending(True,self.remote.get,retry=True))
        self.assertEqual(self.guard.load_state(),state); self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.remote.get_calls,calls); self.assertEqual(self.remote.put_calls,1)

    def test_error_status_is_not_silently_reclassified(self):
        with self.guard.exclusive():
            state=self.guard.load_state(); state['sync']={'kind':'error','message':'fake error'}
            self.guard.save_state(state)
        self.assertFalse(self.guard.check_pending(True,self.remote.get,retry=True))
        self.assertEqual(self.guard.status()['kind'],'error')

    def test_new_guard_does_not_repeat_failed_check(self):
        self.old_pending(); self.remote.fail_get=True
        self.guard.startup(True,self.remote.get); calls=self.remote.get_calls
        second=BackupGuard(self.db,lambda:NOW)
        second.startup(True,self.remote.get)
        self.assertEqual(self.remote.get_calls,calls)


    def test_nonbusiness_sql_does_not_dirty_backup(self):
        before=self.guard.load_state()
        conn=self.guard.connect(write=True)
        try:
            conn.execute('CREATE TABLE test_only_metadata(value TEXT)')
            conn.execute("INSERT INTO test_only_metadata VALUES('fake')")
            conn.commit()
        finally: conn.close()
        self.assertEqual(self.guard.load_state(),before)

    def test_crash_after_marker_before_sqlite_commit_keeps_original_data(self):
        before=self.snapshot(); original=self.guard.save_state
        def crash(state):
            original(state)
            if state['sync']['kind']=='pending': raise RuntimeError('fake crash before commit')
        conn=self.guard.connect(write=True)
        try:
            conn.execute("UPDATE orders SET item_name='not committed' WHERE id=1")
            with patch.object(self.guard,'save_state',crash):
                with self.assertRaises(RuntimeError): conn.commit()
        finally: conn.close()
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.guard.load_state()['sync']['kind'],'pending')
        self.guard.startup(True,self.remote.get)
        self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual(self.remote.put_calls,0)


    def test_journal_write_failure_prevents_business_commit(self):
        before=self.snapshot(); state=self.guard.load_state()
        conn=self.guard.connect(write=True)
        try:
            conn.execute("UPDATE orders SET item_name='must rollback' WHERE id=1")
            with patch.object(self.guard,'save_state',side_effect=OSError('fake journal failure')):
                with self.assertRaises(OSError): conn.commit()
            conn.rollback()
        finally: conn.close()
        self.assertEqual(self.snapshot(),before); self.assertEqual(self.guard.load_state(),state)

    def test_context_exception_rolls_back_without_claiming_change(self):
        before=self.snapshot(); state=self.guard.load_state()
        conn=self.guard.connect(write=True)
        try:
            with self.assertRaises(RuntimeError):
                with conn:
                    conn.execute("UPDATE orders SET item_name='must rollback' WHERE id=1")
                    raise RuntimeError('fake body failure')
        finally: conn.close()
        self.assertEqual(self.snapshot(),before); self.assertEqual(self.guard.load_state(),state)
