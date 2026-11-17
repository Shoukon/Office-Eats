import os
import sys
import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# 網址可用環境變數 APP_URL 覆寫（例如 GitHub Actions 的 Variables）。
DEFAULT_APP_URL = "https://orderpy-3huwovakwtk5iepop2kuxv.streamlit.app"
WAKE_BUTTON_XPATH = "//button[contains(., 'get this app back up')]"


def _frames(driver):
    driver.switch_to.default_content()
    yield None
    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        driver.switch_to.default_content()
        driver.switch_to.frame(frame)
        yield frame
    driver.switch_to.default_content()


def _any_frame(driver, by, selector):
    try:
        for _ in _frames(driver):
            found = driver.find_elements(by, selector)
            if found:
                return found[0]
    except Exception:
        pass
    return None


def find_wake_button(driver):
    return _any_frame(driver, By.XPATH, WAKE_BUTTON_XPATH)


def app_loaded(driver):
    return _any_frame(driver, By.CSS_SELECTOR, "[data-testid='stApp']") is not None


def wake_up():
    app_url = os.environ.get("APP_URL", "").strip() or DEFAULT_APP_URL

    chrome_options = Options()
    chrome_options.add_argument("--headless")  # 無頭模式（不顯示視窗）
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=chrome_options)
    try:
        print(f"開始造訪：{app_url}")
        driver.get(app_url)

        # 休眠中的 Streamlit app 只會顯示喚醒按鈕，單純開網頁不會喚醒；需要點擊。
        time.sleep(5)
        clicked = False
        button = find_wake_button(driver)
        if button is not None:
            print("偵測到休眠頁面，點擊喚醒按鈕。")
            button.click()
            clicked = True
        driver.switch_to.default_content()

        # 等待 app 實際載入。Streamlit Cloud 會把 app 放在 iframe 內，
        # 所以在主文件與所有 iframe 內尋找 Streamlit 主容器。
        try:
            WebDriverWait(driver, 90 if clicked else 45).until(
                lambda d: app_loaded(d) and not find_wake_button(d)
            )
        except TimeoutException:
            raise RuntimeError("等待逾時：app 沒有載入完成，可能仍在休眠或部署失敗。")
        time.sleep(10)  # 讓 WebSocket 連線穩定
        print("喚醒成功！頁面標題為：", driver.title)
    finally:
        driver.quit()


if __name__ == "__main__":
    try:
        wake_up()
    except Exception as exc:
        print(f"發生錯誤：{exc}", file=sys.stderr)
        sys.exit(1)  # 讓 GitHub Actions 顯示失敗，而不是永遠綠燈
