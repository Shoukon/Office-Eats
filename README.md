# Office-Eats

公司午餐／飲料訂購 Streamlit 應用。資料存在本機 SQLite（`lunch.db`），
並以 Fernet 加密後透過 GitHub Contents API 備份到本儲存庫的 `order_data.json`。

## 快速開始（本機）

```sh
python -m venv .venv
# PowerShell: .venv\Scripts\Activate.ps1    POSIX: . .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run order.py
```

需要 Python 3.12 以上。未設定 Secrets 時 app 仍可執行，但資料只存在本機，不會備份。

## Secrets（`.streamlit/secrets.toml` 或 Streamlit Cloud → Settings → Secrets）

```toml
[admin]
password = "管理員密碼"

[github]
token = "具備 Contents 讀寫權限的 fine-grained token"
owner = "Shoukon"
repo = "Office-Eats"
branch = "main"                 # 建議改為 "backup-data"，見 docs/OPERATIONS.md
data_file = "order_data.json"
encryption_key = "Fernet 金鑰（請另外妥善保存，遺失即無法解密備份）"

[default_settings]
colleagues = ["同事A", "同事B"]  # 選用：預設人員名單
```

不要把 Secrets、金鑰、`lunch.db` 或明文匯出檔提交到 Git。

## 測試

```sh
python -m pip install -r requirements-test.txt pytest
python -m pytest tests -q
```

測試只使用假資料與假 GitHub，不會連到正式服務。CI（`.github/workflows/business-tests.yml`）
在 Python 3.12 與 3.13 各跑一次。

## 文件

| 文件 | 內容 |
|---|---|
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 備份、同步異常處理、復原、備份分支切換 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 測試環境、CI、版本鎖定 |
| [docs/CUSTOM_FORMAT.md](docs/CUSTOM_FORMAT.md) | 客製欄位格式 |

## Keep Alive

`keep_alive.py` 由 `.github/workflows/keep_alive_cron.yml` 每 6 小時執行，
點擊休眠頁面的喚醒按鈕。網址可用 GitHub 儲存庫 Variables 的 `APP_URL` 設定；
未設定時使用程式內的預設網址。它只用於保持 app 醒著，不能當作部署成功的證據。
