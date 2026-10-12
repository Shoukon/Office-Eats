"""New concurrency cases only. Independent guarded connections to fake SQLite.

No full application, real credentials, real network or existing suite reruns.
"""
import ast
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import unittest

from backup_safety import BackupGuard, OrderConflict
from test_backup_safety import load_app_functions, NOW
import test_member_rename_acceptance as rename_fixture
from test_order_identity import FakeUI


class MemberRenameConcurrency(unittest.TestCase):
    def setUp(self):
        self.seed = rename_fixture.MemberRenameAcceptance()
        self.seed.setUp()  # fixture creation only; never runs its seven tests
        self.addCleanup(self.seed.doCleanups)
        self.a = self.seed.guard
        self.b = BackupGuard(self.a.db_file, lambda: NOW, timeout=1)
        self.entered = threading.Event(); self.release = threading.Event()
        self.b_attempted = threading.Event()
        self.results = {}; self.errors = {}; self.threads = []

    def read(self):
        conn = self.b.connect()
        try:
            return {'members': list(conn.execute('SELECT id,name FROM order_members ORDER BY id')),
                    'orders': list(conn.execute('SELECT id,name,item_name FROM orders ORDER BY id'))}
        finally: conn.close()

    def client(self, guard, paused=False, label='A'):
        owner = self
        class Proxy:
            def __init__(self, conn): self.conn = conn
            def execute(self, sql, params=()):
                result = self.conn.execute(sql, params)
                if paused and sql.startswith('UPDATE order_members'):
                    owner.entered.set()
                    if not owner.release.wait(5): raise RuntimeError('test release timeout')
                return result
            def __getattr__(self, key): return getattr(self.conn, key)
        def connect(write=False):
            if label == 'B': self.b_attempted.set()
            conn = guard.connect(write=write)
            return Proxy(conn) if paused else conn
        return load_app_functions(['rename_member', 'execute_db', 'create_member_order'], db_connect=connect, backup_guard=guard,
            st=FakeUI(), OrderConflict=OrderConflict, time=SimpleNamespace(sleep=lambda _: None))

    def start(self, label, action):
        def worker():
            try: self.results[label] = action()
            except Exception as exc: self.errors[label] = type(exc).__name__
        thread = threading.Thread(target=worker)
        self.threads.append(thread); thread.start()

    def finish(self):
        self.release.set()
        for thread in self.threads: thread.join(4)
        self.assertFalse(any(t.is_alive() for t in self.threads))
        print('FAKE CONCURRENCY EVIDENCE:', json.dumps(dict(test=self._testMethodName,
              results=self.results, errors=self.errors, snapshot=self.read()), ensure_ascii=True), flush=True)

    def check_names(self, name):
        snapshot = self.read()
        self.assertEqual(snapshot['members'][0][1], name)
        self.assertEqual(snapshot['orders'][0][1], name)
        self.assertEqual(snapshot['members'][1][1], '測試乙')
        self.assertEqual(snapshot['orders'][1][1], '測試乙')

    def test_01_two_renames_serialize_without_partial_commit(self):
        ca, cb = self.client(self.a, True), self.client(self.b, label='B')
        before = self.read()
        self.start('A', lambda: ca['rename_member'](1, 'Fake-A'))
        try:
            self.assertTrue(self.entered.wait(2))
            self.start('B', lambda: cb['rename_member'](1, 'Fake-B'))
            self.assertTrue(self.b_attempted.wait(2))
            self.assertEqual(self.read(), before)  # uncommitted A invisible
        finally: self.finish()
        self.assertEqual(self.errors, {})
        self.assertEqual(self.results, {'A': (True, ''), 'B': (True, '')})
        self.check_names('Fake-B')

    def test_02_stale_order_edit_rejected_after_rename(self):
        token = self.b.order_token(self.b.order_snapshot(), [1])
        ca = self.client(self.a, True)
        self.start('A', lambda: ca['rename_member'](1, 'Fake-A'))
        def edit():
            self.b_attempted.set()
            return self.b.apply_order_action('edit', token, dict(item_name='Fake-edit', price=100,
                    quantity=1, unit_price=100, custom=''))
        try:
            self.assertTrue(self.entered.wait(2)); self.start('B', edit)
            self.assertTrue(self.b_attempted.wait(2))
        finally: self.finish()
        self.assertEqual(self.errors, {'B': 'OrderConflict'})
        self.assertEqual(self.results['A'], (True, ''))
        self.check_names('Fake-A')
        self.assertEqual(self.read()['orders'][0][2], '測試便當')

    def test_03_contender_lock_exhaustion_reports_failure(self):
        self.b.timeout = 0.03
        ca, cb = self.client(self.a, True), self.client(self.b, label='B')
        before = self.read()
        self.start('A', lambda: ca['rename_member'](1, 'Fake-A'))
        try:
            self.assertTrue(self.entered.wait(2))
            result = cb['rename_member'](1, 'Fake-B'); self.results['B'] = result
            self.assertFalse(result[0]); self.assertTrue(result[1])
            self.assertEqual(self.read(), before)
        finally: self.finish()
        self.assertEqual(self.errors, {})
        self.check_names('Fake-A')

    def test_04_failed_rename_rolls_back_before_next_rename(self):
        with self.a.exclusive() as conn:
            conn.execute("CREATE TRIGGER fake_fail_a BEFORE UPDATE OF name ON orders WHEN NEW.name='Fake-A' BEGIN SELECT RAISE(ABORT,'fake failure A'); END")
            conn.commit()
        ca, cb = self.client(self.a, True), self.client(self.b, label='B')
        self.start('A', lambda: ca['rename_member'](1, 'Fake-A'))
        try:
            self.assertTrue(self.entered.wait(2)); self.start('B', lambda: cb['rename_member'](1, 'Fake-B'))
            self.assertTrue(self.b_attempted.wait(2))
        finally: self.finish()
        self.assertEqual(self.errors, {})
        self.assertFalse(self.results['A'][0]); self.assertTrue(self.results['A'][1])
        self.assertEqual(self.results['B'], (True, '')); self.check_names('Fake-B')

    def test_05_queued_order_creation_must_not_restore_old_session_name(self):
        view = self.b.member_snapshot()
        token = {'member_id': view['rows'][0][0], 'name': view['rows'][0][1], 'generation': view['generation']}
        ca, cb = self.client(self.a, True), self.client(self.b, label='B')
        self.start('A', lambda: ca['rename_member'](1, 'Fake-A'))
        try:
            self.assertTrue(self.entered.wait(2))
            def create():
                self.b_attempted.set()
                return cb['create_member_order'](token, ('主餐','Fake-new',100,'',1,NOW,100))
            self.start('B', create)
            self.assertTrue(self.b_attempted.wait(2))
        finally: self.finish()
        self.assertEqual(self.errors, {})
        self.assertEqual(self.results['A'], (True, ''))
        self.assertIsNone(self.results['B'])
        snapshot = self.read()
        self.assertTrue(all(r[1] == 'Fake-A' for r in snapshot['orders'] if r[0] != 2),
                        'Queued order must not commit under obsolete session name')


if __name__ == '__main__':
    unittest.main()
