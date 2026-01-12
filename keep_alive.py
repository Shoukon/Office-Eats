from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import time

def wake_up():
    # 設定 Chrome 瀏覽器選項
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # 無頭模式（不顯示視窗）
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    # 啟動瀏覽器
    driver = webdriver.Chrome(options=chrome_options)
    
    # 【重要】請將下方網址替換成你的 Streamlit App 實際網址
    app_url = "https://orderpy-3huwovakwtk5iepop2kuxv.streamlit.app"
    
    try:
        print(f"開始造訪：{app_url}")
        driver.get(app_url)
        
        # 等待 20 秒，確保 WebSocket 連線成功並完全載入
        time.sleep(20) 
        print("喚醒成功！頁面標題為：", driver.title)
    except Exception as e:
        print(f"發生錯誤：{e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    wake_up()
