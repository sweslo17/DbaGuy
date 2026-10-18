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
| Drivers | psycopg 3（PG）、mysql-connector-python（MySQL）、pymssql（MSSQL）、Couchbase 走 Query Service REST（stdlib `http.client`，不用 SDK；理由見 [④ spec](docs/superpowers/specs/2026-10-18-04-couchbase-design.md) R-04-1）、sqlite3（SQLite） |
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
    - `adapters/mssql/`：`settings`＋`freetds`（連線參數、URL 匯入、app 自己的 freetds.conf）、`catalog`／`introspection`／`schema`、`types`、`adapter`（連線、執行、交易）、`cancel`（取消的階段與決策）、`driver`（唯一 import pymssql 的地方；每次進 driver 都經 `GuardedConnection` 計數）
  - `sessions/worker.py`：每個 session 一條連線、一個執行緒；分頁、暫停、取消都在這裡通用處理
  - `sql/`：純函式的 SQL 文字處理（lexer、切分、分類、補全、copy/匯出格式、表格瀏覽 SELECT；`tsql.py` 是 T-SQL 批次內的語句切分）；API 位移一律 UTF-16，只在 `sql/offsets.py` 轉換。各方言的識別字引號與 literal 只從 `sql/dialects.py` 來，其他地方（含前端）不自己拼
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
| 量測啟動時間與記憶體 | `scripts/measure-startup.sh`（直接執行 build 出來的執行檔並給暫存的 `DBAGUY_DATA_DIR`，不碰正式的 app 資料；只停自己啟動的 PID，正在用的 DbaGuy 不受影響） |

