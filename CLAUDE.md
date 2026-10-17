# DbaGuy

macOS 上自用的 SQL IDE（side project）。支援 PostgreSQL、MySQL、SQLite、Couchbase（自架 7.x）、MSSQL（SQL 帳密驗證）。

## 工作原則（使用者指示）

- **Claude 自主設計與實作**，使用者最後再統一測試。只有真正需要使用者決定的事才問。
- **重功能開發，不過度測試**：只替容易出錯的邏輯（SQL 切分、型別轉換、adapter 與真實 DB 的整合）寫測試；前端以手動驗證為主。
- **MSSQL 公司少用**：遇到真正卡點就擱置，否則照做。
- **範圍以 [feature-priorities.md](docs/research/2026-10-15-feature-priorities.md) 為準**（§3 MoSCoW、§4 Couchbase、§8 使用者回覆），嚴守 Won't 清單，不要包山包海。
- 遵守 `~/.claude/CLAUDE.md`：SOLID、DRY、loguru 分層 log、選用新版且相依性乾淨的套件、完成後回頭檢查正確性。
- **每個階段回頭檢視時（task review、最終 review、子專案收尾），除了對照 plan 是否完整，也要對照 [feature-priorities.md](docs/research/2026-10-15-feature-priorities.md) 整理的痛點與需求**：§2 品質需求 Q1–Q6（輕快低記憶體、穩定不丟工作、可信任、聚焦 80% 情境、多引擎、macOS 慣例）、§3 Must 與該子專案相關的列、§8 使用者回覆。說明這階段解決了哪些、有沒有踩到或退步，避免做出沒解決痛點的玩具。

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
    - `adapters/mysql/`：`settings`（連線參數、SSL、options）、`catalog`／`introspection`／`schema`（物件樹、DDL、補全用 snapshot）、`types`（型別表）、`adapter`（連線、執行、取消、交易）
  - `sessions/worker.py`：每個 session 一條連線、一個執行緒；分頁、暫停、取消都在這裡通用處理
  - `sql/`：純函式的 SQL 文字處理（lexer、切分、分類、補全、copy/匯出格式、表格瀏覽 SELECT）；API 位移一律 UTF-16，只在 `sql/offsets.py` 轉換。各方言的識別字引號與 literal 只從 `sql/dialects.py` 來，其他地方（含前端）不自己拼
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
| 開發（Tauri） | `scripts/dev.sh`（不要直接跑 `npm run tauri dev`：沒有 `DBAGUY_DEV_BACKEND_URL` 時 Tauri 會自己啟動 backend，而且沒有 `DBAGUY_DATA_DIR`，所以用的是正式的 `~/Library/Application Support/DbaGuy` 與正式的 Keychain service `app.dbaguy`） |
| 開發（瀏覽器，給 Claude 用內建瀏覽器驗證） | `scripts/dev.sh --web` 後開 `http://localhost:1420`（資料放 `dev/data/`，不碰正式的 app 資料） |
| 重新產生 API 型別 | `npm run gen:api` |
| 型別檢查／build | `npm run typecheck` / `npm run build` |
| 打包 `.app` | `npm run build:app` |
| 量測啟動時間與記憶體 | `scripts/measure-startup.sh`（以 `open --env` 給 app 暫存的 `DBAGUY_DATA_DIR`，不碰正式的 app 資料） |

測試 DB：PG `127.0.0.1:54317`（dbaguy/dbaguy，另有 `analytics` database）；MySQL `127.0.0.1:33067`（dbaguy/dbaguy，root 同密碼，databases `dbaguy` 與 `analytics`，seed 在 `dev/mysql/init/`）；SSH bastion `127.0.0.1:2222`（dev/dev，可連到 `postgres:5432` 與 `mysql:3306`）。都只綁 `127.0.0.1`。MySQL 的 seed 只在第一次建 volume 時執行，改了 seed 要重建：`docker compose -f dev/docker-compose.yml rm -sfv mysql && docker compose -f dev/docker-compose.yml up -d --build --wait`。

