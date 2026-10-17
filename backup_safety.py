"""Backup coordination for ONE shared SQLite file; no network or secrets here.

The local sidecar contains only statuses, GitHub SHAs and business-data hashes.
BEGIN IMMEDIATE coordinates application writes, import and sync across sessions
and processes using this same database. It is not a multi-instance cloud lock.
"""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import errno
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


TABLES = ("orders", "order_members", "order_options", "config_shop")
OPTION_CATEGORIES = {"main_size", "spicy", "drink_size", "ice", "sugar", "tags", "drink_tags"}
ORDER_FIELDS = ("id", "name", "category", "item_name", "price", "custom", "quantity", "order_time", "is_paid", "unit_price")


class BackupError(ValueError):
    pass


class SafetyBlocked(RuntimeError):
    pass


class OrderConflict(RuntimeError):
    """The displayed order identity/state is no longer safe to write."""


class RemoteRejected(RuntimeError):
    """A definite HTTP rejection, with a safe message (never an API body)."""


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise BackupError(f"{label} 必須是大於等於 {minimum} 的整數。")
    return value


def _text(value, label, blank=False):
    if not isinstance(value, str) or (not blank and not value.strip()):
        raise BackupError(f"{label} 必須是{'可空白的' if blank else '非空白'}文字。")
    return value


