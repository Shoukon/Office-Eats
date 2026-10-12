"""New member-bound creation cases only: fake DB/API and AST function harness."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backup_safety import BackupGuard, OrderConflict
from test_backup_safety import NOW, load_app_functions, fixture
import test_member_rename_acceptance as rename_fixture
from test_order_identity import FakeUI


class MemberOrderIdentity(unittest.TestCase):
    def setUp(self):
        self.seed = rename_fixture.MemberRenameAcceptance()
        self.seed.setUp()  # fixture setup, does NOT run existing seven tests
        self.addCleanup(self.seed.doCleanups)
        self.guard = self.seed.guard
        self.other = BackupGuard(self.guard.db_file, lambda: NOW)
        view = self.guard.member_snapshot()
        self.token = {'member_id': view['rows'][0][0], 'name': view['rows'][0][1], 'generation': view['generation']}
        self.values = ('主餐', 'Fake meal', 200, '小份, 微辣', 2, NOW, 100)
        self.before = self.guard.export()

    def rejected_without_change(self, token=None):
        before = self.guard.export()
        with self.assertRaises(OrderConflict): self.other.create_order(token, self.values)
        self.assertEqual(self.guard.export(), before)

    def test_normal_main_and_drink_creation_keep_payload(self):
        for category, custom in [('主餐','小份, 微辣'), ('飲料','L/無糖/去冰, 加珍珠')]:
            self.assertEqual(self.other.create_order(self.token,(category,'Fake item',200,custom,2,NOW,100)),1)
            row = self.guard.export()['orders'][-1]
            self.assertEqual((row['name'],row['category'],row['custom'],row['price'],row['quantity'],row['unit_price'],row['is_paid']),
                             ('測試甲',category,custom,200,2,100,0))

    def test_rename_rejects_old_token_fresh_selection_succeeds(self):
        self.assertEqual(self.seed.env['rename_member'](1,'Fake renamed'),(True,''))
        self.rejected_without_change(self.token)
        view=self.other.member_snapshot()
        fresh={'member_id':view['rows'][0][0],'name':view['rows'][0][1],'generation':view['generation']}
        self.assertEqual(self.guard.create_order(fresh,self.values),1)
        self.assertEqual(self.guard.export()['orders'][-1]['name'],'Fake renamed')

    def test_delete_and_recreate_same_name_rejects_old_id(self):
        with self.guard.connect(write=True) as conn:
            conn.execute('DELETE FROM order_members WHERE id=1')
            conn.execute("INSERT INTO order_members(name,sort_order) VALUES('測試甲',0)")
            conn.commit()
        conn.close()
        self.assertNotEqual(self.other.member_snapshot()['rows'][0][0],1)
        self.rejected_without_change(self.token)

    def test_missing_or_malformed_identity_cannot_backfill_by_name(self):
        for token in (None,{}, {'name':'測試甲'},dict(self.token,member_id=True),dict(self.token,member_id=999),dict(self.token,name='測試乙')):
            self.rejected_without_change(token)

    def test_replacement_epoch_rejects_identical_id_and_name(self):
        remote = rename_fixture.FakeRemote(self.before)
        # This fixture was initially restored from the same initial SHA.
        self.assertTrue(self.guard.transfer(True,remote.get,remote.put,imported=self.before))
        self.rejected_without_change(self.token)
        self.assertNotEqual(self.guard.member_snapshot()['generation'],self.token['generation'])

    def test_invalid_generation_rejects_with_no_partial_insert(self):
        self.rejected_without_change(dict(self.token,generation='0'*32))

    def test_sql_failure_and_commit_failure_do_not_leave_order(self):
        with self.guard.exclusive() as conn:
            conn.execute("CREATE TRIGGER fake_no_create BEFORE INSERT ON orders BEGIN SELECT RAISE(ABORT,'fake creation failure'); END")
            conn.commit()
        env=load_app_functions(['create_member_order'],backup_guard=self.guard,st=FakeUI(),OrderConflict=OrderConflict,time=SimpleNamespace(sleep=lambda _:None))
        self.assertIsNone(env['create_member_order'](self.token,self.values))
        self.assertEqual(self.guard.export(),self.before)

    def test_commit_journal_failure_rolls_back_insert(self):
        with patch.object(self.guard,'save_state',side_effect=RuntimeError('fake journal failure')):
            with self.assertRaises(RuntimeError): self.guard.create_order(self.token,self.values)
        self.assertEqual(self.guard.export(),self.before)

    def test_both_gui_routes_call_guarded_creation_and_keep_success_conditional(self):
        tree=ast.parse((Path(__file__).parents[1]/'order.py').read_text(encoding='utf-8-sig'))
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='create_member_order']
        self.assertEqual(len(calls),2)
        self.assertEqual({n.args[1].elts[0].value for n in calls},{'主餐','飲料'})
        for call in calls:
            self.assertEqual(call.args[0].args[0].value,'member_token')
            self.assertTrue(any(isinstance(n,ast.If) and n.test is call for n in ast.walk(tree)))
        self.assertFalse(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='execute_db'
            and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str)
            and n.args[0].value.startswith('INSERT INTO orders ') for n in ast.walk(tree)))

    def test_login_persists_id_name_generation_from_member_snapshot(self):
        class RerunSignal(Exception): pass
        ui=FakeUI();ui.pills=lambda *a,**kw:'測試甲'
        def rerun(): raise RerunSignal()
        ui.rerun=rerun
        env=load_app_functions(['login_dialog'],st=ui,backup_guard=self.guard)
        with self.assertRaises(RerunSignal): env['login_dialog']()
        self.assertEqual(ui.session_state['member_token'],self.token)
        self.assertEqual(ui.session_state['user_name'],'測試甲')

    def test_login_picker_key_changes_for_same_name_new_identity(self):
        keys=[]
        ui=FakeUI()
        def pills(*a,**kw): keys.append(kw['key']); return None
        ui.pills=pills
        env=load_app_functions(['login_dialog'],st=ui,backup_guard=self.guard)
        env['login_dialog']()
        with self.guard.connect(write=True) as conn:
            conn.execute('DELETE FROM order_members WHERE id=1')
            conn.execute("INSERT INTO order_members(name,sort_order) VALUES('測試甲',0)")
            conn.commit()
        conn.close()
        env['login_dialog']()
        self.assertNotEqual(keys[0],keys[1])
        self.assertNotIn('member_token',ui.session_state)


if __name__=='__main__': unittest.main()