開發用 backend 固定在 port `18765`（`DBAGUY_DEV_PORT` 可覆寫）。Token 由 `scripts/dev.sh` 每次啟動時隨機產生，傳給 backend（`DBAGUY_TOKEN`）、Vite（`VITE_BACKEND_TOKEN`）與 Tauri dev（`DBAGUY_DEV_BACKEND_TOKEN`）；`.env.development` 只放 URL，沒有固定 token。Backend 沒有 `DBAGUY_TOKEN` 會拒絕啟動（自己手動跑 uvicorn 時要自行設定），且只接受 Host 為 `127.0.0.1`／`localhost` 的請求（`TrustedHostMiddleware`）。

## 架構重點

- 新增 engine：在 `adapters/<engine>/` 實作 `DatabaseAdapter` / `DatabaseConnection` / `StatementResult`，到 `container.default_registry()` 註冊；分頁、取消、事件都不用重寫。
- WS 執行協定：`domain/execution.py`。結果分頁暫停時佔住連線；新指令會先 discard。
- PG 採 text-first 載入（只有 bool/int2/int4/float/bytea 用原生型別），大數字與 JSON 不失真。Adapter 的 `fetch()` 回傳原始值，轉成 JSON-safe cell（`values.encode_row`）統一在 `SessionWorker` 做。
- PG 交易中（`BEGIN` 之後）的單一 SELECT／VALUES／TABLE／WITH 走 server-side cursor（DECLARE/FETCH），提前關閉結果只會 `CLOSE`，不會取消語句而 abort 交易；其他會回傳列的語句在交易中改用 buffered。交易外照舊用 `stream()`。`finished` 事件帶 `transaction_failed`（交易已 abort，只能 ROLLBACK）。`COPY … FROM STDIN／TO STDOUT` 直接拒絕（`unsupported_copy`）。
- MySQL（mysql-connector-python，一律 `use_pure=True`）：
  - 使用者 SQL 走 `_VerbatimCursor`（直接 `cmd_query`，原文送出）。不用 `cursor.execute()`：它的 DELIMITER 改寫會改到文字。也不用 `conn.cursor()`：每句會多送一次 PING。`MULTI_STATEMENTS` 關閉，一次 `run()` 一句；`DELIMITER` 行由 splitter 吃掉，不送到 server。
  - 取消與提前關閉結果：從一條短的側連線送 `KILL QUERY <thread>`，交易保留；關閉時把剩下的結果（含 CALL 後面的 result set）讀掉丟棄。
  - `in_transaction` 讀協定的 status flag，不多一次 round trip；`autocommit` 有快取，只在可能改到它的語句（提到 autocommit、CALL／EXECUTE）之後重新讀。
  - 連線選項 `time_zone`／`sql_mode` 不交給 driver（它會不加引號直接拼進自己的 `SET`）：adapter 在連線後用 `quote_literal` 送一句 `SET SESSION time_zone = '…', SESSION sql_mode = '…'`，在 READ ONLY 之前。URL 匯入照樣把它們放進 profile options。
  - 唯讀 profile：連線後 `SET SESSION TRANSACTION READ ONLY`（server 也會擋住藏在 function／procedure 裡的寫入），再加上 `SafetyPolicy` 的 allow-list。
  - Text-first 載入，型別表在 `adapters/mysql/types.py`：大的 unsigned 整數、DECIMAL、日期不失真（`0000-00-00` 與超過 24 小時的 TIME 照原樣），`TINYINT(1)` 顯示數字。
- 密碼只在 Keychain；`save_password=false` 的連線，在連線設定或 unlock 對話框輸入的密碼只暫存在記憶體到 app 結束。缺少的密碼（含 SSH 密碼驗證的 SSH 密碼）回 `password_required`，`detail` 指出缺哪一個。
  - Keychain service 由 data dir 決定（`container.keychain_service`）：預設的 `~/Library/Application Support/DbaGuy` 用 `app.dbaguy`；`DBAGUY_DATA_DIR` 指到別處（`scripts/dev.sh` 的 `dev/data`、測試、臨時 sidecar、`measure-startup.sh`）一律用 `app.dbaguy.dev`，開發時存的密碼不會寫進或讀到正式 app 的項目。這個改動之前 dev 存在 `app.dbaguy` 的密碼要重新輸入。
