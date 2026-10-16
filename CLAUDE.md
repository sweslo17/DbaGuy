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

OrbStack 提供 Docker，測試 DB 放在 `dev/docker-compose.yml`，連線資訊見下方「開發指令」。

## Repo 結構

- `backend/dbaguy_backend/`：Python sidecar
  - `domain/`：pydantic 模型（profile、metadata、WS 執行協定）
  - `adapters/`：`base.py` 是 5 種 DB 共用的介面；每個 engine 一個子套件，在 `container.default_registry()` 註冊
  - `sessions/worker.py`：每個 session 一條連線、一個執行緒；分頁、暫停、取消都在這裡通用處理
  - `sql/`：純函式的 SQL 文字處理（lexer、切分、分類、補全、copy/匯出格式、表格瀏覽 SELECT）；API 位移一律 UTF-16，只在 `sql/offsets.py` 轉換
  - `services/`：profile、連線（含 SSH tunnel）、metadata、schema 快取、安全政策、history、設定／工作區／scripts
  - `api/`：FastAPI routers；WS 在 `api/sessions.py`
- `src/`：React 前端；API 型別由 `npm run gen:api` 從 OpenAPI 產生（`src/lib/api/schema.d.ts`，不要手改）
  - `features/`：editor（CodeMirror 6）、query（執行流程）、session（WS 控制器、關閉保護）、workspace（自動存檔與還原）、grid、scripts、history、palette、settings
  - `stores/`：zustand；編輯器文字不進 React state（`editorBuffers.ts`，由唯一的 workspace writer 讀取）
  - `lib/`：共用的 `ime.ts`（注音輸入保護）、`shortcuts.ts`、`dialogs.ts`、`native.ts`（Tauri／瀏覽器雙模式）
- `src-tauri/`：Rust 外殼，只負責視窗與 sidecar 生命週期（`src/backend.rs`）
- `dev/`：測試 DB（Docker）與 seed
- `scripts/`：`dev.sh`、`gen-api.sh`、打包用的 `build-backend.sh`／`finalize-app.sh`、量測用的 `measure-startup.sh`

## 開發指令

| 用途 | 指令 |
|---|---|
| 啟動測試 DB | `docker compose -f dev/docker-compose.yml up -d --build --wait` |
| Backend 測試 | `cd backend && .venv/bin/pytest -q` |
| 開發（Tauri） | `scripts/dev.sh` |
| 開發（瀏覽器，給 Claude 用內建瀏覽器驗證） | `scripts/dev.sh --web` 後開 `http://localhost:1420`（資料放 `dev/data/`，不碰正式的 app 資料） |
| 重新產生 API 型別 | `npm run gen:api` |
| 型別檢查／build | `npm run typecheck` / `npm run build` |
| 打包 `.app` | `npm run build:app` |
| 量測啟動時間與記憶體 | `scripts/measure-startup.sh` |

測試 DB：PG `127.0.0.1:54317`（dbaguy/dbaguy，另有 `analytics` database）；SSH bastion `127.0.0.1:2222`（dev/dev，可連到 `postgres:5432`）。兩者都只綁 `127.0.0.1`。

開發用 backend 固定在 port `18765`（`DBAGUY_DEV_PORT` 可覆寫）。Token 由 `scripts/dev.sh` 每次啟動時隨機產生，傳給 backend（`DBAGUY_TOKEN`）、Vite（`VITE_BACKEND_TOKEN`）與 Tauri dev（`DBAGUY_DEV_BACKEND_TOKEN`）；`.env.development` 只放 URL，沒有固定 token。Backend 沒有 `DBAGUY_TOKEN` 會拒絕啟動（自己手動跑 uvicorn 時要自行設定），且只接受 Host 為 `127.0.0.1`／`localhost` 的請求（`TrustedHostMiddleware`）。

## 架構重點

