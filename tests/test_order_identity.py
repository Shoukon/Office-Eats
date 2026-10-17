"""Regression tests for stale IDs after replacement; no real data/network."""
import ast
import copy
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from backup_safety import (BackupGuard, BackupError, OrderConflict, SafetyBlocked,
                           ORDER_FIELDS, content_digest)
from test_backup_safety import IsolatedTest, FakeRemote, fixture, schema, NOW, SOURCE, load_app_functions


def edit_changes(item="edited", unit=120, qty=2, custom="測試客製"):
    return {"item_name": item, "price": unit * qty, "quantity": qty,
            "unit_price": unit, "custom": custom}


class OrderIdentityTests(IsolatedTest):
    def token(self, ids=(1,)):
        return self.guard.order_token(self.guard.order_snapshot(), ids)

    def test_reused_id_different_owner_item_blocks_edit_delete_and_payment(self):
        remote = self.ready()
        old = self.token()
        imported = fixture("new-owner-item", 150)
        imported["members"][0]["name"] = "測試新使用者"
        imported["orders"][0]["name"] = "測試新使用者"
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=imported))
        before = self.snapshot(); remote_before = copy.deepcopy(remote.data)
        for action in ("edit", "delete", "payment"):
            with self.subTest(action=action):
                with self.assertRaises(OrderConflict):
                    self.guard.apply_order_action(action, old, edit_changes() if action == "edit" else None)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(remote.data, remote_before)
        with self.assertRaises(OrderConflict):
            self.guard.expected_order(old)

    def test_identical_reimport_changes_generation_and_blocks_all_old_actions(self):
        remote = self.ready()
        old = self.token(); before_digest = content_digest(self.snapshot())
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=self.snapshot()))
        self.assertEqual(content_digest(self.snapshot()), before_digest)
        self.assertNotEqual(self.token()["generation"], old["generation"])
        for action in ("edit", "delete", "payment"):
            with self.subTest(action=action):
                with self.assertRaises(OrderConflict):
                    self.guard.apply_order_action(action, old, edit_changes() if action == "edit" else None)
                self.assertEqual(content_digest(self.snapshot()), before_digest)

    def test_old_paid_token_cannot_undo_reimported_paid_order(self):
        data = fixture(); data["orders"][0]["is_paid"] = 1
        remote = self.ready(data); old = self.token()
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=self.snapshot()))
        before = self.snapshot()
        with self.assertRaises(OrderConflict):
            self.guard.apply_order_action("undo_payment", old)
        self.assertEqual(self.snapshot(), before)

    def test_generation_is_shared_across_sessions_and_survives_restart(self):
        remote = self.ready(); old = self.token()
        second = BackupGuard(self.db, lambda: NOW)
        self.assertEqual(second.order_snapshot()["generation"], old["generation"])
        self.assertTrue(second.transfer(True, remote.get, remote.put, imported=fixture("replaced")))
        restarted = BackupGuard(self.db, lambda: NOW)
        restarted.prepare(schema)
        self.assertEqual(restarted.startup(True, remote.get), "ready")
        with self.assertRaises(OrderConflict):
            restarted.apply_order_action("delete", old)
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "replaced")

    def test_old_journal_is_upgraded_without_schema_or_export_format_change(self):
        self.ready()
        before = self.snapshot()
        conn = sqlite3.connect(self.db)
        schema_before = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        conn.close()
        with self.guard.exclusive():
            state = self.guard.load_state(); state.pop("order_generation")
            self.guard.save_state(state)
        self.guard.prepare(schema)
        self.assertEqual(len(self.guard.load_state()["order_generation"]), 32)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(), schema_before)
        conn.close()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(self.snapshot()), {"format", "version", "exported_at", "members", "options", "config_shop", "orders"})

    def test_recreated_database_with_surviving_journal_gets_new_generation(self):
        self.ready(); old = self.token(); data = self.snapshot()
        fresh = BackupGuard(self.db.parent / "recreated-fake.db", lambda: NOW)
        with fresh.exclusive():
            fresh.save_state(self.guard.load_state())  # metadata only, fake journal
        fresh.prepare(schema)
        self.assertEqual(fresh.startup(False, lambda: self.fail("no network")), "local_only")
        conn = fresh.connect(write=True)
        try:
            from backup_safety import replace_snapshot, validate_backup
            replace_snapshot(conn, validate_backup(data))
            conn.commit()
        finally:
            conn.close()
        self.assertNotEqual(fresh.order_snapshot()["generation"], old["generation"])
        before = fresh.export()
        with self.assertRaises(OrderConflict):
            fresh.apply_order_action("edit", old, edit_changes())
        self.assertEqual(fresh.export(), before)

    def test_malformed_generation_or_token_fails_closed(self):
        self.ready(); before = self.snapshot()
        for token in (None, {}, {"generation": "wrong", "rows": ()},
                      {"generation": self.token()["generation"], "rows": ()}):
            with self.subTest(token_kind=type(token).__name__):
                with self.assertRaises(OrderConflict):
                    self.guard.apply_order_action("delete", token)
                self.assertEqual(self.snapshot(), before)
        with self.guard.exclusive():
            state = self.guard.load_state(); state["order_generation"] = None
            self.guard.save_state(state)
        with self.assertRaises(SafetyBlocked):
            self.guard.connect(write=True)

    def test_normal_edit_pay_undo_and_delete_with_fresh_tokens(self):
        self.ready(); generation = self.token()["generation"]
        self.assertEqual(self.guard.apply_order_action("edit", self.token(), edit_changes()), 1)
        row = self.snapshot()["orders"][0]
        self.assertEqual((row["item_name"], row["price"], row["quantity"], row["unit_price"], row["custom"]),
                         ("edited", 240, 2, 120, "測試客製"))
        self.assertEqual(self.token()["generation"], generation)  # normal writes don't invalidate unrelated views
        self.assertEqual(self.guard.apply_order_action("payment", self.token()), 1)
        paid = self.token(); before = self.snapshot()
        for action in ("edit", "delete"):
            with self.assertRaises(OrderConflict):
                self.guard.apply_order_action(action, paid, edit_changes() if action == "edit" else None)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.guard.apply_order_action("undo_payment", paid), 1)
        self.assertEqual(self.guard.apply_order_action("delete", self.token()), 1)
        self.assertEqual(self.snapshot()["orders"], [])

    def test_historical_order_missing_member_and_unknown_legacy_unit_price_still_editable(self):
        data = fixture(); data["members"] = []; data["orders"][0]["unit_price"] = None
        self.ready(data); token = self.token()
        self.assertIsNone(token["rows"][0][9])
        self.assertEqual(self.guard.apply_order_action("edit", token, edit_changes(unit=0, qty=1)), 1)
        self.assertEqual(self.snapshot()["orders"][0]["price"], 0)

    def test_fresh_view_after_import_is_not_rejected(self):
        remote = self.ready()
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-order")))
        self.assertEqual(self.guard.apply_order_action("edit", self.token(), edit_changes()), 1)
        self.assertEqual(self.guard.apply_order_action("payment", self.token()), 1)

    def test_changed_identity_content_amount_or_paid_state_rejects_whole_batch(self):
        for field, value in (("name", "other-person"), ("category", "飲料"), ("item_name", "other-item"),
                             ("price", 150), ("custom", "other-custom"), ("quantity", 3),
                             ("order_time", "2026-10-12 13:00:00"), ("is_paid", 1), ("unit_price", 150)):
            with self.subTest(field=field):
                data = fixture(); second = copy.deepcopy(data["orders"][0]); second["id"] = 2
                data["orders"].append(second)
                self.ready(data); token = self.token((1, 2))
                conn = self.guard.connect(write=True)
                try:
                    # The field comes from this fixed test list, never user input.
                    conn.execute(f"UPDATE orders SET {field}=? WHERE id=2", (value,)); conn.commit()
                finally:
                    conn.close()
                before = self.snapshot()
                with self.assertRaises(OrderConflict):
                    self.guard.apply_order_action("payment", token)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.snapshot()["orders"][0]["is_paid"], 0)

    def test_missing_order_rejects_payment_without_partially_marking_others(self):
        data = fixture(); second = copy.deepcopy(data["orders"][0]); second["id"] = 2
        data["orders"].append(second)
        self.ready(data); token = self.token((1, 2))
        conn = self.guard.connect(write=True)
        conn.execute("DELETE FROM orders WHERE id=2"); conn.commit(); conn.close()
        before = self.snapshot()
        with self.assertRaises(OrderConflict):
            self.guard.apply_order_action("payment", token)
        self.assertEqual(self.snapshot(), before)

    def test_normal_batch_and_unrelated_new_orders_do_not_invalidate_selected_rows(self):
        data = fixture(); second = copy.deepcopy(data["orders"][0]); second["id"] = 2
        data["orders"].append(second)
        self.ready(data); token = self.token((1, 2))
        conn = self.guard.connect(write=True)
        conn.execute("INSERT INTO orders(name,category,item_name,price,custom,quantity,order_time,is_paid,unit_price) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("測試甲", "主餐", "new-unshown", 30, "", 1, NOW, 0, 30))
        conn.commit(); conn.close()
        self.assertEqual(self.guard.apply_order_action("payment", token), 2)
        rows = self.snapshot()["orders"]
        self.assertEqual([row["is_paid"] for row in rows], [1, 1, 0])
        self.assertEqual(sum(row["price"] for row in rows if row["is_paid"]), 200)
        self.assertEqual(self.guard.apply_order_action("undo_payment", self.token((1, 2))), 2)

    def test_editing_an_unrelated_order_does_not_reject_legitimate_edit(self):
        data = fixture(); second = copy.deepcopy(data["orders"][0]); second["id"] = 2
        data["orders"].append(second)
        self.ready(data); first = self.token((1,)); second = self.token((2,))
        self.assertEqual(self.guard.apply_order_action("edit", second, edit_changes("other-edit")), 1)
        self.assertEqual(self.guard.apply_order_action("edit", first, edit_changes("first-edit")), 1)
        self.assertEqual([row["item_name"] for row in self.snapshot()["orders"]], ["first-edit", "other-edit"])

    def test_old_clear_confirmation_rejected_but_normal_clear_rule_preserved(self):
        remote = self.ready()
        old = {"generation": self.token()["generation"], "rows": (), "clear_all": True}
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-order")))
        before = self.snapshot()
        with self.assertRaises(OrderConflict):
            self.guard.apply_order_action("clear", old)
        self.assertEqual(self.snapshot(), before)
        fresh = {"generation": self.token()["generation"], "rows": (), "clear_all": True}
        self.assertEqual(self.guard.apply_order_action("clear", fresh), 1)
        self.assertEqual(self.guard.apply_order_action("clear", fresh), 0)  # clearing an empty DB is valid

    def test_validation_failure_does_not_rotate_generation_but_started_replacement_does(self):
        remote = self.ready(); old = self.token(); before = self.snapshot()
        with self.assertRaises(BackupError):
            self.guard.transfer(True, remote.get, remote.put, imported={})
        self.assertEqual(self.token()["generation"], old["generation"])
        remote.mode = "reject"
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("new-order")))
        self.assertEqual(self.snapshot(), before)
        self.assertNotEqual(self.token()["generation"], old["generation"])
        with self.assertRaises(OrderConflict):
            self.guard.apply_order_action("delete", old)
        self.assertEqual(self.guard.apply_order_action("payment", self.token()), 1)

    def test_lost_response_reconciliation_invalidates_old_views(self):
        remote = self.ready(); old = self.token(); before = self.snapshot()
        remote.mode = "lost_response_get_fail"
        self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("replacement")))
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(SafetyBlocked):
            self.guard.apply_order_action("delete", old)
        remote.fail_get = False
        self.assertEqual(self.guard.startup(True, remote.get), "reconciled")
        after = self.snapshot()
        with self.assertRaises(OrderConflict):
            self.guard.apply_order_action("delete", old)
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(remote.put_calls, 1)

    def test_commit_failure_reconciliation_cannot_reenable_old_token(self):
        remote = self.ready(); old = self.token(); before = self.snapshot()
        from contextlib import contextmanager
        original_exclusive = self.guard.exclusive
        class FailCommit:
            def __init__(self, conn): object.__setattr__(self, "conn", conn)
            def __getattr__(self, name): return getattr(self.conn, name)
            def __setattr__(self, name, value): setattr(self.conn, name, value)
            def commit(self): raise sqlite3.OperationalError("injected commit failure")
        @contextmanager
        def failing_exclusive():
            with original_exclusive() as conn:
                yield FailCommit(conn)
        with patch.object(self.guard, "exclusive", failing_exclusive):
            self.assertFalse(self.guard.transfer(True, remote.get, remote.put, imported=fixture("replacement")))
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(SafetyBlocked):
            self.guard.apply_order_action("payment", old)
        self.assertEqual(self.guard.startup(True, remote.get), "reconciled")
        after = self.snapshot()
        with self.assertRaises(OrderConflict):
            self.guard.apply_order_action("payment", old)
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(remote.put_calls, 1)

    def test_action_waiting_for_import_lock_rejects_old_view_after_commit(self):
        remote = self.ready(); old = self.token()
        entered, release = threading.Event(), threading.Event()
        outcomes, errors = [], []
        original_put = remote.put
        def slow_put(data, sha):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test release timeout")
            return original_put(data, sha)
        def importer():
            try:
                outcomes.append(self.guard.transfer(True, remote.get, slow_put, imported=fixture("new-order")))
            except Exception as e:
                errors.append(e)
        def stale_writer():
            try:
                self.guard.apply_order_action("edit", old, edit_changes())
                errors.append(AssertionError("stale writer was accepted"))
            except OrderConflict:
                outcomes.append("rejected")
            except Exception as e:
                errors.append(e)
        first = threading.Thread(target=importer); first.start(); self.assertTrue(entered.wait(3))
        second = threading.Thread(target=stale_writer); second.start()
        release.set(); first.join(4); second.join(4)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, [True, "rejected"])
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "new-order")

    def test_action_failure_rolls_back_and_does_not_consume_valid_token(self):
        self.ready(); token = self.token(); before = self.snapshot()
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TRIGGER fail_payment BEFORE UPDATE OF is_paid ON orders BEGIN SELECT RAISE(ABORT,'injected'); END")
        conn.commit(); conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.guard.apply_order_action("payment", token)
        self.assertEqual(self.snapshot(), before)
        conn = sqlite3.connect(self.db); conn.execute("DROP TRIGGER fail_payment"); conn.commit(); conn.close()
        self.assertEqual(self.guard.apply_order_action("payment", token), 1)