測試 DB：PG `127.0.0.1:54317`（dbaguy/dbaguy，另有 `analytics` database）；MySQL `127.0.0.1:33067`（dbaguy/dbaguy，root 同密碼，databases `dbaguy` 與 `analytics`，seed 在 `dev/mysql/init/`）；SQL Server `127.0.0.1:14337`（sa 與 dbaguy 密碼皆 Dbaguy_dev1；dbaguy 只能開 dbaguy、analytics；amd64 映像經 Rosetta，啟動約 20–40 秒；seed 在 `dev/mssql/init/`，由 `mssql-init` 執行）；SSH bastion `127.0.0.1:2222`（dev/dev，可連到 `postgres:5432`、`mysql:3306` 與 `mssql:1433`）。都只綁 `127.0.0.1`。MySQL 的 seed 只在第一次建 volume 時執行，改了 seed 要重建：`docker compose -f dev/docker-compose.yml rm -sfv mysql && docker compose -f dev/docker-compose.yml up -d --build --wait`（SQL Server 同理：`rm -sfv mssql mssql-init`）。

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
- SQL Server（pymssql 的低階 `_mssql` API，只在 `adapters/mssql/driver.py` import；`min_error_severity = 11`）：
  - 批次原樣送出（沒有 driver 參數，`%` 不會被替換）。`GO [n]` 由 splitter 切成批次，`repeat` 由前端照次數送出（上限 1,000；`GO 0` 不送也不確認），`;` 不切分；分類用 `sql/tsql.py` 逐句判斷再加字詞下限（fail closed，EXEC／未知過程名是 UNKNOWN）。
  - 取消由 `adapters/mssql/cancel.py` 決定，只有 worker 會進 FreeTDS，沒有任何跨執行緒的 `dbcancel`（R-03b-F6：它曾讓 worker 永遠卡在 condition variable，也會在 pymssql `get_row` 轉換已到齊的大值時處理後面已緩衝的 token，釋放／改寫 worker 正在讀的 buffer，造成值損毀、SystemError，離 native crash 只差 allocator 的運氣）。Cancel 到得了的地方只有：等 server 第一個回應時（WAITFOR、慢查詢、鎖等待）由 process 全域的 wait callback（從不在裡面 cancel）每 50 ms 輪詢並 raise，worker 在自己的執行緒送 cancel；PRINT／RAISERROR 的 message handler 裡由 worker 自己 cancel；讀列、結果集之間與暫停頁只設旗標，在 worker 的下一步前停；`run()` 開始後、批次送出前的取消會保留，批次不送出。
  - 升級（`cancel._escalate`，唯一的「先警告再強制」規則）：旗標 1 秒內到不了 worker（讀列中途被鎖擋住、等下一個結果集、NOCOUNT OFF 迴圈只送 row counts），或再按 Cancel 時語句卡住（送出的 cancel 已 ≥ 1 秒還沒結束，即 server 還在 undo；或旗標已 ≥ 1 秒、一個大值還在傳）。一個大值的 byte 進度算進度（worker 讀完那一列就看到旗標），byte 計數（macOS `TCP_CONNECTION_INFO`）只用來判斷進度與選說明文字。不可能有交易時（auto-commit、`@@TRANCOUNT` 為 0、批次沒有交易字詞）shutdown socket（從不 `close()`），回 `connection_lost` 與誠實的說明；可能有交易時不斷線，先顯示一次即時警告（依情況：在結果裡等待（例如鎖）／row counts／資料還在傳／server 還在 undo），**下一次 Cancel 或關 tab** 才強制斷線。連點不會強制：距 cancel 送出或旗標不到 1 秒、距警告不到 0.5 秒的 Cancel 都只當一般 cancel。匯出對話框的 Cancel 按下後 1 秒（或一收到警告）就能再按，並顯示警告。
  - Driver 防護（`driver.GuardedConnection`，`connect()` 只交出它）：每次進 pymssql 都計數；別的執行緒的呼叫在另一條執行緒還在 driver 裡時直接拒絕，cancel 也一樣，沒有例外；`close()` 等計數歸零（最多 3 秒，先 shutdown socket 讓讀取結束），等不到就 ERROR log 並留著不 free。`dbclose` 在別的執行緒還在 FreeTDS 裡時執行會讓整個 sidecar native crash。
  - 手動 commit 是 `SET IMPLICIT_TRANSACTIONS ON`，交易狀態以 `@@TRANCOUNT, XACT_STATE(), @@OPTIONS & 2, DB_NAME()` 快取，只在沒有未讀結果時更新，Commit 為 `WHILE @@TRANCOUNT > 0 COMMIT`。
  - 第一個結果集的欄位型別來自 `sys.dm_exec_describe_first_result_set`（不用 sp_describe：XACT_ABORT ON 時會 rollback 使用者交易）；FOR JSON／FOR XML 的分段結果合成一個值，`real` 以 float32 位數顯示；`SET NOEXEC`／`PARSEONLY`／`FMTONLY`／`SHOWPLAN_*` 之後 session 仍可用。
  - `PRINT`／`RAISERROR … WITH NOWAIT` 經 message listener 即時送出。
  - TLS 的 CA 與主機名驗證寫進 app 自己的 `freetds.conf`（data dir 的 `freetds/`；第一次連 SQL Server 時才建立並設定 `FREETDSCONF`，建 container 不寫檔）；送回 SQL Server 的日期時間常值用帶 `T` 的 ISO 8601（dmy 語系也不會誤讀）；物件樹與補全含 synonym（補全裡沒有欄位）。
  - Metadata 連線（物件樹、detail、補全 snapshot）設 `READ UNCOMMITTED`、`LOCK_TIMEOUT 5000`、`XACT_ABORT ON`：別的 session 未 commit 的 DDL（例如手動 commit tab 裡的 ALTER）不會卡住樹與補全；snapshot 的名稱用 catalog view join，不用會鎖物件的 `COL_NAME`／`OBJECT_NAME`；還是要鎖的（`OBJECT_ID`、`OBJECT_DEFINITION`）5 秒後回「被另一個 session 未 commit 的變更鎖住」，不重連。
  - 通用（所有 engine）：含 U+0000 的文字在安全檢查前就被拒絕（driver 會在 NUL 截斷）；DbaGuy 自己組的字串常值不含原始 NUL（T-SQL 接 `NCHAR(0)`、MySQL 用 `\0`、SQLite 接 `char(0)`；PG 的文字本來就不能有 NUL）。
  - dev 的 `mssql-init` 種完資料後保持閒置，讓重複的 `up --wait` 成功。
