# 子專案 ③：MySQL + MSSQL — 設計

> 日期：2026-10-17 ｜ 狀態：已核准（使用者授權 Claude 自主設計與審核）
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §3、§7（**實作 adapter 前必讀**）、附錄 A；[landscape §5](../../research/2026-10-15-sql-ide-landscape.md)
> 建立在 ①②之上：adapter 介面、SessionWorker、WS 協定、`sql/` 套件（[① spec](2026-10-15-01-foundation-design.md)、[② spec](2026-10-16-02-query-workflow-design.md)）；②的 carries 見 [02a](../notes/2026-10-16-02a-query-backend-followups.md)、[02b](../notes/2026-10-16-02b-grid-and-data-followups.md)、[02c](../notes/2026-10-16-02c-editor-and-query-followups.md)

## 1. 目標

讓 PG／SQLite 已有的每日流程（連線、物件瀏覽、DDL、表格資料、查詢、補全、交易、取消、匯出、history）在 MySQL 與 SQL Server 上同樣可用，使用者不用再為這兩種 DB 開 Navicat 或 SSMS。

| 編號 | 功能 |
|---|---|
| Y1 | MySQL 8.x adapter：連線（含 SSH tunnel、TLS）、物件瀏覽、DDL（`SHOW CREATE`）、表格資料、串流結果、取消、timeout、交易與手動 commit、唯讀 |
| Y2 | MySQL 切分：`DELIMITER`、預存程序／trigger／event 的 `BEGIN…END`、反斜線跳脫字串、`#` 與 `-- ` 註解、`/*! */` 可執行註解 |
| S1 | SQL Server 2016+ adapter（SQL 帳密驗證）：同 Y1，物件階層為 server → database → schema |
| S2 | T-SQL 切分：`GO [n]` 批次分隔；`;` 不切分批次（變數與暫存表以批次為範圍） |
| S3 | T-SQL 訊息：`PRINT`／`RAISERROR` 依序顯示，`WITH NOWAIT` 即時出現 |
| D1 | 方言一致：識別字引號、字串常值、布林與 bytes 常值、補全關鍵字、formatter 語言、CodeMirror 方言、「依此值篩選」全部依連線方言產生 |
| D2 | 安全一致：危險語句確認、唯讀擋寫入（MySQL 在 DB 層 + 應用層；SQL Server 只能應用層，UI 明講） |

### 成功標準

- MySQL 與 SQL Server 各自完成 ② 的成功標準流程：開 query tab → 多句 SQL → ⌘↵ 只跑游標下那句（T-SQL 為那個批次）→ grid 分頁、複製 INSERT（方言正確）→ 匯出 50 萬列 CSV。
- 執行中的長查詢（`SELECT SLEEP(30)`、`WAITFOR DELAY '00:00:30'`）按 Cancel 1 秒內停止，同一連線可繼續使用，外層交易不受影響。
- 串流中的 10 萬列結果中途 Cancel／關閉結果，連線可繼續使用。
- 手動 commit 模式下 UPDATE 後，交易徽章亮起；Commit／Rollback 生效；閒置警告與自動 rollback 照常運作。
- MySQL 唯讀連線：`INSERT`、`SELECT … INTO OUTFILE`、`/*!…*/` 包裝的寫入都被擋。SQL Server 唯讀連線：`INSERT`、`EXEC` 被擋，`DECLARE @x …; SELECT @x` 可執行。
- `DELIMITER $$ … CREATE PROCEDURE … END$$ DELIMITER ;` 的腳本被切成正確的語句，`DELIMITER` 行不送到 server。

### 不在範圍內

MariaDB 專屬功能（相容的部分能用就好，不另測）、MySQL 5.7 以下、Azure AD／Windows 驗證、SQL Server 的 Always Encrypted、圖形化執行計畫、預存程序除錯、Couchbase（④）、grid 內編輯（⑤）。

