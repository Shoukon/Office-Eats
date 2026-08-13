import streamlit as st
import pandas as pd
import sqlite3
import time
import os
import html
from datetime import datetime

# ==========================================
# 0. 系統設定區
# ==========================================
DB_FILE = "lunch.db"

# orders 固定欄位。統計與收款頁會用這份欄位定義做防禦性處理。
ORDER_COLUMNS = [
    "id", "name", "category", "item_name", "price",
    "custom", "quantity", "order_time", "is_paid", "unit_price"
]

# 人員與點餐選項是辦公室固定設定，統一由 Streamlit Secrets 管理。
# [default_settings] -> colleagues
# [default_options]  -> spicy / ice / sugar / tags / drink_tags
# 這些固定設定不寫入 SQLite，避免產生兩套不同的設定來源.

# ==========================================
# 1. 頁面設定與 CSS (純淨無框線排版核心)
# ==========================================
st.set_page_config(page_title="點餐哦各位～ v3.3.9", page_icon="🍱", layout="wide")

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

    /* 客製化文字統一樣式 (稍微放大至 1.0rem 提升閱讀舒適度) */
    .custom-text {
        font-size: 1.0rem; color: #95a5a6; margin-top: 2px; line-height: 1.4;
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
def get_settings_from_secrets():
    colleagues = []
    options = {"spicy": [], "ice": [], "sugar": [], "tags": [], "drink_tags": []}

    try:
        # 3.9 原本誤用了 [settings]；為相容目前已提供的 Secrets，
        # 正式使用 [default_settings]，並同時接受舊版 [settings]。
        settings = st.secrets.get("default_settings", {})
        if not settings:
            settings = st.secrets.get("settings", {})
        colleagues = list(settings.get("colleagues", []))
    except Exception:
        pass

    try:
        # 正式使用 [default_options]，同時接受舊版 [options]。
        secret_options = st.secrets.get("default_options", {})
        if not secret_options:
            secret_options = st.secrets.get("options", {})
        for key in options:
            options[key] = list(secret_options.get(key, []))
    except Exception:
        pass

    return colleagues, options


def unique_clean_list(values):
    result = []
    seen = set()
    for value in values:
        if value is None:
            continue
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


DEFAULT_COLLEAGUES, DEFAULT_OPTIONS = get_settings_from_secrets()
DEFAULT_COLLEAGUES = unique_clean_list(DEFAULT_COLLEAGUES)
if not DEFAULT_COLLEAGUES:
    DEFAULT_COLLEAGUES = ["請在 Streamlit Secrets 的 [settings] 設定人員"]

for _key in DEFAULT_OPTIONS:
    DEFAULT_OPTIONS[_key] = unique_clean_list(DEFAULT_OPTIONS[_key])

# 「無」是辣度的系統內建選項，不需要寫進 Secrets。
DEFAULT_OPTIONS["spicy"] = [v for v in DEFAULT_OPTIONS["spicy"] if v != "無"]


def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, item_name TEXT,
            price INTEGER, custom TEXT, quantity INTEGER, order_time TEXT, is_paid BOOLEAN,
            unit_price INTEGER)""")

        order_columns = {row[1] for row in c.execute("PRAGMA table_info(orders)").fetchall()}
        if "unit_price" not in order_columns:
            c.execute("ALTER TABLE orders ADD COLUMN unit_price INTEGER")
            c.execute("""UPDATE orders
                         SET unit_price = CASE
                             WHEN quantity > 0 AND price % quantity = 0
                             THEN price / quantity
                             ELSE NULL
                         END
                         WHERE unit_price IS NULL""")

        c.execute("""CREATE TABLE IF NOT EXISTS config_shop (
            category TEXT PRIMARY KEY, shop_name TEXT)""")
        c.execute("INSERT OR IGNORE INTO config_shop (category, shop_name) VALUES (?, ?)",
                  ("main", "吃什麼？"))
        c.execute("INSERT OR IGNORE INTO config_shop (category, shop_name) VALUES (?, ?)",
                  ("drink", "喝什麼？"))
        conn.commit()
    finally:
        conn.close()


def execute_db(query, params=()):
    max_retries = 5
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
            c = conn.cursor()
            c.execute(query, params)
            conn.commit()
            conn.close()
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e): time.sleep(0.1)
            else: raise e
    st.error("⚠️ 系統忙碌 (Database Locked)，請稍後再試")
    return False

def get_db(query, params=()):
    max_retries = 3
    for _ in range(max_retries):
        try:
            conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
            df = pd.read_sql_query(query, conn, params=params)
            conn.close()
            return df
        except sqlite3.OperationalError as e:
            if "locked" in str(e): time.sleep(0.2)
            else: break
        except Exception: break
    return pd.DataFrame()

def get_orders_df():
    """安全讀取 orders；失敗/無資料時仍保留固定欄位。"""
    df = get_db("SELECT * FROM orders")
    if df.empty:
        return pd.DataFrame(columns=ORDER_COLUMNS)

    for column in ORDER_COLUMNS:
        if column not in df.columns:
            if column in ("price", "quantity", "is_paid", "unit_price"):
                df[column] = 0
            else:
                df[column] = ""

    return df[ORDER_COLUMNS].copy()


def get_shop_name(cat):
    df = get_db("SELECT shop_name FROM config_shop WHERE category = ?", (cat,))
    if not df.empty: return df.iloc[0]['shop_name']
    return "未設定"

def set_shop_name(cat, name):
    execute_db("UPDATE config_shop SET shop_name = ? WHERE category = ?", (name, cat))

init_db()

# 人員與點餐選項直接來自 Streamlit Secrets，不再同步到 SQLite。
colleagues_list = DEFAULT_COLLEAGUES
spicy_levels = ["無"] + DEFAULT_OPTIONS["spicy"]
ice_levels = DEFAULT_OPTIONS["ice"]
sugar_levels = DEFAULT_OPTIONS["sugar"]
custom_tags_main = DEFAULT_OPTIONS["tags"]
custom_tags_drink = DEFAULT_OPTIONS["drink_tags"]


# ==========================================
# 3. 今日點餐管理
# ==========================================
with st.sidebar:
    st.header("⚙️ 點餐管理")

    st.subheader("1. 今日店家")
    db_main_shop = get_shop_name("main")
    db_drink_shop = get_shop_name("drink")

    new_main_shop = st.text_input("主餐店家", value=db_main_shop).strip()
    new_drink_shop = st.text_input("飲料店家", value=db_drink_shop).strip()

    if new_main_shop and new_main_shop != db_main_shop:
        set_shop_name("main", new_main_shop)
        st.rerun()
    if new_drink_shop and new_drink_shop != db_drink_shop:
        set_shop_name("drink", new_drink_shop)
        st.rerun()

    st.divider()
    st.subheader("2. 清除本次訂單")

    if "confirm_reset" not in st.session_state:
        st.session_state.confirm_reset = False

    if st.button("🗑️ 清除本次訂單", type="secondary"):
        st.session_state.confirm_reset = True

    if st.session_state.confirm_reset:
        st.warning("⚠️ 確定清除本次所有訂單？此動作無法復原。")
        c1, c2 = st.columns(2)

        if c1.button("✅ 確定", key="confirm_reset_orders"):
            if execute_db("DELETE FROM orders"):
                st.session_state.confirm_reset = False
                st.toast("🗑️ 本次訂單已清除！")
                st.rerun()

        if c2.button("❌ 取消", key="cancel_reset_orders"):
            st.session_state.confirm_reset = False
            st.rerun()


# ==========================================
# 4. 統計看板 (全域去框線版本，移除編號)
# ==========================================
@st.fragment
def render_stats_section():
    c_ref_text, c_ref_btn = st.columns([8, 1], vertical_alignment="center")
    with c_ref_text:
        # 字體微調為 0.95rem
        st.markdown(f'<div style="text-align:right; color:gray; font-size:0.95rem; margin:0; padding:0;">最後更新 | {datetime.now().strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)
    with c_ref_btn:
        if st.button("🔄", help="手動刷新", use_container_width=True, key="btn_refresh_stats"): st.rerun()

    r_name = get_shop_name("main")
    d_name = get_shop_name("drink")
    df_all = get_orders_df()
    if df_all.empty: st.info("📦 目前尚無訂單，等待第一筆資料..."); return

    def show_stats_optimized(df_source, title, icon_class):
        # 獨立副本，避免統計區塊互相修改 DataFrame。
        df_source = df_source.copy()

        if df_source.empty:
            st.markdown(
                f'<div class="section-header {icon_class}">'
                f'<div>{title}</div><div>共 0 份</div></div>',
                unsafe_allow_html=True
            )
            st.caption("無資料")
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

        c_sum, c_det = st.columns([1, 1.2])

        with c_sum:
            st.markdown("**📦 彙總表 (店家用)**")

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
                            item_group[item_group["custom"] != ""]
                            .groupby("custom", dropna=False)["quantity"]
                            .sum()
                            .reset_index()
                        )

                        for _, custom_row in custom_group.iterrows():
                            safe_custom = html.escape(str(custom_row["custom"]))
                            st.markdown(
                                f'<div class="custom-text">{safe_custom} ×{custom_row["quantity"]}</div>',
                                unsafe_allow_html=True
                            )

                st.markdown("<hr class='soft-divider'>", unsafe_allow_html=True)

            st.metric("該區總額", f"${df_source['price'].sum()}")

        with c_det:
            st.markdown("**📋 明細表 (核對用)**")
            grouped_by_person = df_source.groupby('name')
            for name, group in grouped_by_person:
                with st.container():
                    safe_user = html.escape(str(name))
                    st.markdown(f'<div style="font-size:1.15rem; font-weight:700; margin-bottom:6px; color:var(--text-color);">👤 {safe_user}</div>', unsafe_allow_html=True)
                    for _, row in group.iterrows():
                        safe_item = html.escape(str(row["item_name"]))
                        safe_cst_html = ""
                        if row['custom']: 
                            safe_cst = html.escape(str(row['custom']))
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
@st.fragment
def render_payment_section():
    c_ref_text, c_ref_btn = st.columns([8, 1], vertical_alignment="center")
    with c_ref_text:
        st.markdown(f'<div style="text-align:right; color:gray; font-size:0.95rem; margin:0; padding:0;">最後更新 | {datetime.now().strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)
    with c_ref_btn:
        if st.button("🔄", help="手動刷新", use_container_width=True, key="btn_refresh_payment"): st.rerun()

    df_all = get_orders_df()
    if df_all.empty: st.write("尚無訂單。"); return
    
    main_shop = get_shop_name("main")
    drink_shop = get_shop_name("drink")
    
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
    
    t1, t2 = st.tabs([f"🍱 主餐明細 ({main_shop})", f"🥤 飲料明細 ({drink_shop})"])
    with t1: _pay_logic_grouped("主餐", df_main, "main")
    with t2: _pay_logic_grouped("飲料", df_drink, "drink")

def _pay_logic_grouped(cat, df, k):
    if df.empty: st.caption("無資料"); return
    unpaid_df = df[df['is_paid'] == 0]
    
    if not unpaid_df.empty:
        grouped_unpaid = unpaid_df.groupby('name')
        st.markdown(f"**⚠️ 待收款 ({len(grouped_unpaid)} 人)**")
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
                    if st.button("收款", key=f"pay_{k}_{name}", use_container_width=True, type="primary"):
                        placeholders = ','.join('?' * len(ids))
                        execute_db(f"UPDATE orders SET is_paid = 1 WHERE id IN ({placeholders})", tuple(ids))
                        st.toast(f"💰 已收: {name} (${total_price})"); st.rerun()
                
                for _, row in group.iterrows():
                    safe_item = html.escape(str(row["item_name"]))
                    safe_cst_html = ""
                    if row['custom']: 
                        safe_cst = html.escape(str(row['custom']))
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
    else: st.success("👍 此區全數已付款！")

    paid_df = df[df['is_paid'] == 1]
    if not paid_df.empty:
        st.write(""); grouped_paid = paid_df.groupby('name')
        with st.expander(f"✅ 已付款名單 ({len(grouped_paid)} 人) - 點此展開撤銷"):
            for name, group in grouped_paid:
                total_price = group['price'].sum()
                ids = group['id'].tolist()
                c1, c2 = st.columns([4, 1], vertical_alignment="center")
                with c1: st.write(f"~~{html.escape(str(name))} (${total_price})~~") 
                with c2:
                    if st.button("撤銷", key=f"undo_{k}_{name}", use_container_width=True):
                        placeholders = ','.join('?' * len(ids))
                        execute_db(f"UPDATE orders SET is_paid = 0 WHERE id IN ({placeholders})", tuple(ids))
                        st.toast(f"↩️ 已撤銷: {name}"); st.rerun()

# ==========================================
# 6. 主畫面與 Dialogs 邏輯
# ==========================================
# 主餐尺寸：固定選項。「無」為預設值，儲存時不寫入 custom。
MAIN_SIZE_OPTIONS = ["無", "小份", "大份"]

st.title("🍱 點餐哦各位～")
tab1, tab2, tab3 = st.tabs(["📝 我要點餐", "📊 統計看板", "💰 收款管理"])

@st.dialog("👤 請選擇你的名字")
def login_dialog():
    st.caption("點擊下方名字即可登入")
    selected = st.pills("人員清單", colleagues_list, selection_mode="single", label_visibility="collapsed")
    if selected:
        st.session_state['user_name'] = selected
        st.rerun()

@st.dialog("🎨 選擇客製化")
def custom_dialog(key_prefix, tag_options):
    st.caption("快速選項 (可複選)")
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
        st.caption("目前沒有快速客製選項")

    st.markdown("---")
    new_manual = st.text_input("或是手動輸入", value=current_manual, placeholder="如：不要XXX...或是加XXX...", key=f"{key_prefix}_manual_widget").strip()
    if st.button("✅ 完成", use_container_width=True, type="primary"):
        st.session_state[f"{key_prefix}_tags"] = new_tags
        st.session_state[f"{key_prefix}_manual"] = new_manual
        st.rerun()

@st.dialog("✏️ 編輯餐點")
def edit_order_dialog(order_id, category, cur_name, cur_price_total, cur_qty, cur_custom):
    # 防止 Dialog 開啟後，其他人先完成收款而造成付款狀態不一致。
    current = get_db("SELECT is_paid, unit_price, category FROM orders WHERE id = ?", (order_id,))
    if current.empty:
        st.error("⚠️ 找不到這筆訂單，可能已被刪除。")
        return
    if int(current.iloc[0]["is_paid"] or 0) == 1:
        st.warning("🔒 這筆訂單已付款，無法修改。")
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
        "主餐名稱" if edit_category == "主餐" else "飲料名稱",
        value=str(cur_name)
    ).strip()

    c_p, c_q = st.columns(2)
    new_unit_price = c_p.number_input("單價", min_value=0, step=5, value=unit_price)
    new_qty = c_q.number_input("數量", min_value=1, step=1, value=int(cur_qty))

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
        if custom_parts and custom_parts[0] in ("小份", "大份"):
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

        # Widget key 使用 order_id，避免不同編輯 Dialog 之間互相污染。
        spicy_key = f"edit_m_spicy_{order_id}"
        tags_key = f"edit_m_tags_{order_id}"
        manual_key = f"edit_m_manual_{order_id}"

        size_key = f"edit_m_size_{order_id}"

        if size_key not in st.session_state:
            st.session_state[size_key] = main_size if main_size in MAIN_SIZE_OPTIONS else "無"
        if spicy_key not in st.session_state:
            st.session_state[spicy_key] = main_spicy if main_spicy in spicy_levels else (spicy_levels[0] if spicy_levels else None)
        if tags_key not in st.session_state:
            st.session_state[tags_key] = main_tags
        if manual_key not in st.session_state:
            st.session_state[manual_key] = main_manual

        if st.session_state.get(size_key) not in MAIN_SIZE_OPTIONS:
            st.session_state[size_key] = "無"
        edit_size = st.pills(
            "尺寸（必選）",
            MAIN_SIZE_OPTIONS,
            default="無",
            key=size_key,
            selection_mode="single"
        )

        if spicy_levels:
            if st.session_state.get(spicy_key) not in spicy_levels:
                st.session_state[spicy_key] = spicy_levels[0]
            edit_spicy = st.pills(
                "辣度",
                spicy_levels,
                default=spicy_levels[0],
                key=spicy_key,
                selection_mode="single"
            )
        else:
            edit_spicy = None
            st.caption("辣度：目前沒有可選項目")

        current_tags = st.session_state.get(tags_key, [])
        valid_tag_set = {str(v) for v in custom_tags_main}
        current_tags = [
            str(v) for v in current_tags
            if v is not None and str(v) in valid_tag_set
        ]
        st.session_state[tags_key] = current_tags

        if custom_tags_main:
            edit_tags = st.pills(
                "快速客製選項（可複選）",
                custom_tags_main,
                default=current_tags,
                key=f"{tags_key}_widget",
                selection_mode="multi"
            )
        else:
            edit_tags = []
            st.caption("目前沒有快速客製選項")

        edit_manual = st.text_input(
            "手動客製",
            value=st.session_state.get(manual_key, ""),
            placeholder="如：不要XXX...或是加XXX...",
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

        drink_size_key = f"edit_d_size_{order_id}"
        drink_sugar_key = f"edit_d_sugar_{order_id}"
        drink_ice_key = f"edit_d_ice_{order_id}"
        drink_tags_key = f"edit_d_tags_{order_id}"
        drink_manual_key = f"edit_d_manual_{order_id}"

        # 先用目前可用選項組成 prefix，從既有 custom 拆出基本飲料設定。
        available_size_values = ["M(中杯)", "L(大杯)", "XL(特大杯)"]
        parsed_size = "L(大杯)"
        parsed_sugar = sugar_levels[0] if sugar_levels else None
        parsed_ice = ice_levels[0] if ice_levels else None
        remainder = raw_custom

        # custom 的基本格式是：尺寸/甜度/冰塊[, 客製...]
        base_part, separator, tag_part = raw_custom.partition(",")
        base_values = [v.strip() for v in base_part.split("/") if v.strip()]

        if base_values:
            if base_values[0] in available_size_values:
                parsed_size = base_values[0]
            if len(base_values) > 1 and base_values[1] in sugar_levels:
                parsed_sugar = base_values[1]
            if len(base_values) > 2 and base_values[2] in ice_levels:
                parsed_ice = base_values[2]

            # 如果第一段不是標準飲料設定，保守保留整段作為手動客製。
            recognized_base_count = (
                (1 if base_values[0] in available_size_values else 0)
                + (1 if len(base_values) > 1 and base_values[1] in sugar_levels else 0)
                + (1 if len(base_values) > 2 and base_values[2] in ice_levels else 0)
            )
            if recognized_base_count >= 1:
                remainder = tag_part.strip() if separator else ""
            else:
                remainder = raw_custom

        if drink_size_key not in st.session_state:
            st.session_state[drink_size_key] = parsed_size
        if drink_sugar_key not in st.session_state:
            st.session_state[drink_sugar_key] = parsed_sugar
        if drink_ice_key not in st.session_state:
            st.session_state[drink_ice_key] = parsed_ice

        if st.session_state.get(drink_size_key) not in available_size_values:
            st.session_state[drink_size_key] = available_size_values[1]
        edit_size = st.pills(
            "尺寸",
            available_size_values,
            default="L(大杯)",
            key=drink_size_key,
            selection_mode="single"
        )

        if sugar_levels:
            if st.session_state.get(drink_sugar_key) not in sugar_levels:
                st.session_state[drink_sugar_key] = sugar_levels[0]
            edit_sugar = st.pills(
                "甜度",
                sugar_levels,
                default=sugar_levels[0],
                key=drink_sugar_key,
                selection_mode="single"
            )
        else:
            edit_sugar = None
            st.caption("甜度：目前沒有可選項目")

        if ice_levels:
            if st.session_state.get(drink_ice_key) not in ice_levels:
                st.session_state[drink_ice_key] = ice_levels[0]
            edit_ice = st.pills(
                "冰塊",
                ice_levels,
                default=ice_levels[0],
                key=drink_ice_key,
                selection_mode="single"
            )
        else:
            edit_ice = None
            st.caption("冰塊：目前沒有可選項目")

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

        if drink_tags_key not in st.session_state:
            st.session_state[drink_tags_key] = parsed_tags
        if drink_manual_key not in st.session_state:
            st.session_state[drink_manual_key] = ", ".join(manual_parts)

        current_drink_tags = [
            str(v) for v in st.session_state.get(drink_tags_key, [])
            if v is not None and str(v) in valid_drink_tags
        ]
        st.session_state[drink_tags_key] = current_drink_tags

        if custom_tags_drink:
            edit_drink_tags = st.pills(
                "快速客製選項（可複選）",
                custom_tags_drink,
                default=current_drink_tags,
                key=f"{drink_tags_key}_widget",
                selection_mode="multi"
            )
        else:
            edit_drink_tags = []
            st.caption("目前沒有快速客製選項")

        edit_drink_manual = st.text_input(
            "手動客製",
            value=st.session_state.get(drink_manual_key, ""),
            placeholder="如：加XXX、不要XXX...",
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

    if st.button("💾 儲存修改", type="primary", use_container_width=True):
        # 儲存前再查一次，並由 SQL 本身限制只能修改未付款訂單。
        current_check = get_db("SELECT is_paid FROM orders WHERE id = ?", (order_id,))
        if current_check.empty:
            st.error("⚠️ 找不到這筆訂單，可能已被刪除。")
            return
        if int(current_check.iloc[0]["is_paid"] or 0) == 1:
            st.error("🔒 這筆訂單已付款，無法修改。")
            return

        if edit_category == "主餐" and edit_size not in MAIN_SIZE_OPTIONS:
            st.error("請選擇主餐尺寸")
            return

        if not new_name:
            st.error("餐點名稱不能為空")
            return

        new_total = int(new_unit_price) * int(new_qty)
        if execute_db(
            "UPDATE orders SET item_name=?, price=?, quantity=?, unit_price=?, custom=? "
            "WHERE id=? AND is_paid=0",
            (new_name, new_total, int(new_qty), int(new_unit_price), new_custom, order_id)
        ):
            st.toast("✅ 餐點已成功更新！")
            st.rerun()


if 'user_name' not in st.session_state: st.session_state['user_name'] = None
if 'm_custom_tags' not in st.session_state: st.session_state['m_custom_tags'] = []
if 'm_custom_manual' not in st.session_state: st.session_state['m_custom_manual'] = ""
if 'd_custom_tags' not in st.session_state: st.session_state['d_custom_tags'] = []
if 'd_custom_manual' not in st.session_state: st.session_state['d_custom_manual'] = ""

with tab1:
    if st.button("🔄 刷新頁面 (手動同步)", type="secondary", use_container_width=True): st.rerun()
    
    with st.container():
        st.markdown('<h5>👤 請問你是誰？</h5>', unsafe_allow_html=True)
        c_user, c_btn = st.columns([3, 1.5], vertical_alignment="center")
        with c_user:
            if st.session_state['user_name']: st.info(f"Hi, **{html.escape(str(st.session_state['user_name']))}**！")
            else: st.warning("⚠️ 尚未選擇名字")
        with c_btn:
            if st.button("👤 登入/切換", use_container_width=True, type="primary" if not st.session_state['user_name'] else "secondary"):
                login_dialog()
        if not st.session_state['user_name']: st.stop()

    user_name = st.session_state['user_name']

    my_orders = get_db("SELECT * FROM orders WHERE name = ?", (user_name,))
    my_sum = my_orders['price'].sum() if not my_orders.empty else 0
    with st.expander(f"📋 {html.escape(str(user_name))} 的待點清單 (合計: ${my_sum})", expanded=True if not my_orders.empty else False):
        if my_orders.empty: st.caption("尚未點餐")
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
                    key=f"btn_edit_{row['id']}",
                    help="已付款，無法修改" if is_paid else "修改",
                    use_container_width=True,
                    disabled=is_paid
                ):
                    edit_order_dialog(
                        row['id'],
                        row['category'],
                        row['item_name'],
                        row['price'],
                        row['quantity'],
                        row['custom']
                    )

                if is_paid:
                    c_btn2.button(
                        "🔒",
                        key=f"btn_del_locked_{row['id']}",
                        help="已付款，無法刪除",
                        use_container_width=True,
                        disabled=True
                    )
                else:
                    with c_btn2.popover("🗑️", help="刪除", use_container_width=True):
                        st.write(f"刪除 **{safe_item_name}**？")
                        if st.button("⭕ 確認", key=f"confirm_del_{row['id']}", type="primary", use_container_width=True):
                            # SQL 再次檢查 is_paid，避免畫面舊資料造成誤刪。
                            if execute_db("DELETE FROM orders WHERE id = ? AND is_paid = 0", (row['id'],)):
                                st.toast("✅ 已刪除")
                                st.rerun()
                
                st.markdown("<hr class='soft-divider'>", unsafe_allow_html=True)
    st.write("") 

    current_main_shop = new_main_shop
    current_drink_shop = new_drink_shop

    c_food, c_drink = st.columns(2)
    with c_food:
        st.markdown(f'<div class="section-header header-food"><div>🍱 {html.escape(str(current_main_shop))} (主餐)</div></div>', unsafe_allow_html=True)
        with st.container():
            m_name_raw = st.text_input("主餐名稱", placeholder="輸入餐點...", key="m_name")
            m_name = m_name_raw.strip()
            
            cp, cq = st.columns(2)
            m_price_unit = cp.number_input("單價", min_value=0, step=5, format="%d", key="m_price")
            m_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="m_qty")
            # Streamlit widget 的 key 必須在 widget 建立前修改。
            # 若上一輪要求重設尺寸，本輪先重設，再建立 st.pills。
            if st.session_state.pop("_reset_m_size", False):
                st.session_state["m_size"] = "無"

            # 主餐尺寸固定為「無 / 小份 / 大份」；「無」為預設且不寫入 custom。
            if st.session_state.get("m_size") not in MAIN_SIZE_OPTIONS:
                st.session_state["m_size"] = "無"
            m_size = st.pills(
                "尺寸（必選）",
                MAIN_SIZE_OPTIONS,
                default="無",
                key="m_size",
                selection_mode="single"
            )

            # Secrets 修改選項後，舊的 session_state 可能還保留已不存在的選項。
            # 例如原本選「無」，後來從 Secrets 移除「無」，st.pills 可能回傳 None。
            if spicy_levels:
                if st.session_state.get("m_spicy") not in spicy_levels:
                    st.session_state["m_spicy"] = spicy_levels[0]
                m_spicy = st.pills(
                    "辣度", spicy_levels, default=spicy_levels[0],
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
            btn_label = f"🎨 客製化 (✅已選{display_count}項)" if display_count else "🎨 客製化 (目前: 無)"

            c_cust_btn, c_cust_clear = st.columns([4, 1])
            with c_cust_btn:
                if st.button(btn_label, type=btn_type, use_container_width=True, key="btn_m_custom"):
                    custom_dialog("m_custom", custom_tags_main)
            with c_cust_clear:
                if st.button("❌", help="清空主餐客製", use_container_width=True, key="clr_m_custom"):
                    st.session_state["m_custom_tags"] = []
                    st.session_state["m_custom_manual"] = ""
                    st.rerun()
            
            if display_parts: st.caption(f"ℹ️ 準備加入: {html.escape(str(display_text))}")

            if st.button("＋ 加入主餐", type="primary", use_container_width=True):
                if m_size not in MAIN_SIZE_OPTIONS:
                    st.toast("🚫 無法加入：請選擇主餐尺寸！", icon="⚠️")
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
                    if execute_db(
                        "INSERT INTO orders "
                        "(name, category, item_name, price, custom, quantity, order_time, is_paid, unit_price) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                        (
                            user_name, "主餐", m_name, total_p, cust, int(m_qty),
                            datetime.now().strftime('%Y-%m-%d %H:%M'), int(m_price_unit)
                        )
                    ):
                        # 新增成功後只清除「客製化」內容。
                        # 尺寸、辣度都屬於目前點餐介面的選擇，保留使用者剛才的設定，
                        # 與飲料尺寸/甜度/冰塊的操作方式一致。
                        st.session_state["m_custom_tags"] = []
                        st.session_state["m_custom_manual"] = ""
                        st.toast(f"✅ 已加入：{m_name} ×{m_qty}"); st.rerun()
                else: st.toast("⚠️ 請輸入主餐名稱")

    with c_drink:
        st.markdown(f'<div class="section-header header-drink"><div>🥤 {html.escape(str(current_drink_shop))} (飲料)</div></div>', unsafe_allow_html=True)
        with st.container():
            d_name_raw = st.text_input("飲料名稱", placeholder="輸入飲料...", key="d_name")
            d_name = d_name_raw.strip()
            
            cp, cq = st.columns(2)
            d_price_unit = cp.number_input("單價", min_value=0, step=5, format="%d", key="d_price")
            d_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="d_qty")
            
            d_size = st.pills(
                "尺寸",
                ["M(中杯)", "L(大杯)", "XL(特大杯)"],
                default="L(大杯)",
                key="d_size",
                selection_mode="single"
            )

            if sugar_levels:
                if st.session_state.get("d_sugar") not in sugar_levels:
                    st.session_state["d_sugar"] = sugar_levels[0]
                d_sugar = st.pills(
                    "甜度", sugar_levels, default=sugar_levels[0],
                    key="d_sugar", selection_mode="single"
                )
            else:
                d_sugar = None
                st.caption("甜度：目前沒有可選項目")

            if ice_levels:
                if st.session_state.get("d_ice") not in ice_levels:
                    st.session_state["d_ice"] = ice_levels[0]
                d_ice = st.pills(
                    "冰塊", ice_levels, default=ice_levels[0],
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
            d_btn_label = f"🎨 客製化 (✅已選{len(d_display_list)}項)" if d_display_list else "🎨 客製化 (目前: 無)"

            dc_btn, dc_clear = st.columns([4, 1])
            with dc_btn:
                if st.button(d_btn_label, type=d_btn_type, use_container_width=True, key="btn_d_custom"):
                    custom_dialog("d_custom", custom_tags_drink)
            with dc_clear:
                if st.button("❌", help="清空飲料客製", use_container_width=True, key="clr_d_custom"):
                    st.session_state["d_custom_tags"] = []
                    st.session_state["d_custom_manual"] = ""
                    st.rerun()

            if d_display_list: st.caption(f"ℹ️ 準備加入: {html.escape(str(d_display_text))}")

            if st.button("＋ 加入飲料", type="primary", use_container_width=True):
                if d_price_unit == 0: st.toast("🚫 無法加入：請輸入金額！", icon="⚠️")
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
                    if execute_db(
                        "INSERT INTO orders "
                        "(name, category, item_name, price, custom, quantity, order_time, is_paid, unit_price) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                        (
                            user_name, "飲料", d_name, total_p, final_cust, int(d_qty),
                            datetime.now().strftime('%Y-%m-%d %H:%M'), int(d_price_unit)
                        )
                    ):
                        # 只清除剛剛送出的飲料客製化，不影響尚未送出的主餐設定。
                        st.session_state["d_custom_tags"] = []
                        st.session_state["d_custom_manual"] = ""
                        st.toast(f"✅ 已加入：{d_name} ×{d_qty}"); st.rerun()
                else: st.toast("⚠️ 請輸入飲料名稱")

with tab2: render_stats_section()
with tab3: render_payment_section()