class FakeUI:
    """Minimal function harness, not a real Streamlit browser test."""
    def __init__(self):
        self.session_state = {}
        self.errors = []
        self.clicked = set()
        self.buttons = []
        self.text_values = {}
        self.toasts = []
        self.sidebar = self
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def dialog(self, *args, **kwargs): return lambda fn: fn
    def fragment(self, *args, **kwargs):
        return args[0] if args and callable(args[0]) else lambda fn: fn
    def columns(self, widths, **kwargs): return [self] * (widths if isinstance(widths, int) else len(widths))
    def container(self, *args, **kwargs): return self
    def expander(self, *args, **kwargs): return self
    def popover(self, *args, **kwargs): return self
    def tabs(self, labels): return [self] * len(labels)
    def button(self, label, **kwargs):
        self.buttons.append((label, kwargs))
        return label in self.clicked
    def text_input(self, label, **kwargs): return self.text_values.get(label, kwargs.get("value", ""))
    def number_input(self, label, **kwargs): return self.text_values.get(label, kwargs.get("value", 0))
    def pills(self, label, options=None, **kwargs):
        return self.session_state.get(kwargs.get("key"), kwargs.get("default", [] if kwargs.get("selection_mode") == "multi" else None))
    def error(self, message): self.errors.append(message)
    def toast(self, message, **kwargs): self.toasts.append(message)
    def rerun(self, **kwargs): pass
    def __getattr__(self, name): return lambda *args, **kwargs: None


def open_delete_request(env,ui):
    """Use the actual displayed opener's captured callback, not a raw ID."""
    ui.buttons.clear()
    env['render_my_orders']('測試甲')
    registered=next(kw for label,kw in ui.buttons if label=='🗑️')
    registered['on_click'](*registered['args'])
    request=ui.session_state.pop('_delete_order_request')
    ui.session_state['_active_order_delete_request']=request
    return request


class PillsSessionStateTests(unittest.TestCase):
    """Execute only existing selection blocks, never application bootstrap."""
    keys = {'m_size':'main_size_options', 'm_spicy':'spicy_levels',
            'd_size':'drink_size_options', 'd_sugar':'sugar_levels', 'd_ice':'ice_levels'}

    def block(self, key):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call)
                  and isinstance(n.func,ast.Attribute) and n.func.attr=='pills'
                  and any(k.arg=='key' and isinstance(k.value,ast.Constant) and k.value.value==key for k in n.keywords))
        return min((n for n in ast.walk(tree) if isinstance(n,ast.If)
                    and n.lineno<=call.lineno<=n.end_lineno),key=lambda n:n.end_lineno-n.lineno)

    def render(self,key,state,options=('first','chosen')):
        ui=FakeUI(); ui.session_state.update(state); seen=[]
        def pills(label,values,**kw):
            self.assertNotIn('default',kw)
            self.assertIn(key,ui.session_state)
            seen.append(ui.session_state[key]); return ui.session_state[key]
        ui.pills=pills
        env={'st':ui,'get_default_option':lambda cat:'chosen',self.keys[key]:list(options)}
        exec(compile(ast.Module(body=[self.block(key)],type_ignores=[]),str(SOURCE),'exec'),env)
        return ui,seen

    def test_initial_defaults_and_existing_selection_preserved(self):
        for key in self.keys:
            for state,expected in (({},'chosen'),({key:'first'},'first')):
                with self.subTest(key=key,state=state):
                    self.assertEqual(self.render(key,state)[1],[expected])

    def test_disabled_or_cleared_selection_uses_existing_fallback(self):
        for key in self.keys:
            for value in ('retired',None):
                with self.subTest(key=key,value=value):
                    self.assertEqual(self.render(key,{key:value})[1],['chosen'])

    def test_empty_options_do_not_create_widget(self):
        for key in self.keys:
            with self.subTest(key=key): self.assertEqual(self.render(key,{},())[1],[])

    def test_main_size_reset_still_sets_database_default(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        reset=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and '_reset_m_size' in ast.unparse(n.test))
        ui=FakeUI(); ui.session_state.update(m_size='first',_reset_m_size=True)
        exec(compile(ast.Module(body=[reset],type_ignores=[]),str(SOURCE),'exec'),
             {'st':ui,'get_default_option':lambda cat:'chosen'})
        self.assertEqual(ui.session_state['m_size'],'chosen')
        self.assertNotIn('_reset_m_size',ui.session_state)

    def test_edit_main_pills_also_use_initialized_state_without_default(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        edit=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='edit_order_dialog')
        found=[]
        for call in ast.walk(edit):
            if not isinstance(call,ast.Call) or not isinstance(call.func,ast.Attribute) or call.func.attr!='pills': continue
            keys=[k.value for k in call.keywords if k.arg=='key']
            if keys and isinstance(keys[0],ast.Name) and keys[0].id in ('size_key','spicy_key'):
                self.assertFalse(any(k.arg=='default' for k in call.keywords))
                found.append(keys[0].id)
        self.assertEqual(set(found),{'size_key','spicy_key'})