- SSH tunnel：ssh 跑在一個 `/bin/sh` watchdog 底下，watchdog 的 stdin 是只有 backend 持有的 pipe，backend 一結束（含 SIGKILL）ssh 就跟著結束。Tunnel 以完整 SSH 設定與 SSH 密碼指紋為 key；連線測試一律用自己的 tunnel。
- 打包流程：`npm run build:app` = `build-backend.sh`（PyInstaller onedir → `src-tauri/resources/backend/`）→ `tauri build` → `finalize-app.sh`。
  - Tauri 複製 resources 時會把 symlink 展開，破壞 PyInstaller 的 `_internal`（重複 dylib、`Python.framework` 少了 `Resources`／`Versions/Current`），所以 `finalize-app.sh` 把 sidecar 原樣放回 `.app` 再重新 ad-hoc 簽章。
  - `backend/entry.py` 先呼叫 `multiprocessing.freeze_support()`（防護用）：multiprocessing 若要啟動輔助程序，會重新執行凍結後的執行檔，少了這行每個子程序都會再啟動一個 backend，無限遞迴。以前 loguru 的 `enqueue=True` 會觸發這件事（resource tracker），現在已不用 `enqueue`。
  - MySQL 只打包 pure driver：spec 排除 C extension（`_mysql_connector`，連同它帶的 dylib）以及用不到的 aio、Django、OpenTelemetry instrumentation、Kerberos／LDAP／OCI／OpenID／WebAuthn plugin。`collect_submodules("mysql.connector")` 保留以 importlib 載入的 locales 與 auth plugins；sqlglot 的 MySQL 方言跟 PG／SQLite 一樣按名稱列在 hidden imports。Pure 套件在 PYZ 裡，不會出現在 `_internal/`，所以打包後的檢查是讓凍結的 sidecar 實際連 dev MySQL 跑一次查詢。
- 選單（`src-tauri/src/menu.rs`）：照 Tauri 預設選單，但 Close Window 改成 ⌘⇧W，⌘W 留給前端關 tab（預設的 Close Window 綁 ⌘W，打包版會直接關掉整個視窗）。
- Log：stderr 與 `~/Library/Logs/DbaGuy/backend.log` 都跟著 `DBAGUY_LOG_LEVEL`（預設 INFO）。SQL 文字是 DEBUG，只有明確開 DEBUG 才會寫進檔案（`scripts/dev.sh` 會開）。

## 已知限制

