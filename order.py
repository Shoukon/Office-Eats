import streamlit as st
import json
from cryptography.fernet import Fernet, InvalidToken
import base64
import urllib.request
import urllib.parse
import urllib.error
import pandas as pd
import sqlite3
import time
import os
import html
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from backup_safety import BackupGuard, BackupError, RemoteRejected, SafetyBlocked, OrderConflict, validate_backup, parse_backup

# ==========================================
# 0. 系統設定區
# ==========================================
DB_FILE = "lunch.db"
DB_PATH = Path(DB_FILE)

# orders 固定欄位。統計與收款頁會用這份欄位定義做防禦性處理。
ORDER_COLUMNS = [
    "id", "name", "category", "item_name", "price",
    "custom", "quantity", "order_time", "is_paid", "unit_price"
]

# 人員名單與主餐／飲料客製選項統一由 SQLite 管理，並加密同步 GitHub。
# Streamlit Secrets 僅保留 [admin] 與 [github]。

# ==========================================
# 1. 頁面設定與 CSS (純淨無框線排版核心)
# ==========================================
VERSION = "v3.7.1"
st.set_page_config(page_title=f"點餐哦各位～ {VERSION}", page_icon="🍱", layout="wide")

custom_css = """
<style>
    /*
     * 辦公室 Windows 11 繁中環境：
     * 中文、英文、數字統一優先使用微軟正黑體。
     *
     * Emoji 不應被強制綁到微軟正黑體：
     * Windows 會自動 fallback 到 Segoe UI Emoji。
     *
     * Streamlit 的 Material Symbols 圖示更不能跟著這個規則，
     * 否則例如 expander 的 expand_more 會被當成普通文字顯示。
     */
    html, body, button, input, textarea, select,
    [data-baseweb], [class*="st-"]:not([data-testid="stIconMaterial"]) {
        font-family:
            "Microsoft JhengHei",
            "微軟正黑體",
            "Segoe UI Emoji",
            "Noto Color Emoji",
            "Apple Color Emoji",
            sans-serif !important;
    }

    /* Streamlit Material Symbols：保留原生圖示字型，避免出現 expand_more / arrow_down 等文字 */
    [data-testid="stIconMaterial"],
    .material-symbols-rounded,
    .material-symbols-outlined {
        font-family: "Material Symbols Rounded" !important;
        font-weight: normal !important;
        font-style: normal !important;
        font-size: inherit;
        line-height: 1;
        letter-spacing: normal;
        text-transform: none;
        white-space: nowrap;
        word-wrap: normal;
        direction: ltr;
        -webkit-font-feature-settings: "liga";
        -webkit-font-smoothing: antialiased;
        font-feature-settings: "liga";
    }

    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    
    /* 頁籤 (Tabs) 優化：現代化且專業的底部指示線 */
    .stTabs [data-baseweb="tab-list"] { 
        gap: 16px; 
        border-bottom: 1px solid rgba(128,128,128,0.2); 
        padding-bottom: 0px; 
    }
    .stTabs [data-baseweb="tab"] {
        height: 52px; border-radius: 8px 8px 0 0; background-color: transparent; 
        padding: 10px 16px; font-weight: 600; color: #7f8c8d; font-size: 1.1rem;
        transition: all 0.2s ease; border: none;
    }
    .stTabs [data-baseweb="tab"]:hover { background-color: rgba(128,128,128,0.05); color: var(--text-color); }
    .stTabs [aria-selected="true"] { background-color: transparent !important; color: #4A90E2 !important; border-bottom: 3px solid #4A90E2 !important; }

    /* 區塊標頭設計 */
    .section-header {
        padding: 14px 20px; border-radius: 8px; margin-bottom: 15px;
        color: white; font-weight: 700; font-size: 1.2rem;
        display: flex; align-items: center; justify-content: space-between;
        box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    }
    .header-food { background: linear-gradient(135deg, #FF8C00, #FF4500); }
    .header-drink { background: linear-gradient(135deg, #008080, #2E8B57); }
    .header-money { background: linear-gradient(135deg, #DAA520, #B8860B); color: white;}

    /* ========== 統一清單排版系統 (無框線極簡風) ========== */
    .list-row {
        display: flex; justify-content: space-between; align-items: flex-start;
        padding: 6px 8px; margin-bottom: 2px;
    }
    .list-col-left { display: flex; flex-direction: column; gap: 2px; }
    .list-title-group { display: flex; align-items: center; flex-wrap: wrap; }
    .list-name { font-size: 1.15rem; font-weight: 700; color: var(--text-color); margin-right: 20px; }
    .list-qty { font-size: 1.15rem; font-weight: 800; color: #FF4B4B; }
    
    /* 移除了等寬字型 (monospace)，讓數字與英文字母跟隨系統平滑字體，視覺更統一 */
    .list-price { font-size: 1.15rem; font-weight: 700; color: #7f8c8d; padding-top: 2px;}

    /* 客製需求文字：降低視覺權重，但保持足夠辨識度 */
    .custom-text {
        font-size: 1.0rem; color: #838484; margin-top: 2px; line-height: 1.4;
    }

    /* 結構化分隔線 */
    hr.soft-divider { border: 0; height: 1px; background: rgba(128,128,128,0.1); margin: 4px 0; }
    hr.person-divider { border: 0; height: 1px; background: rgba(128,128,128,0.3); margin: 16px 0; }
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

# ==========================================
# 2. 資料庫邏輯區
# ==========================================


def db_connect(write=False):
    return backup_guard.connect(write=write)


TAIWAN_TZ = ZoneInfo("Asia/Taipei")

def taiwan_now():
    return datetime.now(TAIWAN_TZ)


def taiwan_now_str():
    return taiwan_now().strftime("%Y-%m-%d %H:%M:%S")


backup_guard = BackupGuard(DB_FILE, taiwan_now_str)


def execute_db(query, params=()):
    for _ in range(5):
        conn=None
        try:
            conn=db_connect(write=True)
            cur=conn.cursor()
            cur.execute(query,params)
            affected=cur.rowcount
            conn.commit()
            return affected
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                time.sleep(0.1)
            else:
                raise
        finally:
            if conn is not None: conn.close()
    st.error("⚠️ 系統忙碌，請稍後再試。")
    return 0


def remember_order_token(key, token):
    # Called BEFORE rerun with the token registered by the displayed widget.
    # Never replace this intent with a snapshot read after the click/import.
    st.session_state[key] = token


def execute_order_action(action, token, changes=None):
    for _ in range(5):
        try:
            return backup_guard.apply_order_action(action, token, changes)
        except (OrderConflict, SafetyBlocked, BackupError) as e:
            st.error(f"⚠️ {e}")
            return None
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            time.sleep(0.1)
    st.error("⚠️ 系統忙碌，本次訂單操作未完成，請重新整理後確認。")
    return None


def create_member_order(member_token, values):
    for _ in range(5):
        try:
            return backup_guard.create_order(member_token, values)
        except (OrderConflict, SafetyBlocked, BackupError) as e:
            st.error(f"⚠️ {e}")
            return None
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                st.error("⚠️ 建單失敗，請重新整理後核對資料。")
                return None
            time.sleep(0.1)
        except Exception:
            st.error("⚠️ 建單未完成，請重新整理後核對資料。")
            return None
    st.error("⚠️ 系統忙碌，本次建單未完成，請稍後再試。")
    return None


def get_db(query, params=()):
    conn=None
    try:
        conn=db_connect()
        return pd.read_sql_query(query,conn,params=params)
    except Exception as e:
        st.error(f"⚠️ 資料庫讀取失敗：{e}")
        result = pd.DataFrame()
        result.attrs["db_error"] = True
        return result
    finally:
        if conn is not None: conn.close()


def get_admin_password():
    try:
        return str(st.secrets.get("admin",{}).get("password","")).strip()
    except Exception:
        return ""


def get_github_settings():
    try:
        cfg=st.secrets.get("github",{})
        return (
            str(cfg.get("token","")).strip(),
            str(cfg.get("owner","")).strip(),
            str(cfg.get("repo","")).strip(),
            str(cfg.get("branch","main")).strip() or "main",
            str(cfg.get("data_file","order_data.json")).strip() or "order_data.json",
            str(cfg.get("encryption_key","")).strip(),
        )
    except Exception:
        return "","","","main","order_data.json",""


def github_is_configured():
    token,owner,repo,branch,path,key=get_github_settings()
    return bool(token and owner and repo and key)


def github_request(method,url,token,payload=None):
    headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}",
             "X-GitHub-Api-Version":"2026-03-10","User-Agent":"office-order-streamlit"}
    data=None
    if payload is not None:
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
        headers["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=data,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def test_github_encryption_key():
    *_,key=get_github_settings()
    if not key: return False,"尚未設定 encryption_key。"
    try:
        f=Fernet(key.encode("utf-8"))
        plain=b"Office-Order encryption test"
        return (True,"加密金鑰正常，可以正常加密／解密。") if f.decrypt(f.encrypt(plain))==plain else (False,"加密金鑰測試失敗。")
    except Exception as e:
        return False,f"encryption_key 格式錯誤：{e}"


def encrypt_github_backup(data):
    *_,key=get_github_settings()
    if not key: raise ValueError("尚未設定 GitHub encryption_key。")
    payload=dict(data); payload["backup_format"]="office-order-encrypted-v1"
    raw=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    return Fernet(key.encode("utf-8")).encrypt(raw).decode("ascii")


def decrypt_github_backup(text):
    *_,key=get_github_settings()
    if not key: raise ValueError("尚未設定 GitHub encryption_key。")
    try:
        raw=Fernet(key.encode("utf-8")).decrypt(text.encode("ascii"))
        data=json.loads(raw.decode("utf-8"))
    except (InvalidToken,ValueError,UnicodeDecodeError,json.JSONDecodeError) as e:
        raise ValueError("GitHub 備份無法解密：加密金鑰不正確，或備份內容已損壞。") from e
    if not isinstance(data,dict) or data.get("backup_format")!="office-order-encrypted-v1":
        raise ValueError("GitHub 備份格式不是目前的加密版本。")
    return validate_backup(data)


def github_get_backup():
    token,owner,repo,branch,path,_=get_github_settings()
    if not (token and owner and repo):
        raise BackupError("尚未設定 GitHub，不能判定遠端備份不存在。")
    base=f"https://api.github.com/repos/{urllib.parse.quote(owner,safe='')}/{urllib.parse.quote(repo,safe='')}"
    ref=urllib.parse.quote(branch,safe='')
    url=f"{base}/contents/{urllib.parse.quote(path,safe='/')}?ref={ref}"
    try:
        result=github_request("GET",url,token)
    except urllib.error.HTTPError as e:
        if e.code!=404:
            raise BackupError(f"GitHub 備份讀取失敗（HTTP {e.code}）。") from e
        # GitHub may conceal inaccessible repositories/files behind a 404.
        # Verify repository AND Contents read permission on this exact ref.
        try:
            github_request("GET",base,token)
            root=github_request("GET",f"{base}/contents?ref={ref}",token)
            if not isinstance(root,list) or any(row.get("path")==path for row in root if isinstance(row,dict)):
                raise BackupError("無法確認備份檔不存在。")
        except Exception as verification_error:
            raise BackupError("GitHub 回傳 404，但尚未確認儲存庫／分支／讀取權限，禁止初始化上傳。") from verification_error
        return None,None
    if not isinstance(result,dict) or not isinstance(result.get("sha"),str) or not result["sha"]:
        raise BackupError("GitHub 備份回應缺少有效 SHA。")
    encoded=result.get("content")
    if result.get("encoding")!="base64" or not isinstance(encoded,str) or not encoded.strip():
        raise BackupError("GitHub 備份內容為空或編碼錯誤；不能視為不存在。")
    try:
        text=base64.b64decode("".join(encoded.split()),validate=True).decode("utf-8")
    except (ValueError,UnicodeDecodeError) as e:
        raise BackupError("GitHub 備份內容損壞。") from e
    return decrypt_github_backup(text),result["sha"]


def github_put_backup(data,sha):
    # Only BackupGuard supplies a snapshot and SHA while holding the same-file
    # coordination lock. NEVER refetch a newer SHA for an older snapshot here.
    token,owner,repo,branch,path,_=get_github_settings()
    if not (token and owner and repo): raise RemoteRejected("尚未設定 GitHub。")
    encrypted=encrypt_github_backup(data)
    payload={"message":f"Update encrypted order data {taiwan_now_str()}",
             "content":base64.b64encode(encrypted.encode("ascii")).decode("ascii"),
             "branch":branch}
    if sha: payload["sha"]=sha
    url=f"https://api.github.com/repos/{urllib.parse.quote(owner,safe='')}/{urllib.parse.quote(repo,safe='')}/contents/{urllib.parse.quote(path,safe='/')}"
    try:
        result=github_request("PUT",url,token,payload)
        return result.get("content",{}).get("sha")
    except urllib.error.HTTPError as e:
        if 400<=e.code<500 and e.code!=408:
            raise RemoteRejected("GitHub 備份版本衝突，未覆寫遠端。" if e.code==409 else f"GitHub 拒絕備份（HTTP {e.code}）。") from e
        raise  # ambiguous response: caller must reconcile with GET, not retry PUT


def get_members():
    return get_db("SELECT id,name,sort_order FROM order_members ORDER BY sort_order,id")


def get_options(category):
    df=get_db("SELECT option_value FROM order_options WHERE category=? ORDER BY sort_order,id",(category,))
    return df["option_value"].astype(str).tolist() if not df.empty else []


def get_option_rows(category):
    return get_db("""SELECT id,option_value,sort_order,is_default
                     FROM order_options WHERE category=?
                     ORDER BY sort_order,id""",(category,))


def get_default_option(category):
    df=get_db("""SELECT option_value FROM order_options
                 WHERE category=? AND is_default=1
                 ORDER BY sort_order,id LIMIT 1""",(category,))
    if not df.empty:
        return str(df.iloc[0]["option_value"])
    vals=get_options(category)
    return vals[0] if vals else None


def set_options(category,values,default_value=None):
    values=[str(v).strip() for v in values if str(v).strip()]
    conn=db_connect(write=True)
    try:
        conn.execute("DELETE FROM order_options WHERE category=?",(category,))
        for idx,v in enumerate(values):
            is_default=1 if default_value is not None and str(v)==str(default_value) else 0
            conn.execute("""INSERT INTO order_options
                            (category,option_value,sort_order,is_default)
                            VALUES(?,?,?,?)""",(category,v,idx,is_default))
        if values and not any(str(v)==str(default_value) for v in values):
            conn.execute("""UPDATE order_options SET is_default=1
                            WHERE category=? AND sort_order=0""",(category,))
        conn.commit()
    finally:
        conn.close()


def get_orders_df():
    df=get_db("SELECT * FROM orders")
    if df.attrs.get("db_error", False): return df
    if df.empty: return pd.DataFrame(columns=ORDER_COLUMNS)
    for col in ORDER_COLUMNS:
        if col not in df.columns: df[col]=0 if col in ("price","quantity","is_paid","unit_price") else ""
    return df[ORDER_COLUMNS].copy()


def get_shop_name(cat):
    df = get_db(
        "SELECT shop_name FROM config_shop WHERE category=?",
        (cat,)
    )
    return str(df.iloc[0]["shop_name"]).strip() if not df.empty else ""


def get_current_shop_names():
    """Single source of truth for UI: current SQLite config_shop values."""
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT category, shop_name FROM config_shop "
            "WHERE category IN ('main','drink')"
        ).fetchall()
    result = {"main": "", "drink": ""}
    for category, shop_name in rows:
        result[category] = "" if shop_name is None else str(shop_name).strip()
    return result


def set_shop_name(cat,name):
    name = str(name).strip()
    conn = db_connect(write=True)
    try:
        conn.execute(
            "INSERT INTO config_shop(category,shop_name) VALUES(?,?) "
            "ON CONFLICT(category) DO UPDATE SET shop_name=excluded.shop_name",
            (cat, name)
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()




def get_db_size():
    try:
        return DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except OSError:
        return 0


def get_order_db_diagnostics():
    """Return SQLite diagnostics for the administrator panel."""
    result = {"exists": DB_PATH.exists(), "size": get_db_size(),
              "tables": {}, "db_error": None, "secret_members": 0}
    for table in ("order_members", "order_options", "orders", "config_shop"):
        try:
            df = get_db(f"SELECT COUNT(*) AS n FROM {table}")
            result["tables"][table] = int(df.iloc[0]["n"]) if not df.empty else 0
        except Exception:
            result["tables"][table] = 0
    try:
        result["secret_members"] = len(st.secrets.get("default_settings", {}).get("colleagues", []))
    except Exception:
        result["secret_members"] = 0
    return result


def export_order_data():
    return backup_guard.export()


def current_backup_result():
    status=backup_guard.status()
    if status["kind"]=="success":
        return ("success", f"🟢 **本機資料與 GitHub 備份一致**\n\n同步時間：{status['at']}（台灣時間）  \n名單：{status['members']} 人｜訂單：{status['orders']} 筆")
    return ("error", f"⚠️ {status['message']}")


def sync_github_backup(show_error=False):
    try:
        ok=backup_guard.transfer(github_is_configured(),github_get_backup,github_put_backup)
        st.session_state["github_sync_result"]=current_backup_result()
    except Exception:
        ok=False
        st.session_state["github_sync_result"]=("error", "⚠️ 備份同步未完成；本機資料未因同步失敗而取消，請重新整理後核對／重試。")
    if not ok and show_error:
        st.error(st.session_state["github_sync_result"][1])
    return ok


def import_order_data(data):
    # No pre-import export/compensating restore. The replacement stays
    # uncommitted until remote confirmation; failed import rolls back in-place.
    ok=backup_guard.transfer(github_is_configured(),github_get_backup,github_put_backup,imported=data)
    st.session_state["github_sync_result"]=current_backup_result()
    if not ok:
        raise BackupError(st.session_state["github_sync_result"][1])
    return True


def restore_from_github_if_new_db(is_new_db):
    # The durable sidecar, not only the presence of tables, decides retry state.
    return backup_guard.startup(github_is_configured(),github_get_backup)


def _init_schema(conn):
    cur=conn.cursor()
    existing={r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('orders','config_shop','order_members','order_options')")}
    is_new_db=not existing
    cur.execute("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,category TEXT,item_name TEXT,price INTEGER,
        custom TEXT,quantity INTEGER,order_time TEXT,is_paid BOOLEAN,unit_price INTEGER)""")
    cols={r[1] for r in cur.execute("PRAGMA table_info(orders)")}
    if "unit_price" not in cols:
        cur.execute("ALTER TABLE orders ADD COLUMN unit_price INTEGER")
        cur.execute("UPDATE orders SET unit_price=CASE WHEN quantity>0 AND price%quantity=0 THEN price/quantity ELSE NULL END WHERE unit_price IS NULL")
    cur.execute("CREATE TABLE IF NOT EXISTS config_shop(category TEXT PRIMARY KEY,shop_name TEXT)")
    cur.execute("INSERT OR IGNORE INTO config_shop VALUES('main','吃什麼？')")
    cur.execute("INSERT OR IGNORE INTO config_shop VALUES('drink','喝什麼？')")
    cur.execute("CREATE TABLE IF NOT EXISTS order_members(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,sort_order INTEGER NOT NULL DEFAULT 0)")
    cur.execute("""CREATE TABLE IF NOT EXISTS order_options(
        id INTEGER PRIMARY KEY AUTOINCREMENT,category TEXT NOT NULL,option_value TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,is_default INTEGER NOT NULL DEFAULT 0,
        UNIQUE(category,option_value))""")
    cols_opt={r[1] for r in cur.execute("PRAGMA table_info(order_options)")}
    if "is_default" not in cols_opt:
        cur.execute("ALTER TABLE order_options ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
    return is_new_db


def init_db():
    return backup_guard.prepare(_init_schema)


try:
    is_new_db=init_db()
    restore_result=restore_from_github_if_new_db(is_new_db)
except Exception:
    st.error("⚠️ 資料保護初始化未完成；請保留資料庫與安全狀態檔，未自動上傳或替換資料。")
    if st.button("🔄 重試資料保護初始化",key="retry_safety_init"):
        st.rerun()
    st.stop()
if restore_result in ("failed","uncertain","adoption_failed","adoption_conflict"):
    st.error(backup_guard.load_state()["sync"]["message"])
    if st.button("🔄 重試還原／核對備份",key="retry_backup_restore"):
        st.rerun()
    st.stop()

def ensure_single_select_defaults():
    bootstrap={
        "main_size":(["無","小份","大份"],"無"),
        "spicy":(["無","微辣","小辣","中辣","大辣"],"無"),
        "drink_size":(["M","L","XL"],"L"),
        "ice":(["完全去冰","去冰","微冰","少冰","正常冰","溫","熱"],"完全去冰"),
        "sugar":(["無糖","一分糖","微糖","少糖","半糖","正常糖"],"無糖"),
    }
    for cat,(values,default_value) in bootstrap.items():
        if not get_options(cat):
            set_options(cat,values,default_value)
        elif get_db(
            "SELECT 1 FROM order_options WHERE category=? AND is_default=1",(cat,)
        ).empty:
            vals=get_options(cat)
            if vals:
                set_options(cat,vals,vals[0])

ensure_single_select_defaults()
if restore_result=="missing" and github_is_configured():
    sync_github_backup(False)

colleagues_list=get_members()["name"].astype(str).tolist()
if not colleagues_list: colleagues_list=["尚未設定人員，請登入管理員新增"]
spicy_levels=get_options("spicy") or ["無"]
ice_levels=get_options("ice")
sugar_levels=get_options("sugar")
main_size_options=get_options("main_size") or ["無"]
drink_size_options=get_options("drink_size") or ["M","L","XL"]
custom_tags_main=get_options("tags")
custom_tags_drink=get_options("drink_tags")

if "user_name" not in st.session_state: st.session_state["user_name"]=None
if "m_custom_tags" not in st.session_state: st.session_state["m_custom_tags"]=[]
if "m_custom_manual" not in st.session_state: st.session_state["m_custom_manual"]=""
if "d_custom_tags" not in st.session_state: st.session_state["d_custom_tags"]=[]
if "d_custom_manual" not in st.session_state: st.session_state["d_custom_manual"]=""
if "admin_logged_in" not in st.session_state: st.session_state["admin_logged_in"]=False
if "github_sync_result" not in st.session_state: st.session_state["github_sync_result"]=None


# ==========================================
# 3. 管理員功能
# ==========================================
def save_and_sync():
    return sync_github_backup(show_error=True)


def member_transaction(operation):
    """Run one member-management intent under the existing shared write lock."""
    for attempt in range(5):
        conn=None
        try:
            conn=db_connect(write=True)
            result=operation(conn)
            if result[0]: conn.commit()
            else: conn.rollback()
            return result
        except Exception as e:
            if conn is not None:
                try: conn.rollback()
                except Exception:
                    return False,"交易回滾未能確認，請重新整理後核對人員資料。"
            if not isinstance(e,sqlite3.OperationalError) or "locked" not in str(e).lower():
                return False,"人員操作失敗，請重新整理後核對資料並重試。"
        finally:
            if conn is not None: conn.close()
        if attempt<4: time.sleep(0.1)
    return False,"系統忙碌，本次人員操作未完成，請稍後再試。"


def add_member(name):
    name=str(name).strip()
    if not name: return False,"姓名不能為空。"
    def insert(conn):
        if conn.execute("SELECT 1 FROM order_members WHERE name=?",(name,)).fetchone():
            return False,"這個姓名已存在。"
        mx=conn.execute("SELECT COALESCE(MAX(sort_order),-1) FROM order_members").fetchone()[0]
        if conn.execute("INSERT INTO order_members(name,sort_order) VALUES(?,?)",(name,int(mx)+1)).rowcount!=1:
            raise sqlite3.IntegrityError("member insert did not affect one row")
        return True,""
    return member_transaction(insert)


def rename_member(member_id,name):
    name=str(name).strip()
    for attempt in range(5):
        conn=None
        try:
            # BackupGuard begins IMMEDIATE and holds its same-file lock until
            # close. Validate and update both tables within that ONE transaction.
            conn=db_connect(write=True)
            current=conn.execute("SELECT name FROM order_members WHERE id=?",(member_id,)).fetchone()
            if current is None: return False,"找不到這位人員。"
            if not name: return False,"姓名不能為空。"
            if conn.execute("SELECT 1 FROM order_members WHERE name=? AND id<>?",(name,member_id)).fetchone():
                return False,"這個姓名已存在。"
            old=str(current[0])
            updated=conn.execute("UPDATE order_members SET name=? WHERE id=?",(name,member_id))
            if updated.rowcount!=1:
                raise sqlite3.IntegrityError("member update did not affect one row")
            # No orders is legal: never require a positive rowcount here.
            conn.execute("UPDATE orders SET name=? WHERE name=?",(name,old))
            conn.commit()
            return True,""
        except Exception as e:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    return False,"改名失敗，交易回滾未能確認；請重新整理後核對資料。"
            if not isinstance(e,sqlite3.OperationalError) or "locked" not in str(e).lower():
                return False,"改名失敗，請重新整理後核對資料並重試。"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    return False,"改名連線關閉失敗；請重新整理後核對資料。"
        if attempt<4:
            time.sleep(0.1)
    return False,"系統忙碌，本次改名未完成，請稍後再試。"


def delete_member(member_id):
    def delete(conn):
        current=conn.execute("SELECT name FROM order_members WHERE id=?",(member_id,)).fetchone()
        if current is None: return False,"找不到這位人員。"
        if conn.execute("DELETE FROM order_members WHERE id=?",(member_id,)).rowcount!=1:
            raise sqlite3.IntegrityError("member delete did not affect one row")
        return True,str(current[0])
    return member_transaction(delete)


def move_member(member_id,direction):
    def swap(conn):
        members=conn.execute("SELECT id,sort_order FROM order_members ORDER BY sort_order,id").fetchall()
        ids=[row[0] for row in members]
        if int(member_id) not in ids: return False,"找不到這位人員。"
        if direction not in (-1,1): return False,"排序方向無效。"
        idx=ids.index(int(member_id)); target=idx+direction
        if target<0 or target>=len(ids): return True,""  # legal no-op
        a,b=members[idx],members[target]
        for mid,position in ((a[0],b[1]),(b[0],a[1])):
            if conn.execute("UPDATE order_members SET sort_order=? WHERE id=?",(position,mid)).rowcount!=1:
                raise sqlite3.IntegrityError("member swap did not affect one row")
        return True,""
    return member_transaction(swap)

@st.dialog("👥 管理點餐人員",width="large")
def manage_members_dialog():
    st.caption("名單儲存在 lunch.db，並會加密同步到 GitHub。")
    members=get_members()
    for _,row in members.iterrows():
        mid,name=int(row["id"]),str(row["name"])
        c1,c2,c3,c4=st.columns([6,0.7,0.7,1])
        c1.write(f"👤 {name}")
        pos=list(members["id"]).index(mid)
        if c2.button("⬆️",key=f"up_{mid}",disabled=pos==0,use_container_width=True):
            ok,msg=move_member(mid,-1)
            if ok: save_and_sync(); st.session_state["reopen_members"]=True; st.rerun()
            else: st.error(msg)
        if c3.button("⬇️",key=f"down_{mid}",disabled=pos==len(members)-1,use_container_width=True):
            ok,msg=move_member(mid,1)
            if ok: save_and_sync(); st.session_state["reopen_members"]=True; st.rerun()
            else: st.error(msg)
        with c4.popover("✏️"):
            nn=st.text_input("姓名",value=name,key=f"rename_{mid}")
            if st.button("儲存",key=f"rename_save_{mid}",use_container_width=True):
                ok,msg=rename_member(mid,nn)
                if ok: save_and_sync(); st.toast("✅ 姓名已修改"); st.session_state["reopen_members"]=True; st.rerun()
                else: st.error(msg)
            st.divider()
            if st.button("🗑️ 刪除這位人員",key=f"delete_member_{mid}",use_container_width=True):
                st.session_state[f"confirm_delete_member_{mid}"]=True
            if st.session_state.get(f"confirm_delete_member_{mid}",False):
                st.warning(f"確定要刪除「{name}」嗎？")
                d1,d2=st.columns(2)
                with d1:
                    if st.button("取消",key=f"cancel_delete_{mid}",use_container_width=True):
                        st.session_state[f"confirm_delete_member_{mid}"]=False
                        st.rerun()
                with d2:
                    if st.button("確定刪除",key=f"confirm_delete_{mid}",use_container_width=True):
                        ok,msg=delete_member(mid)
                        if ok:
                            st.session_state.pop(f"confirm_delete_member_{mid}",None)
                            save_and_sync()
                            st.toast(f"🗑️ 已刪除：{msg}")
                            st.session_state["reopen_members"]=True
                            st.rerun()
                        else:
                            st.error(msg)
    st.divider()
    nm=st.text_input("新增人員",key="new_member_name")
    if st.button("➕ 新增人員",use_container_width=True,type="primary"):
        ok,msg=add_member(nm)
        if ok: save_and_sync(); st.toast(f"✅ 已新增：{nm.strip()}"); st.session_state["reopen_members"]=True; st.rerun()
        else: st.error(msg)
    if st.button("✖️ 完成管理／關閉",key="close_members",use_container_width=True):
        st.session_state["reopen_members"]=False; st.rerun()

@st.dialog("🎨 管理主餐／飲料客製",width="large")
def manage_options_dialog():
    st.caption("主餐與飲料客製選項儲存在 lunch.db，Secrets 不再保存這些固定選項。")
    labels={
        "main_size":"主餐尺寸",
        "spicy":"主餐辣度",
        "drink_size":"飲料尺寸",
        "ice":"飲料冰塊",
        "sugar":"飲料甜度",
        "tags":"主餐客製需求",
        "drink_tags":"飲料客製需求",
    }
    default_categories={"main_size","spicy","drink_size","ice","sugar"}
    for cat,label in labels.items():
        editor_values=get_options(cat)
        default_value=get_default_option(cat) if cat in default_categories else None
        text=st.text_area(
            label,
            value="\n".join(editor_values),
            height=110,
            key=f"admin_opt_{cat}",
            help="一行一個選項；可調整順序；空白行忽略，重複值自動去除。"
        )
        default_choice=None
        if cat in default_categories and editor_values:
            default_choice=st.selectbox(
                "預設值",
                editor_values,
                index=(editor_values.index(default_value)
                       if default_value in editor_values else 0),
                key=f"admin_default_{cat}"
            )
        if st.button(f"💾 儲存{label}",key=f"save_opt_{cat}",use_container_width=True):
            values=[v.strip() for v in text.splitlines() if v.strip()]
            if cat in default_categories:
                if default_choice not in values:
                    default_choice=values[0] if values else None
                set_options(cat,values,default_choice)
            else:
                # 客製需求是 multi-select，沒有預設值。
                set_options(cat,values,None)
            save_and_sync()

            # 選項／預設值已寫入 SQLite；清除目前點餐畫面殘留的
            # widget state，讓返回主畫面時重新採用最新 DB 預設值。
            option_widget_keys = (
                "m_size", "m_spicy", "d_size", "d_sugar", "d_ice",
                "m_custom_tags", "d_custom_tags",
            )
            for widget_key in option_widget_keys:
                st.session_state.pop(widget_key, None)

            st.toast(f"✅ {label}已更新")
            st.session_state["reopen_options"]=True
            st.rerun()
    if st.button("✖️ 完成設定／關閉",key="close_options",use_container_width=True):
        st.session_state["reopen_options"]=False
        st.rerun()

@st.dialog("⚠️ 確認清空全部訂單")
def clear_orders_dialog(order_token):
    st.warning("確定要清空目前全部訂單嗎？此動作無法復原。")
    c1,c2=st.columns(2)
    with c1:
        if st.button("❌ 取消",key="clear_orders_cancel",use_container_width=True):
            st.rerun()
    with c2:
        generation=order_token.get("generation","missing") if isinstance(order_token,dict) else "missing"
        if st.button("🗑️ 確定清除",key=f"clear_orders_confirm_{generation}",use_container_width=True):
            if execute_order_action("clear",order_token) is None:
                return
            if sync_github_backup(False):
                st.toast("🗑️ 全部訂單已清除，GitHub 備份也已更新。")
            else:
                st.toast("🗑️ 本機訂單已清除，但 GitHub 備份未確認成功，請重試同步。")
            st.rerun()


with st.sidebar:
    st.header("⚙️ 點餐管理")
    st.subheader("今日店家")
    db_main_shop=get_shop_name("main"); db_drink_shop=get_shop_name("drink")
    new_main_shop=st.text_input("主餐店家",value=db_main_shop, key="shop_main_input").strip()
    new_drink_shop=st.text_input("飲料店家",value=db_drink_shop, key="shop_drink_input").strip()

    if st.button("💾 儲存今日店家", key="save_shop_names", use_container_width=True):
        conn=db_connect(write=True)
        try:
            conn.execute(
                "INSERT INTO config_shop(category,shop_name) VALUES(?,?) "
                "ON CONFLICT(category) DO UPDATE SET shop_name=excluded.shop_name",
                ("main", new_main_shop)
            )
            conn.execute(
                "INSERT INTO config_shop(category,shop_name) VALUES(?,?) "
                "ON CONFLICT(category) DO UPDATE SET shop_name=excluded.shop_name",
                ("drink", new_drink_shop)
            )
            conn.commit()

            # 驗證：寫入後立即重新讀 SQLite，確保不是只改到 widget state。
            saved = get_current_shop_names()
            if saved["main"] != new_main_shop or saved["drink"] != new_drink_shop:
                raise RuntimeError("SQLite 店家資料寫入後驗證不一致。")

            sync_ok=save_and_sync()
            if sync_ok:
                st.success("✅ 今日店家已儲存並同步")
            else:
                st.warning("⚠️ 店家已儲存到資料庫，但 GitHub 同步未成功，請檢查同步狀態。")
            st.rerun()
        except Exception as e:
            conn.rollback()
            st.error(f"⚠️ 今日店家儲存失敗：{e}")
        finally:
            conn.close()

    st.divider(); st.subheader("🗑️ 資料管理")
    clear_token={"generation":backup_guard.load_state()["order_generation"],"rows":(),"clear_all":True}
    if st.button("🗑️ 清空全部訂單",use_container_width=True,
                 on_click=remember_order_token,args=("clear_order_token",clear_token)):
        clear_orders_dialog(st.session_state.pop("clear_order_token",None))

    st.divider(); st.subheader("🔐 管理員")
    if not st.session_state.admin_logged_in:
        pw=st.text_input("管理員密碼",type="password",key="admin_pw")
        if st.button("🔑 管理員登入",use_container_width=True,type="primary"):
            if pw and pw==get_admin_password(): st.session_state.admin_logged_in=True; st.rerun()
            else: st.error("❌ 管理員密碼錯誤")
    else:
        st.success("🔓 管理員已登入")
        if st.button("👥 管理人員名單",use_container_width=True,type="primary"): manage_members_dialog()
        elif st.session_state.pop("reopen_members",False): manage_members_dialog()
        if st.button("🎨 管理主餐／飲料客製",use_container_width=True):
            st.session_state["reopen_options"]=False
            manage_options_dialog()
        elif st.session_state.pop("reopen_options",False):
            manage_options_dialog()
        if st.button("🔒 管理員登出",use_container_width=True): st.session_state.admin_logged_in=False; st.rerun()

        st.divider(); st.subheader("☁️ GitHub 永久資料")
        result=current_backup_result()
        # A failed current-session attempt (e.g. lock timeout) must not be
        # concealed by a previous successful shared backup status.
        session_result=st.session_state.get("github_sync_result")
        if session_result and session_result[0]=="error":
            result=session_result
        if result:
            kind,msg=result
            if kind == "success":
                st.success(msg)
            else:
                st.error(msg)

        if github_is_configured():
            _, owner, repo, branch, path, _ = get_github_settings()
            st.caption(f"儲存位置：{owner}/{repo}/{path}（{branch}）")
        else:
            st.warning("⚠️ 尚未完整設定 GitHub 備份。")

        ok,msg=test_github_encryption_key()
        st.caption("🔐 encryption_key：正常" if ok else f"🔐 encryption_key：{msg}")

        if st.button("🔄 立即同步目前資料",use_container_width=True,type="primary"):
            if sync_github_backup(show_error=True): st.toast("✅ 已同步到 GitHub")
            st.rerun()

        st.divider(); st.subheader("💾 資料備份")
        data=json.dumps(export_order_data(),ensure_ascii=False,indent=2).encode("utf-8")
        st.download_button("📥 匯出點餐資料",data=data,
                           file_name=f"office_order_backup_{taiwan_now().strftime('%Y%m%d_%H%M%S')}.json",
                           mime="application/json",use_container_width=True)
        uploaded=st.file_uploader("📤 匯入點餐資料",type=["json"],key="order_backup_upload")
        if uploaded is not None:
            st.warning("⚠️ 匯入會取代目前的人員、客製選項、店家與所有訂單。")
            if st.button("✅ 確定匯入此備份",key="confirm_order_import",use_container_width=True):
                try:
                    imported=parse_backup(uploaded.getvalue())
                    import_order_data(imported)
                    st.session_state.pop("order_backup_upload",None)
                    st.toast("✅ 點餐資料匯入成功，GitHub 備份已確認！")
                    st.rerun()
                except (BackupError,SafetyBlocked) as e:
                    st.error(f"❌ 匯入未完成：{e}")
                except Exception:
                    st.error("❌ 匯入未完成；請保留原資料及安全狀態檔，重新整理後核對備份狀態。")

        st.divider(); st.subheader("🗄️ 資料庫資訊")
        diag = get_order_db_diagnostics()
        members_n = diag["tables"].get("order_members", 0)
        orders_n = diag["tables"].get("orders", 0)
        st.caption(f"名單：{members_n} 人　｜　訂單：{orders_n} 筆")
        st.caption(f"資料庫：{DB_PATH}")
        st.caption(f"檔案大小：{diag['size']:,} bytes")

        with st.expander("🔎 資料庫診斷"):
            st.caption(f"資料庫檔案存在：{'是' if diag['exists'] else '否'}")
            for table in ("order_members", "order_options", "orders", "config_shop"):
                st.caption(f"{table}：{diag['tables'].get(table, 0)} 筆")
            st.caption(f"Secrets 名單：{diag['secret_members']} 人")
            if diag.get("db_error"):
                st.error(f"資料庫診斷失敗：{diag['db_error']}")

# ==========================================
# 4. 統計看板 (全域去框線版本，移除編號)
# ==========================================
@st.fragment(run_every="10s")
def render_stats_section():
    df_all = get_orders_df()
    if df_all.attrs.get("db_error", False):
        st.error("⚠️ 暫時無法讀取訂單資料，請稍後重新整理。")
        return
    c_ref_text, c_ref_btn = st.columns([8, 1], vertical_alignment="center")
    with c_ref_text:
        # 字體微調為 0.95rem
        st.markdown(f'<div style="text-align:right; color:color-mix(in srgb, var(--text-color) 58%, transparent); font-size:0.9rem; margin:0; padding:0;">更新於 {taiwan_now_str().split(" ")[1]}</div>', unsafe_allow_html=True)
    with c_ref_btn:
        if st.button("🔄", help="立即重新整理統計資料", use_container_width=True, key="btn_refresh_stats"): st.rerun()

    shop_names = get_current_shop_names()
    r_name = shop_names["main"]
    d_name = shop_names["drink"]
    if df_all.empty:
        st.info("📦 目前尚無訂單，等待第一筆資料...")
        return

    def parse_stats_custom(category, raw_custom):
        """
        店家統計看板顯示與排序。

        主餐排序：
          尺寸：無（不顯示）→ 小份 → 大份
          辣度：無 → 微辣 → 小辣 → 中辣 → 大辣

        飲料排序：
          尺寸：M → L → XL
          甜度：無糖 → 一分糖 → 微糖 → 少糖 → 半糖 → 正常糖
          冰塊：完全去冰 → 去冰 → 微冰 → 少冰 → 正常冰 → 溫 → 熱

        只改統計看板的顯示與排序，不修改資料庫原始 custom。
        """
        raw = str(raw_custom or "").strip()
        if not raw:
            return "", 0, 0, 0, ""

        parts = [p.strip() for p in raw.split(",") if p is not None and p.strip()]

        if category == "主餐":
            main_sizes=get_options("main_size")
            spicy_options=get_options("spicy")
            size_order={value:idx for idx,value in enumerate(main_sizes)}
            spicy_order={value:idx for idx,value in enumerate(spicy_options)}

            size = ""
            spicy = ""

            # 新格式：尺寸, 辣度, 其他客製
            if parts and parts[0] in size_order:
                size = parts.pop(0)

            # 新格式（無尺寸）或舊格式：第一個欄位直接是辣度。
            if parts and parts[0] in spicy_order:
                spicy = parts.pop(0)

            display_parts = []
            if size:
                display_parts.append(size)
            if spicy:
                display_parts.append(spicy)
            display_parts.extend(parts)

            return (
                "・".join(display_parts),
                size_order.get(size, 0),
                spicy_order.get(spicy, 0),
                0,
                "・".join(parts),
            )

        if category == "飲料":
            drink_sizes=get_options("drink_size")
            sugar_options=get_options("sugar")
            ice_options=get_options("ice")
            size_order={value:idx for idx,value in enumerate(drink_sizes)}
            sugar_order={value:idx for idx,value in enumerate(sugar_options)}
            ice_order={value:idx for idx,value in enumerate(ice_options)}

            # 飲料儲存格式是：
            # 尺寸/甜度/冰塊[, 其他客製]
            # 例如：飲料尺寸/甜度/冰塊
            #       飲料尺寸/甜度/冰塊, 加珍珠
            #
            # 3.9.8 錯誤地只用逗號切割，導致整個
            # 「飲料尺寸/甜度/冰塊」被當成一個未知欄位，
            # 因而尺寸權重全部失效。這裡按照實際資料格式解析。
            base_part, separator, tag_part = raw.partition(",")
            base_values = [
                value.strip()
                for value in base_part.split("/")
                if value is not None and value.strip()
            ]

            size = base_values[0] if base_values and base_values[0] in size_order else ""
            sugar = base_values[1] if len(base_values) > 1 and base_values[1] in sugar_order else ""
            ice = base_values[2] if len(base_values) > 2 and base_values[2] in ice_order else ""

            recognized_count = sum(
                value is not None and bool(value)
                for value in (size, sugar, ice)
            )

            # 保留任何未被辨識的基本欄位，避免舊資料或手動資料在看板上消失。
            recognized_base_values = [
                value for value in (size, sugar, ice) if value
            ]
            unknown_base_values = [
                value for value in base_values
                if value not in recognized_base_values
            ]
            extra_parts = unknown_base_values + (
                [
                    value.strip()
                    for value in tag_part.split(",")
                    if value is not None and value.strip()
                ] if separator else []
            )

            if not recognized_count and not extra_parts:
                extra_parts = parts

            # 與主餐統一：所有基本設定與客製項目都用「・」分隔。
            # 例如：飲料尺寸・甜度・冰塊・加珍珠
            display_parts = [value for value in (size, sugar, ice) if value]
            display_parts.extend(extra_parts)

            return (
                "・".join(display_parts),
                size_order.get(size, 99),
                sugar_order.get(sugar, 0),
                ice_order.get(ice, 0),
                "・".join(extra_parts),
            )

        return raw, 99, 99, 99, raw


    def format_stats_custom(category, raw_custom):
        return parse_stats_custom(category, raw_custom)[0]

    def show_stats_optimized(df_source, title, icon_class):
        # 獨立副本，避免統計區塊互相修改 DataFrame。
        df_source = df_source.copy()

        if df_source.empty:
            st.markdown(
                f'<div class="section-header {icon_class}">'
                f'<div>{title}</div><div>共 0 份</div></div>',
                unsafe_allow_html=True
            )
            st.caption("目前無資料")
            return

        df_source["quantity"] = pd.to_numeric(
            df_source["quantity"], errors="coerce"
        ).fillna(0).astype(int)
        df_source["price"] = pd.to_numeric(
            df_source["price"], errors="coerce"
        ).fillna(0).astype(int)

        total_qty = int(df_source["quantity"].sum())
        st.markdown(
            f'<div class="section-header {icon_class}">'
            f'<div>{title}</div><div>共 {total_qty} 份</div></div>',
            unsafe_allow_html=True
        )

        df_source['item_name'] = (
            df_source['item_name']
            .fillna("")
            .astype(str)
            .str.strip()
        )
        df_source['custom'] = (
            df_source['custom']
            .fillna("")
            .astype(str)
            .str.strip()
        )

        # 店家看板只改「顯示內容」，不改資料庫原始 custom。
        stats_parsed = df_source.apply(
            lambda row: parse_stats_custom(row["category"], row["custom"]),
            axis=1,
            result_type="expand"
        )
        stats_parsed.columns = [
            "stats_custom",
            "stats_order_1",
            "stats_order_2",
            "stats_order_3",
            "stats_order_4",
        ]
        df_source = pd.concat([df_source, stats_parsed], axis=1)

        c_sum, c_det = st.columns([1, 1.2])

        with c_sum:
            st.markdown("**📦 出餐彙總**")

            # 第一層只依餐點名稱彙總。
            # custom 是餐點附加資訊，不應讓同一道餐點被拆成多筆主項目。
            summary = (
                df_source.groupby('item_name', dropna=False)['quantity']
                .sum()
                .reset_index()
            )
            summary.columns = ['餐點', '總量']

            for _, row in summary.iterrows():
                item_name = row["餐點"]
                item_group = df_source[df_source["item_name"] == item_name]

                with st.container():
                    c_qty, c_info = st.columns([1, 5], vertical_alignment="center")
                    with c_qty:
                        st.markdown(
                            f'<div style="font-size:1.5rem; font-weight:800; color:#FF4B4B; text-align:left;">×{row["總量"]}</div>',
                            unsafe_allow_html=True
                        )

                    with c_info:
                        safe_name = html.escape(str(item_name))
                        st.markdown(
                            f'<div style="font-size:1.15rem; font-weight:700; color:var(--text-color);">{safe_name}</div>',
                            unsafe_allow_html=True
                        )

                        # 同一餐點底下，再依客製化統計，讓店家知道各種客製要做幾份。
                        custom_group = (
                            item_group[item_group["stats_custom"] != ""]
                            .groupby(
                                [
                                    "stats_custom",
                                    "stats_order_1",
                                    "stats_order_2",
                                    "stats_order_3",
                                    "stats_order_4",
                                ],
                                dropna=False,
                                as_index=False,
                            )["quantity"]
                            .sum()
                            .sort_values(
                                [
                                    "stats_order_1",
                                    "stats_order_2",
                                    "stats_order_3",
                                    "stats_order_4",
                                    "stats_custom",
                                ],
                                kind="stable",
                            )
                        )

                        custom_qty = 0
                        for _, custom_row in custom_group.iterrows():
                            qty = int(custom_row["quantity"])
                            custom_qty += qty
                            safe_custom = html.escape(str(custom_row["stats_custom"]))
                            st.markdown(
                                f'<div class="custom-text">{safe_custom} ×{qty}</div>',
                                unsafe_allow_html=True
                            )

                        # 若部分訂單沒有任何客製需求，補上「無客製」，
                        # 讓明細數量可以完整對應左側的餐點總數。
                        no_custom_qty = int(row["總量"]) - custom_qty
                        # 只有「部分有客製、部分無客製」時才補顯示「無客製」。
                        # 如果全部都無客製，整個餐點沒有其他客製明細，
                        # 就不需要再顯示「無客製 ×總數」。
                        if custom_qty > 0 and no_custom_qty > 0:
                            st.markdown(
                                f'<div class="custom-text">無客製 ×{no_custom_qty}</div>',
                                unsafe_allow_html=True
                            )

                st.markdown("<hr class='soft-divider'>", unsafe_allow_html=True)

            st.metric("本區總額", f"${df_source['price'].sum()}")

        with c_det:
            st.markdown("**📋 訂單明細**")
            grouped_by_person = df_source.groupby('name')
            for name, group in grouped_by_person:
                group = group.sort_values(
                    ["stats_order_1", "stats_order_2", "stats_order_3", "stats_order_4", "stats_custom"],
                    kind="stable",
                )
                with st.container():
                    safe_user = html.escape(str(name))
                    st.markdown(f'<div style="font-size:1.15rem; font-weight:700; margin-bottom:6px; color:var(--text-color);">👤 {safe_user}</div>', unsafe_allow_html=True)
                    for _, row in group.iterrows():
                        safe_item = html.escape(str(row["item_name"]))
                        safe_cst_html = ""
                        if row['stats_custom']:
                            safe_cst = html.escape(str(row['stats_custom']))
                            safe_cst_html = f'<div class="custom-text">{safe_cst}</div>'
                            
                        st.markdown(
                            f'<div class="list-row">'
                            f'  <div class="list-col-left">'
                            f'    <div class="list-title-group"><span class="list-name">{safe_item}</span> <span class="list-qty">× {row["quantity"]}</span></div>'
                            f'    {safe_cst_html}'
                            f'  </div>'
                            f'  <div class="list-price">${row["price"]}</div>'
                            f'</div>', unsafe_allow_html=True
                        )
                st.markdown("<hr class='person-divider'>", unsafe_allow_html=True)

    show_stats_optimized(df_all[df_all['category'] == '主餐'].copy(), f"🍱 {r_name} (主餐)", "header-food")
    st.divider()
    show_stats_optimized(df_all[df_all['category'] == '飲料'].copy(), f"🥤 {d_name} (飲料)", "header-drink")

# ==========================================
# 5. 收款管理 
# ==========================================
@st.fragment(run_every="10s")
def render_payment_section():
    c_ref_text, c_ref_btn = st.columns([8, 1], vertical_alignment="center")
    with c_ref_text:
        st.markdown(f'<div style="text-align:right; color:color-mix(in srgb, var(--text-color) 58%, transparent); font-size:0.9rem; margin:0; padding:0;">更新於 {taiwan_now_str().split(" ")[1]}</div>', unsafe_allow_html=True)
    with c_ref_btn:
        if st.button("🔄", help="立即重新整理收款資料", use_container_width=True, key="btn_refresh_payment"): st.rerun()

    try:
        order_view = backup_guard.order_snapshot()
    except (SafetyBlocked,sqlite3.OperationalError):
        st.error("⚠️ 暫時無法取得可驗證的訂單快照，請稍後重新整理收款資料。")
        return
    df_all = pd.DataFrame(order_view["rows"],columns=ORDER_COLUMNS)
    if df_all.empty: st.write("尚無訂單。"); return
    
    shop_names = get_current_shop_names()
    main_shop = shop_names["main"]
    drink_shop = shop_names["drink"]
    
    df_all["price"] = pd.to_numeric(df_all["price"], errors="coerce").fillna(0).astype(int)
    df_main = df_all[df_all['category'] == '主餐'].copy()
    df_drink = df_all[df_all['category'] == '飲料'].copy()
    
    c_main_prog, c_drink_prog = st.columns(2)
    
    with c_main_prog:
        total_m = df_main['price'].sum() if not df_main.empty else 0
        paid_m = df_main[df_main['is_paid'] == 1]['price'].sum() if not df_main.empty else 0
        prog_m = min(1.0, paid_m / total_m) if total_m > 0 else 0.0
        st.markdown(f'<div class="section-header header-food" style="margin-bottom:8px;">'
                    f'<div>🍱 {html.escape(main_shop)} 收款</div><div>${paid_m} / ${total_m}</div></div>', unsafe_allow_html=True)
        st.progress(prog_m)
        if prog_m >= 1.0 and total_m > 0: st.caption("🎉 主餐款項已全數收齊！")

    with c_drink_prog:
        total_d = df_drink['price'].sum() if not df_drink.empty else 0
        paid_d = df_drink[df_drink['is_paid'] == 1]['price'].sum() if not df_drink.empty else 0
        prog_d = min(1.0, paid_d / total_d) if total_d > 0 else 0.0
        st.markdown(f'<div class="section-header header-drink" style="margin-bottom:8px;">'
                    f'<div>🥤 {html.escape(drink_shop)} 收款</div><div>${paid_d} / ${total_d}</div></div>', unsafe_allow_html=True)
        st.progress(prog_d)
        if prog_d >= 1.0 and total_d > 0: st.caption("🎉 飲料款項已全數收齊！")

    st.write("") 
    
    t1, t2 = st.tabs([f"🍱 主餐訂單（{main_shop}）", f"🥤 飲料訂單（{drink_shop}）"])
    with t1: _pay_logic_grouped("主餐", df_main, "main", order_view)
    with t2: _pay_logic_grouped("飲料", df_drink, "drink", order_view)

def _pay_logic_grouped(cat, df, k, order_view):
    if df.empty: st.caption("目前無資料"); return
    unpaid_df = df[df['is_paid'] == 0]
    
    if not unpaid_df.empty:
        grouped_unpaid = unpaid_df.groupby('name')
        st.markdown(f"**⚠️ 待收款（{len(grouped_unpaid)} 人）**")
        for name, group in grouped_unpaid:
            with st.container():
                total_price = group['price'].sum()
                ids = group['id'].tolist()
                
                c_header, c_btn = st.columns([4, 1], vertical_alignment="center")
                with c_header:
                    st.markdown(f'<div style="display:flex; align-items:center; font-size:1.15rem; margin-bottom:6px;">'
                                f'<b>👤 {html.escape(str(name))}</b>'
                                f'<span style="margin-left:auto; color:#FF4B4B; font-weight:700;">應收: ${total_price}</span>'
                                f'</div>', unsafe_allow_html=True)
                with c_btn:
                    token_key=f"pay_token_{k}_{name}"
                    st.caption(f"確認此人全部未付{cat}：{len(group)} 筆，${total_price}")
                    if st.button("確認收款", key=f"pay_{k}_{name}_{order_view['generation']}", use_container_width=True, type="primary",
                                 on_click=remember_order_token,args=(token_key,backup_guard.order_token(order_view,ids))):
                        token=st.session_state.pop(token_key,None)
                        affected = execute_order_action("payment",token)
                        if token is not None and affected == len(token["rows"]):
                            save_and_sync()
                            confirmed_total=sum(row[4] for row in token["rows"])
                            st.toast(f"💰 已完成收款：{name} (${confirmed_total})")
                            st.rerun()
                        else:
                            st.error("⚠️ 收款狀態未完整更新，請重新整理後確認。")
                
                for _, row in group.iterrows():
                    safe_item = html.escape(str(row["item_name"]))
                    safe_cst_html = ""
                    if row['custom']:
                        # 飲料原本以 "/" 儲存尺寸／甜度／冰塊，
                        # 收款管理顯示時統一改成 "・"，與主餐及統計看板一致。
                        raw_cst = str(row['custom'])
                        if cat == "飲料":
                            raw_cst = raw_cst.replace("/", "・")
                        safe_cst = html.escape(raw_cst)
                        safe_cst_html = f'<div class="custom-text">{safe_cst}</div>'
                        
                    st.markdown(
                        f'<div class="list-row">'
                        f'  <div class="list-col-left">'
                        f'    <div class="list-title-group"><span class="list-name">{safe_item}</span> <span class="list-qty">× {row["quantity"]}</span></div>'
                        f'    {safe_cst_html}'
                        f'  </div>'
                        f'  <div class="list-price">${row["price"]}</div>'
                        f'</div>', unsafe_allow_html=True
                    )
            st.markdown("<hr class='person-divider'>", unsafe_allow_html=True)
    else: st.success("👍 此區款項已全數收齊！")

    paid_df = df[df['is_paid'] == 1]
    if not paid_df.empty:
        st.write(""); grouped_paid = paid_df.groupby('name')
        with st.expander(f"✅ 已收款名單（{len(grouped_paid)} 人）- 點此展開管理"):
            for name, group in grouped_paid:
                total_price = group['price'].sum()
                ids = group['id'].tolist()
                c1, c2 = st.columns([4, 1], vertical_alignment="center")
                with c1: st.write(f"~~{html.escape(str(name))} (${total_price})~~") 
                with c2:
                    token_key=f"undo_token_{k}_{name}"
                    st.caption(f"撤銷此人全部已付{cat}：{len(group)} 筆，${total_price}")
                    if st.button("撤銷收款", key=f"undo_{k}_{name}_{order_view['generation']}", use_container_width=True,
                                 on_click=remember_order_token,args=(token_key,backup_guard.order_token(order_view,ids))):
                        token=st.session_state.pop(token_key,None)
                        affected = execute_order_action("undo_payment",token)
                        if token is not None and affected == len(token["rows"]):
                            save_and_sync()
                            st.toast(f"↩️ 已撤銷收款：{name}")
                            st.rerun()
                        else:
                            st.error("⚠️ 收款狀態未完整更新，請重新整理後確認。")

# ==========================================
# 6. 主畫面與 Dialogs 邏輯
# ==========================================
# 主餐尺寸：固定選項。「無」為預設值，儲存時不寫入 custom。

st.title("🍱 點餐哦各位～")
backup_notice=backup_guard.status()
if backup_notice["kind"]!="success":
    st.warning(f"☁️ {backup_notice['message']}")
tab1, tab2, tab3 = st.tabs(["📝 開始點餐", "📊 餐點統計", "💰 收款管理"])

@st.dialog("👤 請選擇您的姓名")
def login_dialog():
    st.caption("請選擇您的姓名以開始點餐")
    try:
        member_view = backup_guard.member_snapshot()
    except Exception:
        st.error("⚠️ 無法確認人員身分，請重新整理後再選名。")
        return
    selected = st.pills("姓名", [row[1] for row in member_view["rows"]], selection_mode="single", label_visibility="collapsed",
                       key=f"member_picker_{member_view['generation']}_{member_view['rows']}")
    if selected:
        matches = [row for row in member_view["rows"] if row[1] == selected]
        if len(matches) != 1:
            st.error("⚠️ 人員名單已變更，請重新選擇姓名。")
            return
        st.session_state['member_token'] = {"member_id": matches[0][0], "name": matches[0][1],
                                             "generation": member_view["generation"]}
        st.session_state['user_name'] = selected
        st.rerun()

@st.dialog("🎨 客製需求")
def custom_dialog(key_prefix, tag_options):
    st.caption("常用客製需求（可複選）")
    current_tags = st.session_state.get(f"{key_prefix}_tags", [])
    current_manual = st.session_state.get(f"{key_prefix}_manual", "")

    valid_tag_set = {str(value) for value in tag_options}
    current_tags = [
        str(value) for value in current_tags
        if value is not None and str(value) in valid_tag_set
    ]

    if tag_options:
        new_tags = st.pills(
            "客製選項",
            tag_options,
            default=current_tags,
            selection_mode="multi",
            label_visibility="collapsed",
            key=f"{key_prefix}_pills_widget"
        )
    else:
        new_tags = []
        st.caption("目前沒有可用的常用客製需求")

    st.markdown("---")
    new_manual = st.text_input("其他客製需求", value=current_manual, placeholder="例如：不要香菜、加珍珠、醬料另外放", key=f"{key_prefix}_manual_widget").strip()
    if st.button("✅ 完成", use_container_width=True, type="primary"):
        st.session_state[f"{key_prefix}_tags"] = new_tags
        st.session_state[f"{key_prefix}_manual"] = new_manual
        st.rerun()

@st.dialog("✏️ 編輯餐點")
def edit_order_dialog(order_id, category, cur_name, cur_price_total, cur_qty, cur_custom, order_token):
    # 保留開啟畫面的代次與原始快照；不能只按目前 ID 重讀後當作同一筆。
    try:
        expected = backup_guard.expected_order(order_token)
        if expected["id"] != order_id:
            raise OrderConflict("編輯請求與原始訂單不一致，請重新開啟。")
        current = pd.DataFrame([expected])
    except (OrderConflict,SafetyBlocked) as e:
        st.error(f"⚠️ {e}")
        return
    except sqlite3.OperationalError:
        st.error("⚠️ 暫時無法核對訂單，請稍後重新開啟編輯。")
        return
    edit_key=f"{order_id}_{order_token['generation']}"
    if current.empty:
        st.error("⚠️ 找不到這筆訂單，可能已被刪除。")
        return
    if int(current.iloc[0]["is_paid"] or 0) == 1:
        st.warning("🔒 這筆訂單已完成收款，無法修改。")
        return

    # 以資料庫實際 category 為準，避免畫面傳入值與資料庫不一致。
    edit_category = str(current.iloc[0]["category"] or category)

    db_unit_price = current.iloc[0]["unit_price"]
    if pd.isna(db_unit_price):
        unit_price = 0
        st.warning("⚠️ 這是舊版資料，原始單價無法精確還原，請重新輸入單價。")
    else:
        unit_price = int(db_unit_price)

    new_name = st.text_input(
        "餐點名稱" if edit_category == "主餐" else "飲料名稱",
        value=str(cur_name),key=f"edit_item_{edit_key}"
    ).strip()

    c_p, c_q = st.columns(2)
    new_unit_price = c_p.number_input("單價（元）", min_value=0, step=5, value=unit_price,key=f"edit_price_{edit_key}")
    new_qty = c_q.number_input("數量", min_value=1, step=1, value=int(cur_qty),key=f"edit_qty_{edit_key}")

    # ------------------------------------------------------
    # 主餐：與新增主餐相同的辣度 + 客製化快速選項 + 手動輸入
    # ------------------------------------------------------
    if edit_category == "主餐":
        # 解析既有 custom：
        # 第一個區段若是目前辣度選項，就視為辣度；
        # 其餘符合快速選項者恢復成已選按鈕，剩餘內容保留為手動客製。
        raw_custom = str(cur_custom or "").strip()
        main_spicy = None
        main_tags = []
        main_manual = ""

        custom_parts = [
            part.strip() for part in raw_custom.split(",")
            if part is not None and part.strip()
        ]

        # 新格式：尺寸, 辣度, 快速客製...
        # 舊資料沒有尺寸時，第一段仍可能直接是辣度，因此兼容舊格式。
        main_size = "無"
        if custom_parts and custom_parts[0] in get_options("main_size"):
            main_size = custom_parts.pop(0)

        if custom_parts and custom_parts[0] in spicy_levels:
            main_spicy = custom_parts.pop(0)

        valid_main_tags = {str(v) for v in custom_tags_main}
        recognized_tags = []
        manual_parts = []
        for part in custom_parts:
            if part in valid_main_tags:
                recognized_tags.append(part)
            else:
                manual_parts.append(part)

        main_tags = recognized_tags
        main_manual = ", ".join(manual_parts)

        # Widget key 使用 order_id + 資料代次，避免同 ID 的匯入資料沿用舊草稿。
        spicy_key = f"edit_m_spicy_{edit_key}"
        tags_key = f"edit_m_tags_{edit_key}"
        manual_key = f"edit_m_manual_{edit_key}"

        size_key = f"edit_m_size_{edit_key}"

        if size_key not in st.session_state:
            st.session_state[size_key] = main_size if main_size in main_size_options else get_default_option("main_size")
        if spicy_key not in st.session_state:
            st.session_state[spicy_key] = main_spicy if main_spicy in spicy_levels else (spicy_levels[0] if spicy_levels else None)
        if manual_key not in st.session_state:
            st.session_state[manual_key] = main_manual

        if st.session_state.get(size_key) not in main_size_options:
            st.session_state[size_key] = get_default_option("main_size")
        edit_size = st.pills(
            "尺寸（必選）",
            main_size_options,
            key=size_key,
            selection_mode="single"
        )

        if spicy_levels:
            if st.session_state.get(spicy_key) not in spicy_levels:
                st.session_state[spicy_key] = spicy_levels[0]
            edit_spicy = st.pills(
                "辣度",
                spicy_levels,
                key=spicy_key,
                selection_mode="single"
            )
        else:
            edit_spicy = None
            st.caption("辣度：目前沒有可選項目")

        tags_widget_key = f"edit_m_tags_widget_{edit_key}"
        if tags_widget_key not in st.session_state:
            valid_tag_set = {str(v) for v in custom_tags_main}
            st.session_state[tags_widget_key] = [
                str(v) for v in main_tags
                if v is not None and str(v) in valid_tag_set
            ]

        if custom_tags_main:
            edit_tags = st.pills(
                "常用客製需求（可複選）",
                custom_tags_main,
                key=tags_widget_key,
                selection_mode="multi"
            )
        else:
            edit_tags = []
            st.caption("目前沒有可用的常用客製需求")

        edit_manual = st.text_input(
            "其他客製需求",
            value=st.session_state.get(manual_key, ""),
            placeholder="例如：不要香菜、加珍珠、醬料另外放",
            key=f"{manual_key}_widget"
        ).strip()

        # --------------------------------------------------
        # 儲存前重新組合成與新增主餐一致的 custom 格式
        # --------------------------------------------------
        clean_tags = [
            str(v).strip() for v in edit_tags
            if v is not None and str(v).strip()
        ]
        parts = []
        if edit_size and str(edit_size).strip() != "無":
            parts.append(str(edit_size).strip())
        if edit_spicy and str(edit_spicy).strip() != "無":
            parts.append(str(edit_spicy).strip())
        if clean_tags:
            parts.append(", ".join(clean_tags))
        if edit_manual:
            parts.append(edit_manual)

        new_custom = ", ".join(parts) if parts else ""

    # ------------------------------------------------------
    # 飲料：與新增飲料相同的尺寸 + 甜度 + 冰塊 + 客製化
    # ------------------------------------------------------
    else:
        raw_custom = str(cur_custom or "").strip()

        drink_size_key = f"edit_d_size_{edit_key}"
        drink_sugar_key = f"edit_d_sugar_{edit_key}"
        drink_ice_key = f"edit_d_ice_{edit_key}"
        drink_tags_key = f"edit_d_tags_{edit_key}"
        drink_manual_key = f"edit_d_manual_{edit_key}"

        # 編輯沿用原值，不把停用選項搬到備註後再添加新預設。
        # 停用值只加入這筆訂單的選單，不修改全域選項或備份格式。
        base_part, separator, tag_part = raw_custom.partition(",")
        base_values = [v.strip() for v in base_part.split("/")]
        parsed = [None, None, None]
        if "/" in base_part or base_part.strip() in drink_size_options:
            parsed = [(base_values[i] or None) if i < len(base_values) else None for i in range(3)]
            remainder = ", ".join([v for v in base_values[3:] if v] +
                                  ([tag_part.strip()] if separator and tag_part.strip() else []))
        else:
            remainder = raw_custom  # 舊純文字客製：保留為手動內容，不猜測基本選項。

        edit_options = []
        for label, values, key, original in (
            ("尺寸", drink_size_options, drink_size_key, parsed[0]),
            ("甜度", sugar_levels, drink_sugar_key, parsed[1]),
            ("冰塊", ice_levels, drink_ice_key, parsed[2]),
        ):
            options = list(values)
            if original and original not in options:
                options.append(original)
                st.caption(f"{label}原選項「{original}」已停用；未更改時保留原值。")
            if key not in st.session_state:
                st.session_state[key] = original
            if st.session_state.get(key) is not None and st.session_state[key] not in options:
                st.session_state[key] = original
            if options:
                selected = st.pills(label, options, key=key, selection_mode="single")
            else:
                selected = None
                st.caption(f"{label}：目前沒有可選項目")
            edit_options.append(selected)
        edit_size, edit_sugar, edit_ice = edit_options

        # 從 remainder 恢復快速客製與手動客製。
        valid_drink_tags = {str(v) for v in custom_tags_drink}
        parsed_tags = []
        manual_parts = []
        for part in [
            p.strip() for p in remainder.split(",")
            if p is not None and p.strip()
        ]:
            if part in valid_drink_tags:
                parsed_tags.append(part)
            else:
                manual_parts.append(part)

        if drink_manual_key not in st.session_state:
            st.session_state[drink_manual_key] = ", ".join(manual_parts)

        drink_tags_widget_key = f"edit_d_tags_widget_{edit_key}"
        if drink_tags_widget_key not in st.session_state:
            st.session_state[drink_tags_widget_key] = [
                str(v) for v in parsed_tags
                if v is not None and str(v) in valid_drink_tags
            ]

        if custom_tags_drink:
            edit_drink_tags = st.pills(
                "常用客製需求（可複選）",
                custom_tags_drink,
                key=drink_tags_widget_key,
                selection_mode="multi"
            )
        else:
            edit_drink_tags = []
            st.caption("目前沒有可用的常用客製需求")

        edit_drink_manual = st.text_input(
            "其他客製需求",
            value=st.session_state.get(drink_manual_key, ""),
            placeholder="例如：加珍珠、不要冰、醬料另外放",
            key=f"{drink_manual_key}_widget"
        ).strip()

        clean_drink_options = [
            str(v).strip() for v in [edit_size, edit_sugar, edit_ice]
            if v is not None and str(v).strip()
        ]
        new_custom = "/".join(clean_drink_options)

        clean_drink_tags = [
            str(v).strip() for v in edit_drink_tags
            if v is not None and str(v).strip()
        ]
        if clean_drink_tags:
            new_custom += f"{', ' if new_custom else ''}{', '.join(clean_drink_tags)}"
        if edit_drink_manual:
            new_custom += f"{', ' if new_custom else ''}{edit_drink_manual}"

    if st.button(
        "💾 儲存修改",
        type="primary",
        use_container_width=True,
        key=f"save_edit_{edit_key}",
    ):
        # 直接在 Dialog 的本次執行中儲存。
        # 不使用 on_click callback，避免 Dialog callback 中觸發 rerun
        # 造成白畫面；所有 widget 值均取本次執行當下的回傳值。
        if edit_category == "主餐":
            save_tags = [
                str(v).strip() for v in (edit_tags or [])
                if v is not None and str(v).strip()
            ]
            save_manual = str(
                st.session_state.get(f"{manual_key}_widget", edit_manual)
            ).strip()

            save_parts = []
            if edit_size and str(edit_size).strip() != "無":
                save_parts.append(str(edit_size).strip())
            if edit_spicy and str(edit_spicy).strip() != "無":
                save_parts.append(str(edit_spicy).strip())
            if save_tags:
                save_parts.append(", ".join(save_tags))
            if save_manual:
                save_parts.append(save_manual)
            save_custom = ", ".join(save_parts) if save_parts else ""
        else:
            save_tags = [
                str(v).strip() for v in (edit_drink_tags or [])
                if v is not None and str(v).strip()
            ]
            save_manual = str(
                st.session_state.get(f"{drink_manual_key}_widget", edit_drink_manual)
            ).strip()

            save_options = [
                str(v).strip() for v in [edit_size, edit_sugar, edit_ice]
                if v is not None and str(v).strip()
            ]
            save_custom = "/".join(save_options)

            if save_tags:
                save_custom += f"{', ' if save_custom else ''}{', '.join(save_tags)}"
            if save_manual:
                save_custom += f"{', ' if save_custom else ''}{save_manual}"

        if edit_category == "主餐" and edit_size not in main_size_options:
            st.error("請選擇餐點尺寸")
        elif not new_name:
            st.error("餐點名稱不能為空")
        else:
            new_total = int(new_unit_price) * int(new_qty)
            affected = execute_order_action("edit",order_token,{
                "item_name":new_name,"price":new_total,"quantity":int(new_qty),
                "unit_price":int(new_unit_price),"custom":save_custom,
            })

            if affected == 1:
                save_and_sync()
                for _prefix in (
                    "edit_m_size_", "edit_m_spicy_", "edit_m_tags_",
                    "edit_m_manual_", "edit_d_size_", "edit_d_sugar_",
                    "edit_d_ice_", "edit_d_tags_", "edit_d_manual_",
                ):
                    st.session_state.pop(f"{_prefix}{edit_key}", None)
                    st.session_state.pop(f"{_prefix}{edit_key}_widget", None)

                st.session_state.pop(f"edit_m_tags_widget_{edit_key}", None)
                st.session_state.pop(f"edit_d_tags_widget_{edit_key}", None)

                for field in ("item","price","qty"):
                    st.session_state.pop(f"edit_{field}_{edit_key}",None)
                st.toast("✅ 餐點已成功更新！")
                st.rerun()
            elif affected in (0,None):
                st.error(
                    "⚠️ 餐點沒有更新，可能已完成收款或資料已被其他人修改，"
                    "請重新開啟編輯後再試。"
                )
            else:
                st.error(f"⚠️ 更新筆數異常（{affected}），請重新整理後確認。")



def render_my_orders(user_name):
    try:
        order_view = backup_guard.order_snapshot(user_name)
    except (SafetyBlocked,sqlite3.OperationalError):
        st.error("⚠️ 暫時無法取得可驗證的訂單快照，請稍後重新整理。")
        return
    my_orders = pd.DataFrame(order_view["rows"],columns=ORDER_COLUMNS)
    my_sum = my_orders['price'].sum() if not my_orders.empty else 0
    with st.expander(f"📋 {html.escape(str(user_name))} 的訂單（合計：${my_sum}）", expanded=True if not my_orders.empty else False):
        if my_orders.empty: st.caption("目前尚無訂單")
        else:
            for _, row in my_orders.iterrows():
                c_icon, c_info, c_btn1, c_btn2 = st.columns([0.4, 4.5, 0.7, 0.7], vertical_alignment="center")

                c_icon.markdown('<div style="font-size:1.4rem; text-align:center;">' + ("🍱" if row['category'] == '主餐' else "🥤") + '</div>', unsafe_allow_html=True)

                safe_item_name = html.escape(str(row["item_name"]))
                safe_cst_html = ""
                if row['custom']:
                    safe_custom = html.escape(str(row['custom']))
                    safe_cst_html = f'<div class="custom-text">{safe_custom}</div>'

                c_info.markdown(
                    f'<div style="display:flex; flex-direction:column; justify-content:center;">'
                    f'  <div style="display:flex; align-items:center; justify-content:space-between; width:100%;">'
                    f'    <div><span class="list-name">{safe_item_name}</span> <span class="list-qty">× {row["quantity"]}</span></div>'
                    f'    <div class="list-price" style="margin-right:15px;">${row["price"]}</div>'
                    f'  </div>'
                    f'  {safe_cst_html}'
                    f'</div>', unsafe_allow_html=True
                )

                is_paid = int(row["is_paid"] or 0) == 1

                if c_btn1.button(
                    "🔒" if is_paid else "✏️",
                    key=f"btn_edit_{row['id']}_{order_view['generation']}",
                    help="已完成收款，無法修改" if is_paid else "修改",
                    use_container_width=True,
                    disabled=is_paid,
                    on_click=remember_order_token,args=("clicked_edit_token",backup_guard.order_token(order_view,[row['id']]))
                ):
                    # 不在 5 秒 fragment 內直接開 Dialog。
                    # 先記住要編輯的訂單，再做一次完整 app rerun，
                    # 由主流程開啟 Dialog，避免 fragment 自動刷新與 Dialog 狀態互相干擾。
                    st.session_state["_edit_order_request"] = {
                        "id": int(row["id"]),
                        "category": row["category"],
                        "item_name": row["item_name"],
                        "price": row["price"],
                        "quantity": row["quantity"],
                        "custom": row["custom"],
                        "order_token": st.session_state.pop("clicked_edit_token",None),
                    }
                    st.rerun()

                if is_paid:
                    c_btn2.button(
                        "🔒",
                        key=f"btn_del_locked_{row['id']}",
                        help="已完成收款，無法刪除",
                        use_container_width=True,
                        disabled=True
                    )
                else:
                    with c_btn2.popover("🗑️", help="刪除", use_container_width=True):
                        st.write(f"刪除 **{safe_item_name}**？")
                        token_key=f"delete_token_{row['id']}"
                        if st.button("⭕ 確認", key=f"confirm_del_{row['id']}_{order_view['generation']}", type="primary", use_container_width=True,
                                     on_click=remember_order_token,args=(token_key,backup_guard.order_token(order_view,[row['id']]))):
                            # SQL 再次檢查 is_paid，避免畫面舊資料造成誤刪。
                            if execute_order_action("delete",st.session_state.pop(token_key,None)):
                                save_and_sync()
                                st.toast("✅ 已刪除")
                                st.rerun()

                st.markdown("<hr class='soft-divider'>", unsafe_allow_html=True)
    st.write("")

if 'user_name' not in st.session_state: st.session_state['user_name'] = None
if 'm_custom_tags' not in st.session_state: st.session_state['m_custom_tags'] = []
if 'm_custom_manual' not in st.session_state: st.session_state['m_custom_manual'] = ""
if 'd_custom_tags' not in st.session_state: st.session_state['d_custom_tags'] = []
if 'd_custom_manual' not in st.session_state: st.session_state['d_custom_manual'] = ""

with tab2: render_stats_section()
with tab3: render_payment_section()

with tab1:
    if st.button("🔄 重新整理資料", type="secondary", use_container_width=True): st.rerun()
    
    with st.container():
        st.markdown('<h5>👤 請選擇您的姓名</h5>', unsafe_allow_html=True)
        c_user, c_btn = st.columns([3, 1.5], vertical_alignment="center")
        with c_user:
            if st.session_state['user_name']: st.info(f"目前使用者：**{html.escape(str(st.session_state['user_name']))}**")
            else: st.warning("⚠️ 尚未選擇姓名")
        with c_btn:
            if st.button("👤 選擇／切換使用者", use_container_width=True, type="primary" if not st.session_state['user_name'] else "secondary"):
                login_dialog()
        if not st.session_state['user_name']: st.stop()

    user_name = st.session_state['user_name']

    render_my_orders(user_name)

    # 編輯 Dialog 必須由主 App 流程開啟，而不是從 5 秒 fragment 內直接開啟。
    # 這樣 fragment 的週期性 rerun 不會干擾 Dialog 的 widget/session state。
    edit_request = st.session_state.get("_edit_order_request")
    if edit_request:
        st.session_state.pop("_edit_order_request", None)
        edit_order_dialog(
            edit_request["id"],
            edit_request["category"],
            edit_request["item_name"],
            edit_request["price"],
            edit_request["quantity"],
            edit_request["custom"],
            edit_request.get("order_token"),
        )

    current_main_shop = get_shop_name("main")
    current_drink_shop = get_shop_name("drink")

    c_food, c_drink = st.columns(2)
    with c_food:
        st.markdown(f'<div class="section-header header-food"><div>🍱 {html.escape(str(current_main_shop))} (主餐)</div></div>', unsafe_allow_html=True)
        with st.container():
            m_name_raw = st.text_input("餐點名稱", placeholder="例如：肉圓、雞排飯", key="m_name")
            m_name = m_name_raw.strip()
            
            cp, cq = st.columns(2)
            m_price_unit = cp.number_input("單價（元）", min_value=0, step=5, format="%d", key="m_price")
            m_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="m_qty")
            # Streamlit widget 的 key 必須在 widget 建立前修改。
            # 若上一輪要求重設尺寸，本輪先重設，再建立 st.pills。
            if st.session_state.pop("_reset_m_size", False):
                st.session_state["m_size"] = get_default_option("main_size")

            # 主餐尺寸固定為「無 / 小份 / 大份」；「無」可由資料庫設定為預設；預設值不寫入 custom。
            if st.session_state.get("m_size") not in main_size_options:
                st.session_state["m_size"] = get_default_option("main_size")
            if main_size_options:
                if st.session_state.get("m_size") not in main_size_options:
                    st.session_state["m_size"] = get_default_option("main_size") or main_size_options[0]
                m_size = st.pills(
                    "尺寸（必選）",
                    main_size_options,
                    key="m_size",
                    selection_mode="single"
                )
            else:
                m_size = None
                st.caption("主餐尺寸：目前沒有可選項目")

            # 管理員修改選項後，舊的 session_state 可能暫時保留已不存在的值。
            # 
            if spicy_levels:
                spicy_default = get_default_option("spicy") or spicy_levels[0]
                if st.session_state.get("m_spicy") not in spicy_levels:
                    st.session_state["m_spicy"] = spicy_default
                m_spicy = st.pills(
                    "辣度", spicy_levels,
                    key="m_spicy", selection_mode="single"
                )
            else:
                m_spicy = None
                st.caption("辣度：目前沒有可選項目")

            current_tags = st.session_state.get("m_custom_tags", [])
            current_manual = st.session_state.get("m_custom_manual", "")
            display_list = current_tags.copy()
            if current_manual:
                display_list.append(current_manual)

            # 尺寸是獨立欄位，不算客製化。
            # 只有快速客製與手動客製才會影響客製化按鈕的狀態。
            display_parts = [
                str(value).strip()
                for value in display_list
                if value is not None and str(value).strip()
            ]
            display_text = ", ".join(display_parts) if display_parts else "無"
            display_count = len(display_parts)

            btn_type = "primary" if display_parts else "secondary"
            btn_label = f"🎨 客製需求（已選 {display_count} 項）" if display_count else "🎨 客製需求（無）"

            c_cust_btn, c_cust_clear = st.columns([4, 1])
            with c_cust_btn:
                if st.button(btn_label, type=btn_type, use_container_width=True, key="btn_m_custom"):
                    custom_dialog("m_custom", custom_tags_main)
            with c_cust_clear:
                if st.button("❌", help="清空主餐客製需求", use_container_width=True, key="clr_m_custom"):
                    st.session_state["m_custom_tags"] = []
                    st.session_state["m_custom_manual"] = ""
                    st.rerun()
            
            if display_parts: st.caption(f"ℹ️ 即將加入：{html.escape(str(display_text))}")

            if st.button("＋ 加入主餐", type="primary", use_container_width=True):
                if m_size not in main_size_options:
                    st.toast("🚫 無法加入：請選擇餐點尺寸！", icon="⚠️")
                elif m_price_unit == 0:
                    st.toast("🚫 無法加入：請輸入金額！", icon="⚠️")
                elif m_name:
                    parts = []

                    # 尺寸與辣度一樣，「無」代表不需要特別註記，因此不寫入 custom。
                    if m_size and str(m_size).strip() != "無":
                        parts.append(str(m_size).strip())

                    # m_spicy 可能因 Secrets 修改而暫時為 None；只有有效文字才加入。
                    if m_spicy and str(m_spicy).strip() != "無":
                        parts.append(str(m_spicy).strip())

                    clean_display_list = [
                        str(value).strip()
                        for value in display_list
                        if value is not None and str(value).strip()
                    ]
                    if clean_display_list:
                        parts.append(", ".join(clean_display_list))

                    cust = ", ".join(parts) if parts else ""
                    
                    total_p = int(m_price_unit) * int(m_qty)
                    if create_member_order(
                        st.session_state.get("member_token"),
                        (
                            "主餐", m_name, total_p, cust, int(m_qty),
                            taiwan_now().strftime('%Y-%m-%d %H:%M'), int(m_price_unit)
                        )
                    ):
                        save_and_sync()
                        # 新增成功後只清除「客製化」內容。
                        # 尺寸、辣度都屬於目前點餐介面的選擇，保留使用者剛才的設定，
                        # 與飲料尺寸/甜度/冰塊的操作方式一致。
                        st.session_state["m_custom_tags"] = []
                        st.session_state["m_custom_manual"] = ""
                        st.toast(f"✅ 已加入：{m_name} ×{m_qty}"); st.rerun()
                else: st.toast("⚠️ 請輸入餐點名稱")

    with c_drink:
        st.markdown(f'<div class="section-header header-drink"><div>🥤 {html.escape(str(current_drink_shop))} (飲料)</div></div>', unsafe_allow_html=True)
        with st.container():
            d_name_raw = st.text_input("飲料名稱", placeholder="例如：紅茶、青茶", key="d_name")
            d_name = d_name_raw.strip()
            
            cp, cq = st.columns(2)
            d_price_unit = cp.number_input("單價（元）", min_value=0, step=5, format="%d", key="d_price")
            d_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="d_qty")
            
            if drink_size_options:
                if st.session_state.get("d_size") not in drink_size_options:
                    st.session_state["d_size"] = get_default_option("drink_size") or drink_size_options[0]
                d_size = st.pills(
                    "尺寸（必選）",
                    drink_size_options,
                    key="d_size",
                    selection_mode="single"
                )
            else:
                d_size = None
                st.caption("飲料尺寸：目前沒有可選項目")

            if sugar_levels:
                sugar_default = get_default_option("sugar") or sugar_levels[0]
                if st.session_state.get("d_sugar") not in sugar_levels:
                    st.session_state["d_sugar"] = sugar_default
                d_sugar = st.pills(
                    "甜度", sugar_levels,
                    key="d_sugar", selection_mode="single"
                )
            else:
                d_sugar = None
                st.caption("甜度：目前沒有可選項目")

            if ice_levels:
                ice_default = get_default_option("ice") or ice_levels[0]
                if st.session_state.get("d_ice") not in ice_levels:
                    st.session_state["d_ice"] = ice_default
                d_ice = st.pills(
                    "冰塊", ice_levels,
                    key="d_ice", selection_mode="single"
                )
            else:
                d_ice = None
                st.caption("冰塊：目前沒有可選項目")
            
            d_current_tags = st.session_state.get("d_custom_tags", [])
            d_current_manual = st.session_state.get("d_custom_manual", "")
            d_display_list = d_current_tags.copy()
            if d_current_manual: d_display_list.append(d_current_manual)
            d_display_text = ", ".join(d_display_list) if d_display_list else "無"
            
            d_btn_type = "primary" if d_display_list else "secondary"
            d_btn_label = f"🎨 客製需求（已選 {len(d_display_list)} 項）" if d_display_list else "🎨 客製需求（無）"

            dc_btn, dc_clear = st.columns([4, 1])
            with dc_btn:
                if st.button(d_btn_label, type=d_btn_type, use_container_width=True, key="btn_d_custom"):
                    custom_dialog("d_custom", custom_tags_drink)
            with dc_clear:
                if st.button("❌", help="清空飲料客製需求", use_container_width=True, key="clr_d_custom"):
                    st.session_state["d_custom_tags"] = []
                    st.session_state["d_custom_manual"] = ""
                    st.rerun()

            if d_display_list: st.caption(f"ℹ️ 即將加入：{html.escape(str(d_display_text))}")

            if st.button("＋ 加入飲料", type="primary", use_container_width=True):
                if d_size not in drink_size_options:
                    st.toast("🚫 無法加入：請選擇飲料尺寸！", icon="⚠️")
                elif d_price_unit == 0:
                    st.toast("🚫 無法加入：請輸入金額！", icon="⚠️")
                elif d_name:
                    drink_options = [d_size, d_sugar, d_ice]
                    clean_drink_options = [
                        str(value).strip()
                        for value in drink_options
                        if value is not None and str(value).strip()
                    ]
                    base_config = "/".join(clean_drink_options)

                    clean_drink_tags = [
                        str(value).strip()
                        for value in d_display_list
                        if value is not None and str(value).strip()
                    ]

                    final_cust = base_config
                    if clean_drink_tags:
                        final_cust += f"{', ' if final_cust else ''}{', '.join(clean_drink_tags)}"

                    total_p = int(d_price_unit) * int(d_qty)
                    if create_member_order(
                        st.session_state.get("member_token"),
                        (
                            "飲料", d_name, total_p, final_cust, int(d_qty),
                            taiwan_now().strftime('%Y-%m-%d %H:%M'), int(d_price_unit)
                        )
                    ):
                        save_and_sync()
                        # 只清除剛剛送出的飲料客製化，不影響尚未送出的主餐設定。
                        st.session_state["d_custom_tags"] = []
                        st.session_state["d_custom_manual"] = ""
                        st.toast(f"✅ 已加入：{d_name} ×{d_qty}"); st.rerun()
                else: st.toast("⚠️ 請輸入飲料名稱")