## 2. 關鍵決策

| 決策 | 選項 | 結論與理由 |
|---|---|---|
| MySQL 的物件階層 | (a) **SQLite 形：database 以 `schema` 節點呈現、一條連線看全部**；(b) PG 形：每個 database 一條連線 | **(a)**。MySQL 的 database 就是 schema（`information_schema.SCHEMATA`），一條連線本來就能跨庫查詢，`` `db`.`t` `` 兩段式名稱與現有 `qualified()`、snapshot、`relation_path` 完全相容。代價：query tab 沒有 database 切換器，使用者打 `USE` 或寫完整名稱。 |
| SQL Server 的物件階層 | (a) **PG 形：`per_database_connection`**；(b) 單連線 + 三段式名稱 | **(a)**。server → database → schema 與 PG 相同；樹、snapshot、表格分頁、database 切換器全部沿用。 |
| T-SQL 的「一句」 | (a) 用 `;` 切；(b) **`GO` 批次** | **(b)**。社群對 DBeaver 的主要抱怨就是 `;` 切開後 `DECLARE @x` 失效（feature-priorities M5）。⌘↵ 跑游標所在的批次；要跑一部分就選取。沒有 `GO` 的腳本整份是一個批次（與 SSMS 相同）。 |
| Timeout | (a) server 端；(b) **沿用 worker 計時 + `cancel()`** | **(b)**，② 已是跨 engine 的設計，只要每個 adapter 的 `cancel()` 可靠。 |
| MySQL 取消 | **另開一條短連線送 `KILL QUERY <connection_id>`** | 實測：`SLEEP` 1.01 秒停止（`SLEEP` 回傳 1，不是錯誤 → adapter 以「已送出 kill」判定為取消）；串流中 0.03 秒以 1317 錯誤中止；連線與交易保留。 |
| SQL Server 取消 | (a) wait callback 內同執行緒 cancel；(b) **依階段：送出後等待中 → 跨執行緒 `cancel()`；串流中 → 只設旗標，由 worker 執行緒在 fetch 之間 `cancel()`** | 實測 (a) 無效且讓連線卡死；(b) 中等待階段 1.07 秒停止、串流階段立即停止，連線、暫存表、外層交易都保留。研究文件記錄「串流中跨執行緒 cancel 會卡死」，(b) 用 adapter 內的階段鎖保證不發生。 |
| 交易狀態（結果暫停時不能再發查詢） | **adapter 快取狀態；沒有未讀結果時才用一個便宜查詢更新** | MySQL 用協定狀態旗標（`in_transaction`，不需往返）加 `SELECT @@autocommit`；SQL Server 用 `SELECT @@TRANCOUNT, XACT_STATE()`。結果暫停期間回報上次的值，結果結束後立即更新。 |
| SQL Server 手動 commit | (a) pymssql `autocommit=False`；(b) **連線一律 `autocommit=True`，手動模式送 `SET IMPLICIT_TRANSACTIONS ON`** | (a) 會偷偷送 `BEGIN TRAN`（研究 §7）。(b) 的語意與 MySQL `autocommit=0`、PG 手動模式一致：第一句資料語句自動開交易。 |
| MySQL driver 模式 | mysql-connector-python：**純 Python（`use_pure=True`）** vs C extension | 純 Python：打包小（469 KB vs 20 MB）、PyInstaller 無隱藏模組問題、行為一致。實作時量測 10 萬列串流；比 PG 慢超過 3 倍才改用 C extension。 |
| 「依此值篩選」 | (a) 前端 TS 加方言分支；(b) **移到 backend `POST /sql/value-condition`** | **(b)**。引號、跳脫、布林、`N''` 規則只在 `sql/dialects.py` 一處（DRY，② 已定「SQL 文字智慧放 backend」）。 |
| SQL Server 欄位型別名稱 | (a) driver 粗略型別碼；(b) **`sp_describe_first_result_set`，失敗時從值推斷** | pymssql 的型別碼不可靠（date 回報為 binary）。只在批次的第一個結果集查一次。 |

