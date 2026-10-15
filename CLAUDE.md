# DbaGuy

macOS 上自用的 SQL IDE（side project）。支援 PostgreSQL、MySQL、SQLite、Couchbase（自架 7.x）、MSSQL（SQL 帳密驗證）。

## 工作原則（使用者指示）

- **Claude 自主設計與實作**，使用者最後再統一測試。只有真正需要使用者決定的事才問。
- **重功能開發，不過度測試**：只替容易出錯的邏輯（SQL 切分、型別轉換、adapter 與真實 DB 的整合）寫測試；前端以手動驗證為主。
- **MSSQL 公司少用**：遇到真正卡點就擱置，否則照做。
- **範圍以 [feature-priorities.md](docs/research/2026-10-15-feature-priorities.md) 為準**（§3 MoSCoW、§4 Couchbase、§8 使用者回覆），嚴守 Won't 清單，不要包山包海。
- 遵守 `~/.claude/CLAUDE.md`：SOLID、DRY、loguru 分層 log、選用新版且相依性乾淨的套件、完成後回頭檢查正確性。

## 技術選型

| 層 | 選擇 |
|---|---|
| Shell | Tauri 2；Rust 端只負責視窗、選單與啟動/監控 sidecar |
| Frontend | React + TypeScript + Vite |
| Backend sidecar | Python 3.13、poetry、FastAPI、pydantic v2、loguru |
| Drivers | psycopg 3（PG）、mysql-connector-python（MySQL）、pymssql（MSSQL）、couchbase 4.x（Couchbase）、sqlite3（SQLite） |
| 本地資料 | `~/Library/Application Support/DbaGuy/`；密碼存 macOS Keychain |

選型理由與各 DB 的卡點：[landscape §5](docs/research/2026-10-15-sql-ide-landscape.md)、[feature-priorities §7](docs/research/2026-10-15-feature-priorities.md)（**實作 adapter 前必讀**）。

## 工作流程

- 每個子專案依序：spec（`docs/superpowers/specs/`）→ plan（`docs/superpowers/plans/`）→ 實作。
- 每個子專案一個 branch `feat/NN-name`，完成後 `git merge --no-ff` 回 `main` 並 push。
- Repo 是 **public**：不要 commit 任何真實連線資訊或密碼；測試 DB 只用 `dev/` 裡的假帳密。

## 測試資料庫（OrbStack）

OrbStack 提供 Docker，測試 DB 放在 `dev/docker-compose.yml`（隨子專案逐步加入）。

## 進度

| # | 子專案 | 狀態 |
|---|---|---|
| ① | 地基：Tauri + sidecar、adapter 介面、連線管理、PG + SQLite 物件瀏覽 | 進行中 |
| ② | 核心查詢流程：編輯器、結果 grid、執行範圍、取消、history、安全 | 未開始 |
| ③ | MySQL + MSSQL adapter | 未開始 |
| ④ | Couchbase 一級支援 | 未開始 |
| ⑤ | Grid 內編輯（暫存修改、預覽 DML、一次 commit） | 未開始 |
| ⑥ | 資料工具：SQL dump／還原、跨 DB 傳輸 | 未開始 |
| ⑦ | ER 圖（唯讀） | 未開始 |
| ⑧ | MCP server（唯讀） | 未開始 |
