# Office-Eats v3.8.0

辦公室點餐系統，正式入口為 `order.py`。資料保存在 SQLite，4.1 格式備份經 Fernet 加密後同步至 GitHub。

- [開發、測試與部署狀態確認](docs/DEVELOPMENT.md)
- [備份分支評估、異常處理與復原](docs/OPERATIONS.md)

測試使用假 SQLite 與 FakeRemote，不需要正式 Secrets。不要直接執行 `order.py` 來跑單元測試。
正式部署來源為 `main`；PR、CI 通過、Cloud 更新成功與精確部署 commit 是不同證據。
本批不切換正式備份目的地，不合併或部署。


## v3.8.0

- 修正單筆訂單刪除成功後，確認視窗未關閉；取消不寫入，失敗可重試或取消。
- 刪除確認移除多餘圓圈圖示，使用原生 primary 按鈕及等寬操作欄，保留焦點提示。
- 保留訂單身分、付款狀態驗證及本機儲存／GitHub 同步失敗處理。

此版本資訊不代表已建立 tag、GitHub Release 或已部署。