## 3. 架構

### 3.1 Adapter 介面調整（`adapters/base.py`）

不新增抽象方法（`FakeConnection` 與既有 adapter 不受影響），只加有預設值的成員：

- `AdapterCapabilities` 新增：
  - `cancel_aborts_transaction: bool = False`（PG 為 True；決定前端「取消匯出會中止交易」的警告是否出現）
  - `server_read_only: bool = True`（DB 層能否強制唯讀；SQL Server 為 False，連線對話框顯示說明）
- `EngineInfo` 加上 `per_database_connection`、`cancel_aborts_transaction`、`server_read_only`，前端以此取代 `dialect === "postgres"` 的判斷。
- `DatabaseConnection.set_message_listener(listener: Callable[[ServerMessage], None] | None) -> None`，預設不做事。Worker 在 `run()` 前註冊，listener 在 worker 執行緒上把訊息立即送出成 `message` 事件；`take_messages()` 照舊作為沒有 listener 時的路徑。SQL Server 實作即時訊息，其他 engine 不變。
- `relation_path` 依 `dialect` 產生的 segment 不變（MySQL 的 database 就是 `schema` 節點）。

### 3.2 方言規則（`sql/dialects.py`）

`Dialect` 新增 `mysql`、`tsql`。`LexRules` 擴充並把魔術字串改成具名欄位：

| 欄位 | PG | SQLite | MySQL | T-SQL |
|---|---|---|---|---|
| `nested_block_comments` | ✓ | — | — | ✓ |
| 行註解 | `--` | `--` | `-- `（後面要空白）、`#` | `--` |
| 字串內反斜線跳脫 | 只有 `E''` | — | ✓（`'` 與 `"`） | — |
| `"` 的角色 | 識別字 | 識別字 | 字串 | 識別字 |
| 識別字引號 | `"` | `"`、`` ` ``、`[]` | `` ` `` | `[]`、`"` |
| `$` 引號／`$N` 參數 | ✓／✓（拆成兩個欄位） | —／— | —／— | —／— |
| 可執行註解 `/*! */`、`/*+ */` | — | — | 視為**程式碼**（新 token 類型 `EXEC_COMMENT`，不是 INSIGNIFICANT） | — |
| `@var`、`#temp` 字元 | — | — | `@` | `@`、`#` |
| 客戶端指令 | — | — | `DELIMITER x` | `GO [n]` |
| body 追蹤 | `begin_atomic` | `trigger` | `compound`（CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT 內的 BEGIN…END） | `tsql_block`（BEGIN…END、BEGIN TRY/CATCH） |

- `quote_ident(dialect, name)`、`quote_literal(dialect, value)`、`bool_literal(dialect, value)`、`bytes_literal(dialect, hex)` 成為唯一的常值與識別字來源：MySQL `` `a``b` ``、`'it\\'s'`（反斜線也跳脫）、`TRUE/FALSE`、`X'0102'`；T-SQL `[a]]b]`、`N'中文'`、`1/0`、`0x0102`。`formatting._BOOL_SQL`／`_BYTES_SQL` 改為呼叫這些函式。
- 保留字表：MySQL 8.4、T-SQL 各一份（只放需要加引號的字）。

### 3.3 切分（`sql/lexer.py`、`sql/splitter.py`）

