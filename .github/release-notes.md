macOS 上的 SQL IDE，支援 PostgreSQL、MySQL、SQL Server、Couchbase（自架 7.x）與 SQLite。

## 下載與安裝

1. 下載下面的 `.dmg`，打開後把 **DbaGuy** 拖進 **Applications**。
2. 這個 app 沒有用 Apple Developer ID 簽章，也沒有公證，所以 macOS 第一次開啟時會擋下來（常見的訊息是「已損毀，無法打開」）。在「終端機」執行一次：

   ```bash
   xattr -dr com.apple.quarantine /Applications/DbaGuy.app
   ```

   之後就能照常開啟。
3. 第一次開啟或更新版本之後，macOS 可能會問是否允許 DbaGuy 使用「鑰匙圈」裡存的密碼，選「允許」或「永遠允許」。

需求：Apple Silicon 的 Mac（M 系列晶片），macOS 15 或更新版本。`.dmg.sha256` 是檔案的 SHA-256（`shasum -a 256 <檔名>` 比對）。

## 資料放在哪裡

- 設定、連線、History、工作區：`~/Library/Application Support/DbaGuy/`（更新版本不影響）
- 密碼：macOS 鑰匙圈
- Log：`~/Library/Logs/DbaGuy/`

自用的 side project：由 GitHub Actions 在 macOS 上打包，沒有公證。