class DrinkDialogStateTests(IsolatedTest):
    """Real widget registration in AppTest; browser dismiss events remain untested."""
    def app(self):
        import tempfile
        previous_tempdir=tempfile.tempdir
        tempfile.tempdir=str(self.db.parent)
        try:
            from streamlit.testing.v1 import AppTest
        finally:
            tempfile.tempdir=previous_tempdir
        source=SOURCE.read_text(encoding='utf-8-sig'); tree=ast.parse(source)
        definitions=[]
        for name in ('open_drink_custom_dialog','custom_dialog'):
            node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            definitions.append(('@st.dialog("Fake drink custom")\n' if name=='custom_dialog' else '')+ast.get_source_segment(source,node))
        script='import streamlit as st\n'+'\n\n'.join(definitions)+'''
st.session_state.setdefault('d_custom_tags', [])
st.session_state.setdefault('d_custom_manual', '')
st.session_state.setdefault('m_custom_manual', 'fake meal unchanged')
if st.button('Fake open', key='open'):
    st.session_state['_test_dialog_visible']=True
    open_drink_custom_dialog(['fake tag'])
elif st.session_state.get('_test_dialog_visible', False):
    custom_dialog('d_custom', ['fake tag'])
'''
        path=self.db.parent/'fake_dialog_only.py'; path.write_text(script,encoding='utf-8')
        app=AppTest.from_file(str(path)); app.secrets={'fake_only':True}; app.run()
        return app

    def open(self,app):
        app.button(key='open').click().run()
        self.assertEqual(len(app.exception),0)
        self.assertFalse(any('default value' in warning.value for warning in app.warning))

    def test_unconfirmed_draft_is_discarded_on_new_open_intent(self):
        app=self.app(); self.open(app)
        app.text_input[0].set_value('fake unsaved').run()
        self.assertEqual(app.text_input[0].value,'fake unsaved')
        self.assertEqual(app.session_state['d_custom_manual'],'')
        # New open intent with old widget state still present (not a browser X event).
        self.open(app)
        self.assertEqual(app.text_input[0].value,'')
        self.assertEqual(app.session_state['d_custom_manual'],'')

    def test_open_replaces_stale_tag_widget_and_preserves_committed_values(self):
        ui=FakeUI()
        ui.session_state.update(d_custom_tags=['fake tag'],d_custom_manual='fake confirmed',
            d_custom_pills_widget=['fake draft'],d_custom_manual_widget='fake unsaved',
            m_custom_manual='fake meal unchanged')
        env=load_app_functions(['open_drink_custom_dialog'],st=ui,custom_dialog=lambda *args:None)
        env['open_drink_custom_dialog'](['fake tag'])
        self.assertEqual(ui.session_state['d_custom_pills_widget'],['fake tag'])
        self.assertEqual(ui.session_state['d_custom_manual_widget'],'fake confirmed')
        self.assertEqual(ui.session_state['d_custom_tags'],['fake tag'])
        self.assertEqual(ui.session_state['d_custom_manual'],'fake confirmed')
        self.assertEqual(ui.session_state['m_custom_manual'],'fake meal unchanged')

    def test_completed_then_cleared_business_values_reopen_empty(self):
        app=self.app(); self.open(app)
        app.text_input[0].set_value('fake completed').run()
        done=next(button for button in app.button if button.label=='✅ 完成')
        done.click().run()
        self.assertEqual(app.session_state['d_custom_manual'],'fake completed')
        app.session_state['d_custom_tags']=[]; app.session_state['d_custom_manual']=''
        self.open(app)
        self.assertEqual(app.text_input[0].value,'')
        self.assertEqual(app.session_state['d_custom_pills_widget'],[])
        self.assertEqual(app.session_state['m_custom_manual'],'fake meal unchanged')

    def test_successful_creation_clear_path_and_reopen(self):
        source=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        creation=next(n for n in ast.walk(source) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call) and isinstance(n.test.func,ast.Name)
            and n.test.func.id=='create_member_order' and n.test.args[1].elts[0].value=='飲料')
        resets=[n for n in creation.body if isinstance(n,ast.Assign)
                and isinstance(n.targets[0],ast.Subscript)
                and isinstance(n.targets[0].slice,ast.Constant)
                and n.targets[0].slice.value in ('d_custom_tags','d_custom_manual')]
        self.assertEqual(len(resets),2)
        app=self.app(); self.open(app)
        app.text_input[0].set_value('fake order custom').run()
        next(b for b in app.button if b.label=='✅ 完成').click().run()
        exec(compile(ast.Module(body=resets,type_ignores=[]),str(SOURCE),'exec'),
             {'st':SimpleNamespace(session_state=app.session_state)})
        self.open(app)
        self.assertEqual(app.text_input[0].value,'')
        self.assertEqual(app.session_state['d_custom_tags'],[])


class SaveSyncFeedbackTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.remote=self.ready()
        self.ui=FakeUI(); self.synced=[]
        harness=OrderUIIntegrationTests(); harness.guard=self.guard
        self.env=harness.env(self.ui,self.synced)
        self.env.update(load_app_functions(['sync_github_backup','current_backup_result','create_member_order'],
            **self.env, github_is_configured=lambda:True,
            github_get_backup=self.remote.get,github_put_backup=self.remote.put))

    def check_feedback(self, synced):
        self.assertEqual(self.ui.errors,[])
        if synced:
            self.assertEqual(self.ui.toasts,[])
            return
        self.assertEqual(len(self.ui.toasts),1)
        message=self.ui.toasts[0]
        self.assertIn('本機已儲存',message)
        self.assertTrue(message.startswith('⚠️'))
        self.assertEqual('請重試同步' in message,not synced)
        self.assertEqual(self.ui.errors,[])

    def test_successful_payment_and_undo_are_silent_and_still_sync(self):
        for category,key in [('主餐','main'),('飲料','drink')]:
            conn=self.guard.connect(write=True)
            try:
                conn.execute('UPDATE orders SET category=? WHERE id=1',(category,)); conn.commit()
            finally: conn.close()
            for button,paid in [('確認收款',1),('撤銷收款',0)]:
                with self.subTest(category=category,button=button):
                    self.ui.clicked.clear(); self.ui.toasts.clear(); self.ui.buttons.clear()
                    view=self.guard.order_snapshot(); frame=pd.DataFrame(view['rows'],columns=ORDER_FIELDS)
                    markdown=[]; captions=[]
                    self.ui.markdown=lambda text,**kw:markdown.append(text)
                    self.ui.write=lambda text,**kw:markdown.append(str(text))
                    self.ui.caption=captions.append
                    self.env['_pay_logic_grouped'](category,frame,key,view)
                    registered=next(kw for label,kw in self.ui.buttons if label==button)
                    registered['on_click'](*registered['args']); self.ui.clicked.add(button)
                    self.env['_pay_logic_grouped'](category,frame,key,view)
                    self.assertEqual(self.snapshot()['orders'][0]['is_paid'],paid)
                    self.assertEqual(self.remote.data['orders'][0]['is_paid'],paid)
                    self.assertTrue(any('$100' in text for text in markdown))
                    self.assertFalse(any('此人全部' in text for text in captions))
                    self.check_feedback(True)

    def test_successful_edit_and_delete_are_silent_and_still_sync(self):
        token=self.guard.order_token(self.guard.order_snapshot(),[1])
        self.ui.clicked.add('💾 儲存修改'); self.ui.text_values['餐點名稱']='Fake silent edit'
        self.env['edit_order_dialog'](1,'主餐','測試便當',100,1,'',token)
        self.assertEqual(self.snapshot()['orders'][0]['item_name'],'Fake silent edit')
        self.assertEqual(self.remote.data['orders'][0]['item_name'],'Fake silent edit')
        self.check_feedback(True)
        self.ui.clicked.clear(); self.ui.buttons.clear()
        request=open_delete_request(self.env,self.ui)
        self.ui.clicked.add('確認刪除')
        self.env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot()['orders'],[])
        self.assertEqual(self.remote.data['orders'],[]); self.check_feedback(True)

    def test_shop_feedback_is_silent_on_success_and_warns_on_sync_failure(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call)
            and any(k.arg=='key' and isinstance(k.value,ast.Constant)
                    and k.value.value=='save_shop_names' for k in n.test.keywords))
        statuses=[]; warnings=[]
        self.ui.success=statuses.append; self.ui.warning=warnings.append
        self.ui.clicked.add('💾 儲存今日店家')
        def read_shops():
            conn=self.guard.connect()
            try: return dict(conn.execute('SELECT category,shop_name FROM config_shop'))
            finally: conn.close()
        for failure in (False,True):
            with self.subTest(sync_failure=failure):
                self.remote.fail_get=failure; warnings.clear()
                scope=dict(self.env,db_connect=self.guard.connect,
                    get_current_shop_names=read_shops,
                    # Only the feedback contract is under test for this sidebar;
                    # real FakeRemote sync is exercised by the order cases.
                    save_and_sync=lambda:not failure,
                    new_main_shop='Fake shop',new_drink_shop='Fake drinks')
                exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),scope)
                self.assertEqual(read_shops(),
                                 {'main':'Fake shop','drink':'Fake drinks'})
                self.assertEqual(statuses,[]); self.assertEqual(len(warnings),int(failure))
                if failure: self.assertIn('同步未成功',warnings[0])
                self.assertEqual(self.ui.errors,[])

    def test_meal_and_drink_creation_distinguish_confirmed_and_failed_sync(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        cases=[n for n in ast.walk(tree) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call) and isinstance(n.test.func,ast.Name)
            and n.test.func.id=='create_member_order']
        view=self.guard.member_snapshot()
        member={'member_id':view['rows'][0][0],'name':view['rows'][0][1],'generation':view['generation']}
        self.ui.session_state['member_token']=member
        for node in cases:
            category=node.test.args[1].elts[0].value
            for failure in (False,True):
                with self.subTest(category=category,failure=failure):
                    self.remote.fail_get=failure; self.ui.toasts.clear(); self.ui.errors.clear()
                    before=len(self.snapshot()['orders'])
                    scope=dict(self.env,m_name='Fake meal',d_name='Fake drink',total_p=100,
                        cust='fake custom',final_cust='L/無糖/去冰',m_qty=1,d_qty=1,
                        m_price_unit=100,d_price_unit=100,
                        taiwan_now=lambda:SimpleNamespace(strftime=lambda _:NOW))
                    exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),scope)
                    self.assertEqual(len(self.snapshot()['orders']),before+1)
                    self.check_feedback(not failure)

    def test_failed_creation_does_not_sync_or_show_success(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call) and isinstance(n.test.func,ast.Name)
            and n.test.func.id=='create_member_order')
        before=self.snapshot(); scope=dict(self.env,total_p=100,m_name='Fake',m_qty=1,
            cust='',m_price_unit=100,taiwan_now=lambda:SimpleNamespace(strftime=lambda _:NOW))
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),scope)
        self.assertEqual(self.snapshot(),before)
        self.assertTrue(self.ui.errors); self.assertEqual(self.ui.toasts,[])
        self.assertEqual(self.remote.put_calls,0)

    def test_edit_and_delete_saved_locally_despite_sync_failure(self):
        self.remote.fail_get=True
        token=self.guard.order_token(self.guard.order_snapshot(),[1])
        self.ui.clicked.add('💾 儲存修改'); self.ui.text_values['餐點名稱']='Fake edit'
        self.env['edit_order_dialog'](1,'主餐','測試便當',100,1,'',token)
        self.assertEqual(self.snapshot()['orders'][0]['item_name'],'Fake edit')
        self.check_feedback(False)
        self.ui.clicked.clear(); self.ui.toasts.clear()
        request=open_delete_request(self.env,self.ui)
        self.ui.clicked.add('確認刪除')
        self.env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot()['orders'],[]); self.check_feedback(False)

    def test_payment_and_undo_saved_locally_despite_sync_failure(self):
        self.remote.fail_get=True
        for button,paid in [('確認收款',1),('撤銷收款',0)]:
            self.ui.clicked.clear(); self.ui.toasts.clear(); self.ui.buttons.clear()
            view=self.guard.order_snapshot(); frame=pd.DataFrame(view['rows'],columns=ORDER_FIELDS)
            self.env['_pay_logic_grouped']('主餐',frame,'main',view)
            registered=next(kw for label,kw in self.ui.buttons if label==button)
            registered['on_click'](*registered['args']); self.ui.clicked.add(button)
            self.env['_pay_logic_grouped']('主餐',frame,'main',view)
            self.assertEqual(self.snapshot()['orders'][0]['is_paid'],paid)
            self.check_feedback(False)

    def test_ambiguous_put_reports_pending_without_losing_local_write(self):
        self.remote.mode='lost_response_get_fail'
        token=self.guard.order_token(self.guard.order_snapshot(),[1])
        self.ui.clicked.add('💾 儲存修改'); self.ui.text_values['餐點名稱']='Fake saved'
        self.env['edit_order_dialog'](1,'主餐','測試便當',100,1,'',token)
        self.assertEqual(self.snapshot()['orders'][0]['item_name'],'Fake saved')
        self.assertTrue(self.guard.load_state()['attempt'])
        self.check_feedback(False)

    def test_save_contract_suppresses_generic_sync_error(self):
        flags=[]
        scope=load_app_functions(['save_and_sync','show_save_feedback'],st=self.ui,
            sync_github_backup=lambda show_error=False:flags.append(show_error) or False)
        self.assertIs(scope['save_and_sync'](),False)
        scope['show_save_feedback']('訂單已儲存',False)
        self.assertEqual(flags,[False]); self.check_feedback(False)

    def test_unconfigured_backup_keeps_local_edit_and_reports_retry(self):
        self.env['sync_github_backup'].__globals__['github_is_configured']=lambda:False
        token=self.guard.order_token(self.guard.order_snapshot(),[1])
        self.ui.clicked.add('💾 儲存修改'); self.ui.text_values['餐點名稱']='Fake local only'
        self.env['edit_order_dialog'](1,'主餐','測試便當',100,1,'',token)
        self.assertEqual(self.snapshot()['orders'][0]['item_name'],'Fake local only')
        self.assertEqual(self.remote.put_calls,0); self.check_feedback(False)

    def test_member_management_reports_local_success_when_backup_fails(self):
        self.remote.fail_get=True
        self.env.update(load_app_functions(['member_transaction','add_member','delete_member',
            'rename_member','move_member','manage_members_dialog'], **self.env,
            db_connect=self.guard.connect,
            get_members=lambda:pd.DataFrame(self.snapshot()['members'])))
        for key,label in [(None,'新增'),('up_2','排序'),('rename_save_1','姓名'),('confirm_delete_2','刪除')]:
            self.ui.toasts.clear(); self.ui.errors.clear()
            self.ui.text_values.update({'新增人員':'Fake new','姓名':'Fake renamed'})
            self.ui.session_state['confirm_delete_member_2']=True
            self.ui.button=lambda text,**kw:(text=='➕ 新增人員' if key is None else kw.get('key')==key)
            self.env['manage_members_dialog']()
            self.check_feedback(False); self.assertIn(label,self.ui.toasts[0])
        members=self.snapshot()['members']
        self.assertEqual(len(members),1); self.assertEqual(members[0]['name'],'Fake renamed')
        self.assertEqual(self.snapshot()['orders'][0]['name'],'Fake renamed')