- 密碼只在 Keychain；`save_password=false` 的連線，在連線設定或 unlock 對話框輸入的密碼只暫存在記憶體到 app 結束。缺少的密碼（含 SSH 密碼驗證的 SSH 密碼）回 `password_required`，`detail` 指出缺哪一個。
  - Keychain service 由 data dir 決定（`container.keychain_service`）：預設的 `~/Library/Application Support/DbaGuy` 用 `app.dbaguy`；`DBAGUY_DATA_DIR` 指到別處（`scripts/dev.sh` 的 `dev/data`、測試、臨時 sidecar、`measure-startup.sh`）一律用 `app.dbaguy.dev`，開發時存的密碼不會寫進或讀到正式 app 的項目。這個改動之前 dev 存在 `app.dbaguy` 的密碼要重新輸入。
- SSH tunnel：ssh 跑在一個 `/bin/sh` watchdog 底下，watchdog 的 stdin 是只有 backend 持有的 pipe，backend 一結束（含 SIGKILL）ssh 就跟著結束。Tunnel 以完整 SSH 設定與 SSH 密碼指紋為 key；連線測試一律用自己的 tunnel。
- 打包流程：`npm run build:app` = `build-backend.sh`（PyInstaller onedir → `src-tauri/resources/backend/`）→ `tauri build` → `finalize-app.sh`。
  - Tauri 複製 resources 時會把 symlink 展開，破壞 PyInstaller 的 `_internal`（重複 dylib、`Python.framework` 少了 `Resources`／`Versions/Current`），所以 `finalize-app.sh` 把 sidecar 原樣放回 `.app` 再重新 ad-hoc 簽章。
  - `backend/entry.py` 先呼叫 `multiprocessing.freeze_support()`（防護用）：multiprocessing 若要啟動輔助程序，會重新執行凍結後的執行檔，少了這行每個子程序都會再啟動一個 backend，無限遞迴。以前 loguru 的 `enqueue=True` 會觸發這件事（resource tracker），現在已不用 `enqueue`。
  - MySQL 只打包 pure driver：spec 排除 C extension（`_mysql_connector`，連同它帶的 dylib）以及用不到的 aio、Django、OpenTelemetry instrumentation、Kerberos／LDAP／OCI／OpenID／WebAuthn plugin。`collect_submodules("mysql.connector")` 保留以 importlib 載入的 locales 與 auth plugins；sqlglot 的 MySQL 方言跟 PG／SQLite 一樣按名稱列在 hidden imports。Pure 套件在 PYZ 裡，不會出現在 `_internal/`，所以打包後的檢查是讓凍結的 sidecar 實際連 dev MySQL 跑一次查詢。
  - SQL Server：pymssql 的 `_mssql`／`_pymssql`（FreeTDS 連在裡面）與它自帶的 OpenSSL（`pymssql/.dylibs/libssl.3.dylib`、`libcrypto.3.dylib`）隨 sidecar 打包；spec 把兩個 extension 與 sqlglot 的 T-SQL 方言按名稱列在 hidden imports（`driver.py` 的 import 與 PyInstaller 的 hook 本來就會收，列名稱是保險）。PyInstaller 把 `@loader_path/.dylibs/…` 改寫成 `@rpath/…`（rpath `@loader_path/..`），`_internal/libssl.3.dylib`、`libcrypto.3.dylib` 是指向 pymssql 副本的 symlink，Python 的 `ssl`（MySQL TLS）也用這一份（和 Homebrew 的同為 OpenSSL 3.6.3、同一組設定）；psycopg_binary 自帶另一份。`build-backend.sh` 在 bundle 裡找不到 `_mssql*.so` 就失敗。sidecar 裡每個 `.so`／`.dylib` 都過 `codesign --verify --strict`，`finalize-app.sh` 不必另外重簽；bundle 的 libcrypto 版本和 venv Python 的 `ssl.OPENSSL_VERSION` 不同時 `build-backend.sh` 失敗。打包後的檢查：凍結的 sidecar 以 TLS 登入 SQL Server、分類 T-SQL、列物件樹、串流含即時訊息的批次，也測過取消。