- **MySQL `DELIMITER`**：只在語句開頭的一行（`DELIMITER` + 任意非空白字串，到行尾）。這一行不屬於任何 span、不送 server；之後以新的分隔字串切分；span 的 `text` 不含分隔字串，`terminator_end` 在分隔字串之後。`DELIMITER ;` 恢復。
- **MySQL compound body**：沒有 `DELIMITER` 時，`CREATE [DEFINER=…] PROCEDURE|FUNCTION|TRIGGER|EVENT … BEGIN … END` 內的 `;` 不切分。追蹤規則：`BEGIN`、`CASE` 進入；`END CASE`、裸 `END` 離開；`END IF|LOOP|WHILE|REPEAT` 不影響深度；標籤（`lbl: BEGIN`）照常。
- **T-SQL `GO`**：獨立一行（前後可有空白，行尾可有 `--` 註解）的 `GO` 或 `GO <n>`。`GO` 行不屬於任何 span；`GO n` 的 n 記在 span 上（`repeat`），執行時重複 n 次。`;` 不切分。字串、註解、`[…]` 內的 `GO` 不算。
- `StatementSpan` 新增 `repeat: int = 1`。`/sql/split` 回傳它；前端執行時照次數送出。
- 分類器（`classify`）對 T-SQL 批次逐句解析（sqlglot 可解析多句），合併成最嚴格的結果。

### 3.4 分類與安全（`sql/classifier.py`、`services/safety.py`）

- sqlglot 方言：`MySQL`、`TSQL`（以名稱靜態 import，PyInstaller 規則同 ②）。
- 補洞（詞法規則，在 sqlglot 失敗或回傳 `Command` 時）：
  - `SELECT … INTO OUTFILE|DUMPFILE` → WRITE（寫 server 檔案）；`SELECT … INTO @var` → READ。
  - `LOAD DATA`、`LOAD XML` → WRITE；`HANDLER`、`DO` → WRITE。
  - 含 `EXEC_COMMENT` token 的語句 → UNKNOWN（唯讀時擋、strict 時確認）。
  - `DELIMITER` 不會進到分類器（切分時已移除）。
  - T-SQL：`DECLARE`（含 table variable）、`PRINT`、`SET @var = …`、`SELECT @var = …` → READ；`EXEC`／`EXECUTE`、`sp_executesql` → UNKNOWN（程序可能寫入）；`WAITFOR` → READ；`DBCC`、`BACKUP`、`RESTORE`、`BULK INSERT`、`KILL` → UNKNOWN／WRITE。
- 唯讀允許的 SESSION 語句（依方言的白名單，取代只認 PG 的 `_harmless_pg_setting`）：
  - MySQL：`USE`、`SET NAMES`、`SET CHARACTER SET`、`SET @var = …`、`SET [SESSION] sql_mode`、`SET [SESSION] time_zone`。`SET … TRANSACTION … READ WRITE` 與 `transaction_read_only` 永遠擋。
  - T-SQL：`USE`、`SET NOCOUNT|ANSI_*|ARITHABORT|QUOTED_IDENTIFIER|DATEFORMAT|LANGUAGE|LOCK_TIMEOUT|STATISTICS IO|STATISTICS TIME|SHOWPLAN_*`、`SET TRANSACTION ISOLATION LEVEL …`。
- 唯讀的 DB 層強制：
  - MySQL：連線後 `SET SESSION TRANSACTION READ ONLY`；手動模式的交易一律唯讀。改回 READ WRITE 的 SET 被分類器擋。
  - SQL Server：沒有唯讀 session（landscape §5），只靠應用層；`server_read_only = False`，連線對話框勾選唯讀時顯示「SQL Server 無法在連線層強制唯讀，DbaGuy 會擋下寫入語句；需要絕對保證請用唯讀帳號」。
- 一個 span 只送一句：MySQL 連線關閉 `CLIENT_MULTI_STATEMENTS`（實測 server 會拒絕多句），所以分類器看到的就是實際執行的全部。

### 3.5 MySQL adapter（`adapters/mysql/`）