class OrderUIIntegrationTests(IsolatedTest):
    def env(self, ui, synced):
        import html
        names = ["dismiss_order_delete", "request_order_delete", "delete_order_dialog", "serialize_drink_options", "remember_order_token", "execute_order_action", "edit_order_dialog", "render_my_orders", "render_payment_section", "_pay_logic_grouped", "clear_orders_dialog", "save_and_sync", "show_save_feedback"]
        return load_app_functions(names, st=ui, backup_guard=self.guard, pd=pd, html=html,
                                  ORDER_COLUMNS=list(ORDER_FIELDS), OrderConflict=OrderConflict,
                                  sync_github_backup=lambda show_error=False: synced.append(True) or True,
                                  main_size_options=["無"], spicy_levels=["無"], custom_tags_main=[],
                                  drink_size_options=["M", "L"], sugar_levels=["無糖"], ice_levels=["去冰"], custom_tags_drink=[],
                                  get_options=lambda cat: ["無"] if cat in ("main_size", "spicy") else [],
                                  taiwan_now_str=lambda: NOW,
                                  get_default_option=lambda cat: "無" if cat in ("main_size", "spicy") else "L")

    def test_real_edit_dialog_blocks_old_token_and_accepts_normal_edit(self):
        remote = self.ready(); old_view = self.guard.order_snapshot(); old = self.guard.order_token(old_view, [1])
        ui = FakeUI(); synced = []; env = self.env(ui, synced)
        ui.clicked.add("💾 儲存修改"); ui.text_values["餐點名稱"] = "new-edit"
        env["edit_order_dialog"](1, "主餐", "測試便當", 100, 1, "", old)
        self.assertEqual(self.snapshot()["orders"][0]["item_name"], "new-edit")
        self.assertEqual(synced, [True])
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=fixture("replacement")))
        before = self.snapshot(); synced.clear()
        env["edit_order_dialog"](1, "主餐", "測試便當", 100, 1, "", old)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(synced, [])
        self.assertTrue(ui.errors)

    def test_normal_drink_edit_keeps_existing_custom_format(self):
        data = fixture(); data["orders"][0]["category"] = "飲料"
        data["orders"][0]["custom"] = "L/無糖/去冰, 加珍珠"
        self.ready(data); token = self.guard.order_token(self.guard.order_snapshot(), [1])
        ui = FakeUI(); synced = []; env = self.env(ui, synced)
        ui.clicked.add("💾 儲存修改")
        ui.text_values.update({"飲料名稱":"edited-drink", "單價（元）":50, "數量":2})
        env["edit_order_dialog"](1, "飲料", "測試便當", 100, 1, "L/無糖/去冰, 加珍珠", token)
        row = self.snapshot()["orders"][0]
        self.assertEqual((row["item_name"],row["price"],row["quantity"],row["custom"]),
                         ("edited-drink",100,2,"L/無糖/去冰, 加珍珠"))
        self.assertEqual(ui.errors, []); self.assertEqual(synced, [True])

    def test_drink_retired_options_preserved_when_only_quantity_changes(self):
        custom='舊大杯/舊半糖/舊少冰, 加珍珠'
        data=fixture(); data['orders'][0].update(category='飲料',custom=custom)
        self.ready(data); token=self.guard.order_token(self.guard.order_snapshot(),[1])
        ui=FakeUI(); env=self.env(ui,[])
        ui.clicked.add('💾 儲存修改'); ui.text_values['數量']=2
        env['edit_order_dialog'](1,'飲料','測試便當',100,1,custom,token)
        row=self.snapshot()['orders'][0]
        self.assertEqual(row['custom'],custom)
        self.assertEqual((row['quantity'],row['price']),(2,200))

    def test_drink_empty_custom_does_not_acquire_defaults_on_edit(self):
        data=fixture(); data['orders'][0].update(category='飲料',custom='')
        self.ready(data); token=self.guard.order_token(self.guard.order_snapshot(),[1])
        ui=FakeUI(); env=self.env(ui,[]); ui.clicked.add('💾 儲存修改')
        env['edit_order_dialog'](1,'飲料','測試便當',100,1,'',token)
        self.assertEqual(self.snapshot()['orders'][0]['custom'],'')

    def test_drink_one_retired_option_can_be_explicitly_replaced(self):
        custom='L/舊半糖/去冰, 加珍珠'
        data=fixture(); data['orders'][0].update(category='飲料',custom=custom)
        self.ready(data); token=self.guard.order_token(self.guard.order_snapshot(),[1])
        ui=FakeUI(); env=self.env(ui,[])
        env['edit_order_dialog'](1,'飲料','測試便當',100,1,custom,token)
        self.assertEqual(ui.session_state[f"edit_d_sugar_1_{token['generation']}"],'舊半糖')
        ui.session_state[f"edit_d_sugar_1_{token['generation']}"]='無糖'
        ui.clicked.add('💾 儲存修改')
        env['edit_order_dialog'](1,'飲料','測試便當',100,1,custom,token)
        self.assertEqual(self.snapshot()['orders'][0]['custom'],'L/無糖/去冰, 加珍珠')

    def test_drink_retired_values_preserved_even_when_all_options_removed(self):
        custom='舊大杯/舊半糖/舊少冰, 加珍珠'
        data=fixture(); data['orders'][0].update(category='飲料',custom=custom)
        self.ready(data); token=self.guard.order_token(self.guard.order_snapshot(),[1])
        ui=FakeUI(); env=self.env(ui,[])
        env.update(drink_size_options=[],sugar_levels=[],ice_levels=[])
        ui.clicked.add('💾 儲存修改')
        env['edit_order_dialog'](1,'飲料','測試便當',100,1,custom,token)
        self.assertEqual(self.snapshot()['orders'][0]['custom'],custom)

    def test_drink_legacy_manual_custom_does_not_acquire_defaults(self):
        custom='加珍珠, 不要吸管'
        data=fixture(); data['orders'][0].update(category='飲料',custom=custom)
        self.ready(data); token=self.guard.order_token(self.guard.order_snapshot(),[1])
        ui=FakeUI(); env=self.env(ui,[]); ui.clicked.add('💾 儲存修改')
        env['edit_order_dialog'](1,'飲料','測試便當',100,1,custom,token)
        self.assertEqual(self.snapshot()['orders'][0]['custom'],custom)

    def test_drink_custom_dialog_commits_only_on_done(self):
        ui=FakeUI(); ui.session_state.update(d_custom_tags=['加珍珠'],d_custom_manual='不要吸管')
        before=dict(ui.session_state)
        env=load_app_functions(['custom_dialog','open_drink_custom_dialog'],st=ui)
        ui.text_values['其他客製需求']='新備註'
        env['open_drink_custom_dialog'](['加珍珠'])
        self.assertEqual(ui.session_state['d_custom_tags'],before['d_custom_tags'])
        self.assertEqual(ui.session_state['d_custom_manual'],before['d_custom_manual'])
        ui.clicked.add('✅ 完成')
        env['custom_dialog']('d_custom',['加珍珠'])
        self.assertEqual(ui.session_state['d_custom_manual'],'新備註')
        self.assertEqual(ui.session_state['d_custom_tags'],['加珍珠'])

    def test_payment_native_tokens_accept_legacy_null_unit_despite_pandas_nan(self):
        data = fixture(); data["orders"][0]["unit_price"] = None
        second = copy.deepcopy(data["orders"][0]); second.update(id=2,unit_price=100)
        data["orders"].append(second)
        self.ready(data); view = self.guard.order_snapshot()
        frame = pd.DataFrame(view["rows"],columns=ORDER_FIELDS)
        self.assertTrue(pd.isna(frame.iloc[0]["unit_price"]))
        ui = FakeUI(); synced = []; env = self.env(ui, synced)
        env["_pay_logic_grouped"]("主餐",frame,"main",view)
        registered = next(kwargs for label, kwargs in ui.buttons if label == "確認收款")
        registered["on_click"](*registered["args"])
        self.assertIsNone(ui.session_state["pay_token_main_測試甲"]["rows"][0][9])
        ui.clicked.add("確認收款")
        env["_pay_logic_grouped"]("主餐",frame,"main",view)
        self.assertEqual([row["is_paid"] for row in self.snapshot()["orders"]],[1,1])
        self.assertEqual(ui.errors, []); self.assertEqual(synced,[True])

    def test_payment_uses_preclick_token_not_refreshed_replacement_data(self):
        remote = self.ready(); view = self.guard.order_snapshot(); old = self.guard.order_token(view, [1])
        ui = FakeUI(); synced = []; env = self.env(ui, synced)
        env["_pay_logic_grouped"]("主餐", pd.DataFrame(view["rows"], columns=ORDER_FIELDS), "main", view)
        _, registered = next((label, kwargs) for label, kwargs in ui.buttons if label == "確認收款")
        registered["on_click"](*registered["args"])  # old displayed event runs before rerender
        self.assertEqual(ui.session_state["pay_token_main_測試甲"], old)
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=fixture("replacement")))
        fresh_view = self.guard.order_snapshot(); before = self.snapshot()
        ui.clicked.add("確認收款")  # even a forced new-key button event cannot bypass backend token check
        env["_pay_logic_grouped"]("主餐", pd.DataFrame(fresh_view["rows"], columns=ORDER_FIELDS), "main", fresh_view)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(synced, [])
        self.assertTrue(ui.errors)
        registered = next(kwargs for label, kwargs in reversed(ui.buttons) if label == "確認收款")
        registered["on_click"](*registered["args"])
        env["_pay_logic_grouped"]("主餐", pd.DataFrame(fresh_view["rows"], columns=ORDER_FIELDS), "main", fresh_view)
        self.assertEqual(self.snapshot()["orders"][0]["is_paid"], 1)
        self.assertEqual(synced, [True])

    def test_delete_uses_preclick_token_and_does_not_sync_rejection(self):
        remote = self.ready(); ui = FakeUI(); synced = []; env = self.env(ui, synced)
        request=open_delete_request(env,ui)
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=fixture("replacement")))
        before=self.snapshot(); ui.clicked.add("確認刪除")
        env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot(),before); self.assertEqual(synced,[])
        ui.clicked.clear()
        fresh=open_delete_request(env,ui)
        ui.clicked.add("確認刪除"); env['delete_order_dialog'](fresh)
        self.assertEqual(self.snapshot()["orders"],[]); self.assertEqual(synced,[True])

    def test_undo_payment_uses_original_paid_snapshot_and_accepts_fresh_token(self):
        data = fixture(); data["orders"][0]["is_paid"] = 1
        remote = self.ready(data); view = self.guard.order_snapshot()
        ui = FakeUI(); synced = []; env = self.env(ui, synced)
        env["_pay_logic_grouped"]("主餐", pd.DataFrame(view["rows"],columns=ORDER_FIELDS), "main", view)
        registered = next(kwargs for label, kwargs in ui.buttons if label == "撤銷收款")
        registered["on_click"](*registered["args"])
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=self.snapshot()))
        fresh = self.guard.order_snapshot(); before = self.snapshot()
        ui.clicked.add("撤銷收款")
        env["_pay_logic_grouped"]("主餐", pd.DataFrame(fresh["rows"],columns=ORDER_FIELDS), "main", fresh)
        self.assertEqual(self.snapshot(), before); self.assertEqual(synced, [])
        registered = next(kwargs for label, kwargs in reversed(ui.buttons) if label == "撤銷收款")
        registered["on_click"](*registered["args"])
        env["_pay_logic_grouped"]("主餐", pd.DataFrame(fresh["rows"],columns=ORDER_FIELDS), "main", fresh)
        self.assertEqual(self.snapshot()["orders"][0]["is_paid"], 0); self.assertEqual(synced, [True])

    def test_clear_dialog_rejects_old_confirmation_and_accepts_fresh_generation(self):
        remote = self.ready(); old = {"generation":self.guard.order_snapshot()["generation"],"rows":(),"clear_all":True}
        self.assertTrue(self.guard.transfer(True, remote.get, remote.put, imported=fixture("replacement")))
        ui = FakeUI(); synced = []; env = self.env(ui, synced)
        ui.clicked.add("🗑️ 確定清除"); before = self.snapshot()
        env["sync_github_backup"] = lambda show_error=False: synced.append(True) or True
        env["clear_orders_dialog"](old)
        self.assertEqual(self.snapshot(), before); self.assertEqual(synced, [])
        fresh = {"generation":self.guard.order_snapshot()["generation"],"rows":(),"clear_all":True}
        env["clear_orders_dialog"](fresh)
        self.assertEqual(self.snapshot()["orders"], []); self.assertEqual(synced, [True])

    def test_id_routes_have_no_raw_id_writes_and_use_captured_tokens(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for name in ("edit_order_dialog", "delete_order_dialog", "_pay_logic_grouped", "clear_orders_dialog"):
            with self.subTest(route=name):
                calls = [node for node in ast.walk(funcs[name]) if isinstance(node, ast.Call)]
                self.assertFalse(any(isinstance(node.func, ast.Name) and node.func.id == "execute_db" for node in calls))
                self.assertTrue(any(isinstance(node.func, ast.Name) and node.func.id == "execute_order_action" for node in calls))

    def test_unavailable_snapshot_or_edit_check_does_not_offer_unsafe_fallback(self):
        self.ready(); view = self.guard.order_snapshot(); token = self.guard.order_token(view,[1])
        before = self.snapshot()
        for failure in (SafetyBlocked("injected pending"),sqlite3.OperationalError("database is locked")):
            for route in ("render_my_orders","render_payment_section","edit_order_dialog"):
                with self.subTest(route=route,failure=type(failure).__name__):
                    ui = FakeUI(); synced = []; env = self.env(ui,synced)
                    method="expected_order" if route=="edit_order_dialog" else "order_snapshot"
                    with patch.object(self.guard,method,side_effect=failure):
                        if route=="edit_order_dialog":
                            env[route](1,"主餐","測試便當",100,1,"",token)
                        elif route=="render_my_orders":
                            env[route]("測試甲")
                        else:
                            env[route]()
                    self.assertTrue(ui.errors); self.assertEqual(synced,[])
                    self.assertFalse(any(label in ("✏️","確認刪除","確認收款","撤銷收款","💾 儲存修改") for label,_ in ui.buttons))
                    self.assertEqual(self.snapshot(),before)


class ShopLockAcceptance(unittest.TestCase):
    snapshot = IsolatedTest.snapshot
    ready = IsolatedTest.ready
    def setUp(self):
        IsolatedTest.setUp(self)
        self.remote=self.ready()
        self.ui=FakeUI(); self.synced=[]
        harness=OrderUIIntegrationTests(); harness.guard=self.guard
        self.env=harness.env(self.ui,self.synced)
        self.env.update(load_app_functions(['sync_github_backup','current_backup_result'],
            **self.env, github_is_configured=lambda:True,
            github_get_backup=self.remote.get,github_put_backup=self.remote.put))
    def run_shop(self, failure=None):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call)
            and any(k.arg=='key' and isinstance(k.value,ast.Constant)
                    and k.value.value=='save_shop_names' for k in n.test.keywords))
        self.ui.clicked.add('💾 儲存今日店家')
        self.warnings=[]; self.ui.warning=self.warnings.append
        connect=self.guard.connect
        class FaultConnection:
            def __init__(self,conn): self.conn=conn
            def execute(self,sql,params=()):
                if failure=='second' and params and params[0]=='drink':
                    raise sqlite3.OperationalError('fake second write failure')
                return self.conn.execute(sql,params)
            def commit(self):
                if failure=='commit': raise sqlite3.OperationalError('fake commit failure')
                return self.conn.commit()
            def __getattr__(self,key): return getattr(self.conn,key)
        scope=dict(self.env,db_connect=lambda write=False:FaultConnection(connect(write=write)),
                   new_main_shop='Fake main changed',new_drink_shop='Fake drink changed')
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),scope)

    def test_real_lock_released_before_successful_sync(self):
        self.run_shop()
        self.assertEqual(self.ui.errors,[]); self.assertEqual(self.warnings,[])
        self.assertEqual(self.snapshot()['config_shop'],self.remote.data['config_shop'])
        self.assertEqual(self.remote.put_calls,1)

    def test_sync_failure_keeps_both_local_shop_updates(self):
        self.remote.fail_get=True
        self.run_shop()
        self.assertEqual(dict((r['category'],r['shop_name']) for r in self.snapshot()['config_shop']),
                         {'main':'Fake main changed','drink':'Fake drink changed'})
        self.assertEqual(self.remote.put_calls,0); self.assertEqual(len(self.warnings),1)
        self.assertEqual(self.ui.errors,[])

    def test_second_shop_failure_rolls_back_and_does_not_sync(self):
        before=self.snapshot(); gets=self.remote.get_calls
        self.run_shop('second')
        self.assertEqual(self.snapshot(),before); self.assertEqual(self.remote.get_calls,gets)
        self.assertEqual(self.remote.put_calls,0); self.assertTrue(self.ui.errors)

    def test_commit_failure_rolls_back_and_does_not_sync(self):
        before=self.snapshot(); gets=self.remote.get_calls
        self.run_shop('commit')
        self.assertEqual(self.snapshot(),before); self.assertEqual(self.remote.get_calls,gets)
        self.assertEqual(self.remote.put_calls,0); self.assertTrue(self.ui.errors)