- Backend 意外結束（Tauri）：前端（`src/app/BackendBanner.tsx`）立刻自動重啟，5 分鐘內最多 3 次，再多就顯示手動 Restart 的 banner；editor 內容在前端，重啟後照樣存到新 backend，開著的結果、交易與 session 都沒了（tab 提供 Reconnect）。
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
  - 關閉暫停中的結果時的 drain：KILL QUERY 送出後，讀列不設時間上限，但能讀的只剩 server 已經送出的列（本機 125 萬列約 3.7 秒，TLS 4.1 秒）；等 server 把被 KILL 的語句收尾（慢的 rollback、CONTINUE HANDLER、後面幾個 `SLEEP()`）也不設上限。10 秒上限只用在兩處，超過就放棄這條連線（顯示 Reconnect，交易也跟著沒了）：KILL 送不出去時的整個 drain；以及 KILL 之後 CALL 又送來 result set（代表 KILL 被 `SLEEP()` 吞掉）之後，再等下一個 result set 的時間合計（例如 procedure 在迴圈裡 `SLEEP()`，一直送結果）。
  - Pure driver 的串流以絕對預算驗收（10 萬列 ≤ 2.0 秒、500 列一頁 ≤ 25 ms），不跟 PG 比：本機實測 10 萬列 0.59–0.60 秒、最慢一頁 3.5 ms。上面的 drain 與大量匯出若嫌慢，之後的優化是 C extension 或 bulk read（carry）。
  - Table／view 超過 10,000 個的 server，補全用的 snapshot 只載入目前的 database（連線沒指定 database 就不載入）。這是縮小範圍，不是大小上限：目前的 database 本身再大也會整個載入。
  - Tab 不會跟著 `USE` 切換：補全的預設 schema 一直是 metadata 連線的 database（profile 的 database），`USE` 之後補全建議的未加前綴名稱可能屬於另一個 database。從 History 重開的 MySQL tab 會連到當時記錄的 database，UI 沒有 database picker 可以改；schema snapshot 每個 profile 只有一份（`SchemaCache` 對沒有 per-database 連線的 engine 忽略 database）。
  - Dev 的 `mysql` container 帶 `--log-bin-trust-function-creators=1`：8.4 預設開 binary log，沒有 SUPER 的 `dbaguy` 才能建 function 與 trigger。
- SQL Server：
  - 唯讀只在應用層（SQL Server 沒有 session 級唯讀，連線設定對話框有說明）：欄位名叫 `serializable`／`repeatableread` 的查詢在唯讀下會被擋；唯讀對 RECEIVE（Service Broker）的縮窄只在 SQL Server 2022 驗證過。
  - 一個批次只回報最後一句的影響列數；錯誤後同批次後面的結果不顯示（server 仍會執行完）。
  - `@@ROWCOUNT`／`@@ERROR` 在之後的 GO 批次讀到的是 DbaGuy 自己的狀態查詢（同一批次內是使用者的值）。
  - 取消只發生在 worker 自己的執行緒：讀列中途被鎖擋住、批次在兩個結果集之間等待（例如 SELECT 後的 `WAITFOR`、下一句被鎖擋住）、`NOCOUNT OFF` 的迴圈（只送影響列數、沒有訊息；`SET NOCOUNT ON` 後就可以取消），Cancel（含 timeout）1 秒後 auto-commit 直接斷線（`connection_lost`，訊息說明原因，已完成的語句保留），可能有交易時先警告、再按 Cancel 或關 tab 才斷線。大值的 byte 進度只有 macOS 量得到；其他平台一個超過 1 秒才傳完的值也會被當成卡住。
  - 使用者自己 `SET XACT_ABORT ON` 時，取消或丟棄暫停中的結果會讓 SQL Server rollback 開著的交易（沒有匯出取消警告）。
  - Lexer 假設 `QUOTED_IDENTIFIER ON`（pymssql 連線預設）：`SET QUOTED_IDENTIFIER OFF` 之後 `"…"` 是字串，切句與安全檢查的判斷可能和 server 不同。
  - Tab 不會跟著 `USE` 切換：database picker 與補全一直是選定的 database（狀態查詢有讀 `DB_NAME()`，但 `SessionStateEvent` 沒有 database 欄位）。
  - 物件樹與 DDL 只列 rowstore 的 clustered／nonclustered index，不列 columnstore、XML、spatial index；第二個以後的結果集，欄位標題的型別是從前 100 列猜的；procedure 裡的錯誤沒有 editor 位置（訊息帶 procedure 與行號）。
  - Metadata 讀 `READ UNCOMMITTED`：別的 session 未 commit 的 DDL 會先出現在樹與補全裡（rollback 後下次 refresh 消失）。
  - datetime2(7)／datetimeoffset(7)／time(7) 在使用者的查詢裡掉第 7 位小數（driver 只有微秒），依值篩選對這些值不會命中；sql_variant 以 bytes 顯示。
  - TLS：verify-ca／verify-full 需要 CA 檔、不能讀 macOS 鑰匙圈，verify-full 不能經 SSH tunnel；不支援 named instance、strict（TDS 8.0）加密、Windows／AD 驗證；TCP keepalive 只有 FreeTDS 預設。
  - 登入失敗的原因可能缺漏（pymssql 的錯誤狀態是 process 全域的）。
  - T-SQL 補全的範圍是整個批次（`;` 不切分）。
  - 每句多 2 次 round trip（狀態讀取＋describe），之後的效能項目。
  - 串流以絕對預算驗收（10 萬列 ≤ 2.0 秒、一頁 ≤ 25 ms）：本機實測（含 driver 防護每列的計數）10 萬列 0.27–0.32 秒、最慢一頁 ≤ 3.0 ms。
