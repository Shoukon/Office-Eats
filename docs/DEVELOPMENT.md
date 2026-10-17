# 開發與驗證

## 已觀察的環境（2026-10-17）

| 範圍 | Python | Streamlit | Pandas | cryptography | 證據界線 |
|---|---|---|---|---|---|
| Windows 隔離函式測試 | 3.12.14 | 1.54.0 | 2.3.3 | 50.0.2 | 既有測試與本批測試實際執行；不是完整瀏覽器驗收 |
| Cloud 日誌、正式唯讀頁面 | 3.13.16 | 1.66.0 | 3.0.6 | 50.0.2 | 先前同日已觀察啟動及更新；精確部署 SHA 未提供 |
| Devcontainer 宣告 | 3.11 image | 依正式 requirements 解析 | 同左 | 同左 | 沒有實際建置容器驗收 |

Cloud 日誌曾顯示平台將 pyarrow 26.0.0 改為 24.0.0。Windows 套件與 Linux 實際解析不可視為相同。

## 安全測試

`requirements-test.txt` 鎖定已驗證的直接測試依賴；
`requirements-cloud-check.txt` 是依 Cloud 觀察值設定的 CI 相容性目標，
不是已通過本機驗證的宣稱。兩者都不是完整的間接依賴／hash lock。
正式 `requirements.txt` 本批不變：不得在未驗證前把 Cloud 降成 Windows 測試版本，
也不因版本較新就升級。待兩個 CI 環境及必要 UI 驗收通過後，另案決定正式鎖版。

以下命令僅用於無正式 Secrets／資料的開發副本；現有測試環境可直接使用，無需重裝：

```sh
python -m venv .venv-test
# POSIX: . .venv-test/bin/activate
# PowerShell: .venv-test\Scripts\Activate.ps1
# 確認已啟用該 venv；僅在需要建置新環境時安裝
python -m pip install -r requirements-test.txt
python -m pip check
# POSIX: export PYTHONPATH=tests:.
# PowerShell: $env:PYTHONPATH='tests;.'
python -B -m unittest -q test_backup_safety test_final_acceptance test_order_identity test_member_order_identity test_member_rename_acceptance test_member_rename_concurrency
```

Windows 本機可用 `tzdata` 補足 ZoneInfo；CI 為 Ubuntu，不能因此宣稱 Windows／Cloud 行為相同。
測試 AST 抽取函式，使用新假 SQLite，並封鎖 fixture 中的真實 urllib transport。
少量 AppTest／假 UI 驗證不等於完整 Streamlit、dialog 關閉、雙 Session 或 Cloud 生命週期驗收。
不要執行 keep_alive.py 或以正式服務做失敗注入。

## CI

`business-tests.yml` 以只讀 repository 權限、停用 checkout 憑證持久化、精確來源 sparse checkout 執行六個模組。
不檢出 order_data.json、Secrets、DB 或 .env；不執行 keep_alive，不啟動完整應用，
不使用 production Secrets，不上傳測試資料、業務內容或 artifact，也不 commit／push 備份。
觸發路徑排除備份資料提交；既有 Keep Alive workflow 保留。
CI 會下載測試套件，但沒有讓 Office-Eats 初始化或接觸正式 GitHub 備份。
CI 的 Linux／Cloud 相容性結果必須以實際 Actions 結果為準，不能以 YAML 存在當作成功。

## 部署狀態確認

1. 確認 PR 已合併且相關提交是 main 祖先，不把開發分支當正式來源。
2. 由有權限者登入 Cloud，核對 repository、main、order.py、Python 設定與日誌。
3. 記錄 Updated app 與錯誤；若沒有部署 SHA，只能用合併時間、日誌與唯讀行為交叉確認。
4. 不把 Keep Alive、單元測試、頁面版本標籤當作精確部署 hash。
5. 寫入、付款及故障注入只在隔離假資料環境驗收；正式頁面只讀。任何部署由負責人另行核准。

復原與備份目的地切換見 [OPERATIONS.md](OPERATIONS.md)。