class DrinkPositionAcceptance(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.serialize=load_app_functions(['serialize_drink_options'])['serialize_drink_options']

    def roundtrip(self, values):
        custom=self.serialize(*values)
        data=fixture(); data['orders'][0].update(category='飲料',custom=custom)
        self.ready(data)
        harness=OrderUIIntegrationTests(); harness.guard=self.guard
        for repeat in range(2):
            token=self.guard.order_token(self.guard.order_snapshot(),[1])
            ui=FakeUI(); env=harness.env(ui,[])
            env['edit_order_dialog'](1,'飲料','Fake drink',100,1,custom,token)
            for field,expected in zip(('size','sugar','ice'),values):
                self.assertEqual(ui.session_state[f"edit_d_{field}_1_{token['generation']}"],expected)
            ui.clicked.add('💾 儲存修改')
            env['edit_order_dialog'](1,'飲料','Fake drink',100,1,custom,token)
            self.assertEqual(ui.errors,[])
            self.assertEqual(self.snapshot()['orders'][0]['custom'],custom)
        return custom

    def test_empty_size_roundtrips(self):
        self.assertEqual(self.roundtrip((None,'無糖','去冰')),'/無糖/去冰')

    def test_empty_sugar_roundtrips(self):
        self.assertEqual(self.roundtrip(('L',None,'去冰')),'L//去冰')

    def test_empty_ice_roundtrips(self):
        self.assertEqual(self.roundtrip(('L','無糖',None)),'L/無糖/')

    def test_all_empty_stays_empty(self):
        self.assertEqual(self.roundtrip((None,None,None)),'')

    def test_clearing_middle_field_then_reopening_keeps_ice(self):
        data=fixture(); data['orders'][0].update(category='飲料',custom='L/無糖/去冰')
        self.ready(data); harness=OrderUIIntegrationTests(); harness.guard=self.guard
        token=self.guard.order_token(self.guard.order_snapshot(),[1]); ui=FakeUI()
        env=harness.env(ui,[])
        env['edit_order_dialog'](1,'飲料','Fake',100,1,'L/無糖/去冰',token)
        ui.session_state[f"edit_d_sugar_1_{token['generation']}"]=None
        ui.clicked.add('💾 儲存修改')
        env['edit_order_dialog'](1,'飲料','Fake',100,1,'L/無糖/去冰',token)
        custom=self.snapshot()['orders'][0]['custom']; self.assertEqual(custom,'L//去冰')
        fresh=FakeUI(); token=self.guard.order_token(self.guard.order_snapshot(),[1])
        harness.env(fresh,[])['edit_order_dialog'](1,'飲料','Fake',100,1,custom,token)
        self.assertIsNone(fresh.session_state[f"edit_d_sugar_1_{token['generation']}"])
        self.assertEqual(fresh.session_state[f"edit_d_ice_1_{token['generation']}"],'去冰')

    def test_stats_preserve_empty_positions_and_retired_text(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        parser=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='parse_stats_custom')
        scope={'stats_values':lambda cat:{'drink_size':['M','L'],'sugar':['少糖','無糖'],'ice':['少冰','去冰']}[cat]}
        exec(compile(ast.Module(body=[parser],type_ignores=[]),str(SOURCE),'exec'),scope)
        for raw,weights in [('L//去冰',(1,0,1)),('/無糖/去冰',(99,1,1)),('L/無糖/',(1,1,0))]:
            with self.subTest(raw=raw):
                parsed=scope['parse_stats_custom']('飲料',raw)
                self.assertEqual(parsed[1:4],weights)
                self.assertNotIn('・・',parsed[0])
        parsed=scope['parse_stats_custom']('飲料','L/舊糖/舊冰, 加珍珠')
        for value in ('舊糖','舊冰','加珍珠'): self.assertIn(value,parsed[0])
        self.assertIn('不要吸管',scope['parse_stats_custom']('飲料','不要吸管')[0])

    def test_new_order_button_uses_same_positional_format(self):
        self.ready(); harness=OrderUIIntegrationTests(); harness.guard=self.guard
        ui=FakeUI(); env=harness.env(ui,[]); ui.clicked.add('＋ 加入飲料')
        view=self.guard.member_snapshot()
        ui.session_state['member_token']={'member_id':view['rows'][0][0],
            'name':view['rows'][0][1],'generation':view['generation']}
        env.update(load_app_functions(['create_member_order'],**env))
        env.update(d_size='L',drink_size_options=['L'],d_sugar=None,d_ice='去冰',
            d_price_unit=50,d_qty=2,d_name='Fake new drink',d_display_list=[],
            taiwan_now=lambda:SimpleNamespace(strftime=lambda fmt:NOW))
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call) and isinstance(n.test.func,ast.Attribute)
            and n.test.func.attr=='button' and n.test.args and isinstance(n.test.args[0],ast.Constant)
            and n.test.args[0].value=='＋ 加入飲料')
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),env)
        self.assertEqual(ui.errors,[])
        row=self.snapshot()['orders'][-1]
        self.assertEqual((row['custom'],row['price'],row['quantity']),('L//去冰',100,2))