- 打包版量測（Apple Silicon、macOS 26；03b 時量，已加入 mysql-connector 與 pymssql）：冷啟動到第一個 API 請求約 1.2–1.4 秒（每次重新 build 後的第一次啟動約 4 秒）；各程序 RSS 加總約 269–297 MB（上限值，共用頁面重複計算），`footprint` 加總約 173 MB（backend 約 72 MB）。和 03a（0.9–1.1 秒、156 MB）的差距沒有在 app 層級驗證過：只有 sidecar 做過同機 A/B（03a 與 03b 都約 0.47 秒、70–72 MB），所以差距不在 backend，原因（外殼、前端或機器負載）未查。Sidecar 約 41 MB → 約 49 MB：加入 SQL Server 多約 7 MB（`_mssql`／`_pymssql` 與 OpenSSL 共 6.9 MB），加入 MySQL 只多約 0.3 MB；如果讓 PyInstaller 自動收進 mysql 的 C extension 與它帶的 dylib，會再多約 13 MB。

## 進度

| # | 子專案 | 狀態 |
|---|---|---|
| ① | 地基：Tauri + sidecar、adapter 介面、連線管理、PG + SQLite 物件瀏覽 | 完成 |
| ② | 核心查詢流程：編輯器、結果 grid、執行範圍、取消、history、安全 | 完成（followups：[02a](docs/superpowers/notes/2026-10-16-02a-query-backend-followups.md)、[02b](docs/superpowers/notes/2026-10-16-02b-grid-and-data-followups.md)、[02c](docs/superpowers/notes/2026-10-16-02c-editor-and-query-followups.md)，含打包版待驗清單與後續子專案的 carries） |
| ③ | MySQL + MSSQL adapter | 完成（followups：[03a](docs/superpowers/notes/2026-10-18-03a-dialects-and-mysql-followups.md)、[03b](docs/superpowers/notes/2026-10-18-03b-sql-server-followups.md)，含公司 SQL Server 檢查清單與打包版待驗清單） |
| ④ | Couchbase 一級支援 | 未開始 |
| ⑤ | Grid 內編輯（暫存修改、預覽 DML、一次 commit） | 未開始 |
| ⑥ | 資料工具：SQL dump／還原、跨 DB 傳輸 | 未開始 |
| ⑦ | ER 圖（唯讀） | 未開始 |
| ⑧ | MCP server（唯讀） | 未開始 |
