import streamlit as st
import pandas as pd
import sqlite3
import time
import os
from datetime import datetime

# --- 1. 全域設定與 CSS 美化 ---
st.set_page_config(page_title="點餐哦各位～ v2.0", page_icon="🍱", layout="wide")

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

    /* 防止 iPhone 自動放大與鍵盤干擾 */
    @media screen and (max-width: 768px) {
        input, select, textarea {
            font-size: 16px !important; 
        }
    }
    
    /* 收款卡片特別樣式 */
    .pay-card {
        padding: 10px;
        border-radius: 8px;
        background-color: var(--secondary-background-color);
        margin-bottom: 8px;
        border-left: 5px solid #FF4B4B; /* 未付紅線 */
    }
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

# --- 2. 資料庫邏輯 ---
DB_FILE = "lunch.db"
DEFAULT_COLLEAGUES = [
    "阿修", "阿文", "小昏", "Jeff", "明穎", "薯條", "阿莨", "吳姐", 
    "妙莉", "歆媛", "白白", "小熊", "之之", "方方", "企鵝", 
    "欣蘋", "博榮", "欣蓉", "小安", "姷瑢"
]
DEFAULT_OPTIONS = {
    "spicy": ["不辣", "微辣", "小辣", "中辣", "大辣"],
    "ice": ["正常冰", "少冰", "微冰", "去冰", "完全去冰", "溫", "熱"],
    "sugar": ["正常糖", "少糖", "半糖", "微糖", "一分糖", "無糖"],
    "tags": ["不要蔥", "不要蒜", "不要香菜", "飯少", "加飯"]
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

def get_config_list(table, col, cat=None):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
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
    execute_db(f"DELETE FROM {table}" + (f" WHERE category = '{cat}'" if cat else ""))
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

df_spicy = get_config_list("config_options", "option_value", "spicy")
spicy_levels = ["無"] + df_spicy["option_value"].tolist()

df_ice = get_config_list("config_options", "option_value", "ice")
ice_levels = df_ice["option_value"].tolist()

df_sugar = get_config_list("config_options", "option_value", "sugar")
sugar_levels = df_sugar["option_value"].tolist()

df_tags = get_config_list("config_options", "option_value", "tags")
custom_tags = df_tags["option_value"].tolist()

# --- 4. 側邊欄 (重構：開放功能置頂，進階功能隱藏) ---
with st.sidebar:
    st.header("⚙️ 開團管理 (全員可用)")
    
    # === 1. 編輯店家 (開放) ===
    # 移除 Expander，使其直接可見，方便大家填寫
    st.subheader("1. 今日店家")
    restaurant_name = st.text_input("主餐店家", "好吃雞肉飯")
    drink_shop_name = st.text_input("飲料店家", "清新飲料")
    
    st.divider()

    # === 2. 清空資料庫 (開放) ===
    st.subheader("2. 每日重置")
    if "confirm_reset" not in st.session_state:
        st.session_state.confirm_reset = False

    if st.button("🗑️ 跨日清空資料庫", type="secondary"):
        st.session_state.confirm_reset = True
    
    if st.session_state.confirm_reset:
        st.warning("確定清空所有訂單？")
        c1, c2 = st.columns(2)
        if c1.button("✅ 確定"):
            execute_db("DELETE FROM orders")
            execute_db("VACUUM")
            st.session_state.confirm_reset = False
            st.toast("🗑️ 資料庫已重置！")
            st.rerun()
        if c2.button("❌ 取消"):
            st.session_state.confirm_reset = False
            st.rerun()

    st.divider()

    # === 3. 進階系統維護 (摺疊隱藏) ===
    # 這邊放比較少用到的設定，避免誤觸，但也不設密碼，方便需要時打開
    with st.expander("🔧 進階系統設定 (人員/菜單)"):
        st.caption("⚠️ 此區影響全域設定，請小心操作")
        
        st.write("**👥 人員名單**")
        edited_colleagues = st.data_editor(df_colleagues, num_rows="dynamic", 
            column_config={"name": st.column_config.TextColumn("姓名", required=True)},
            key="ed_col", use_container_width=True, hide_index=True)
        if st.button("💾 儲存人員"):
            update_config_list("config_colleagues", "name", edited_colleagues)
            st.toast("✅ 已更新"); time.sleep(0.5); st.rerun()
            
        st.divider()
        
        st.write("**🛠️ 菜單選項**")
        t1, t2, t3, t4 = st.tabs(["辣度", "冰塊", "甜度", "客製"])
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
        render_opt(t4, "tags", get_config_list("config_options", "option_value", "tags"), "標籤")

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
    
    if prog == 1.0: 
        st.success("🎉 太棒了！款項已全數收齊！")
    
    t1, t2 = st.tabs(["🍱 主餐收款", "🥤 飲料收款"])
    with t1: _pay_logic_card_style("主餐", df_all[df_all['category'] == '主餐'], "main")
    with t2: _pay_logic_card_style("飲料", df_all[df_all['category'] == '飲料'], "drink")

def _pay_logic_card_style(cat, df, k):
    if df.empty: st.caption("無資料"); return
    
    unpaid_df = df[df['is_paid'] == 0]
    paid_df = df[df['is_paid'] == 1]
    
    if not unpaid_df.empty:
        st.markdown(f"**⚠️ 待收款 ({len(unpaid_df)} 筆)**")
        for idx, row in unpaid_df.iterrows():
            with st.container(border=True):
                c1, c2 = st.columns([3, 1.2])
                with c1:
                    st.markdown(f"**{row['name']}** - <span style='color:#FF4B4B; font-weight:bold'>${row['price']}</span>", unsafe_allow_html=True)
                    st.caption(f"{row['item_name']} (x{row['quantity']})")
                with c2:
                    if st.button("收款", key=f"pay_{k}_{row['id']}", use_container_width=True, type="primary"):
                        execute_db("UPDATE orders SET is_paid = 1 WHERE id = ?", (row['id'],))
                        st.toast(f"💰 已收: {row['name']}")
                        st.rerun()
    else:
        st.success("👍 此區全數已付款！")

    if not paid_df.empty:
        st.write("") 
        with st.expander(f"✅ 已付款名單 ({len(paid_df)} 筆) - 點此展開撤銷"):
            for idx, row in paid_df.iterrows():
                c1, c2 = st.columns([3, 1.2])
                with c1:
                    st.write(f"~~{row['name']} - ${row['price']}~~") 
                with c2:
                    if st.button("撤銷", key=f"undo_{k}_{row['id']}", use_container_width=True):
                        execute_db("UPDATE orders SET is_paid = 0 WHERE id = ?", (row['id'],))
                        st.toast(f"↩️ 已撤銷: {row['name']}")
                        st.rerun()

# --- 6. 主頁面 ---
st.title("🍱 點餐哦各位～")
tab1, tab2, tab3 = st.tabs(["📝 我要點餐", "📊 統計看板", "💰 收款管理"])

with tab1:
    if st.button("🔄 刷新頁面", type="secondary", use_container_width=True): st.rerun()
    with st.container(border=True):
        st.markdown('<h5>👤 請問你是誰？</h5>', unsafe_allow_html=True)
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
            
            m_spicy = st.pills("辣度", spicy_levels, default=spicy_levels[0], key="m_spicy", selection_mode="single")
            with st.popover("👇 選擇客製化 (點此展開)", use_container_width=True):
                st.caption("請選擇客製需求 (可複選)")
                m_other = st.pills("客製選項", custom_tags, key="m_other", selection_mode="multi", label_visibility="collapsed")
            if m_other: st.caption(f"✅ 已選客製: {', '.join(m_other)}")
            
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
            
            d_size = st.pills("尺寸", ["L", "M", "XL"], default="L", key="d_size", selection_mode="single")
            d_ice = st.pills("冰塊", ice_levels, default=ice_levels[0], key="d_ice", selection_mode="single")
            d_sugar = st.pills("甜度", sugar_levels, default=sugar_levels[0], key="d_sugar", selection_mode="single")
            
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
