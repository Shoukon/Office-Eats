import streamlit as st
import pandas as pd
import sqlite3
import time
from datetime import datetime

# ==========================================
# 0. 系統設定區
# ==========================================
try:
    ADMIN_PASSWORD = st.secrets["admin"]["password"]
except Exception:
    ADMIN_PASSWORD = "3345678"

DB_FILE = "lunch.db"

# ==========================================
# 1. 頁面設定與 CSS (包含圖示字型)
# ==========================================
st.set_page_config(page_title="點餐哦各位～ v3.3", page_icon="🍱", layout="wide")

custom_css = """
<style>
    /* 載入 Material Symbols 圖示庫 */
    @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined');
    
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    
    .stTabs [data-baseweb="tab-list"] { gap: 8px; background-color: transparent; }
    .stTabs [data-baseweb="tab"] {
        height: 50px; border-radius: 8px;
        background-color: transparent; 
        padding: 10px 20px; font-weight: 600; border: none; color: gray;
        font-size: 1rem;
        transition: all 0.2s;
    }
    .stTabs [aria-selected="true"] {
        background-color: var(--secondary-background-color) !important;
        border-bottom: 3px solid #FF4B4B !important; 
        border-radius: 8px 8px 0 0 !important; 
    }

    .section-header {
        padding: 12px 18px; border-radius: 8px; margin-bottom: 15px;
        color: white; font-weight: 700; font-size: 1.15rem;
        display: flex; align-items: center; justify-content: space-between;
    }

    .header-food { background: linear-gradient(135deg, #FF8C00, #FF4500); }
    .header-drink { background: linear-gradient(135deg, #008080, #2E8B57); }

    .card-title { font-size: 1.1rem; font-weight: 700; }
    .price-tag { color: #FF4B4B; font-weight: 700; }
    .refresh-text { color: gray; font-size: 0.8rem; text-align: right; }
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

# ==========================================
# 2. 資料庫邏輯 (精簡版)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, item_name TEXT, price INTEGER, custom TEXT, quantity INTEGER, order_time TEXT, is_paid BOOLEAN)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config_colleagues (name TEXT PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config_shop (category TEXT PRIMARY KEY, shop_name TEXT)''')
    c.execute("INSERT OR IGNORE INTO config_shop VALUES ('main', '吃什麼？')")
    c.execute("INSERT OR IGNORE INTO config_shop VALUES ('drink', '喝什麼？')")
    conn.commit(); conn.close()

def execute_db(query, params=()):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit(); conn.close()

def get_db(query, params=()):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

init_db()

# ==========================================
# 3. 頁面邏輯
# ==========================================
st.title("🍱 點餐哦各位～")
tab1, tab2, tab3 = st.tabs(["📝 我要點餐", "📊 統計看板", "💰 收款管理"])

with tab1:
    if 'user_name' not in st.session_state: st.session_state.user_name = None
    
    # 登入區塊
    with st.container(border=True):
        col1, col2 = st.columns([4, 1])
        if st.session_state.user_name:
            col1.info(f"使用者：**{st.session_state.user_name}**")
        else:
            col1.warning("請先選擇使用者")
        
        if col2.button("👤 登入/切換"):
            st.session_state.user_name = st.selectbox("選擇姓名", get_db("SELECT name FROM config_colleagues")['name'].tolist())
            st.rerun()

    if st.session_state.user_name:
        # 待點清單 (修復亂碼：直接使用文字與 emoji)
        my_orders = get_db("SELECT * FROM orders WHERE name = ?", (st.session_state.user_name,))
        with st.expander(f"📋 {st.session_state.user_name} 的訂單 (共 {len(my_orders)} 筆)"):
            for _, row in my_orders.iterrows():
                c1, c2, c3, c4 = st.columns([0.5, 3, 1, 1])
                c1.write("🍱" if row['category'] == '主餐' else "🥤")
                c2.write(f"**{row['item_name']}** x{row['quantity']}")
                c3.write(f"${row['price']}")
                if c4.button("🗑️", key=f"del_{row['id']}"):
                    execute_db("DELETE FROM orders WHERE id = ?", (row['id'],))
                    st.rerun()
                if row['custom']:
                    st.caption(f"備註: {row['custom']}")
        
        # 點餐區
        c_food, c_drink = st.columns(2)
        with c_food:
            st.markdown('<div class="section-header header-food">🍱 主餐</div>', unsafe_allow_html=True)
            name = st.text_input("餐點名稱", key="m_name")
            price = st.number_input("單價", step=5, key="m_price")
            if st.button("加入主餐"):
                execute_db("INSERT INTO orders (name, category, item_name, price, quantity, is_paid) VALUES (?, ?, ?, ?, ?, 0)", 
                           (st.session_state.user_name, "主餐", name, price, 1))
                st.rerun()
        with c_drink:
            st.markdown('<div class="section-header header-drink">🥤 飲料</div>', unsafe_allow_html=True)
            name = st.text_input("飲料名稱", key="d_name")
            price = st.number_input("價格", step=5, key="d_price")
            if st.button("加入飲料"):
                execute_db("INSERT INTO orders (name, category, item_name, price, quantity, is_paid) VALUES (?, ?, ?, ?, ?, 0)", 
                           (st.session_state.user_name, "飲料", name, price, 1))
                st.rerun()

with tab2:
    st.header("📊 統計看板")
    df = get_db("SELECT * FROM orders")
    if not df.empty:
        st.dataframe(df.groupby(['item_name', 'category'])['quantity'].sum())

with tab3:
    st.header("💰 收款管理")
    df = get_db("SELECT * FROM orders")
    st.write(df)
