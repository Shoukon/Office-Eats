import streamlit as st
import pandas as pd
import sqlite3
import time
import os
from datetime import datetime

# --- 1. 全域設定與 CSS 美化 ---
st.set_page_config(page_title="點餐囉！各位～ v1.2", page_icon="🍱", layout="wide")

custom_css = """
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    
    .stTabs [data-baseweb="tab-list"] { gap: 8px; background-color: transparent; }
    .stTabs [data-baseweb="tab"] {
        height: 50px; border-radius: 8px;
        background-color: var(--secondary-background-color); 
        padding: 10px 20px; font-weight: 600; border: none; color: var(--text-color);
    }
    .stTabs [aria-selected="true"] { background-color: #FF4B4B !important; color: white !important; }
    
    .section-header {
        padding: 12px 15px; border-radius: 8px; margin-bottom: 15px;
        color: white; font-weight: bold; font-size: 1.1rem;
        display: flex; align-items: center; gap: 10px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    :root {
        --food-gradient: linear-gradient(135deg, #FF8C00, #FF4500);
        --drink-gradient: linear-gradient(135deg, #008080, #2E8B57);
        --money-gradient: linear-gradient(135deg, #DAA520, #B8860B);
    }
    .header-food { background: var(--food-gradient); }
    .header-drink { background: var(--drink-gradient); }
    .header-money { background: var(--money-gradient); color: white;}
    
    div[data-testid="stMetric"] {
        background-color: var(--secondary-background-color);
        border: 1px solid rgba(255, 255, 255, 0.1); padding: 15px; border-radius: 8px;
    }
    
    /* 按鈕風格化 */
    div[data-testid="column"]:nth-of-type(1) div[data-testid="stVerticalBlock"] > div.stButton > button[kind="primary"] {
        background: var(--food-gradient); color: white; border: none; transition: opacity 0.3s;
    }
    div[data-testid="column"]:nth-of-type(2) div[data-testid="stVerticalBlock"] > div.stButton > button[kind="primary"] {
        background: var(--drink-gradient); color: white; border: none; transition: opacity 0.3s;
    }
    div.stButton > button[kind="primary"]:hover { opacity: 0.9; border: none !important; }
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

# --- 2. 資料庫邏輯 ---
DB_FILE = "lunch.db"
DEFAULT_COLLEAGUES = [
    "小昏", "阿文"
]
DEFAULT_OPTIONS = {
    "spicy": ["不辣", "微辣", "小辣", "中辣", "大辣"],
    "ice": ["正常冰", "少冰", "微冰", "去冰", "完全去冰", "溫", "熱"],
    "sugar": ["正常糖", "少糖", "半糖", "微糖", "一分糖", "無糖"],
    "tags": ["不要蔥", "不要蒜", "不要薑", "不要瓜類", "不要高麗菜", "不要香菜"]
}

def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, item_name TEXT,
        price INTEGER, custom TEXT, quantity INTEGER, order_time TEXT, is_paid BOOLEAN)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config_colleagues (name TEXT PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config_options (
        category TEXT, option_value TEXT, PRIMARY KEY (category, option_value))''')
    
    # Init Defaults
    c.execute("SELECT count(*) FROM config_colleagues")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO config_colleagues (name) VALUES (?)", [(n,) for n in DEFAULT_COLLEAGUES])
    c.execute("SELECT count(*) FROM config_options")
    if c.fetchone()[0] == 0:
        for cat, options in DEFAULT_OPTIONS.items():
            c.executemany("INSERT INTO config_options (category, option_value) VALUES (?, ?)", 
                          [(cat, opt) for opt in options])
    conn.commit()
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
    st.error("系統忙碌 (DB Locked)")
    return False

def get_db(query, params=()):
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return df
    except Exception: return pd.DataFrame()

def get_db_size():
    try: return os.path.getsize(DB_FILE) / 1024
    except FileNotFoundError: return 0

# === [關鍵修改] 加入 ORDER BY rowid 以確保順序 ===
def get_config_list(table, col, cat=None):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    # 修改 SQL 語句，強制依照 rowid 排序 (即寫入順序)
    if cat:
        q = f"SELECT {col} FROM {table} WHERE category = ? ORDER BY rowid"
        p = (cat,)
    else:
        q = f"SELECT {col} FROM {table} ORDER BY rowid"
        p = ()
        
    df = pd.read_sql_query(q, conn, params=p)
    conn.close()
    return df

def update_config_list(table, col, new_df, cat=None):
    # 先刪除舊資料
    execute_db(f"DELETE FROM {table}" + (f" WHERE category = '{cat}'" if cat else ""))
    
    # 再依序寫入新資料 (這樣 rowid 就會依照新順序產生)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    c = conn.cursor()
    if cat:
        data = [(cat, row[col]) for _, row in new_df.iterrows() if row[col]]
        c.executemany(f"INSERT INTO {table} (category, {col}) VALUES (?, ?)", data)
    else:
        data = [(row[col],) for _, row in new_df.iterrows() if row[col]]
        c.executemany(f"INSERT INTO {table} ({col}) VALUES (?)", data)
    conn.commit()
    conn.close()

init_db()

# --- 3. 讀取設定 ---
df_colleagues = get_config_list("config_colleagues", "name")
colleagues_list = df_colleagues["name"].tolist() if not df_colleagues.empty else ["請新增人員"]

# 這些 List 現在會嚴格依照後台表格的順序排列
df_spicy = get_config_list("config_options", "option_value", "spicy")
spicy_levels = ["無"] + df_spicy["option_value"].tolist()

df_ice = get_config_list("config_options", "option_value", "ice")
ice_levels = df_ice["option_value"].tolist()

df_sugar = get_config_list("config_options", "option_value", "sugar")
sugar_levels = df_sugar["option_value"].tolist()

df_tags = get_config_list("config_options", "option_value", "tags")
custom_tags = df_tags["option_value"].tolist()

# --- 4. 側邊欄 ---
with st.sidebar:
    st.header("⚙️ 團主設定")
    with st.expander("📝 編輯店家", expanded=True):
        restaurant_name = st.text_input("主餐店家", "好吃雞肉飯")
        drink_shop_name = st.text_input("飲料店家", "清新飲料")
    st.divider()
    with st.expander("👥 人員管理"):
        edited_colleagues = st.data_editor(df_colleagues, num_rows="dynamic", 
            column_config={"name": st.column_config.TextColumn("姓名", required=True)},
            key="ed_col", use_container_width=True, hide_index=True)
        if st.button("💾 儲存人員"):
            update_config_list("config_colleagues", "name", edited_colleagues)
            st.toast("✅ 已更新"); time.sleep(0.5); st.rerun()
    with st.expander("🛠️ 選項管理"):
        t1, t2, t3, t4 = st.tabs(["辣度", "冰塊", "甜度", "客製"])
        def render_opt(tab, cat, df, lbl):
            with tab:
                ed = st.data_editor(df, num_rows="dynamic",
                    column_config={"option_value": st.column_config.TextColumn(lbl, required=True)},
                    key=f"ed_{cat}", use_container_width=True, hide_index=True)
                if st.button(f"儲存{lbl}", key=f"btn_{cat}"):
                    update_config_list("config_options", "option_value", ed, cat)
                    st.toast("✅ 已更新"); time.sleep(0.5); st.rerun()
        
        # 重新讀取確保順序正確 (雖然後面有 rerender 但這樣比較保險)
        render_opt(t1, "spicy", get_config_list("config_options", "option_value", "spicy"), "辣度")
        render_opt(t2, "ice", get_config_list("config_options", "option_value", "ice"), "冰塊")
        render_opt(t3, "sugar", get_config_list("config_options", "option_value", "sugar"), "甜度")
        render_opt(t4, "tags", get_config_list("config_options", "option_value", "tags"), "標籤")
    st.divider()
    if "confirm_reset" not in st.session_state: st.session_state.confirm_reset = False
    if st.button("🗑️ 清空資料庫", type="secondary"): st.session_state.confirm_reset = True
    if st.session_state.confirm_reset:
        st.warning("確定清空？")
        c1, c2 = st.columns(2)
        if c1.button("✅ 確定"):
            execute_db("DELETE FROM orders"); execute_db("VACUUM")
            st.session_state.confirm_reset = False; st.toast("🗑️ 已清空"); st.rerun()
        if c2.button("❌ 取消"): st.session_state.confirm_reset = False; st.rerun()

# --- 5. 統計看板 ---
@st.fragment(run_every=10)
def render_stats_section(r_name, d_name):
    st.caption(f"🔄 自動刷新 | {datetime.now().strftime('%H:%M:%S')}")
    df_all = get_db("SELECT * FROM orders")
    if df_all.empty: st.info("📦 等待第一筆訂單..."); return

    def show_stats(df_source, title, icon_class):
        st.markdown(f'<div class="section-header {icon_class}">{title} ({len(df_source)})</div>', unsafe_allow_html=True)
        if df_source.empty: st.caption("無資料"); return
        c_sum, c_det = st.columns([1, 1.2])
        with c_sum:
            st.markdown("**📦 彙總表**")
            summary = df_source.groupby(['item_name', 'custom'])['quantity'].sum().reset_index()
            summary.columns = ['餐點', '客製', '總量']
            st.dataframe(summary, use_container_width=True, hide_index=True)
            st.metric("該區總額", f"${df_source['price'].sum()}")
        with c_det:
            st.markdown("**📋 明細表**")
            detail = df_source[['name', 'item_name', 'custom', 'quantity', 'price']].copy()
            detail.columns = ['姓名', '餐點', '客製', '數量', '小計']
            st.dataframe(detail, use_container_width=True, hide_index=True)

    show_stats(df_all[df_all['category'] == '主餐'], "🍱 主餐統計", "header-food")
    st.divider()
    show_stats(df_all[df_all['category'] == '飲料'], "🥤 飲料統計", "header-drink")

@st.fragment(run_every=10)
def render_payment_section():
    st.caption(f"🔄 自動刷新 | {datetime.now().strftime('%H:%M:%S')}")
    df_all = get_db("SELECT * FROM orders")
    if df_all.empty: st.write("尚無訂單。"); return
    
    total = df_all['price'].sum()
    paid = df_all[df_all['is_paid'] == 1]['price'].sum()
    prog = paid / total if total > 0 else 0
    st.markdown(f'<div class="section-header header-money">💰 收款進度：${paid} / ${total}</div>', unsafe_allow_html=True)
    st.progress(prog)
    if prog == 1.0: st.balloons(); st.success("🎉 款項全數收齊！")
    
    t1, t2 = st.tabs(["🍱 主餐收款", "🥤 飲料收款"])
    with t1: _pay_logic("主餐", df_all[df_all['category'] == '主餐'], "main")
    with t2: _pay_logic("飲料", df_all[df_all['category'] == '飲料'], "drink")

def _pay_logic(cat, df, k):
    if df.empty: st.caption("無資料"); return
    show_unpaid = st.toggle(f"只看未付 ({cat})", key=f"tg_{k}")
    display = df[df['is_paid'] == 0] if show_unpaid else df
    if display.empty and show_unpaid: st.success("👍 都付完了！"); return
    edited = st.data_editor(display[['id', 'name', 'item_name', 'price', 'is_paid']],
        column_config={"id": None, "name": "姓名", "item_name": "品項", "price": "金額", "is_paid": "已付"},
        disabled=["name", "item_name", "price"], hide_index=True, key=f"ed_{k}", use_container_width=True)
    
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    cur = conn.cursor()
    for _, row in edited.iterrows():
        cur.execute("UPDATE orders SET is_paid = ? WHERE id = ?", (1 if row['is_paid'] else 0, row['id']))
    conn.commit(); conn.close()

# --- 6. 主頁面 ---
st.title("🍱 Office Eats")
tab1, tab2, tab3 = st.tabs(["📝 我要點餐", "📊 統計看板", "💰 收款管理"])

with tab1:
    if st.button("🔄 刷新頁面", type="secondary", use_container_width=True): st.rerun()
    with st.container(border=True):
        st.markdown('<h5>👤 第一步：請問你是誰？</h5>', unsafe_allow_html=True)
        user_name = st.selectbox("選擇名字", colleagues_list, label_visibility="collapsed")

    my_orders = get_db("SELECT * FROM orders WHERE name = ?", (user_name,))
    my_sum = my_orders['price'].sum() if not my_orders.empty else 0
    
    with st.expander(f"📋 {user_name} 的待購清單 (合計: ${my_sum})", expanded=True if not my_orders.empty else False):
        if my_orders.empty: st.caption("尚未點餐")
        else:
            for _, row in my_orders.iterrows():
                c1, c2, c3, c4 = st.columns([0.5, 2.5, 1, 1])
                c1.write("🍱" if row['category'] == '主餐' else "🥤")
                c2.write(f"**{row['item_name']}** x{row['quantity']}")
                c3.write(f"${row['price']}")
                
                with c4.popover("🗑️", help="點擊開啟刪除確認"):
                    st.write(f"確定刪除 **{row['item_name']}**？")
                    if st.button("⭕ 確認刪除", key=f"confirm_del_{row['id']}", type="primary"):
                        execute_db("DELETE FROM orders WHERE id = ?", (row['id'],))
                        st.toast("✅ 已刪除")
                        st.rerun()
                st.caption(f"└ {row['custom']}")
    st.write("") 

    c_food, c_drink = st.columns(2)
    
    with c_food:
        st.markdown(f'<div class="section-header header-food">🍱 {restaurant_name} (主餐)</div>', unsafe_allow_html=True)
        with st.container(border=True):
            m_name = st.text_input("主餐名稱", placeholder="輸入餐點...", key="m_name")
            cp, cq = st.columns(2)
            m_price_unit = cp.number_input("單價", min_value=0, step=5, format="%d", key="m_price")
            m_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="m_qty")
            m_spicy = st.selectbox("辣度", spicy_levels, key="m_spicy")
            m_other = st.multiselect("客製", custom_tags, key="m_other")
            
            if st.button("＋ 加入主餐", type="primary", use_container_width=True):
                if m_price_unit == 0:
                    st.toast("🚫 無法加入：請輸入金額！", icon="⚠️")
                elif m_name:
                    cust = f"{m_spicy}" if m_spicy != "無" else ""
                    if m_other: cust += f" {','.join(m_other)}"
                    total_p = m_price_unit * m_qty
                    if execute_db("INSERT INTO orders (name, category, item_name, price, custom, quantity, order_time, is_paid) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                                  (user_name, "主餐", m_name, total_p, cust, m_qty, datetime.now().strftime('%Y-%m-%d %H:%M'))):
                        st.toast(f"✅ 已加入：{m_name} x{m_qty}")
                        st.rerun()
                else:
                    st.toast("⚠️ 請輸入主餐名稱")

    with c_drink:
        st.markdown(f'<div class="section-header header-drink">🥤 {drink_shop_name} (飲料)</div>', unsafe_allow_html=True)
        with st.container(border=True):
            d_name = st.text_input("飲料名稱", placeholder="輸入飲料...", key="d_name")
            cp, cq = st.columns(2)
            d_price_unit = cp.number_input("單價", min_value=0, step=5, format="%d", key="d_price")
            d_qty = cq.number_input("數量", min_value=1, step=1, value=1, key="d_qty")
            d_size = st.radio("尺寸", ["L", "M", "XL"], horizontal=True, key="d_size")
            ci, cu = st.columns(2)
            d_ice = ci.selectbox("冰塊", ice_levels, key="d_ice")
            d_sugar = cu.selectbox("甜度", sugar_levels, key="d_sugar")
            
            if st.button("＋ 加入飲料", type="primary", use_container_width=True):
                if d_price_unit == 0:
                    st.toast("🚫 無法加入：請輸入金額！", icon="⚠️")
                elif d_name:
                    cust = f"{d_size}/{d_ice}/{d_sugar}"
                    total_p = d_price_unit * d_qty
                    if execute_db("INSERT INTO orders (name, category, item_name, price, custom, quantity, order_time, is_paid) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                                  (user_name, "飲料", d_name, total_p, cust, d_qty, datetime.now().strftime('%Y-%m-%d %H:%M'))):
                        st.toast(f"✅ 已加入：{d_name} x{d_qty}")
                        st.rerun()
                else:
                    st.toast("⚠️ 請輸入飲料名稱")

with tab2: render_stats_section(restaurant_name, drink_shop_name)
with tab3: render_payment_section()