- **連線**：`mysql.connector.connect(use_pure=True, autocommit=True, client_flags=[-MULTI_STATEMENTS], connection_timeout=10, charset="utf8mb4")`。TCP keepalive 由 socket 選項開啟。記下 `connection_id`。`options` 傳給 driver，保留鍵（user、password、host、port、database、client_flags、use_pure、allow_local_infile…）不得覆寫；`allow_local_infile` 一律關閉。
- **TLS**（`SslConfig.mode` 對應）：disable → `ssl_disabled`；prefer → 預設（嘗試 TLS、不驗證）；require → 必須 TLS、不驗證；verify-ca → `ssl_verify_cert` + CA；verify-full → 再加 `ssl_verify_identity`。經 SSH tunnel 時 verify-full 若 driver 無法對真實 host 名稱驗證，連線失敗並說明（fail closed）。
- **URL**：`mysql://`、`mysql+mysqlconnector://`、`mysql+pymysql://`、`jdbc:mysql://`；`ssl-mode`／`sslmode` 查詢參數對應 `SslConfig`。Registry 對 `jdbc:` 前綴先去掉再判斷 scheme。
- **執行**：`cursor(raw=True)`、unbuffered，一律不帶參數；結果逐頁 `fetchmany`。`CALL` 之後迴圈 `nextset()` 到結束。零列結果的欄位標題來自 `description`。
- **型別**（text-first，同 PG）：raw bytes 依欄位型別轉換 — 整數 → int、`FLOAT/DOUBLE` → float、`DECIMAL` → str、`DATE/DATETIME/TIMESTAMP/TIME/YEAR` → str（保留 `0000-00-00`）、`JSON` → str（category json）、二進位 charset（63）的 BLOB/BINARY/VARBINARY → bytes、`BIT` → int、其他 → utf-8 字串。Category 對照表在 `adapters/mysql/types.py`。
- **取消**：`cancel()` 用相同 endpoint 與帳密開短連線（`connection_timeout=3`）送 `KILL QUERY <id>`，並記下「這句已被 kill」；該句之後回傳的結果或 1317 錯誤一律轉成 `StatementCancelled`。`StatementResult.close()` 對未讀完的串流也用 `KILL QUERY` 再丟棄剩餘封包，交易不受影響。
- **交易狀態**：`in_transaction` 來自協定狀態旗標（不往返）；`autocommit` 為 adapter 記錄的值，沒有未讀結果時用 `SELECT @@autocommit` 校正（使用者自己打 `SET autocommit=…`）。`set_autocommit` 送 `SET autocommit=0|1`，交易中時回 `transaction_open`。`transaction_failed` 永遠 False。DDL 隱含 commit，狀態旗標自然反映。
- **錯誤**：`errno` + SQLSTATE + 訊息；連線類錯誤（2006、2013、2055 等）→ `connection_lost`；錯誤位置只有「at line N」時轉成 `ErrorPosition(line, 1)`。
- **物件瀏覽**：root → 資料庫（kind `schema`；`information_schema`、`mysql`、`performance_schema`、`sys` 標為系統）→ 資料夾 Tables／Views／Functions／Procedures／Triggers／Events（只列非空）→ 物件；表 → Columns／Indexes／Foreign keys／Triggers。新增 kind `event`、資料夾 `EVENTS`。
- **DDL**：`SHOW CREATE TABLE|VIEW|PROCEDURE|FUNCTION|TRIGGER|EVENT`（server 原文，`ddl_is_generated = False`）。
- **Snapshot**：一次查 `information_schema` 的 SCHEMATA、TABLES、COLUMNS、KEY_COLUMN_USAGE（PK、FK）、ROUTINES，涵蓋所有非系統資料庫；`default_schema` 與 `search_path` 為連線的目前資料庫。關聯數超過 10,000 時只載入目前資料庫並記 log。
- **Server info**：`VERSION()`、`DATABASE()`、`CURRENT_USER()`。

### 3.6 SQL Server adapter（`adapters/mssql/`）