- 新增 engine：在 `adapters/<engine>/` 實作 `DatabaseAdapter` / `DatabaseConnection` / `StatementResult`，到 `container.default_registry()` 註冊；分頁、取消、事件都不用重寫。
- WS 執行協定：`domain/execution.py`。結果分頁暫停時佔住連線；新指令會先 discard。
- PG 採 text-first 載入（只有 bool/int2/int4/float/bytea 用原生型別），大數字與 JSON 不失真。Adapter 的 `fetch()` 回傳原始值，轉成 JSON-safe cell（`values.encode_row`）統一在 `SessionWorker` 做。
- PG 交易中（`BEGIN` 之後）的單一 SELECT／VALUES／TABLE／WITH 走 server-side cursor（DECLARE/FETCH），提前關閉結果只會 `CLOSE`，不會取消語句而 abort 交易；其他會回傳列的語句在交易中改用 buffered。交易外照舊用 `stream()`。`finished` 事件帶 `transaction_failed`（交易已 abort，只能 ROLLBACK）。`COPY … FROM STDIN／TO STDOUT` 直接拒絕（`unsupported_copy`）。
- 密碼只在 Keychain；`save_password=false` 的連線，在連線設定或 unlock 對話框輸入的密碼只暫存在記憶體到 app 結束。缺少的密碼（含 SSH 密碼驗證的 SSH 密碼）回 `password_required`，`detail` 指出缺哪一個。
- SSH tunnel：ssh 跑在一個 `/bin/sh` watchdog 底下，watchdog 的 stdin 是只有 backend 持有的 pipe，backend 一結束（含 SIGKILL）ssh 就跟著結束。Tunnel 以完整 SSH 設定與 SSH 密碼指紋為 key；連線測試一律用自己的 tunnel。
- 打包流程：`npm run build:app` = `build-backend.sh`（PyInstaller onedir → `src-tauri/resources/backend/`）→ `tauri build` → `finalize-app.sh`。
  - Tauri 複製 resources 時會把 symlink 展開，破壞 PyInstaller 的 `_internal`（重複 dylib、`Python.framework` 少了 `Resources`／`Versions/Current`），所以 `finalize-app.sh` 把 sidecar 原樣放回 `.app` 再重新 ad-hoc 簽章。
  - `backend/entry.py` 先呼叫 `multiprocessing.freeze_support()`（防護用）：multiprocessing 若要啟動輔助程序，會重新執行凍結後的執行檔，少了這行每個子程序都會再啟動一個 backend，無限遞迴。以前 loguru 的 `enqueue=True` 會觸發這件事（resource tracker），現在已不用 `enqueue`。
- 選單（`src-tauri/src/menu.rs`）：照 Tauri 預設選單，但 Close Window 改成 ⌘⇧W，⌘W 留給前端關 tab（預設的 Close Window 綁 ⌘W，打包版會直接關掉整個視窗）。
- Log：stderr 與 `~/Library/Logs/DbaGuy/backend.log` 都跟著 `DBAGUY_LOG_LEVEL`（預設 INFO）。SQL 文字是 DEBUG，只有明確開 DEBUG 才會寫進檔案（`scripts/dev.sh` 會開）。

## 已知限制

- Bastion 測試在 1 分鐘內重跑可能失敗：故意輸入錯誤密碼的測試會觸發 OpenSSH 的 PerSourcePenalties。等一下再跑，或 `docker compose -f dev/docker-compose.yml restart bastion`。
- pytest 設定過濾了 Starlette 的 httpx2 deprecation warning；要不要把 TestClient 改用 httpx2 尚未決定。
- 每次重新 build 後 ad-hoc 簽章都會變，Keychain 可能再跳一次「允許存取」。App 未公證（`spctl` 會拒絕），只供本機自用。
- PG 沒有資料列的 SELECT，欄位標題來自 describe。多語句腳本在已開啟的交易裡執行時，任一句失敗會讓整個交易 abort（前端逐句送出，失敗後只能 ROLLBACK）。
- 打包版量測（Apple Silicon、macOS 26）：冷啟動到第一個 API 請求約 0.8–1.3 秒；各程序 RSS 加總約 305–310 MB（上限值，共用頁面重複計算；`footprint` 實際約 150 MB）。這是拿掉 loguru `enqueue=True` 之前的數字：當時它衍生的 resource tracker 程序占約 37 MB，現在已沒有這個程序（尚未重新量測）。

## 進度

| # | 子專案 | 狀態 |
|---|---|---|
| ① | 地基：Tauri + sidecar、adapter 介面、連線管理、PG + SQLite 物件瀏覽 | 完成 |
| ② | 核心查詢流程：編輯器、結果 grid、執行範圍、取消、history、安全 | 完成（followups：[02a](docs/superpowers/notes/2026-10-16-02a-query-backend-followups.md)、[02b](docs/superpowers/notes/2026-10-16-02b-grid-and-data-followups.md)、[02c](docs/superpowers/notes/2026-10-16-02c-editor-and-query-followups.md)，含打包版待驗清單與後續子專案的 carries） |
| ③ | MySQL + MSSQL adapter | 未開始 |
| ④ | Couchbase 一級支援 | 未開始 |
| ⑤ | Grid 內編輯（暫存修改、預覽 DML、一次 commit） | 未開始 |
| ⑥ | 資料工具：SQL dump／還原、跨 DB 傳輸 | 未開始 |
| ⑦ | ER 圖（唯讀） | 未開始 |
| ⑧ | MCP server（唯讀） | 未開始 |