class BackupNoticeRegression(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.remote=self.ready(); self.ui=FakeUI()
        self.warnings=[]; self.successes=[]
        self.ui.warning=self.warnings.append; self.ui.success=self.successes.append
        self.env=load_app_functions(['current_backup_result'],backup_guard=self.guard,st=self.ui)

    def render_notices(self):
        self.warnings.clear(); self.successes.clear(); self.ui.errors.clear()
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        index=next(i for i,n in enumerate(tree.body) if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='backup_notice' for t in n.targets))
        exec(compile(ast.Module(body=tree.body[index:index+2],type_ignores=[]),str(SOURCE),'exec'),self.env)
        bodies=[value for parent in ast.walk(tree) for _,value in ast.iter_fields(parent)
                if isinstance(value,list)]
        for body in bodies:
            for i,node in enumerate(body):
                if (isinstance(node,ast.Assign) and isinstance(node.value,ast.Call)
                        and isinstance(node.value.func,ast.Name) and node.value.func.id=='current_backup_result'
                        and any(isinstance(t,ast.Name) and t.id=='result' for t in node.targets)):
                    exec(compile(ast.Module(body=body[i:i+4],type_ignores=[]),str(SOURCE),'exec'),self.env)
                    return
        self.fail('Admin notice block missing')

    def test_both_notices_clear_after_read_only_confirmation_and_reruns(self):
        with self.guard.exclusive():
            state=self.guard.load_state(); state['sync']={'kind':'pending','message':'old pending'}
            self.guard.save_state(state)
        self.ui.session_state['github_sync_result']=self.env['current_backup_result']()
        self.ui.session_state['github_sync_error_status']=self.guard.status()
        self.render_notices()
        self.assertEqual(len(self.warnings),1); self.assertEqual(len(self.ui.errors),1)
        self.guard.startup(True,self.remote.get)
        initialize=load_app_functions(['ensure_single_select_defaults'],db_connect=self.guard.connect)['ensure_single_select_defaults']
        # Restore test fixture already including all defaults, then confirm it.
        initialize(); self.assertTrue(self.guard.transfer(True,self.remote.get,self.remote.put))
        calls=self.remote.get_calls
        for _ in range(3):
            initialize(); self.guard.startup(True,self.remote.get); self.render_notices()
            self.assertEqual(self.warnings,[]); self.assertEqual(self.ui.errors,[])
            self.assertEqual(len(self.successes),1)
        self.assertEqual(self.remote.get_calls,calls)

    def test_real_unsynced_change_keeps_both_notices(self):
        self.write_item('not backed up')
        self.guard.startup(True,self.remote.get); self.render_notices()
        self.assertEqual(len(self.warnings),1); self.assertEqual(len(self.ui.errors),1)
        self.assertEqual(self.successes,[]); self.assertEqual(self.remote.put_calls,0)

    def test_session_lock_error_is_not_hidden_by_unchanged_old_success(self):
        self.ui.session_state['github_sync_result']=('error','fake lock timeout')
        self.ui.session_state['github_sync_error_status']=self.guard.status()
        self.render_notices()
        self.assertEqual(self.warnings,[])
        self.assertEqual(self.ui.errors,['fake lock timeout'])
        self.assertEqual(self.successes,[])
        # Even the same content/time can be newly confirmed by another session.
        self.assertTrue(self.guard.transfer(True,self.remote.get,self.remote.put))
        self.render_notices()
        self.assertEqual(self.ui.errors,[]); self.assertEqual(len(self.successes),1)

    def test_explicit_pending_retry_button_only_reads_remote(self):
        with self.guard.exclusive():
            state=self.guard.load_state(); state['sync']={'kind':'pending','message':'old pending'}
            self.guard.save_state(state)
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Call) and isinstance(n.test.func,ast.Attribute)
            and n.test.func.attr=='button' and n.test.args and isinstance(n.test.args[0],ast.Constant)
            and n.test.args[0].value=='🔎 唯讀核對 GitHub 備份')
        self.ui.clicked.add('🔎 唯讀核對 GitHub 備份'); before=self.snapshot()
        self.env.update(github_is_configured=lambda:True,github_get_backup=self.remote.get)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),self.env)
        self.assertEqual(self.remote.put_calls,0); self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual(self.ui.session_state['github_sync_result'][0],'success')


    def test_sync_failure_survives_unreadable_followup_status(self):
        env=load_app_functions(['sync_github_backup','current_backup_result'],st=self.ui,
            backup_guard=self.guard,github_is_configured=lambda:True,
            github_get_backup=self.remote.get,github_put_backup=self.remote.put)
        with patch.object(self.guard,'transfer',side_effect=RuntimeError('fake lock failure')):
            with patch.object(self.guard,'status',side_effect=sqlite3.OperationalError('fake read failure')):
                self.assertFalse(env['sync_github_backup']())
        self.assertEqual(self.ui.session_state['github_sync_result'][0],'error')
        self.assertIsNone(self.ui.session_state['github_sync_error_status'])
        self.assertEqual(self.remote.put_calls,0)


class StatsQueryCountTests(IsolatedTest):
    def setUp(self):
        super().setUp(); self.ready()
        self.queries=[]; self.observed=[]; self.ui=FakeUI(); self.markdown=[]
        self.ui.markdown=lambda text,**kw:self.markdown.append(text)
        def read(query,params=()):
            self.queries.append(params[0])
            conn=self.guard.connect()
            try:
                frame=pd.read_sql_query(query,conn,params=params)
                self.observed.append(frame['option_value'].tolist())
                return frame
            finally: conn.close()
        self.env=load_app_functions(['render_stats_section','get_options'],
            st=self.ui,pd=pd,html=__import__('html'),get_db=read,
            get_current_shop_names=lambda:{'main':'Fake','drink':'Fake'},
            taiwan_now_str=lambda:NOW)

    def render(self, count, category='主餐'):
        row=fixture()['orders'][0]; row.update(category=category,
            custom='無, 微辣' if category=='主餐' else 'L//去冰')
        self.env['get_orders_df']=lambda:pd.DataFrame([dict(row,id=i+1) for i in range(count)])
        self.env['render_stats_section']()

    def test_twenty_meals_read_two_option_categories_once_each(self):
        self.render(20)
        self.assertEqual(self.queries,['main_size','spicy'])
        self.assertTrue(any('共 20 份' in text for text in self.markdown))

    def test_twenty_drinks_read_three_option_categories_once_each(self):
        self.render(20,'飲料')
        self.assertEqual(self.queries,['drink_size','sugar','ice'])
        self.assertTrue(any('去冰' in text for text in self.markdown))

    def test_next_render_rereads_edited_settings(self):
        self.render(20)
        conn=self.guard.connect(write=True)
        try:
            conn.execute("INSERT INTO order_options(category,option_value,sort_order,is_default) VALUES('spicy','微辣',0,1)")
            conn.commit()
        finally: conn.close()
        self.render(20)
        self.assertEqual(self.queries,['main_size','spicy','main_size','spicy'])
        self.assertEqual(self.observed[-1],['微辣'])