- **連線**：import 時設 `_mssql.min_error_severity = 11`。`pymssql.connect(server, port, user, password, database, autocommit=True, login_timeout=10, appname="DbaGuy", encryption=…, tds_version="7.4")`。不使用 `timeout=`（process 共用）。記下 `@@SPID`。`per_database_connection = True`。
- **TLS**：`SslConfig.mode` 對應 pymssql `encryption`：disable → `off`、prefer → `request`、require／verify-* → `require`；CA 與主機名驗證若 pymssql 參數不足，產生 `freetds.conf`（app data 目錄，每個 profile 一個 section，`ca file`、`check certificate hostname`），在 sidecar 啟動時設 `FREETDSCONF`。實作時先確認 pymssql 2.4.4 的參數能否涵蓋，能涵蓋就不產生檔案。經 SSH tunnel 時同 MySQL 規則（fail closed）。
- **URL**：`mssql://`、`sqlserver://`、`mssql+pymssql://`、`jdbc:sqlserver://host:port;databaseName=…;encrypt=…`。
- **執行**：連線一律 `autocommit=True`；批次原樣送出（不帶參數，`%` 不跳脫）；多結果集用 `nextset()`。`GO n` 由前端送 n 次（見 3.8），維持「一個 execute 一句」的協定。
- **取消（階段鎖）**：adapter 內一把鎖保護 `phase ∈ {idle, executing, streaming}`。
  - worker 執行緒進入 `execute()`／`nextset()` 前設 `executing`，返回時（持鎖）改成 `streaming`。
  - `cancel()`：持鎖；`executing` → 跨執行緒呼叫 `cancel()`（實測 1.07 秒）；`streaming` 或 `idle` → 只設旗標。
  - `fetch()` 與 `next_result()` 進入時檢查旗標：有旗標就在 worker 執行緒上 `cancel()` 並拋 `StatementCancelled`。`close()` 未讀完時在 worker 執行緒上 `cancel()`（實測不影響交易與暫存表）。
  - 不使用 wait callback 內 cancel（實測會讓連線無法使用）。
- **交易**：手動模式 `SET IMPLICIT_TRANSACTIONS ON|OFF`。狀態快取：沒有未讀結果時查 `SELECT @@TRANCOUNT, XACT_STATE()`（一次往返），`in_transaction = trancount > 0`、`transaction_failed = xact_state == -1`；結果暫停期間回報快取。`commit`／`rollback` 送 `IF @@TRANCOUNT > 0 COMMIT|ROLLBACK`。`set_autocommit` 在交易中回 `transaction_open`。`cancel_aborts_transaction = False`（XACT_ABORT 預設 OFF）；若 `XACT_STATE() = -1`，前端沿用 PG 的「只能 ROLLBACK」狀態。
- **訊息**：`set_msghandler` 收 `PRINT`／`RAISERROR` severity < 11，透過 message listener 即時送出；severity ≥ 11 是錯誤，帶錯誤號碼與行號（`ErrorPosition(line, 1)`）。
- **型別**：pymssql 原生值：UUID → str、`datetimeoffset` → 含時區的 ISO 字串、Decimal → str、`sql_variant` 的 bytes → hex 預覽（UI 提示可 CAST）、xml → str（category text）、`bit` → bool。欄位型別名稱與 category：批次第一個結果集用 `sp_describe_first_result_set`（同一連線、執行前、失敗就略過），其餘結果集從第一頁的值推斷。
- **錯誤**：連線類錯誤（20003、20004、20006、20009、20047 等 DB-Lib 錯誤）→ `connection_lost`。
- **物件瀏覽**：root → 資料庫（`master`、`model`、`msdb`、`tempdb` 標為系統；只列有權限的）→ schema（`sys`、`INFORMATION_SCHEMA`、`guest`、`db_*` 角色 schema 標為系統）→ 資料夾 Tables／Views／Functions／Procedures／Sequences／Synonyms／Types → 物件；表 → Columns／Indexes／Foreign keys／Triggers。新增 kind `synonym`、資料夾 `SYNONYMS`。
- **DDL**：檢視、程序、函式、trigger 用 `OBJECT_DEFINITION()`（原文）；表由欄位、預設值、PK／UK／FK／CHECK、索引組成（`ddl_is_generated = True`）；sequence、synonym、user type 由 catalog 組成。
- **Snapshot**：目前資料庫的 `sys.schemas`、`sys.objects`、`sys.columns`、`sys.types`、`sys.key_constraints`、`sys.foreign_key_columns`，固定數量的查詢；`default_schema` 為 `SCHEMA_NAME()`（通常 `dbo`）。
- **Server info**：`@@VERSION` 第一行、`DB_NAME()`、`SUSER_SNAME()`。

