import streamlit as st
import pandas as pd
import sqlite3
import time
import os
from datetime import datetime

# ==========================================
# 0. 系統設定區
# ==========================================
ADMIN_PASSWORD = "0678678"
DB_FILE = "lunch.db"

# ==========================================
# 1. 頁面設定與 CSS (視覺核心)
# ==========================================
st.set_page_config(page_title="點餐哦各位～ v2.8", page_icon="🍱", layout="wide")

custom_css = """
<style>
    /* 全域設定 */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    
    /* Tabs 優化 */
    .stTabs [data-baseweb="tab-list"] { gap: 8px; background-color: transparent; padding-bottom: 5px; }
    .stTabs [data-baseweb="tab"] {
        height: 50px; border-radius: 8px;
        background-color: var(--secondary-background-color); 
        padding: 10px 20px; font-weight: 600; border: none; color: var(--text-color);
        font-size: 1rem;
    }
    .stTabs [aria-selected="true"] { background-color: #FF4B4B !important; color: white !important; }

    /* 統一區塊標題 */
    .section-header {
        padding: 12px 18px; border-radius: 8px; margin-bottom: 15px;
        color: white; font-weight: 700; font-size: 1.15rem;
        display: flex; align-items: center; justify-content: space-between;
        box-shadow: 0 2px 5px rgba(0,0,0,0.15);
    }

    /* 顏色主題變數 */
    :root {
        --food-gradient: linear-gradient(135deg, #FF8C00, #FF4500);
        --drink-gradient: linear-gradient(135deg, #008080, #2E8B57);
        --money-gradient: linear-gradient(135deg, #DAA520, #B8860B);
        --text-main: 1.15rem;   /* 卡片標題大小 */
        --text-body: 1rem;      /* 內文大小 */
        --text-meta: 0.9rem;    /* 備註/客製化大小 */
    }
    .header-food { background: var(--food-gradient); }
    .header-drink { background: var(--drink-gradient); }
    .header-money { background: var(--money-gradient); color: white;}

    /* Metric 優化 */
    div[data-testid="stMetric"] {
        background-color: var(--secondary-background-color);
        border: 1px solid rgba(255, 255, 255, 0.1); padding: 15px; border-radius: 8px;
    }

    /* 按鈕樣式統一 */
    div.stButton > button[kind="primary"] { font-weight: 600; font-size: 1rem; }
    div[data-testid="column"]:nth-of-type(1) div[data-testid="stVerticalBlock"] > div.stButton > button[kind="primary"] {
        background: var(--food-gradient); color: white; border: none; transition: opacity 0.3s;
    }
    div[data-testid="column"]:nth-of-type(2) div[data-testid="stVerticalBlock"] > div.stButton > button[kind="primary"] {
        background: var(--drink-gradient); color: white; border: none; transition: opacity 0.3s;
    }
    div.stButton > button[kind="primary"]:hover { opacity: 0.9; border: none !important; }

    /* 手機輸入框字體防縮放 */
    @media screen and (max-width: 768px) {
        input, select, textarea { font-size: 16px !important; }
    }

    /* === 自定義字體排版系統 (Typography) === */
    .card-title { font-size: var(--text-main); font-weight: 700; margin-bottom: 2px; }
    .card-text { font-size: var(--text-body); font-weight: 400; display: flex; align-items: center; }
    .card-meta { font-size: var(--text-meta); color: gray; margin-top: 2px; line-height: 1.4; }
    .price-tag { color: #FF4B4B; font-weight: 700; font-family: monospace; font-size: 1.1rem; }
    .price-tag-sm { color: gray; font-size: 0.9rem; }
    
    /* 數量 Badge 優化 */
    .qty-badge {
        font-size: 1.4rem; font-weight: 800; color: #FF4B4B; 
        text-align: center; width: 100%;
        display: flex; align-items: center; justify-content: center; height: 100%;
        border-right: 1px solid rgba(255,255,255,0.1);
    }

    /* 分隔線與自動刷新文字 */
    hr { margin: 1.5em 0; }
    .refresh-text { color: gray; font-size: 0.8rem; margin-bottom: 5px; text-align: right;}
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

# ==========================================
# 2. 資料庫邏輯
# ==========================================
DEFAULT_COLLEAGUES = [
    "小昏", "阿文"
]
DEFAULT_OPTIONS = {
    "spicy": ["微辣", "小辣", "中辣", "大辣"],
    "ice": ["正常冰", "微冰", "少冰", "去冰", "完全去冰", "溫", "熱"],
    "sugar": ["正常糖", "少糖", "半糖", "微糖", "一分糖", "無糖"],
    "tags": ["不要蔥", "不要蒜", "不要香菜", "飯少", "加飯"],
    "drink_tags": ["加珍珠", "加椰果", "加仙草", "加布丁"]
}

def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, item_name TEXT,
            price INTEGER, custom TEXT, quantity INTEGER, order_time TEXT, is_paid BOOLEAN)''')
        c.execute('''CREATE TABLE IF NOT EXISTS config_colleagues (name TEXT PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS config_options (
            category TEXT, option_value TEXT, PRIMARY KEY (category, option_value))''')
        c.execute('''CREATE TABLE IF NOT EXISTS config_shop (
            category TEXT PRIMARY KEY, shop_name TEXT)''')

        for n in DEFAULT_COLLEAGUES:
            c.execute("INSERT OR IGNORE INTO config_colleagues (name) VALUES (?)", (n,))
        for cat, options in DEFAULT_OPTIONS.items():
            for opt in options:
                c.execute("INSERT OR IGNORE INTO config_options (category, option_value) VALUES (?, ?)", (cat, opt))
        c.execute("INSERT OR IGNORE INTO config_shop (category, shop_name) VALUES (?, ?)", ("main", "吃什麼？"))
        c.execute("INSERT OR IGNORE INTO config_shop (category, shop_name) VALUES (?, ?)", ("drink", "喝什麼？"))
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
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return df
    except Exception: return pd.DataFrame()

def get_config_list(table, col, cat=None):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        if cat:
            q = f"SELECT {col} FROM {table} WHERE category = ? ORDER BY rowid"
            p = (cat,)
        else:
            q = f"SELECT {col} FROM {table} ORDER BY rowid"
            p = ()
        df = pd.read_sql_query(q, conn, params=p)
        return df
    finally:
        conn.close()

def update_config_list(table, col, new_df, cat=None):
    execute_db(f"DELETE FROM {table}" + (f" WHERE category = '{cat}'" if cat else ""))
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    c = conn.cursor()
    try:
        if cat:
            data = [(cat, row[col]) for _, row in new_df.iterrows() if row[col]]
            c.executemany(f"INSERT INTO {table} (category, {col}) VALUES (?, ?)", data)
        else:
            data = [(row[col],) for _, row in new_df.iterrows() if row[col]]
            c.executemany(f"INSERT INTO {table} ({col}) VALUES (?)", data)
        conn.commit()
    finally:
        conn.close()

def get_shop_name(cat):
    df = get_db("SELECT shop_name FROM config_shop WHERE category = ?", (cat,))
    if not df.empty: return df.iloc[0]['shop_name']
    return "未設定"

def set_shop_name(cat, name):
    execute_db("UPDATE config_shop SET shop_name = ? WHERE category = ?", (name, cat))

init_db()

# 讀取設定
df_colleagues = get_config_list("config_colleagues", "name")
colleagues_list = df_colleagues["name"].tolist() if not df_colleagues.empty else ["請新增人員"]
df_spicy = get_config_list("config_options", "option_value", "spicy")
spicy_levels = ["無"] + df_spicy["option_value"].tolist()
df_ice = get_config_list("config_options", "option_value", "ice")
ice_levels = df_ice["option_value"].tolist()
df_sugar = get_config_list("config_options", "option_value", "sugar")
sugar_levels = df_sugar["option_value"].tolist()
df_tags = get_config_list("config_options", "option_value", "tags")
custom_tags_main = df_tags["option_value"].tolist() 
df_drink_tags = get_config_list("config_options", "option_value", "drink_tags")
custom_tags_drink = df_drink_tags["option_value"].tolist()

# ==========================================
# 3. 側邊欄
# ==========================================
with st.sidebar:
    st.header("⚙️ 開團管理")
    st.subheader("1. 今日店家")
    db_main_shop = get_shop_name("main")
    db_drink_shop = get_shop_name("drink")
    
    new_main_shop = st.text_input("主餐店家", value=db_main_shop)
    new_drink_shop = st.text_input("飲料店家", value=db_drink_shop)
    
    if new_main_shop != db_main_shop:
        set_shop_name("main", new_main_shop)
        st.rerun()
    if new_drink_shop != db_drink_shop:
        set_shop_name("drink", new_drink_shop)
        st.rerun()

    st.divider()
    st.subheader("2. 資料重置")
    if "confirm_reset" not in st.session_state: st.session_state.confirm_reset = False
    
    if st.button("🗑️ 清空資料庫", type="secondary"): 
        st.session_state.confirm_reset = True
    
    if st.session_state.confirm_reset:
        st.warning("⚠️ 確定清空？此動作無法復原。")
        c1, c2 = st.columns(2)
        if c1.button("✅ 確定"):
            execute_db("DELETE FROM orders")
            execute_db("VACUUM")
            st.session_state.confirm_reset = False
            st.toast("🗑️ 資料庫已重置完成！")
            st.rerun()
        if c2.button("❌ 取消"):
            st.session_state.confirm_reset = False
            st.rerun()
    st.divider()

    with st.expander("🔧 進階設定"):
        pwd_input = st.text_input("輸入管理員密碼", type="password", key="admin_pwd")
        if pwd_input == ADMIN_PASSWORD:
            st.success("🔓 已解鎖")
            st.write("**👥 人員名單**")
            edited_colleagues = st.data_editor(df_colleagues, num_rows="dynamic", 
                column_config={"name": st.column_config.TextColumn("姓名", required=True)},
                key="ed_col", use_container_width=True, hide_index=True)
            if st.button("💾 儲存人員"):
                update_config_list("config_colleagues", "name", edited_colleagues)
                st.toast("✅ 已更新"); time.sleep(0.5); st.rerun()
            st.divider()
            st.write("**🛠️ 菜單選項**")
            t1, t2, t3, t4, t5 = st.tabs(["辣度", "冰塊", "甜度", "🍱主餐客製", "🥤飲料客製"])
            def render_opt(tab, cat, df, lbl):
                with tab:
                    ed = st.data_editor(df, num_rows="dynamic",
                        column_config={"option_value": st.column_config.TextColumn(lbl, required=True)},
                        key=f"ed_{cat}", use_container_width=True, hide_index=True)
                    if st.button(f"儲存{lbl}", key=f"btn_{cat}"):
                        update_config_list("config_options", "option_value", ed, cat)
                        st.toast("✅ 已更新"); time.sleep(0.5); st.rerun()
            render_opt(t1, "spicy", get_config_list("config_options", "option_value", "spicy"), "辣度")
            render_opt(t2, "ice", get_config_list("config_options", "option_value", "ice"), "冰塊")
            render_opt(t3, "sugar", get_config_list("config_options", "option_value", "sugar"), "甜度")
            render_opt(t4, "tags", get_config_list("config_options", "option_value", "tags"), "主餐客製")
            render_opt(t5, "drink_tags", get_config_list("config_options", "option_value", "drink_tags"), "飲料客製")
        elif pwd_input: st.error("🚫 密碼錯誤")
        else: st.caption("修改人員或菜單需驗證")

# ==========================================
# 4. 統計看板 (Visual Optimized)
# ==========================================
@st.fragment(run_every=10)
def render_stats_section():
    st.markdown(f'<div class="refresh-text">🔄 自動刷新 | {datetime.now().strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)
    r_name = get_shop_name("main")
    d_name = get_shop_name("drink")
    df_all = get_db("SELECT * FROM orders")
    if df_all.empty: st.info("📦 目前尚無訂單，等待第一筆資料..."); return

    def show_stats_optimized(df_source, title, icon_class):
        total_qty = df_source['quantity'].sum() if not df_source.empty else 0
        st.markdown(f'<div class="section-header {icon_class}"><div>{title}</div><div>共 {total_qty} 份</div></div>', unsafe_allow_html=True)
        if df_source.empty: st.caption("無資料"); return
        c_sum, c_det = st.columns([1, 1.2])
        
        # --- 彙總表 (店家用) ---
        with c_sum:
            st.markdown("**📦 彙總表 (店家用)**")
            summary = df_source.groupby(['item_name', 'custom'])['quantity'].sum().reset_index()
            summary.columns = ['餐點', '客製', '總量']
            for idx, row in summary.iterrows():
                with st.container(border=True):
                    c_qty, c_info = st.columns([1, 4])
                    with c_qty: 
                        st.markdown(f'<div class="qty-badge">x{row["總量"]}</div>', unsafe_allow_html=True)
                    with c_info:
                        # [v3.4] 使用 CSS Class 統一字體
                        st.markdown(f'<div class="card-title">{idx + 1}. {row["餐點"]}</div>', unsafe_allow_html=True)
                        if row['客製']: 
                            safe_custom = row['客製'].replace("|", "<span style='color:#FF4B4B; font-weight:bold'>|</span>")
                            st.markdown(f'<div class="card-meta">{safe_custom}</div>', unsafe_allow_html=True)
            st.metric("該區總額", f"${df_source['price'].sum()}")

        # --- 明細表 (核對用) ---
        with c_det:
            st.markdown("**📋 明細表 (核對用)**")
            grouped_by_person = df_source.groupby('name')
            for name, group in grouped_by_person:
                with st.container(border=True):
                    st.markdown(f'<div class="card-title">👤 {name}</div>', unsafe_allow_html=True)
                    for _, row in group.iterrows():
                        # [v3.4] 統一字體大小，價格使用 monospace
                        item_str = f'<div class="card-text">• {row["item_name"]} (x{row["quantity"]}) &nbsp;<span class="price-tag-sm">${row["price"]}</span></div>'
                        st.markdown(item_str, unsafe_allow_html=True)
                        if row['custom']: 
                            safe_custom = row['custom'].replace("|", " <span style='color:#FF4B4B; font-weight:bold'>|</span> ")
                            st.markdown(f'<div class="card-meta" style="margin-left:14px;">└ {safe_custom}</div>', unsafe_allow_html=True)

    show_stats_optimized(df_all[df_all['category'] == '主餐'], f"🍱 {r_name} (主餐)", "header-food")
    st.divider()
    show_stats_optimized(df_all[df_all['category'] == '飲料'], f"🥤 {d_name} (飲料)", "header-drink")

# ==========================================
# 5. 收款管理 (Visual Optimized)
# ==========================================
@st.fragment(run_every=10)
def render_payment_section():
    st.markdown(f'<div class="refresh-text">🔄 自動刷新 | {datetime.now().strftime("%H:%M:%S")}</div>', unsafe_allow_html=True)
    df_all = get_db("SELECT * FROM orders")
    if df_all.empty: st.write("尚無訂單。"); return
    
    total = df_all['price'].sum()
    paid = df_all[df_all['is_paid'] == 1]['price'].sum()
    prog = paid / total if total > 0 else 0
    st.markdown(f'<div class="section-header header-money"><div>💰 收款進度</div><div>${paid} / ${total}</div></div>', unsafe_allow_html=True)
    st.progress(prog)
    if prog == 1.0 and total > 0: st.success("🎉 太棒了！款項已全數收齊！")
    
    t1, t2 = st.tabs(["🍱 主餐收款", "🥤 飲料收款"])
    with t1: _pay_logic_grouped("主餐", df_all[df_all['category'] == '主餐'], "main")
    with t2: _pay_logic_grouped("飲料", df_all[df_all['category'] == '飲料'], "drink")

def _pay_logic_grouped(cat, df, k):
    if df.empty: st.caption("無資料"); return
    unpaid_df = df[df['is_paid'] == 0]
    
    if not unpaid_df.empty:
        grouped_unpaid = unpaid_df.groupby('name')
        st.markdown(f"**⚠️ 待收款 ({len(grouped_unpaid)} 人)**")
        for name, group in grouped_unpaid:
            total_price = group['price'].sum()
            ids = group['id'].tolist()
            with st.container(border=True):
                # [v3.4] 收款卡片排版優化：姓名與價格平行
                c_header, c_btn = st.columns([3, 1.2])
                with c_header:
                    st.markdown(f'<div style="display:flex; justify-content:space-between; align-items:center;">'
                                f'<span class="card-title">👤 {name}</span>'
                                f'<span class="price-tag">${total_price}</span>'
                                f'</div>', unsafe_allow_html=True)
                with c_btn:
                    if st.button("收款", key=f"pay_{k}_{name}", use_container_width=True, type="primary"):
                        placeholders = ','.join('?' * len(ids))
                        execute_db(f"UPDATE orders SET is_paid = 1 WHERE id IN ({placeholders})", tuple(ids))
                        st.toast(f"💰 已收: {name} (${total_price})"); st.rerun()
                st.markdown("---")
                for _, row in group.iterrows():
                    # [v3.4] 明細行距調整
                    r1, r2 = st.columns([4, 1])
                    with r1: 
                        st.markdown(f'<span class="card-text"><b>{row["item_name"]}</b> &nbsp;<span style="color:gray; font-size:0.9rem">x{row["quantity"]}</span></span>', unsafe_allow_html=True)
                    with r2: 
                        st.markdown(f'<div style="text-align:right" class="price-tag-sm">${row["price"]}</div>', unsafe_allow_html=True)
                    if row['custom']: 
                        safe_custom = row['custom'].replace("|", " <span style='color:#FF4B4B; font-weight:bold'>|</span> ")
                        st.markdown(f'<div class="card-meta">└ {safe_custom}</div>', unsafe_allow_html=True)
    else: st.success("👍 此區全數已付款！")

    paid_df = df[df['is_paid'] == 1]
    if not paid_df.empty:
        st.write(""); grouped_paid = paid_df.groupby('name')
        with st.expander(f"✅ 已付款名單 ({len(grouped_paid)} 人) - 點此展開撤銷"):
            for name, group in grouped_paid:
                total_price = group['price'].sum()
                ids = group['id'].tolist()
                c1, c2 = st.columns([3, 1.2])
                with c1: st.write(f"~~{name} (${total_price})~~") 
                with c2:
                    if st.button("撤銷", key=f"undo_{k}_{name}", use_container_width=True):
                        placeholders = ','.join('?' * len(ids))
                        execute_db(f"UPDATE orders SET is_paid = 0 WHERE id IN ({placeholders})", tuple(ids))
                        st.toast(f"↩️ 已撤銷: {name}"); st.rerun()

# ==========================================
# 6. 主畫面 (Main App)
# ==========================================
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
    new_tags = st.pills("客製選項", tag_options, default=current_tags, selection_mode="multi", label_visibility="collapsed", key=f"{key_prefix}_pills_widget")
    st.markdown("---")
    new_manual = st.text_input("或是手動輸入", value=current_manual, placeholder="如：不要XXX...或是加XXX..."", key=f"{key_prefix}_manual_widget")
    if st.button("✅ 完成", use_container_width=True, type="primary"):
        st.session_state[f"{key_prefix}_tags"] = new_tags
        st.session_state[f"{key_prefix}_manual"] = new_manual
        st.rerun()

if 'user_name' not in st.session_state: st.session_state['user_name'] = None
if 'm_custom_tags' not in st.session_state: st.session_state['m_custom_tags'] = []
if 'm_custom_manual' not in st.session_state: st.session_state['m_custom_manual'] = ""
if 'd_custom_tags' not in st.session_state: st.session_state['d_custom_tags'] = []
if 'd_custom_manual' not in st.session_state: st.session_state['d_custom_manual'] = ""

with tab1:
    if st.button("🔄 刷新頁面 (手動同步)", type="secondary", use_container_width=True): st.rerun()
    
    with st.container(border=True):
        st.markdown('<h5>👤 請問你是誰？</h5>', unsafe_allow_html=True)
        c_user, c_btn = st.columns([3, 1.5])
        with c_user:
            if st.session_state['user_name']: st.info(f"Hi, **{st.session_state['user_name']}**！")
            else: st.warning("⚠️ 尚未選擇名字")
        with c_btn:
            if st.button("👤 登入/切換", use_container_width=True, type="primary" if not st.session_state['user_name'] else "secondary"):
                login_dialog()
        if not st.session_state['user_name']: st.stop()

    user_name = st.session_state['user_name']

    my_orders = get_db("SELECT * FROM orders WHERE name = ?", (user_name,))
    my_sum = my_orders['price'].sum() if not my_orders.empty else 0
    with st.expander(f"📋 {user_name} 的待點清單 (合計: ${my_sum})", expanded=True if not my_orders.empty else False):
        if my_orders.empty: st.caption("尚未點餐")
        else:
            for _, row in my_orders.iterrows():
                # [v3.4] 自我清單也統一視覺
                c1, c2, c3, c4 = st.columns([0.5, 2.5, 1, 1])
                c1.write("🍱" if row['category'] == '主餐' else "🥤")
                c2.markdown(f'<span class="card-text"><b>{row["item_name"]}</b> x{row["quantity"]}</span>', unsafe_allow_html=True)
                c3.markdown(f'<span class="price-tag-sm">${row["price"]}</span>', unsafe_allow_html=True)
                with c4.popover("🗑️", help="點擊開啟刪除確認"):
                    st.write(f"確定刪除 **{row['item_name']}**？")
                    if st.button("⭕ 確認刪除", key=f"confirm_del_{row['id']}", type="primary"):
                        execute_db("DELETE FROM orders WHERE id = ?", (row['id'],))
                        st.toast("✅ 已刪除"); st.rerun()
                if row['custom']:
                    safe_custom = row['custom'].replace("|", " <span style='color:#FF4B4B; font-weight:bold'>|</span> ")
                    st.caption(f"└ {safe_custom}", unsafe_allow_html=True)
    st.write("") 

    current_main_shop = new_main_shop
    current_drink_shop = new_drink_shop

    c_food, c_drink = st.columns(2)
    with c_food:
        st.markdown(f'<div class="section-header header-food"><div>🍱 {current_main_shop} (主餐)</div></div>', unsafe_allow_html=True)
        with st.container(border=True):
            m_name = st.text_input("主餐名稱", placeholder="輸入餐點...", key="m_name")
            cp, cq = st.columns(2)
            m_price_unit = cp.number_input("單價", min_value=0, step=5, format="%d", key="m_price")
            m_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="m_qty")
            m_spicy = st.pills("辣度", spicy_levels, default=spicy_levels[0], key="m_spicy", selection_mode="single")
            
            current_tags = st.session_state.get("m_custom_tags", [])
            current_manual = st.session_state.get("m_custom_manual", "")
            display_list = current_tags.copy()
            if current_manual: display_list.append(current_manual)
            display_text = ", ".join(display_list) if display_list else "無"
            
            btn_type = "primary" if display_list else "secondary"
            btn_label = f"🎨 選擇客製化 (✅已選{len(display_list)}項)" if display_list else "🎨 選擇客製化 (目前: 無)"
            
            c_cust_btn, c_cust_clear = st.columns([4, 1])
            with c_cust_btn:
                if st.button(btn_label, type=btn_type, use_container_width=True, key="btn_m_custom"):
                    custom_dialog("m_custom", custom_tags_main)
            with c_cust_clear:
                if st.button("❌", help="清空主餐客製", use_container_width=True, key="clr_m_custom"):
                    st.session_state["m_custom_tags"] = []
                    st.session_state["m_custom_manual"] = ""
                    st.rerun()
            
            if display_list: st.caption(f"ℹ️ 準備加入: {display_text}")

            if st.button("＋ 加入主餐", type="primary", use_container_width=True):
                if m_price_unit == 0: st.toast("🚫 無法加入：請輸入金額！", icon="⚠️")
                elif m_name:
                    parts = []
                    if m_spicy != "無": parts.append(m_spicy)
                    if display_list: parts.append(", ".join(display_list))
                    cust = " | ".join(parts) if parts else ""
                    
                    total_p = m_price_unit * m_qty
                    if execute_db("INSERT INTO orders (name, category, item_name, price, custom, quantity, order_time, is_paid) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                                  (user_name, "主餐", m_name, total_p, cust, m_qty, datetime.now().strftime('%Y-%m-%d %H:%M'))):
                        st.session_state["m_custom_tags"] = []
                        st.session_state["m_custom_manual"] = ""
                        st.session_state["d_custom_tags"] = []
                        st.session_state["d_custom_manual"] = ""
                        st.toast(f"✅ 已加入：{m_name} x{m_qty}"); st.rerun()
                else: st.toast("⚠️ 請輸入主餐名稱")

    with c_drink:
        st.markdown(f'<div class="section-header header-drink"><div>🥤 {current_drink_shop} (飲料)</div></div>', unsafe_allow_html=True)
        with st.container(border=True):
            d_name = st.text_input("飲料名稱", placeholder="輸入飲料...", key="d_name")
            cp, cq = st.columns(2)
            d_price_unit = cp.number_input("單價", min_value=0, step=5, format="%d", key="d_price")
            d_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="d_qty")
            
            d_size = st.pills("尺寸", ["M", "L", "XL"], default="L", key="d_size", selection_mode="single")
            d_sugar = st.pills("甜度", sugar_levels, default=sugar_levels[0], key="d_sugar", selection_mode="single")
            d_ice = st.pills("冰塊", ice_levels, default=ice_levels[0], key="d_ice", selection_mode="single")
            
            d_current_tags = st.session_state.get("d_custom_tags", [])
            d_current_manual = st.session_state.get("d_custom_manual", "")
            d_display_list = d_current_tags.copy()
            if d_current_manual: d_display_list.append(d_current_manual)
            d_display_text = ", ".join(d_display_list) if d_display_list else "無"
            
            d_btn_type = "primary" if d_display_list else "secondary"
            d_btn_label = f"🎨 選擇客製化 (✅已選{len(d_display_list)}項)" if d_display_list else "🎨 選擇客製化 (目前: 無)"

            dc_btn, dc_clear = st.columns([4, 1])
            with dc_btn:
                if st.button(d_btn_label, type=d_btn_type, use_container_width=True, key="btn_d_custom"):
                    custom_dialog("d_custom", custom_tags_drink)
            with dc_clear:
                if st.button("❌", help="清空飲料客製", use_container_width=True, key="clr_d_custom"):
                    st.session_state["d_custom_tags"] = []
                    st.session_state["d_custom_manual"] = ""
                    st.rerun()

            if d_display_list: st.caption(f"ℹ️ 準備加入: {d_display_text}")

            if st.button("＋ 加入飲料", type="primary", use_container_width=True):
                if d_price_unit == 0: st.toast("🚫 無法加入：請輸入金額！", icon="⚠️")
                elif d_name:
                    base_config = f"{d_size}/{d_sugar}/{d_ice}"
                    final_cust = base_config
                    if d_display_list: final_cust += f" | {', '.join(d_display_list)}"

                    total_p = d_price_unit * d_qty
                    if execute_db("INSERT INTO orders (name, category, item_name, price, custom, quantity, order_time, is_paid) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                                  (user_name, "飲料", d_name, total_p, final_cust, d_qty, datetime.now().strftime('%Y-%m-%d %H:%M'))):
                        st.session_state["d_custom_tags"] = []
                        st.session_state["d_custom_manual"] = ""
                        st.session_state["m_custom_tags"] = []
                        st.session_state["m_custom_manual"] = ""
                        st.toast(f"✅ 已加入：{d_name} x{d_qty}"); st.rerun()
                else: st.toast("⚠️ 請輸入飲料名稱")

with tab2: render_stats_section()
with tab3: render_payment_section()