class DeleteDialogUIAcceptance(IsolatedTest):
    def setUp(self):
        super().setUp()
        data=fixture(); other=copy.deepcopy(data['orders'][0]); other.update(id=2,is_paid=1,item_name='Fake paid untouched')
        data['orders'].append(other)
        self.remote=self.ready(data); self.before=self.snapshot()
        self.ui=FakeUI(); harness=OrderUIIntegrationTests(); harness.guard=self.guard
        self.env=harness.env(self.ui,[])
        self.env.update(load_app_functions(['sync_github_backup','current_backup_result'],
            **self.env,github_is_configured=lambda:True,
            github_get_backup=self.remote.get,github_put_backup=self.remote.put))

    def test_opener_captures_token_and_confirmation_is_explicit(self):
        request=open_delete_request(self.env,self.ui)
        self.assertEqual(request['token']['generation'],self.guard.order_snapshot()['generation'])
        self.env['delete_order_dialog'](request)
        button=next(kw for label,kw in self.ui.buttons if label=='確認刪除')
        self.assertEqual(button['type'],'primary'); self.assertTrue(button['use_container_width'])
        self.assertNotIn('icon',button)
        self.assertEqual(self.snapshot(),self.before); self.assertEqual(self.remote.put_calls,0)

    def test_cancel_closes_intent_without_writing_or_sync(self):
        request=open_delete_request(self.env,self.ui); state=self.guard.load_state()
        self.ui.clicked.add('取消'); self.env['delete_order_dialog'](request)
        self.assertEqual(self.ui.session_state['_closed_delete_intent'],request['intent'])
        self.assertNotIn('_delete_order_request',self.ui.session_state)
        self.assertEqual(self.snapshot(),self.before); self.assertEqual(self.guard.load_state(),state)
        self.assertEqual(self.remote.put_calls,0)

    def test_success_removes_only_target_and_closes_without_repeated_sync(self):
        request=open_delete_request(self.env,self.ui); self.ui.clicked.add('確認刪除')
        self.env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot()['orders'],[self.before['orders'][1]])
        self.assertEqual(self.remote.data['orders'],[self.before['orders'][1]])
        self.assertEqual(self.ui.session_state['_closed_delete_intent'],request['intent'])
        self.assertNotIn('_delete_order_request',self.ui.session_state)
        for _ in range(3): self.env['delete_order_dialog'](request)
        self.assertEqual(self.remote.put_calls,1)
        self.assertEqual(self.ui.errors,[])

    def test_sql_failure_keeps_orders_and_can_cancel(self):
        conn=self.guard.connect(write=True)
        try:
            conn.execute("CREATE TRIGGER fake_delete_failure BEFORE DELETE ON orders BEGIN SELECT RAISE(ABORT,'fake delete failure'); END")
            conn.commit()
        finally: conn.close()
        request=open_delete_request(self.env,self.ui); self.ui.clicked.add('確認刪除')
        self.env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot(),self.before); self.assertTrue(self.ui.errors)
        self.assertEqual(self.remote.put_calls,0)
        self.assertNotIn('_closed_delete_intent',self.ui.session_state)
        self.ui.clicked.clear(); self.ui.clicked.add('取消')
        self.env['delete_order_dialog'](request)
        self.assertEqual(self.ui.session_state['_closed_delete_intent'],request['intent'])

    def test_imported_same_id_rejects_original_intent(self):
        request=open_delete_request(self.env,self.ui)
        self.assertTrue(self.guard.transfer(True,self.remote.get,self.remote.put,imported=fixture('replacement')))
        before=self.snapshot(); puts=self.remote.put_calls
        self.ui.clicked.add('確認刪除'); self.env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot(),before); self.assertEqual(self.remote.put_calls,puts)
        self.assertTrue(self.ui.errors)

    def test_payment_after_open_prevents_delete(self):
        request=open_delete_request(self.env,self.ui)
        token=self.guard.order_token(self.guard.order_snapshot(),[1])
        self.guard.apply_order_action('payment',token); before=self.snapshot()
        self.ui.clicked.add('確認刪除'); self.env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot(),before); self.assertEqual(self.remote.put_calls,0)
        self.assertTrue(self.ui.errors)

    def test_sync_failure_still_closes_after_local_delete_and_warns(self):
        request=open_delete_request(self.env,self.ui); self.remote.fail_get=True
        self.ui.clicked.add('確認刪除'); self.env['delete_order_dialog'](request)
        self.assertEqual(self.snapshot()['orders'],[self.before['orders'][1]])
        self.assertEqual(self.ui.session_state['_closed_delete_intent'],request['intent'])
        self.assertTrue(any('本機已儲存' in text for text in self.ui.toasts))
        self.assertEqual(self.remote.put_calls,0)

    def test_dismiss_callback_clears_active_intent_without_writing(self):
        request=open_delete_request(self.env,self.ui); before=self.guard.load_state()
        self.env['dismiss_order_delete']()
        self.assertNotIn('_active_order_delete_request',self.ui.session_state)
        self.assertNotIn('_delete_order_request',self.ui.session_state)
        self.assertEqual(self.ui.session_state['_closed_delete_intent'],request['intent'])
        self.assertEqual(self.snapshot(),self.before); self.assertEqual(self.guard.load_state(),before)
        self.assertEqual(self.remote.put_calls,0)


class DeleteDialogAppTests(unittest.TestCase):
    """Real Streamlit dialog registration/reruns; no browser or full app."""
    def setUp(self):
        self.seed=DeleteDialogUIAcceptance(); self.seed.setUp()
        self.addCleanup(self.seed.doCleanups)

    def app(self):
        import tempfile
        previous=tempfile.tempdir; tempfile.tempdir=str(self.seed.db.parent)
        try:
            from streamlit.testing.v1 import AppTest
        finally: tempfile.tempdir=previous
        script="""import ast, html
import streamlit as st
import pandas as pd
from backup_safety import BackupGuard, OrderConflict, ORDER_FIELDS
from test_backup_safety import load_app_functions, FakeRemote, NOW, SOURCE
"""
        script+=f"backup_guard=BackupGuard({str(self.seed.db)!r},lambda:NOW)\n"
        script+=f"remote=st.session_state.setdefault('fake_remote',FakeRemote({self.seed.before!r}))\n"
        script+="""env=load_app_functions(['dismiss_order_delete','request_order_delete','delete_order_dialog','render_my_orders',
    'remember_order_token','execute_order_action','save_and_sync','show_save_feedback',
    'sync_github_backup','current_backup_result'],st=st,backup_guard=backup_guard,pd=pd,html=html,
    ORDER_COLUMNS=list(ORDER_FIELDS),OrderConflict=OrderConflict,
    github_is_configured=lambda:True,github_get_backup=remote.get,github_put_backup=remote.put,
    edit_request=None)
env['render_my_orders']('測試甲')
# Execute the real top-level delete dispatch, not a test-only visibility flag.
tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
for parent in ast.walk(tree):
    body=getattr(parent,'body',None)
    if not isinstance(body,list): continue
    for i,node in enumerate(body):
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='delete_request' for t in node.targets) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute) and node.value.func.attr=='pop':
            exec(compile(ast.Module(body=body[i:i+4],type_ignores=[]),str(SOURCE),'exec'),env)
"""
        path=self.seed.db.parent/'fake_single_delete_ui.py'; path.write_text(script,encoding='utf-8')
        app=AppTest.from_file(str(path)); app.secrets={'fake_only':True}; app.run()
        self.assertEqual(len(app.exception),0)
        return app

    def open(self,app):
        next(b for b in app.button if b.label=='🗑️').click().run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(len(app.get('dialog')),1)
        self.assertTrue(any(b.label=='確認刪除' for b in app.button))

    def test_success_closes_real_dialog_and_rerun_keeps_it_closed(self):
        app=self.app(); self.open(app)
        next(b for b in app.button if b.label=='確認刪除').click().run()
        self.assertEqual(len(app.exception),0)
        with self.assertRaises(KeyError):
            app.session_state['_active_order_delete_request']
        # AppTest retains fragment deltas until a complete app run; verify that
        # run explicitly rather than claiming a browser overlay was observed.
        app.run()
        self.assertEqual(len(app.get('dialog')),0)
        self.assertFalse(any(b.label=='確認刪除' for b in app.button))
        self.assertEqual(self.seed.snapshot()['orders'],[self.seed.before['orders'][1]])
        self.assertEqual(app.session_state['fake_remote'].put_calls,1)
        app.run()
        self.assertEqual(len(app.get('dialog')),0)
        self.assertEqual(app.session_state['fake_remote'].put_calls,1)

    def test_cancel_closes_real_dialog_without_data_changes(self):
        app=self.app(); self.open(app)
        next(b for b in app.button if b.label=='取消').click().run()
        self.assertEqual(len(app.exception),0)
        with self.assertRaises(KeyError):
            app.session_state['_active_order_delete_request']
        app.run()
        self.assertEqual(len(app.get('dialog')),0)
        self.assertEqual(self.seed.snapshot(),self.seed.before)
        self.assertEqual(app.session_state['fake_remote'].put_calls,0)

    def test_sql_failure_keeps_dialog_open_and_cancel_remains_available(self):
        conn=self.seed.guard.connect(write=True)
        try:
            conn.execute("CREATE TRIGGER fake_fail BEFORE DELETE ON orders BEGIN SELECT RAISE(ABORT,'fake failure'); END")
            conn.commit()
        finally: conn.close()
        app=self.app(); self.open(app)
        next(b for b in app.button if b.label=='確認刪除').click().run()
        self.assertEqual(len(app.exception),0); self.assertEqual(len(app.get('dialog')),1)
        self.assertTrue(app.error); self.assertEqual(self.seed.snapshot(),self.seed.before)
        self.assertEqual(app.session_state['fake_remote'].put_calls,0)
        next(b for b in app.button if b.label=='取消').click().run()
        with self.assertRaises(KeyError):
            app.session_state['_active_order_delete_request']
        app.run()
        self.assertEqual(len(app.get('dialog')),0)

    def test_version_and_native_confirmation_label(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        version=next(n.value.value for n in tree.body if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in n.targets))
        self.assertEqual(version,'v3.8.0')
        app=self.app(); self.open(app)
        confirm=next(b for b in app.button if b.label=='確認刪除')
        self.assertEqual(confirm.proto.type,'primary')
        self.assertNotIn('⭕',confirm.label)


class EntryRerun(Exception):
    pass