### 3.7 其他 backend

- `select_builder`：只換引號（分頁仍由 worker 處理，不需要 `TOP`／`OFFSET FETCH`）。`_single_expression` 依新的 lexer 規則，MySQL 的反斜線字串與 `#` 註解不再讓篩選守衛誤判。
- `POST /sql/value-condition {dialect, column, value, category}` → `{condition}`，取代前端 `filterForValue`。
- `completion`：MySQL 與 T-SQL 的 `STATEMENT_KEYWORDS`／`CLAUSE_KEYWORDS`（MySQL：`SHOW`、`USE`、`REPLACE`、`ON DUPLICATE KEY UPDATE`、`LIMIT`；T-SQL：`TOP`、`OUTPUT`、`EXEC`、`CROSS APPLY`、`OUTER APPLY`、`OFFSET … FETCH`、`WITH (NOLOCK)`）；`scope` 的 alias 停止字加入 `APPLY`、表提示 `WITH (…)`。
- `formatting`：INSERT／IN list 改用 `sql/dialects.py` 的常值函式。
- `container.default_registry()` 註冊兩個 adapter。PyInstaller：`sqlglot` mysql／tsql 四個模組各自 hiddenimport；`pymssql` 用 contrib hook；mysql-connector 加 `collect_submodules("mysql.connector")`（純 Python）並排除 C extension。打包後實際連兩種 DB 驗證。

### 3.8 Frontend

- `Dialect` 型別自動擴充（`gen:api`）；`SQL_DIALECTS`（CodeMirror `MySQL`、`MSSQL`）、formatter（`mysql`、`transactsql`）、補全與 copy 的 `"postgres"` 預設值改成依連線方言。
- `ConnectionPicker`：database 切換器依 `EngineInfo.per_database_connection`（PG、SQL Server 有；MySQL、SQLite 沒有）。
- 匯出取消警告與 `TransactionBadge` 的「只能 Rollback」依 `cancel_aborts_transaction`／`transaction_failed` 顯示。
- `ConnectionDialog`：URL 範例依 engine；SSL 欄位標籤依 engine（MySQL：ssl-mode 對照；SQL Server：Encryption off/request/require + CA）；SQL Server 勾選唯讀時顯示 3.4 的說明。
- `filterForValue` 改呼叫 `/sql/value-condition`。
- 執行時 span 帶 `repeat`（T-SQL `GO n`）就送出 n 次，Messages 每次一列。
- `kinds.tsx` 補 `event`、`synonym` 圖示與 Events、Synonyms 資料夾標籤。

## 4. 開發與測試環境

- `dev/docker-compose.yml` 新增：
  - `mysql`：`mysql:8.4`，`127.0.0.1:33067:3306`，帳密 `dbaguy/dbaguy`（root 同密碼），`dev/mysql/init/*.sql` 種子。
  - `mssql`：`mcr.microsoft.com/mssql/server:2022-latest`（`platform: linux/amd64`，OrbStack 以 Rosetta 執行，實測約 14 秒就緒），`127.0.0.1:14337:1433`，SA 密碼 `Dbaguy_dev1`（僅供本機測試，與其他假帳密一樣只綁 127.0.0.1）；種子由 `mssql-init` 一次性容器用 `/opt/mssql-tools18/bin/sqlcmd` 執行 `dev/mssql/init/*.sql`（以 `GO` 分批）。
  - bastion 可經 service 名稱連到 `mysql:3306`、`mssql:1433`。
