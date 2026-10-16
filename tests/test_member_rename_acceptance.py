"""Acceptance assertions on fresh fake SQLite for atomic member rename.

AST extraction only; no complete application, credentials or real network.
"""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
from backup_safety import BackupGuard
from test_backup_safety import fixture, schema, load_app_functions, FakeRemote, NOW
from test_order_identity import FakeUI


class MemberRenameAcceptance(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.test-tmp' / 'member-rename-acceptance'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='case-', dir=parent))
        self.guard = BackupGuard(self.root / 'fake.db', lambda: NOW)
        self.guard.prepare(schema)
        data = fixture()
        data['members'].append({'id': 2, 'name': '測試乙', 'sort_order': 1})
        other = copy.deepcopy(data['orders'][0]); other.update(id=2, name='測試乙')
        data['orders'].append(other)
        remote = FakeRemote(data)
        self.assertEqual(self.guard.startup(True, remote.get), 'restored')
        self.before = self.guard.export()
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('Real network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.ui = FakeUI()
        self.connect_calls = 0
        self.sql_trace = []
        self.fail_second = False
        class ConnectionProxy:
            def __init__(inner, conn): inner.conn = conn
            def execute(inner, query, params=()):
                if self.fail_second and query.startswith('UPDATE orders'):
                    raise sqlite3.OperationalError('database is locked')
                return inner.conn.execute(query, params)
            def __getattr__(inner, name): return getattr(inner.conn, name)
        def connect(write=False):
            if write:
                self.connect_calls += 1
            conn = self.guard.connect(write=write)
            if write: conn.set_trace_callback(self.sql_trace.append)
            return ConnectionProxy(conn) if write else conn
        def get_db(query, params=()):
            conn = self.guard.connect()
            try: return pd.read_sql_query(query, conn, params=params)
            finally: conn.close()
        self.env = load_app_functions(['execute_db', 'rename_member'],
            st=self.ui, db_connect=connect, get_db=get_db,
            time=SimpleNamespace(sleep=lambda _: None))

    def evidence(self, result):
        snapshot = self.guard.export()
        print('FAKE DATA EVIDENCE:', json.dumps(dict(test=self._testMethodName, result=result,
              members=[(r['id'],r['name']) for r in snapshot['members']],
              orders=[(r['id'],r['name']) for r in snapshot['orders']],
              errors=self.ui.errors, sql_trace=self.sql_trace), ensure_ascii=True), flush=True)
        return snapshot

    def test_normal_rename_updates_both_and_leaves_other_member_unchanged(self):
        result = self.env['rename_member'](1, '測試改名')
        snapshot = self.evidence(result)
        self.assertEqual(result, (True, ''))
        self.assertEqual(snapshot['members'][0]['name'], '測試改名')
        self.assertEqual(snapshot['orders'][0]['name'], '測試改名')
        self.assertEqual(snapshot['members'][1]['name'], '測試乙')
        self.assertEqual(snapshot['orders'][1]['name'], '測試乙')

    def test_second_sql_exception_must_rollback_entire_rename(self):
        with self.guard.exclusive() as conn:
            conn.execute("CREATE TRIGGER injected_failure BEFORE UPDATE OF name ON orders BEGIN SELECT RAISE(ABORT, 'fake second SQL failure'); END")
            conn.commit()
        result = self.env['rename_member'](1, '測試改名')
        self.assertFalse(result[0]); self.assertTrue(result[1])
        snapshot = self.evidence(result)
        self.assertEqual(snapshot, self.before, 'Both tables must remain unchanged after second SQL failure')

    def test_second_update_exhausted_lock_retries_must_not_report_success(self):
        self.fail_second = True
        result = self.env['rename_member'](1, '測試改名')
        snapshot = self.evidence(result)
        self.assertTrue(result[1])
        self.assertEqual(self.connect_calls, 5)
        self.assertEqual((result[0], snapshot), (False, self.before),
                         'Second update failure must neither report success nor leave partial commit')

    def test_duplicate_name_rejected_without_writes(self):
        result = self.env['rename_member'](1, '測試乙')
        self.assertFalse(result[0])
        self.assertEqual(self.guard.export(), self.before)
        self.assertEqual(self.connect_calls, 1)  # validation now within write lock

    def test_member_without_orders_can_rename(self):
        with self.guard.connect(write=True) as conn:
            conn.execute("INSERT INTO order_members(name,sort_order) VALUES('測試無單',2)")
            conn.commit()
        conn.close()
        self.assertEqual(self.env['rename_member'](3, '測試無單改名'), (True, ''))
        snapshot = self.guard.export()
        self.assertEqual(snapshot['members'][2]['name'], '測試無單改名')
        self.assertEqual(snapshot['orders'], self.before['orders'])

    def test_commit_journal_failure_rolls_back_both_tables(self):
        with patch.object(self.guard, 'save_state', side_effect=RuntimeError('fake commit journal failure')):
            result = self.env['rename_member'](1, '測試改名')
        self.assertFalse(result[0]); self.assertTrue(result[1])
        self.assertEqual(self.guard.export(), self.before)
        self.assertIn('ROLLBACK', self.sql_trace)

    def test_ui_sync_and_success_only_after_transaction_success(self):
        class RerunSignal(Exception): pass
        for fail in (True, False):
            self.ui.errors.clear(); self.ui.toasts.clear()
            self.fail_second = fail
            synced = []
            self.ui.button = lambda label, **kw: kw.get('key') == 'rename_save_1'
            self.ui.text_values['姓名'] = '測試改名'
            def rerun(): raise RerunSignal()
            self.ui.rerun = rerun
            ui_env = load_app_functions(['manage_members_dialog','show_save_feedback'], **self.env,
                get_members=lambda: pd.DataFrame(self.guard.export()['members']),
                save_and_sync=lambda: synced.append(True) or True)
            if fail:
                ui_env['manage_members_dialog']()
                self.assertEqual(synced, []); self.assertEqual(self.ui.toasts, [])
                self.assertTrue(self.ui.errors)
                self.assertEqual(self.guard.export(), self.before)
            else:
                with self.assertRaises(RerunSignal): ui_env['manage_members_dialog']()
                self.assertEqual(synced, [True])
                self.assertEqual(self.ui.toasts, [])
                self.assertEqual(self.guard.export()['orders'][0]['name'], '測試改名')


class MemberManagementBatchAcceptance(unittest.TestCase):
    """Only this batch: reuse existing fake DB fixture, not its rename tests."""
    def setUp(self):
        self.seed=MemberRenameAcceptance()
        self.seed.setUp()
        self.addCleanup(self.seed.doCleanups)
        self.guard=self.seed.guard
        self.before=self.guard.export()
        self.ui=self.seed.ui
        self.env=load_app_functions(
            ['member_transaction','add_member','delete_member','move_member','get_db','get_orders_df','render_stats_section'],
            st=self.ui, db_connect=self.guard.connect, pd=pd,
            ORDER_COLUMNS=list(self.before['orders'][0]),
            time=SimpleNamespace(sleep=lambda _:None),
            taiwan_now_str=lambda:NOW,
            get_current_shop_names=lambda:{'main':'Fake','drink':''})

    def inject(self, sql):
        conn=self.guard.connect(write=True)
        try: conn.execute(sql); conn.commit()
        finally: conn.close()

    def test_add_and_delete_normal_preserve_orders(self):
        self.assertEqual(self.env['add_member']('Fake-new'),(True,''))
        member=self.guard.export()['members'][-1]
        self.assertEqual(self.env['delete_member'](member['id']),(True,'Fake-new'))
        self.assertEqual(self.guard.export(),self.before)

    def test_duplicate_and_missing_member_rejected(self):
        self.assertFalse(self.env['add_member']('測試甲')[0])
        self.assertFalse(self.env['delete_member'](999)[0])
        self.assertEqual(self.guard.export(),self.before)

    def test_insert_sql_failure_preserves_all_data(self):
        self.inject("CREATE TRIGGER fake_insert BEFORE INSERT ON order_members BEGIN SELECT RAISE(ABORT,'fake insert'); END")
        self.assertFalse(self.env['add_member']('Fake-new')[0])
        self.assertEqual(self.guard.export(),self.before)

    def test_delete_sql_failure_preserves_all_data(self):
        self.inject("CREATE TRIGGER fake_delete BEFORE DELETE ON order_members BEGIN SELECT RAISE(ABORT,'fake delete'); END")
        self.assertFalse(self.env['delete_member'](1)[0])
        self.assertEqual(self.guard.export(),self.before)

    def test_lock_exhaustion_never_reports_success(self):
        with patch.dict(self.env,db_connect=lambda **kw: (_ for _ in ()).throw(sqlite3.OperationalError('database is locked'))):
            for fn,args in [('add_member',('Fake-new',)),('delete_member',(1,)),('move_member',(1,1))]:
                result=self.env[fn](*args)
                self.assertFalse(result[0]); self.assertTrue(result[1])
        self.assertEqual(self.guard.export(),self.before)

    def test_swap_success_and_boundary_noop(self):
        self.assertEqual(self.env['move_member'](1,-1),(True,''))
        self.assertEqual(self.guard.export(),self.before)
        self.assertEqual(self.env['move_member'](1,1),(True,''))
        after=self.guard.export()
        self.assertEqual([(m['id'],m['sort_order']) for m in after['members']],[(2,0),(1,1)])
        self.assertEqual(after['orders'],self.before['orders'])

    def test_swap_second_sql_failure_rolls_back_first(self):
        self.inject("CREATE TRIGGER fake_swap BEFORE UPDATE OF sort_order ON order_members WHEN OLD.id=2 BEGIN SELECT RAISE(ABORT,'fake second swap'); END")
        self.assertFalse(self.env['move_member'](1,1)[0])
        self.assertEqual(self.guard.export(),self.before)

    def test_commit_failure_rolls_back_member_operations(self):
        with patch.object(self.guard,'save_state',side_effect=RuntimeError('fake journal failure')):
            for fn,args in [('add_member',('Fake-new',)),('delete_member',(1,)),('move_member',(1,1))]:
                self.assertFalse(self.env[fn](*args)[0])
        self.assertEqual(self.guard.export(),self.before)

    def test_failed_read_not_rendered_as_empty_statistics(self):
        info=[]; self.ui.info=info.append
        with patch.dict(self.env,db_connect=lambda **kw: (_ for _ in ()).throw(sqlite3.OperationalError('fake read failure'))):
            self.assertTrue(self.env['get_orders_df']().attrs['db_error'])
            self.env['render_stats_section']()
        self.assertTrue(self.ui.errors)
        self.assertEqual(info,[])
        self.assertEqual(self.guard.export(),self.before)

    def test_genuinely_empty_statistics_remain_valid(self):
        conn=self.guard.connect(write=True)
        try: conn.execute('DELETE FROM orders'); conn.commit()
        finally: conn.close()
        info=[]; self.ui.info=info.append
        self.assertFalse(self.env['get_orders_df']().attrs.get('db_error',False))
        self.env['render_stats_section']()
        self.assertEqual(len(info),1); self.assertEqual(self.ui.errors,[])

    def test_payment_render_precedes_name_stop_and_identity_guard_remains(self):
        import ast
        from test_backup_safety import SOURCE
        tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call)]
        payment=[n.lineno for n in calls if isinstance(n.func,ast.Name) and n.func.id=='render_payment_section']
        stops=[n.lineno for n in calls if isinstance(n.func,ast.Attribute) and n.func.attr=='stop']
        name_stop=max(stops)
        self.assertEqual(len(payment),1); self.assertLess(payment[0],name_stop)
        creations=[n for n in calls if isinstance(n.func,ast.Name) and n.func.id=='create_member_order']
        self.assertEqual(len(creations),2)
        for call in creations: self.assertIn('member_token',ast.unparse(call.args[0]))

    def test_member_ui_failure_does_not_sync_or_show_success(self):
        for key in ('up_2','down_1','confirm_delete_1',None):
            with self.subTest(key=key):
                self.ui.errors.clear(); self.ui.toasts.clear()
                self.ui.session_state['confirm_delete_member_1']=True
                self.ui.button=lambda label,**kw: (kw.get('key')==key if key is not None else label=='➕ 新增人員')
                self.ui.text_values['新增人員']='Fake-new'
                synced=[]
                env=load_app_functions(['manage_members_dialog'],st=self.ui,
                    get_members=lambda:pd.DataFrame(self.before['members']),
                    move_member=lambda *args:(False,'fake failure'),
                    delete_member=lambda *args:(False,'fake failure'),
                    add_member=lambda *args:(False,'fake failure'),
                    rename_member=lambda *args:(False,'fake failure'),
                    save_and_sync=lambda:synced.append(True))
                env['manage_members_dialog']()
                self.assertTrue(self.ui.errors)
                self.assertEqual(synced,[]); self.assertEqual(self.ui.toasts,[])

    def test_payment_scope_captions_removed_but_buttons_remain_without_selected_user(self):
        from test_order_identity import OrderUIIntegrationTests
        synced=[]; captions=[]; self.ui.caption=captions.append
        harness=OrderUIIntegrationTests(); harness.guard=self.guard
        env=harness.env(self.ui,synced)
        view=self.guard.order_snapshot()
        frame=pd.DataFrame(view['rows'],columns=env['ORDER_COLUMNS'])
        env['_pay_logic_grouped']('主餐',frame,'main',view)
        self.assertFalse(any('確認此人全部未付' in text for text in captions))
        self.assertTrue(any(label=='確認收款' for label,_ in self.ui.buttons))
        frame['is_paid']=1
        env['_pay_logic_grouped']('主餐',frame,'main',view)
        self.assertFalse(any('撤銷此人全部已付' in text for text in captions))
        self.assertTrue(any(label=='撤銷收款' for label,_ in self.ui.buttons))
        self.assertNotIn('user_name',self.ui.session_state)
        self.assertEqual(synced,[])