class EntryFragmentAcceptance(IsolatedTest):
    """Direct fragment-body tests; timer scheduling and browser clicks are not simulated."""
    def setUp(self):
        super().setUp()
        self.remote=self.ready(); self.ui=FakeUI()
        self.ui.session_state.update(m_name='Fake meal',d_name='Fake drink',m_price=100,d_price=50,
            m_qty=1,d_qty=1,m_custom_tags=[],m_custom_manual='',d_custom_tags=[],d_custom_manual='')
        self.ui.text_input=lambda label,**kw:self.ui.session_state.get(kw.get('key'),kw.get('value',''))
        self.ui.number_input=lambda label,**kw:self.ui.session_state.get(kw.get('key'),kw.get('value',0))
        self.scopes=[]
        def rerun(**kw):
            self.scopes.append(kw.get('scope','app')); raise EntryRerun()
        self.ui.rerun=rerun
        self.env=load_app_functions(['render_order_entry','db_connect','get_db','get_options',
            'get_default_option','get_shop_name','ensure_single_select_defaults','serialize_drink_options',
            'create_member_order','save_and_sync','sync_github_backup','current_backup_result','show_save_feedback'],
            st=self.ui,backup_guard=self.guard,pd=pd,html=__import__('html'),OrderConflict=OrderConflict,
            github_is_configured=lambda:True,github_get_backup=self.remote.get,github_put_backup=self.remote.put,
            taiwan_now=lambda:SimpleNamespace(strftime=lambda _:NOW))
        self.env['ensure_single_select_defaults']()
        self.assertTrue(self.guard.transfer(True,self.remote.get,self.remote.put))
        self.ui.session_state['member_token']={'member_id':1,'name':'測試甲',
            'generation':self.guard.member_snapshot()['generation']}
        self.before=self.snapshot(); self.calls=(self.remote.get_calls,self.remote.put_calls)

    def render(self):
        self.env['render_order_entry']()

    def test_price_sequences_keep_values_without_writes_or_sync(self):
        for key in ('m_price','d_price'):
            for count in (1,5,10,20):
                start=self.ui.session_state[key]
                for _ in range(count):
                    self.ui.session_state[key]+=5; self.render()
                self.assertEqual(self.ui.session_state[key],start+count*5)
        self.assertEqual(self.snapshot(),self.before)
        self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual((self.remote.get_calls,self.remote.put_calls),self.calls)
        self.assertEqual(self.scopes,[])

    def test_create_both_categories_uses_last_price_quantity_and_custom_once(self):
        for category,label,prefix,custom in [('主餐','＋ 加入主餐','m','Fake meal custom'),
                                             ('飲料','＋ 加入飲料','d','L/無糖/去冰, Fake drink custom')]:
            self.ui.session_state.update({prefix+'_price':135,prefix+'_qty':3,
                prefix+'_custom_manual':'Fake meal custom' if prefix=='m' else 'Fake drink custom'})
            self.ui.session_state.update(m_size='無',m_spicy='無',d_size='L',d_sugar='無糖',d_ice='去冰')
            self.ui.clicked={label}; puts=self.remote.put_calls
            with self.assertRaises(EntryRerun): self.render()
            row=self.snapshot()['orders'][-1]
            self.assertEqual((row['category'],row['price'],row['unit_price'],row['quantity'],row['custom']),
                             (category,405,135,3,custom))
            self.assertEqual(self.remote.put_calls,puts+1)
            self.assertEqual(self.guard.status()['kind'],'success')
            self.ui.clicked.clear(); self.render()
            self.assertEqual(self.remote.put_calls,puts+1)
            self.assertEqual(self.scopes[-1],'app')

    def test_custom_dialog_open_intents_escape_to_app_without_writes(self):
        original=self.ui.button
        for key,prefix in [('btn_m_custom','m_custom'),('btn_d_custom','d_custom')]:
            self.ui.button=lambda label,**kw:kw.get('key')==key
            with self.assertRaises(EntryRerun):self.render()
            self.assertEqual(self.ui.session_state.pop('_entry_custom_request'),prefix)
        self.ui.button=original
        self.assertEqual(self.snapshot(),self.before)
        self.assertEqual((self.remote.get_calls,self.remote.put_calls),self.calls)
        self.assertEqual(self.scopes,['app','app'])

    def test_invalid_member_still_blocks_creation_and_sync(self):
        self.ui.session_state.pop('member_token'); self.ui.clicked={'＋ 加入主餐'}
        self.render()
        self.assertTrue(self.ui.errors); self.assertEqual(self.snapshot(),self.before)
        self.assertEqual((self.remote.get_calls,self.remote.put_calls),self.calls)

    def test_entry_fragment_boundary_and_original_refresh_intervals(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        functions={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
        entry=functions['render_order_entry']
        self.assertEqual(ast.unparse(entry.decorator_list[0]),'st.fragment')
        for key in ('m_price','d_price'):
            call=next(n for n in ast.walk(entry) if isinstance(n,ast.Call)
                and any(k.arg=='key' and isinstance(k.value,ast.Constant) and k.value.value==key for k in n.keywords))
            self.assertEqual(next(k.value.value for k in call.keywords if k.arg=='step'),5)
        names={n.func.id for n in ast.walk(entry) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
        self.assertFalse(names.intersection({'render_stats_section','render_payment_section','render_my_orders','init_db','ensure_single_select_defaults'}))
        timed={n.name:ast.literal_eval(n.decorator_list[0].keywords[0].value)
            for n in functions.values() if n.decorator_list and isinstance(n.decorator_list[0],ast.Call)
            and any(k.arg=='run_every' for k in n.decorator_list[0].keywords)}
        self.assertEqual(timed,{'render_stats_section':'10s','render_payment_section':'10s'})
        self.assertFalse(functions['render_my_orders'].decorator_list)

    def test_repeated_refresh_reads_preserve_data_status_and_remote_calls(self):
        env=load_app_functions(['render_stats_section','render_payment_section','_pay_logic_grouped',
            'get_orders_df','get_current_shop_names','remember_order_token'],**self.env,
            ORDER_COLUMNS=list(ORDER_FIELDS),taiwan_now_str=lambda:NOW)
        for _ in range(5):
            env['render_stats_section']();env['render_payment_section']();self.render()
        self.assertEqual(self.snapshot(),self.before)
        self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual((self.remote.get_calls,self.remote.put_calls),self.calls)

        __import__('gc').collect()

    def test_refresh_during_slow_sync_preserves_data_and_does_not_sync_again(self):
        import time
        started=threading.Event(); results=[]
        def slow_get():
            started.set();time.sleep(.15);return self.remote.get()
        def synchronize():results.append(self.guard.transfer(True,slow_get,self.remote.put))
        env=load_app_functions(['render_payment_section','_pay_logic_grouped','get_current_shop_names',
            'remember_order_token'],**self.env,ORDER_COLUMNS=list(ORDER_FIELDS),taiwan_now_str=lambda:NOW)
        worker=threading.Thread(target=synchronize);worker.start()
        try:
            self.assertTrue(started.wait(3))
            begin=time.perf_counter();env['render_payment_section']()
            elapsed=(time.perf_counter()-begin)*1000
        finally:worker.join(5)
        self.assertFalse(worker.is_alive());self.assertEqual(results,[True])
        self.assertEqual(self.snapshot(),self.before)
        self.assertEqual(self.guard.status()['kind'],'success')
        self.assertEqual((self.remote.get_calls,self.remote.put_calls),(self.calls[0]+1,self.calls[1]+1))
        print('FAKE REFRESH DURING SYNC:',round(elapsed,2),'ms; one GET/PUT from sync only')
        __import__('gc').collect()


class EntryFragmentAppTests(IsolatedTest):
    """Real widget registration and dialog state; AppTest is not a timed browser dispatcher."""
    def run_app(self,app):
        # AppTest's ButtonGroup expects a list; production single-select pills
        # return a scalar. Supply the equivalent browser selection explicitly.
        for group in app.get('button_group'):
            value=app.session_state[group.key]
            group.set_value([value] if isinstance(value,str) else ([] if value is None else value))
        app.run()
        return app

    def app(self):
        self.remote=self.ready()
        ui=FakeUI()
        env=load_app_functions(['ensure_single_select_defaults','db_connect'],backup_guard=self.guard,st=ui)
        env['ensure_single_select_defaults']()
        self.guard.transfer(True,self.remote.get,self.remote.put)
        import tempfile
        previous=tempfile.tempdir; tempfile.tempdir=str(self.db.parent)
        try:
            from streamlit.testing.v1 import AppTest
        finally: tempfile.tempdir=previous
        names=['render_order_entry','get_db','db_connect','get_options','get_default_option','get_shop_name',
            'serialize_drink_options','create_member_order','save_and_sync','sync_github_backup',
            'current_backup_result','show_save_feedback','open_drink_custom_dialog','custom_dialog']
        script="""import streamlit as st
import pandas as pd
import sqlite3,html,time
from types import SimpleNamespace
from backup_safety import BackupGuard,OrderConflict
from test_backup_safety import load_app_functions,FakeRemote,NOW
G=BackupGuard(DB,lambda:NOW)
if 'remote' not in st.session_state:
 st.session_state['remote']=FakeRemote(G.export())
 st.session_state['remote'].sha=G.load_state()['remote_sha']
remote=st.session_state['remote']
member=G.member_snapshot()
st.session_state.setdefault('member_token',{'member_id':1,'name':'測試甲','generation':member['generation']})
for key,value in dict(m_name='Fake meal',d_name='Fake drink',m_custom_tags=[],m_custom_manual='',d_custom_tags=[],d_custom_manual='').items():st.session_state.setdefault(key,value)
env=load_app_functions(NAMES,st=st,pd=pd,html=html,backup_guard=G,OrderConflict=OrderConflict,
 github_is_configured=lambda:True,github_get_backup=remote.get,github_put_backup=remote.put,
 taiwan_now=lambda:SimpleNamespace(strftime=lambda _:NOW))
env['render_order_entry']()
request=st.session_state.pop('_entry_custom_request',None)
if request=='m_custom':env['custom_dialog']('m_custom',env['get_options']('tags'))
elif request=='d_custom':env['open_drink_custom_dialog'](env['get_options']('drink_tags'))
"""
        path=self.db.parent/'fake_entry_only.py'
        path.write_text('DB='+repr(str(self.db))+'\nNAMES='+repr(names)+'\n'+script,encoding='utf-8')
        app=AppTest.from_file(str(path));app.secrets={'fake_only':True};app.run()
        self.assertEqual(len(app.exception),0);return app

    def test_main_and_drink_prices_survive_dialog_open_and_committed_state_refresh(self):
        app=self.app()
        app.number_input(key='m_price').set_value(135);self.run_app(app)
        app.number_input(key='d_price').set_value(65);self.run_app(app)
        app.button(key='btn_d_custom').click();self.run_app(app)
        self.assertEqual(len(app.exception),0);self.assertEqual(len(app.get('dialog')),1)
        self.assertEqual(app.text_input(key='d_custom_manual_widget').value,'')
        # AppTest dispatches a full run rather than a dialog fragment event.
        # Existing DrinkDialogStateTests cover draft completion separately.
        app.session_state['d_custom_manual']='Fake extra'
        self.run_app(app)
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.number_input(key='m_price').value,135)
        self.assertEqual(app.number_input(key='d_price').value,65)
        self.assertEqual(app.session_state['d_custom_manual'],'Fake extra')
        self.assertEqual(app.session_state['remote'].put_calls,0)

    def test_latest_widget_price_and_quantity_create_and_sync_both_categories(self):
        app=self.app()
        for prefix,label in [('m','＋ 加入主餐'),('d','＋ 加入飲料')]:
            app.number_input(key=prefix+'_price').set_value(125);self.run_app(app)
            app.number_input(key=prefix+'_qty').set_value(2);self.run_app(app)
            before=app.session_state['remote'].put_calls
            next(b for b in app.button if b.label==label).click();self.run_app(app)
            self.assertEqual(len(app.exception),0)
            row=self.snapshot()['orders'][-1]
            self.assertEqual((row['unit_price'],row['quantity'],row['price']),(125,2,250))
            self.assertEqual(app.session_state['remote'].put_calls,before+1)
            self.run_app(app);self.assertEqual(app.session_state['remote'].put_calls,before+1)

    def test_clear_custom_does_not_reset_price_or_sync(self):
        app=self.app();app.number_input(key='d_price').set_value(75);self.run_app(app)
        app.session_state['d_custom_manual']='Fake committed';self.run_app(app)
        app.button(key='clr_d_custom').click();self.run_app(app)
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.number_input(key='d_price').value,75)
        self.assertEqual(app.session_state['d_custom_manual'],'')
        self.assertEqual(app.session_state['remote'].put_calls,0)