- Bastion 測試在 1 分鐘內重跑可能失敗：故意輸入錯誤密碼的測試會觸發 OpenSSH 的 PerSourcePenalties。等一下再跑，或 `docker compose -f dev/docker-compose.yml restart bastion`。
- pytest 設定過濾了 Starlette 的 httpx2 deprecation warning；要不要把 TestClient 改用 httpx2 尚未決定。
- 每次重新 build 後 ad-hoc 簽章都會變，Keychain 可能再跳一次「允許存取」。App 未公證（`spctl` 會拒絕），只供本機自用。
- PG 沒有資料列的 SELECT，欄位標題來自 describe。多語句腳本在已開啟的交易裡執行時，任一句失敗會讓整個交易 abort（前端逐句送出，失敗後只能 ROLLBACK）。
- MySQL：
  - Lexer 假設 server 用預設的 sql_mode：不偵測 `ANSI_QUOTES`／`NO_BACKSLASH_ESCAPES`。在這種 server 上，切句與安全檢查對字串的判斷可能和 server 不同。
  - SSL mode `disable`（含匯入的 `useSSL=false`）時，pure driver 做不了 caching_sha2_password 的完整登入，所以 server 重啟後第一次用密碼登入會失敗（Access denied）。錯誤訊息附提示：第一次登入要用 TLS（SSL mode Prefer 或 Require），或改用 mysql_native_password 的帳號。帳密真的打錯時也會出現這個提示。
  - 取消剛好在語句結束時送到，會回報成已取消（實際已執行完）。
  - Procedure 裡的 `SLEEP()` 會吞掉 KILL QUERY（提早回傳，procedure 接著跑下一句），所以取消或關閉這種 CALL 時，後面的結果仍會送來，被讀掉丟棄。
  - 關閉暫停中的結果時的 drain：KILL QUERY 送出後，讀列不設時間上限，但能讀的只剩 server 已經送出的列（本機 125 萬列約 3.7 秒，TLS 4.1 秒）。10 秒上限只用在兩處，超過就放棄這條連線（顯示 Reconnect）：KILL 送不出去時的整個 drain；以及 KILL 之後等待 CALL 下一個 result set 的時間合計（例如 procedure 在迴圈裡 `SLEEP()`，吞掉 KILL 後還一直送結果）。
  - Pure driver 的串流以絕對預算驗收（10 萬列 ≤ 2.0 秒、500 列一頁 ≤ 25 ms），不跟 PG 比：本機實測 10 萬列 0.59–0.60 秒、最慢一頁 3.5 ms。上面的 drain 與大量匯出若嫌慢，之後的優化是 C extension 或 bulk read（carry）。
  - Table／view 超過 10,000 個的 server，補全用的 snapshot 只載入目前的 database（連線沒指定 database 就不載入）。這是縮小範圍，不是大小上限：目前的 database 本身再大也會整個載入。
  - Tab 不會跟著 `USE` 切換：補全的預設 schema 一直是 metadata 連線的 database（profile 的 database），`USE` 之後補全建議的未加前綴名稱可能屬於另一個 database。從 History 重開的 MySQL tab 會連到當時記錄的 database，UI 沒有 database picker 可以改；schema snapshot 每個 profile 只有一份（`SchemaCache` 對沒有 per-database 連線的 engine 忽略 database）。
  - Dev 的 `mysql` container 帶 `--log-bin-trust-function-creators=1`：8.4 預設開 binary log，沒有 SUPER 的 `dbaguy` 才能建 function 與 trigger。
- 打包版量測（Apple Silicon、macOS 26；03a 時量，已拿掉 loguru `enqueue=True`、已加入 mysql-connector）：冷啟動到第一個 API 請求約 0.9–1.1 秒（每次重新 build 後的第一次啟動約 2.3 秒）；各程序 RSS 加總約 293–305 MB（上限值，共用頁面重複計算），`footprint` 加總約 156 MB（backend 約 70 MB）。Sidecar 約 41 MB，加入 MySQL 只多約 0.3 MB；如果讓 PyInstaller 自動收進 C extension 與它帶的 dylib，會變成約 54 MB。

## 進度

| # | 子專案 | 狀態 |
|---|---|---|
| ① | 地基：Tauri + sidecar、adapter 介面、連線管理、PG + SQLite 物件瀏覽 | 完成 |
| ② | 核心查詢流程：編輯器、結果 grid、執行範圍、取消、history、安全 | 完成（followups：[02a](docs/superpowers/notes/2026-10-16-02a-query-backend-followups.md)、[02b](docs/superpowers/notes/2026-10-16-02b-grid-and-data-followups.md)、[02c](docs/superpowers/notes/2026-10-16-02c-editor-and-query-followups.md)，含打包版待驗清單與後續子專案的 carries） |
| ③ | MySQL + MSSQL adapter | 進行中：03a 方言基礎 + MySQL 完成；03b SQL Server 待做 |
| ④ | Couchbase 一級支援 | 未開始 |
| ⑤ | Grid 內編輯（暫存修改、預覽 DML、一次 commit） | 未開始 |
| ⑥ | 資料工具：SQL dump／還原、跨 DB 傳輸 | 未開始 |
| ⑦ | ER 圖（唯讀） | 未開始 |
| ⑧ | MCP server（唯讀） | 未開始 |