if __name__ == '__main__':
    unittest.main()


class OptionInitializationAcceptance(unittest.TestCase):
    setUp = MemberRenameAcceptance.setUp
    # Reuse fake SQLite setup, but do not inherit unrelated test methods.
    def test_option_read_failure_keeps_all_data(self):
        self.check_read_failure('SELECT id,option_value')

    def test_default_read_failure_keeps_all_data(self):
        self.check_read_failure('SELECT 1 FROM order_options')

    def check_read_failure(self, prefix):
        before=self.guard.export()
        connect=self.guard.connect
        class FailingRead:
            def __init__(self, conn): self.conn=conn
            def execute(self, query, params=()):
                if query.startswith(prefix): raise sqlite3.OperationalError('fake read failure')
                return self.conn.execute(query,params)
            def __getattr__(self,key): return getattr(self.conn,key)
        env=load_app_functions(['ensure_single_select_defaults'],
            db_connect=lambda write=False:FailingRead(connect(write=write)))
        with self.assertRaises(sqlite3.OperationalError): env['ensure_single_select_defaults']()
        self.assertEqual(self.guard.export(),before)

    def test_empty_categories_initialize_and_existing_options_survive(self):
        env=load_app_functions(['ensure_single_select_defaults'],db_connect=self.guard.connect)
        env['ensure_single_select_defaults']()
        after=self.guard.export()
        self.assertEqual(after['options']['main_size'],self.before['options']['main_size'])
        self.assertTrue(after['options']['drink_size'])
        env['ensure_single_select_defaults']()
        self.assertEqual(self.guard.export(),after)

    def test_failed_dataframe_reads_raise_instead_of_becoming_empty(self):
        failed=pd.DataFrame(); failed.attrs['db_error']=True
        env=load_app_functions(['get_options','get_default_option'],get_db=lambda *a:failed)
        for name in ('get_options','get_default_option'):
            with self.assertRaises(sqlite3.OperationalError): env[name]('main_size')