def parse_backup(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BackupError("備份必須使用 UTF-8 編碼。") from exc
    try:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise BackupError("JSON 欄位重複，無法確認資料內容。")
                result[key] = value
            return result
        data = json.loads(text, object_pairs_hook=unique_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BackupError("備份不是有效的 JSON。") from exc
    return validate_backup(data)


def validate_backup(data):
    """Validate before DELETE; support 4.1 and approved unversioned legacy data.

    Legacy defaults are the same as the baseline importer: missing sort_order
    uses list position, string options have is_default=0, missing custom/paid/
    unit_price become empty/0/None. Deleted members' historic orders are legal.
    """
    if not isinstance(data, dict) or not data:
        raise BackupError("備份不可為空物件，且必須是 JSON 物件。")
    if data.get("format") not in (None, "office-order-backup"):
        raise BackupError("未知備份格式；請先確認相容規則。")
    version = data.get("version")
    if version is not None and version != "4.1":
        raise BackupError("未知備份版本；請先確認相容規則，原資料未替換。")
    if version == "4.1" and data.get("format") != "office-order-backup":
        raise BackupError("4.1 備份缺少正確的 format。")
    for key, typ in (("members", list), ("options", dict), ("config_shop", list), ("orders", list)):
        if key not in data or not isinstance(data[key], typ):
            raise BackupError(f"備份缺少必要欄位 {key} 或資料型別錯誤。")
    result = copy.deepcopy(data)
    if version == "4.1":
        if "exported_at" not in result:
            raise BackupError("4.1 備份缺少 exported_at。")
        _text(result["exported_at"], "匯出時間")
        try:
            datetime.fromisoformat(result["exported_at"])
        except ValueError as exc:
            raise BackupError("匯出時間格式錯誤。") from exc
    names, member_ids = set(), set()
    for index, member in enumerate(result["members"]):
        if not isinstance(member, dict) or "name" not in member:
            raise BackupError("人員資料缺少 name 或型別錯誤。")
        if version == "4.1" and {"id", "sort_order"} - member.keys():
            raise BackupError("4.1 人員資料缺少 id／sort_order。")
        name = _text(member["name"], "人員姓名").strip()
        if name in names:
            raise BackupError("人員姓名重複。")
        names.add(name)
        member["name"] = name
        member["sort_order"] = _integer(member.get("sort_order", index), "人員排序")
        if "id" in member:
            mid = _integer(member["id"], "人員 ID", 1)
            if mid in member_ids:
                raise BackupError("人員 ID 重複。")
            member_ids.add(mid)
    for category, values in result["options"].items():
        if category not in OPTION_CATEGORIES or not isinstance(values, list):
            raise BackupError("客製選項類別未知或資料型別錯誤。")
        seen, defaults, cleaned = set(), 0, []
        for option in values:
            if isinstance(option, str):
                value, default = option, 0
            elif isinstance(option, dict) and "value" in option:
                value, default = option["value"], option.get("is_default", 0)
            else:
                raise BackupError("客製選項缺少 value 或資料型別錯誤。")
            value = _text(value, "客製選項").strip()
            if type(default) not in (int, bool) or default not in (0, 1):
                raise BackupError("客製預設狀態必須是 0 或 1。")
            if value in seen:
                raise BackupError("同類別客製選項重複。")
            seen.add(value)
            defaults += int(default)
            cleaned.append({"value": value, "is_default": int(default)})
        if defaults > 1:
            raise BackupError("同類別客製選項不可有多個預設值。")
        result["options"][category] = cleaned
    shops = set()
    for shop in result["config_shop"]:
        if not isinstance(shop, dict) or set(("category", "shop_name")) - shop.keys():
            raise BackupError("店家資料缺少 category／shop_name。")
        cat = shop["category"]
        if not isinstance(cat, str) or cat not in ("main", "drink") or cat in shops:
            raise BackupError("店家類別未知或重複。")
        shops.add(cat)
        shop["shop_name"] = _text(shop["shop_name"], "店家名稱", blank=True).strip()
    if shops != {"main", "drink"}:
        raise BackupError("店家資料必須包含 main 與 drink，名稱可空白。")
    ids = set()
    required = {"id", "name", "category", "item_name", "price", "quantity", "order_time"}
    if version == "4.1":
        required |= {"custom", "is_paid", "unit_price"}
    for order in result["orders"]:
        if not isinstance(order, dict) or required - order.keys():
            raise BackupError("訂單缺少必要欄位或資料型別錯誤。")
        oid = _integer(order["id"], "訂單 ID", 1)
        if oid in ids:
            raise BackupError("訂單 ID 重複。")
        ids.add(oid)
        for key in ("name", "item_name", "order_time"):
            _text(order[key], f"訂單 {key}")
        if not isinstance(order["category"], str) or order["category"] not in ("主餐", "飲料"):
            raise BackupError("訂單類別必須是主餐或飲料。")
        try:
            datetime.fromisoformat(order["order_time"])
        except ValueError as exc:
            raise BackupError("訂單時間格式錯誤。") from exc
        qty = _integer(order["quantity"], "訂單數量", 1)
        price = _integer(order["price"], "訂單總額")
        paid = order.get("is_paid", 0)
        if type(paid) not in (int, bool) or paid not in (0, 1):
            raise BackupError("付款狀態必須是 0 或 1。")
        order["is_paid"] = int(paid)
        order["custom"] = _text(order.get("custom", ""), "客製需求", blank=True)
        unit = order.get("unit_price")
        if unit is not None:
            _integer(unit, "訂單單價")
            if unit * qty != price:
                raise BackupError("訂單單價、數量與總額不一致。")
        order["unit_price"] = unit
    return result


def export_snapshot(conn, timestamp):
    """Caller holds a read transaction or BEGIN IMMEDIATE across all SELECTs."""
    conn.row_factory = sqlite3.Row
    data = {"format": "office-order-backup", "version": "4.1", "exported_at": timestamp,
            "members": [], "options": {}, "config_shop": [], "orders": []}
    data["members"] = [dict(row) for row in conn.execute(
        "SELECT id,name,sort_order FROM order_members ORDER BY sort_order,id")]
    for row in conn.execute("SELECT category,option_value,is_default FROM order_options ORDER BY category,sort_order,id"):
        data["options"].setdefault(row["category"], []).append(
            {"value": row["option_value"], "is_default": int(row["is_default"] or 0)})
    data["config_shop"] = [dict(row) for row in conn.execute("SELECT category,shop_name FROM config_shop ORDER BY category")]
    data["orders"] = [dict(row) for row in conn.execute(
        "SELECT id,name,category,item_name,price,custom,quantity,order_time,is_paid,unit_price FROM orders ORDER BY id")]
    return data


def replace_snapshot(conn, data):
    """No commit here: replacement and its eventual rollback are ONE transaction."""
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")
    for member in data["members"]:
        conn.execute("INSERT INTO order_members(name,sort_order) VALUES(?,?)", (member["name"], member["sort_order"]))
    for cat, values in data["options"].items():
        for index, option in enumerate(values):
            conn.execute("INSERT INTO order_options(category,option_value,sort_order,is_default) VALUES(?,?,?,?)",
                         (cat, option["value"], index, option["is_default"]))
    for shop in data["config_shop"]:
        conn.execute("INSERT INTO config_shop(category,shop_name) VALUES(?,?)", (shop["category"], shop["shop_name"]))
    for order in data["orders"]:
        conn.execute("INSERT INTO orders(id,name,category,item_name,price,custom,quantity,order_time,is_paid,unit_price) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     tuple(order[key] for key in ("id", "name", "category", "item_name", "price", "custom", "quantity", "order_time", "is_paid", "unit_price")))


def content_digest(data):
    """Compare business content, not export time, member surrogate IDs or envelope."""
    data = validate_backup(data)
    business = {key: data[key] for key in ("members", "options", "config_shop", "orders")}
    business["members"] = sorted(
        [{"name": row["name"], "sort_order": row["sort_order"]} for row in data["members"]], key=lambda row: row["sort_order"])
    business["config_shop"] = sorted(data["config_shop"], key=lambda row: row["category"])
    business["orders"] = sorted(data["orders"], key=lambda row: row["id"])
    return hashlib.sha256(json.dumps(business, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class _WriteConnection(sqlite3.Connection):
    guard = None
    file_lock = None

    def business_digest(self):
        # Snapshot helpers use sqlite3.Row; preserve callers' cursor semantics.
        row_factory = self.row_factory
        try:
            snapshot = export_snapshot(self, self.guard.timestamp())
            # Detect stored changes without adding new validation rules to local
            # writes. Remote confirmation still uses strict content_digest().
            business = {key: snapshot[key] for key in ("members", "options", "config_shop", "orders")}
            return hashlib.sha256(json.dumps(business, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
        finally:
            self.row_factory = row_factory

    def commit(self):
        digest = self.business_digest() if self.in_transaction else self.before_digest
        if self.in_transaction and digest != self.before_digest:
            state = self.guard.load_state()
            state["sync"] = {"kind": "pending", "message": "本機資料已變更，尚未確認 GitHub 備份。"}
            state.pop("pending_check", None)
            self.guard.save_state(state)  # durable dirty marker BEFORE real commit
        super().commit()
        self.before_digest = digest  # advance only after SQLite confirms commit

    def __exit__(self, exc_type, exc, traceback):
        # sqlite3's C context manager otherwise bypasses the Python override.
        if exc_type is not None:
            self.rollback()
        else:
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        return False

    def close(self):
        try:
            super().close()
        finally:
            if self.file_lock is not None:
                self.file_lock.__exit__(None, None, None)
                self.file_lock = None


class BackupGuard:
    def __init__(self, db_file, timestamp, timeout=10):
        self.db_file = str(Path(db_file).resolve())
        self.state_path = Path(self.db_file + ".safety.json")
        self.timestamp = timestamp
        self.timeout = timeout

    def load_state(self):
        if not self.state_path.exists():
            return {"state_version": 1, "restore": "ready", "adoption_blocked": False, "baseline_known": False, "remote_sha": None,
                    "attempt": None, "sync": {"kind": "pending", "message": "尚未確認 GitHub 備份。"}}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or state.get("state_version") != 1
                    or state.get("restore") not in ("pending", "ready")
                    or type(state.get("adoption_blocked")) is not bool
                    or type(state.get("baseline_known")) is not bool
                    or "remote_sha" not in state or "attempt" not in state
                    or (state["remote_sha"] is not None and not isinstance(state["remote_sha"], str))
                    or not isinstance(state.get("sync"), dict)
                    or state["sync"].get("kind") not in ("pending", "error", "success")
                    or not isinstance(state["sync"].get("message"), str)):
                raise ValueError("invalid state")
            attempt = state["attempt"]
            generation = state.get("order_generation")
            if "order_generation" in state and (not isinstance(generation, str) or len(generation) != 32
                    or any(char not in "0123456789abcdef" for char in generation)):
                raise ValueError("invalid order generation")
            if attempt is not None and (not isinstance(attempt, dict) or attempt.get("kind") not in ("sync", "import")
                    or any(not isinstance(attempt.get(key), str) or len(attempt[key]) != 64
                           for key in ("before", "target"))):
                raise ValueError("invalid attempt")
            if state["sync"]["kind"] == "success":
                status = state["sync"]
                if (not isinstance(status.get("digest"), str) or len(status["digest"]) != 64
                        or not isinstance(status.get("at"), str)
                        or any(type(status.get(key)) is not int or status[key] < 0 for key in ("members", "orders"))):
                    raise ValueError("invalid confirmed status")
            return state
        except (ValueError, OSError) as exc:
            raise SafetyBlocked("本機安全狀態無法讀取；停止寫入，請保留狀態檔並檢查。") from exc

    def save_state(self, state):
        # Same-db writers hold BEGIN IMMEDIATE while updating this journal.
        fd, name = tempfile.mkstemp(prefix=self.state_path.name + ".", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def file_lock(self):
        # Preserve coordination across SQLite commit/rollback and sidecar writes.
        # Advisory OS locks are released on process death. Same local filesystem
        # only: do not use this as a distributed/cloud-instance lock.
        with open(str(self.db_file) + ".safety.lock", "a+b") as stream:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise sqlite3.OperationalError("database is locked (backup coordination)") from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def connect(self, write=False):
        conn = sqlite3.connect(self.db_file, timeout=self.timeout, check_same_thread=False,
                               factory=_WriteConnection if write else sqlite3.Connection)
        if write:
            try:
                conn.file_lock = self.file_lock()
                conn.file_lock.__enter__()
                conn.execute("BEGIN IMMEDIATE")
                state = self.load_state()
                if state["restore"] != "ready" or state.get("attempt") or state["adoption_blocked"]:
                    raise SafetyBlocked("還原或遠端寫入結果尚未確認，已阻止資料修改；請重新整理後重試核對。")
                conn.guard = self
                conn.before_digest = conn.business_digest()
            except BaseException:
                conn.close()
                raise
        return conn

    @contextmanager
    def exclusive(self):
        with self.file_lock():
            conn = sqlite3.connect(self.db_file, timeout=self.timeout)
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
            finally:
                conn.close()  # uncommitted transactions are rolled back, never replayed

    def prepare(self, schema_initializer):
        with self.exclusive() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            existing = tables.intersection(TABLES)
            if existing and existing != set(TABLES):
                raise SafetyBlocked("SQLite 業務資料表不完整；請先確認資料來源，未自動覆寫或還原。")
            state = self.load_state()
            if "order_generation" not in state or not existing:
                # Upgrade only the local journal; no business schema/backup change.
                state["order_generation"] = uuid.uuid4().hex
                self.save_state(state)
            if existing and state.get("attempt"):
                return state["restore"] == "pending"
            if not existing:
                state.update(restore="pending", adoption_blocked=False, baseline_known=False, remote_sha=None, attempt=None)
                self.save_state(state)  # survives a crash after schema creation
            elif not self.state_path.exists():
                state["adoption_blocked"] = True
                self.save_state(state)  # pre-existing DB needs conservative adoption
            schema_initializer(conn)
            conn.commit()
            return state["restore"] == "pending"

    def startup(self, configured, remote_get):
        with self.exclusive() as conn:
            state = self.load_state()
            if state.get("attempt"):
                return self._reconcile(conn, state, remote_get)
            if state["restore"] == "ready":
                if not configured:
                    state["adoption_blocked"] = False
                    state["sync"] = {"kind": "error", "message": "未設定 GitHub；資料僅儲存在本機。"}
                    self.save_state(state)
                    return "local_only"
                if not state.get("baseline_known"):
                    try:
                        remote, sha = remote_get()
                        local = export_snapshot(conn, self.timestamp())
                        if remote is None:
                            state.update(baseline_known=True, remote_sha=None, adoption_blocked=False)
                            state["sync"] = {"kind": "pending", "message": "已確認遠端備份不存在，等待首次同步。"}
                            self.save_state(state)
                            return "missing"
                        if content_digest(remote) != content_digest(local):
                            state["adoption_blocked"] = True
                            state["sync"] = {"kind": "error", "message": "首次接管時本機與遠端資料不同；已保留雙方，請先人工核對資料來源，禁止自動覆寫。"}
                            self.save_state(state)
                            return "adoption_conflict"
                        state.update(baseline_known=True, remote_sha=sha, adoption_blocked=False)
                        self._success(state, local)
                        self.save_state(state)
                        return "adopted"
                    except Exception:
                        state["adoption_blocked"] = True
                        state["sync"] = {"kind": "error", "message": "首次接管無法確認遠端資料；已保留本機資料，請重試核對。"}
                        self.save_state(state)
                        return "adoption_failed"
                self._check_pending(conn, state, remote_get)
                return "ready"
            if not configured:
                # Preserve baseline's local-only setup. Later configuration must
                # sync this existing local data, not silently restore over it.
                state["restore"] = "ready"
                state["sync"] = {"kind": "error", "message": "未設定 GitHub；資料僅儲存在本機。"}
                self.save_state(state)
                return "local_only"
            try:
                data, sha = remote_get()  # None is ONLY verified file absence
                if data is None:
                    state.update(restore="ready", baseline_known=True, remote_sha=None)
                    state["sync"] = {"kind": "pending", "message": "已確認遠端備份不存在，等待首次同步。"}
                    self.save_state(state)
                    return "missing"
                data = validate_backup(data)
                self._replace_snapshot(conn, data, state)
                snapshot = export_snapshot(conn, self.timestamp())
                # Journal restored content before commit. A crash can safely
                # retry because no user writes are allowed while pending.
                conn.commit()
                state.update(restore="ready", baseline_known=True, remote_sha=sha)
                self._success(state, snapshot)
                self.save_state(state)
                return "restored"
            except Exception as exc:
                conn.rollback()
                detail = str(exc) if isinstance(exc, BackupError) else ("讀取逾時" if isinstance(exc, TimeoutError) else "讀取／資料庫交易失敗")
                state["sync"] = {"kind": "error", "message": f"首次還原失敗（{detail}）；已保留重試狀態，未上傳初始化資料。"}
                self.save_state(state)
                return "failed"

    def check_pending(self, configured, remote_get, retry=False):
        """Read-only remote verification; never uploads or replaces local data."""
        if not configured:
            return False
        with self.exclusive() as conn:
            return self._check_pending(conn, self.load_state(), remote_get, retry)

    def _check_pending(self, conn, state, remote_get, retry=False):
        if (state["restore"] != "ready" or state["adoption_blocked"]
                or not state["baseline_known"] or state.get("attempt")
                or state["sync"]["kind"] != "pending"):
            return False
        local = export_snapshot(conn, self.timestamp())
        check = {"digest": content_digest(local), "remote_sha": state["remote_sha"]}
        if not retry and state.get("pending_check") == check:
            return False
        # Durable before GET: reruns/process restarts cannot cause a retry storm.
        # A new local change clears this record; failures need explicit retry.
        state["pending_check"] = check
        self.save_state(state)
        try:
            remote, sha = remote_get()
            remote = validate_backup(remote)
            if not isinstance(sha, str) or not sha:
                raise BackupError("缺少可確認的遠端版本。")
            if content_digest(remote) == check["digest"]:
                state.update(remote_sha=sha, baseline_known=True)
                self._success(state, local)
                self.save_state(state)
                return True
            message = "本機與 GitHub 備份不同；已保留雙方，請核對後再同步。"
        except Exception:
            message = "GitHub 備份唯讀核對未成功；本機資料已保留，請重試核對。"
        state["sync"] = {"kind": "pending", "message": message}
        self.save_state(state)
        return False

    def _success(self, state, snapshot):
        state.pop("pending_check", None)
        state["sync"] = {"kind": "success", "message": "本機資料與 GitHub 備份已確認一致。",
                         "confirmation_id": uuid.uuid4().hex,
                         "at": self.timestamp(), "digest": content_digest(snapshot),
                         "members": len(snapshot["members"]), "orders": len(snapshot["orders"])}
        state["attempt"] = None

    def _reconcile(self, conn, state, remote_get):
        attempt = state["attempt"]
        try:
            remote, sha = remote_get()
            local = export_snapshot(conn, self.timestamp())
            if remote is not None and content_digest(remote) == attempt["target"]:
                if attempt["kind"] == "import":
                    if content_digest(local) not in (attempt["before"], attempt["target"]):
                        raise SafetyBlocked("本機資料已在待確認期間變更，不能自動替換。")
                    if content_digest(local) != attempt["target"]:
                        self._replace_snapshot(conn, validate_backup(remote), state)
                    snapshot = export_snapshot(conn, self.timestamp())
                    conn.commit()
                elif content_digest(local) != attempt["target"]:
                    raise SafetyBlocked("本機資料與待確認備份不同，不能自動覆寫遠端。")
                else:
                    snapshot = local
                state.update(remote_sha=sha, baseline_known=True)
                self._success(state, snapshot)
                self.save_state(state)
                return "reconciled"
        except Exception:
            conn.rollback()
        # Seeing the old SHA immediately after timeout doesn't prove the PUT
        # cannot finish later. Retain the journal and NEVER resend blindly.
        state["sync"] = {"kind": "error", "message": "GitHub 寫入結果仍不明；已保留原資料並暫停寫入，請重試核對。"}
        self.save_state(state)
        return "uncertain"

    def transfer(self, configured, remote_get, remote_put, imported=None):
        # Validation completes before acquiring a write transaction or DELETE.
        if imported is not None:
            imported = validate_backup(imported)
        with self.exclusive() as conn:
            state = self.load_state()
            if state["restore"] != "ready":
                raise SafetyBlocked("首次還原尚未完成，禁止上傳或匯入。")
            if state["adoption_blocked"]:
                raise SafetyBlocked("首次接管尚未確認資料來源，禁止上傳或匯入；請先人工核對。")
            if state.get("attempt"):
                if imported is not None:
                    raise SafetyBlocked("先核對尚未確認的備份結果；這次匯入尚未執行，請重新整理後再確認。")
                result = self._reconcile(conn, state, remote_get)
                return result == "reconciled"
            if not configured:
                state["sync"] = {"kind": "error", "message": "未設定 GitHub；匯入未執行／本機變更尚未備份。"}
                self.save_state(state)
                return False
            before = export_snapshot(conn, self.timestamp())
            try:
                remote, sha = remote_get()
                if remote is not None:
                    validate_backup(remote)
                if not state.get("baseline_known") and remote is not None and content_digest(remote) != content_digest(before):
                    raise RemoteRejected("尚未建立可信同步基準，且本機與遠端不同；禁止自動覆寫，請先人工核對。")
                if state.get("baseline_known") and sha != state.get("remote_sha"):
                    raise RemoteRejected("遠端備份版本已改變，請先核對；未覆寫遠端。")
                if imported is not None:
                    self._replace_snapshot(conn, imported, state)
                snapshot = export_snapshot(conn, self.timestamp())
                # A stale snapshot is never supplied by a caller: it is built
                # AFTER this same-file exclusive write lock has been acquired.
                state["attempt"] = {"kind": "import" if imported is not None else "sync",
                                    "before": content_digest(before), "target": content_digest(snapshot)}
                state["sync"] = {"kind": "error", "message": "GitHub 備份正在確認；尚未宣告同步成功。"}
                self.save_state(state)
                try:
                    new_sha = remote_put(snapshot, sha)
                    if not isinstance(new_sha, str) or not new_sha:
                        raise RuntimeError("missing confirmed SHA")
                except RemoteRejected:
                    raise
                except Exception:
                    conn.rollback()  # original data, never compensating DELETE
                    conn.execute("BEGIN IMMEDIATE")
                    result = self._reconcile(conn, state, remote_get)
                    return result == "reconciled"
                conn.commit()
                state.update(remote_sha=new_sha, baseline_known=True)
                self._success(state, snapshot)
                self.save_state(state)
                return True
            except Exception as exc:
                conn.rollback()
                # An attempted PUT journal must survive unexpected local
                # commit/status errors too; reconcile before further writes.
                if not isinstance(exc, RemoteRejected) and state.get("attempt"):
                    state["sync"] = {"kind": "error", "message": "本機提交／同步確認失敗；請先核對遠端結果。"}
                else:
                    state["attempt"] = None
                    state["sync"] = {"kind": "error", "message": str(exc) if isinstance(exc, RemoteRejected) else "備份讀取或匯入交易失敗，原資料已保留。"}
                self.save_state(state)
                return False

    def export(self):
        conn = sqlite3.connect(self.db_file, timeout=self.timeout)
        try:
            conn.execute("BEGIN")
            return export_snapshot(conn, self.timestamp())
        finally:
            conn.close()

    def _replace_snapshot(self, conn, data, state):
        # Persist a new identity generation BEFORE replacement can commit.
        # A rollback/crash may invalidate old views conservatively, but cannot
        # let a pre-import view write a new order with the same ID/content.
        state["order_generation"] = uuid.uuid4().hex
        self.save_state(state)
        replace_snapshot(conn, data)

    def member_snapshot(self):
        """Stable member IDs and replacement epoch from ONE same-file lock."""
        with self.exclusive() as conn:
            state = self.load_state()
            if not state.get("order_generation"):
                raise SafetyBlocked("人員身分保護尚未初始化，請重新整理。")
            return {"generation": state["order_generation"], "rows": tuple(
                tuple(row) for row in conn.execute("SELECT id,name FROM order_members ORDER BY sort_order,id"))}

    def create_order(self, member_token, values):
        """Reject stale selection under the same lock/transaction as INSERT."""
        conn = self.connect(write=True)
        try:
            state = self.load_state()
            if (not isinstance(member_token, dict) or type(member_token.get("member_id")) is not int
                    or member_token["member_id"] <= 0 or not isinstance(member_token.get("name"), str)
                    or not member_token["name"] or not state.get("order_generation")
                    or member_token.get("generation") != state["order_generation"]):
                raise OrderConflict("人員身分或資料版本已失效；請重新選擇姓名後再建單。")
            member = conn.execute("SELECT name FROM order_members WHERE id=?", (member_token["member_id"],)).fetchone()
            if member is None or member[0] != member_token["name"]:
                raise OrderConflict("人員已改名或刪除；請重新選擇姓名後再建單。")
            if not isinstance(values, tuple) or len(values) != 7:
                raise OrderConflict("建單資料不完整，請重新確認。")
            category, item, price, custom, quantity, timestamp, unit = values
            if (category not in ("主餐", "飲料") or not isinstance(item, str) or not item.strip()
                    or not isinstance(custom, str) or not isinstance(timestamp, str) or not timestamp
                    or any(type(v) is not int or v <= 0 for v in (price, quantity, unit))
                    or price != quantity * unit):
                raise OrderConflict("建單資料無效，請重新確認。")
            affected = conn.execute("INSERT INTO orders (name,category,item_name,price,custom,quantity,order_time,is_paid,unit_price) VALUES(?,?,?,?,?,?,?,0,?)",
                (member[0], category, item, price, custom, quantity, timestamp, unit)).rowcount
            if affected != 1:
                raise OrderConflict("訂單未完整新增，請重新確認。")
            conn.commit()
            return affected
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def order_snapshot(self, name=None):
        """Capture native SQLite rows AND their generation under the same lock."""
        with self.exclusive() as conn:
            state = self.load_state()
            if "order_generation" not in state:
                raise SafetyBlocked("訂單身分保護尚未初始化，請重新整理。")
            query = "SELECT " + ",".join(ORDER_FIELDS) + " FROM orders"
            params = ()
            if name is not None:
                query += " WHERE name=?"
                params = (name,)
            rows = tuple(tuple(row) for row in conn.execute(query + " ORDER BY id", params))
            return {"generation": state["order_generation"], "rows": rows}

    def order_token(self, view, ids):
        selected = set(ids)
        rows = tuple(row for row in view["rows"] if row[0] in selected)
        if not selected or len(rows) != len(selected) or len(selected) != len(ids):
            raise OrderConflict("訂單資料已變更，請重新整理後再操作。")
        return {"generation": view["generation"], "rows": rows}

    def _check_order_token(self, conn, token, paid=None, rows_required=True):
        state = self.load_state()
        if state["restore"] != "ready" or state.get("attempt") or state["adoption_blocked"]:
            raise SafetyBlocked("資料還原或備份結果尚未確認，請先核對後再操作訂單。")
        generation = state.get("order_generation")
        if (not isinstance(token, dict) or not generation
                or token.get("generation") != generation):
            raise OrderConflict("訂單資料已匯入或替換，舊畫面無法操作；請重新整理或重新開啟。")
        rows = token.get("rows")
        if not isinstance(rows, tuple) or (rows_required and not rows):
            raise OrderConflict("缺少有效的原始訂單快照，請重新開啟後操作。")
        ids = set()
        for expected in rows:
            if (not isinstance(expected, tuple) or len(expected) != len(ORDER_FIELDS)
                    or type(expected[0]) is not int or expected[0] <= 0 or expected[0] in ids):
                raise OrderConflict("原始訂單快照無效，請重新開啟後操作。")
            ids.add(expected[0])
            actual = conn.execute("SELECT " + ",".join(ORDER_FIELDS) + " FROM orders WHERE id=?", (expected[0],)).fetchone()
            if actual is None or tuple(actual) != expected or (paid is not None and actual[8] != paid):
                raise OrderConflict("訂單身分、內容或付款狀態已變更，請重新核對後操作。")
        return rows

    def expected_order(self, token):
        with self.exclusive() as conn:
            rows = self._check_order_token(conn, token, paid=0)
            if len(rows) != 1:
                raise OrderConflict("編輯必須對應一筆原始訂單。")
            return dict(zip(ORDER_FIELDS, rows[0]))

    def apply_order_action(self, action, token, changes=None):
        """Revalidate and write under ONE existing guarded transaction.

        Target IDs come exclusively from the validated original snapshot, never
        from a second ID parameter or a newly-read replacement row.
        """
        conn = self.connect(write=True)
        try:
            if action == "clear":
                # Existing 'clear ALL current orders' rule is preserved within
                # one generation. A confirmation from before import is rejected.
                if not isinstance(token, dict) or token.get("clear_all") is not True:
                    raise OrderConflict("缺少清空確認的資料代次。")
                self._check_order_token(conn, token, rows_required=False)
                affected = conn.execute("DELETE FROM orders").rowcount
            else:
                paid = 1 if action == "undo_payment" else 0
                rows = self._check_order_token(conn, token, paid=paid)
                ids = tuple(row[0] for row in rows)
                if action in ("edit", "delete") and len(ids) != 1:
                    raise OrderConflict("編輯或刪除必須對應一筆原始訂單。")
                if action == "edit":
                    if not isinstance(changes, dict) or set(changes) != {"item_name", "price", "quantity", "unit_price", "custom"}:
                        raise OrderConflict("編輯欄位不完整。")
                    _text(changes["item_name"], "餐點名稱")
                    _text(changes["custom"], "客製需求", blank=True)
                    qty = _integer(changes["quantity"], "數量", 1)
                    unit = _integer(changes["unit_price"], "單價")
                    if _integer(changes["price"], "總額") != qty * unit:
                        raise OrderConflict("編輯金額與數量不一致。")
                    affected = conn.execute("UPDATE orders SET item_name=?, price=?, quantity=?, unit_price=?, custom=? WHERE id=? AND is_paid=0",
                        tuple(changes[key] for key in ("item_name", "price", "quantity", "unit_price", "custom")) + ids).rowcount
                elif action == "delete":
                    affected = conn.execute("DELETE FROM orders WHERE id=? AND is_paid=0", ids).rowcount
                elif action in ("payment", "undo_payment"):
                    placeholders = ",".join("?" for _ in ids)
                    affected = conn.execute(f"UPDATE orders SET is_paid=? WHERE id IN ({placeholders}) AND is_paid=?", (1 - paid,) + ids + (paid,)).rowcount
                else:
                    raise OrderConflict("未知訂單操作。")
                if affected != len(ids):
                    raise OrderConflict("訂單未完整更新，已取消本次操作，請重新核對。")
            conn.commit()
            return affected
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def status(self):
        state = self.load_state()
        status = state["sync"]
        if status.get("kind") == "success" and content_digest(self.export()) != status.get("digest"):
            return {"kind": "pending", "message": "本機資料已變更，尚未確認 GitHub 備份。"}
        return status