- 種子內容（兩邊對等）：兩個資料庫／schema、`big_table` 10 萬列、型別表（大整數、decimal、日期含 MySQL 零日期、JSON、blob、UUID、datetimeoffset、sql_variant）、需要引號的識別字（`Order Items`、保留字欄名）、view、帶多結果集的程序、trigger、MySQL event、SQL Server synonym 與 sequence。
- `tests/support.py`：`requires_mysql`、`requires_mssql`（import 時探測）、`make_mysql_profile`、`make_mssql_profile`。
- 測試（依「只測容易出錯的邏輯」）：
  - 單元：lexer／splitter（DELIMITER、compound body、反斜線字串、`#`、`/*!`、GO 與 `GO n`、字串內 GO）、classifier 補洞、safety 白名單、`dialects` 常值與引號、`value-condition`、formatting INSERT。
  - 整合（真實 DB）：連線與錯誤密碼、server info、物件樹與 DDL、snapshot、10 萬列串流分頁、執行中與串流中 cancel、timeout、手動模式交易狀態、唯讀、型別轉換、多結果集、SQL Server 訊息順序。
- 打包驗證：`npm run build:app` 後用打包的 sidecar 連兩種 DB 跑一句查詢。
- 公司實機才能驗的項目（附錄 A：TLS／CA、strict、取消三種情境、PRINT 順序、型別、權限）列入給使用者的最終測試清單。

## 5. 執行計畫拆分

一個 branch `feat/03-mysql-mssql`，兩份 plan 依序執行：

1. **03a：方言基礎 + MySQL** — `Dialect` 擴充、`LexRules` 重構、切分（DELIMITER、compound body）、分類與安全、常值與引號、`value-condition`、能力欄位與前端方言接線、MySQL adapter、dev 環境與測試、打包。
2. **03b：SQL Server** — T-SQL 規則（GO、批次分類、白名單）、message listener、adapter（階段鎖取消、IMPLICIT_TRANSACTIONS、狀態快取、型別描述）、物件與 DDL、dev 環境（init 容器）與測試、打包。

MSSQL 依使用者指示：遇到真正卡點（例如 Rosetta 下無法穩定執行、pymssql 打包失敗）就記錄並擱置該項，其他照做。

## 6. 錯誤處理與 log

- 每個 adapter 在 `open` 記 INFO（engine、host、database、TLS 模式，不含密碼）、cancel 記 INFO（方式與耗時）、SQL 文字只在 DEBUG。
- 取消失敗（KILL 連線開不起來、跨執行緒 cancel 例外）記 WARNING 並把錯誤帶回 `cancel` 結果；worker 照既有流程回報。
- 連線類錯誤一律轉成 `connection_lost`，前端沿用 Reconnect。

## 7. 風險

| 風險 | 處理 |
|---|---|
| SQL Server 在 Rosetta 下偶發不穩 | 測試標記 `requires_mssql`，探測失敗就略過；CI 不存在，影響僅限本機 |
| pymssql 跨執行緒 cancel 在某些時機仍卡住 | 階段鎖讓串流期間絕不跨執行緒；整合測試涵蓋 WAITFOR 與串流兩種情境；公司實機再驗 |
| MySQL 純 Python 串流太慢 | 量測 10 萬列；超過 PG 3 倍改 C extension |
| T-SQL 以批次為單位，沒有 `GO` 的大腳本 ⌘↵ 會跑整份 | 與 SSMS 相同；執行前高亮整個範圍，使用者看得到；可選取部分執行 |
| `information_schema` 很慢（大量表） | 10,000 關聯以上只載入目前資料庫 |
