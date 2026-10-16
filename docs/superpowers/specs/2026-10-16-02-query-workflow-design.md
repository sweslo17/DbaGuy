# 子專案 ②：核心查詢流程 — 設計

> 日期：2026-10-16 ｜ 狀態：已核准（使用者授權 Claude 自主設計與審核）
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §3（M3–M13、S2、S3、S7、S8）、§7；子專案 ① 的 [followups](../notes/2026-10-16-01-foundation-followups.md)
> 建立在子專案 ① 之上：adapter 介面、SessionWorker、WS 執行協定、metadata service（見 [① spec](2026-10-15-01-foundation-design.md)）

## 1. 目標

把 ① 的極簡 console 換成每天能用的查詢流程，讓使用者日常查 PG 與 SQLite 不用再開別的工具。

| 編號 | 功能 | 來源 |
|---|---|---|
| E1 | CodeMirror 6 編輯器：多 tab、語法上色、括號、搜尋、多游標、註解切換、vim 模式（可選） | M4、S7 |
| E2 | 三種執行範圍：游標下語句（預設）、選取範圍、全部；執行前高亮範圍；正確切分 `$$`、`BEGIN ATOMIC`、trigger body、巢狀註解 | M5 |
| E3 | Schema-aware 補全：表、欄位、alias、schema、關鍵字、依 FK 建議 JOIN 條件；快速 describe 游標下的表 | M6、M2 |
| E4 | 精確錯誤位置：編輯器標出錯誤行列，點擊跳過去 | M10 |
| E5 | 安全：危險語句確認、唯讀連線在 DB 層與應用層都擋寫入、交易狀態與閒置交易警告／自動 rollback、手動 commit 模式 | M11 |
| E6 | 取消與每個 tab 的 timeout | M12 |
| E7 | 永久、可搜尋的 history；saved queries 存成資料夾裡的 `.sql` 檔 | M13 |
| E8 | 當機保護與 session 還原：未存檔的編輯內容即時存到本地，重開 app 還原所有 tab | M4 |
| E9 | 指令面板（⌘⇧P）與快速開啟（⌘P：tab、連線、script、資料庫物件） | M3 |
| E10 | Formatter（手動觸發、本地執行）、長查詢完成時的 macOS 通知 | S3、S8 |
| G1 | 結果 grid：虛擬化列與欄、欄寬調整、固定欄、儲存格範圍選取、鍵盤操作、值的正確呈現 | M7 |
| G2 | JSON tree 與直式 record 檢視、值檢視器（長文字／JSON／bytes） | M8 |
| G3 | 複製成 TSV/CSV/JSON/Markdown/INSERT/IN list；選取格的 count/sum/avg/min/max；完整結果串流匯出到檔案 | M9 |
| G4 | 表格資料檢視：雙擊表 → Data／Columns／DDL；不寫 SQL 的篩選（WHERE）與排序、計數 | S2 |

### 成功標準

- 在 PG 與 SQLite 上完成：開 query tab → 寫多句 SQL → ⌘↵ 只跑游標下那句 → 結果在 grid 中分頁、選取、複製成各種格式 → 匯出 50 萬列到 CSV 不卡 UI。
- 補全在 5,000 張表的 schema 上，backend 回應時間 p95 < 50 ms。
- 一個 PG 交易中執行 UPDATE 後閒置，3 分鐘出現警告；設定自動 rollback 時會在時限到達時 rollback 並通知。
- 關掉 app（或 app 當掉）再打開：所有 tab、未存檔內容、游標位置都還原。
- `UPDATE t SET x = 1`（沒有 WHERE）在 standard 安全等級下會先跳確認；唯讀連線的寫入一律被擋。
- 10 萬列 × 30 欄的結果捲動維持流暢（不卡頓）。

### 不在範圍內

Grid 內編輯（⑤）、MySQL/MSSQL/Couchbase 的切分規則與補全（③④ 時擴充 dialect）、Visual EXPLAIN、query 參數、FK 導覽、ER 圖、資料傳輸、MCP。

## 2. 關鍵決策

| 決策 | 選項 | 結論與理由 |
|---|---|---|
| SQL 文字智慧（切分、分類、補全、格式輸出）放哪裡 | (a) 前端 TS；(b) **backend Python** | **(b)**。⑥ 的 dump 還原、⑧ 的 MCP 都要用同一套切分與分類（DRY）；sqlglot 有成熟的 dialect 支援；測試用 pytest。代價是每次補全一個 localhost HTTP 往返（約 2–5 ms），可接受。 |
| Grid | (a) Glide Data Grid（canvas，2026-01 後無更新，IME／選取／無障礙差）；(b) RevoGrid（web component，樣式整合麻煩）；(c) AG Grid（剪貼簿、範圍選取是 Enterprise）；(d) **自製 DOM grid + TanStack Virtual** | **(d)**。需要的功能（虛擬化、選取、複製、釘欄、之後 ⑤ 的編輯）都在可控範圍；樣式與 app 一致；不受授權限制。 |
| 編輯器 | CodeMirror 6 vs Monaco | **CodeMirror 6**（① 研究結論：輕、每個 editor 獨立 state）。`@codemirror/lang-sql` 已封存但 npm 版可用，鎖定 6.10.0。 |
| 工作區與設定的保存 | localStorage vs **backend 本地 JSON 檔** | **本地檔案**（使用者偏好本地檔案；localStorage 在 WebView 中不可靠且無法跨 dev/打包版）。 |
| 複製與匯出格式 | 前端各自實作 vs **backend 共用 formatter** | **backend**：複製（選取的列）與匯出（完整結果）共用同一套 formatter；INSERT 的 dialect 引號規則只寫一次。 |
| 危險語句判斷 | 只在前端 vs **前端預檢 + backend 強制** | 前端呼叫 `/sql/analyze` 決定要不要跳確認；backend 執行時再檢查一次，未確認就拒絕（防止繞過）。 |

## 3. 架構

### 3.1 Backend 新模組

```
backend/dbaguy_backend/
├─ sql/                      # 純函式，不碰 DB
│  ├─ dialects.py            # Dialect（postgres、sqlite；之後 mysql、tsql、sqlpp）、識別字引號、字串常值
│  ├─ lexer.py               # 輕量 tokenizer：字串、引號識別字、註解（PG 巢狀）、dollar quote、word、標點
│  ├─ offsets.py             # Python code point ↔ UTF-16 code unit 轉換
│  ├─ splitter.py            # split(text, dialect) -> list[StatementSpan]
│  ├─ classifier.py          # classify(sql, dialect) -> Classification
│  ├─ completion.py          # complete(...) -> CompletionResult；describe_at(...)
│  ├─ select_builder.py      # 表格資料檢視的 SELECT 組裝
│  └─ formatting.py          # rows → tsv/csv/json/jsonl/markdown/insert/in_list；串流 writer
├─ services/
│  ├─ schema_cache.py        # SchemaSnapshot per (profile, database)；TTL、失效
│  ├─ safety.py              # SafetyPolicy：依 profile 的安全等級與唯讀決定要不要確認或阻擋
│  ├─ history_service.py     # history.db（SQLite FTS5 trigram）
│  ├─ workspace_service.py   # workspace.json（tab、緩衝區、游標）
│  ├─ settings_service.py    # settings.json
│  └─ scripts_service.py     # scripts 資料夾的讀寫（禁止跳出根目錄）
└─ api/
   ├─ sql.py                 # /sql/split、/sql/analyze、/sql/complete、/sql/describe、/sql/format-rows、/sql/browse-select
   ├─ history.py, workspace.py, settings.py, scripts.py
   └─ metadata.py            # 新增 /metadata/search（快速開啟用）、/metadata/schema/refresh
```

**API 中所有文字位移一律用 UTF-16 code unit**（JavaScript 字串的原生單位），在 `sql/offsets.py` 轉換；後端內部用 code point。這樣 emoji 等 BMP 以外字元也不會錯位。

### 3.2 Adapter 介面擴充

```python
class DatabaseAdapter(ABC):
    dialect: ClassVar[Dialect]                      # 新增：給切分、分類、補全、引號用

class DatabaseConnection(ABC):
    def schema_snapshot(self) -> SchemaSnapshot: ...  # 新增：目前 database 的 schema/表/欄/FK
    def set_autocommit(self, enabled: bool) -> None: ...  # 新增
    def autocommit(self) -> bool: ...               # 新增
    def commit(self) -> None: ...                   # 新增
    def rollback(self) -> None: ...                 # 新增
```

- PG：`set_autocommit` 只在 IDLE 時切換 psycopg 的 `autocommit`（交易中切換會報錯，回 `StatementError(code="transaction_open")`）。`commit()`／`rollback()` 呼叫 psycopg 的同名方法。
- SQLite：手動模式把 `isolation_level` 設成 `"DEFERRED"`（寫入前自動 BEGIN），自動模式設回 `None`。
- **唯讀在 DB 層強制**：PG 連線時若 profile `read_only`，執行 `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`；SQLite 已用 `mode=ro`。
- `SchemaSnapshot`：

```python
class SnapshotColumn(BaseModel):  name: str; type_name: str
class SnapshotForeignKey(BaseModel): columns: list[str]; ref_schema: str; ref_table: str; ref_columns: list[str]
class SnapshotRelation(BaseModel):
    name: str; kind: Literal["table", "view", "materialized_view"]
    columns: list[SnapshotColumn]; foreign_keys: list[SnapshotForeignKey] = []
class SnapshotSchema(BaseModel): name: str; system: bool; relations: list[SnapshotRelation]; functions: list[str] = []
class SchemaSnapshot(BaseModel):
    database: str | None; default_schema: str | None; search_path: list[str]
    schemas: list[SnapshotSchema]; built_at: datetime
```

PG 用少數幾個批次查詢一次取完（pg_class + pg_attribute + pg_constraint），**不要每張表各查一次**（feature-priorities §3 G-29）。系統 schema 只收 `pg_catalog` 的函式名稱，不收表。SQLite 用 `sqlite_master` + `pragma_table_xinfo` + `pragma_foreign_key_list`（table-valued function，可以一次 JOIN 完）。

### 3.3 執行協定擴充（WS）

```
client → server（新增或擴充）
  execute     + confirmed: bool = false      # 危險語句已經使用者確認
              + timeout_s: float | null      # 這次執行的 timeout（null 用 tab 設定）
              + history: bool = true         # 表格資料檢視的查詢不進 history
  commit      {request_id}
  rollback    {request_id}
  set_autocommit {request_id, enabled}
  export      {request_id, sql, format, path, options}   # 完整結果串流寫檔

server → client（新增或擴充）
  confirmation_required {request_id, reasons[], classification}   # 隨後送 finished(skipped=true)
  progress    {request_id, rows}                                    # 匯出進度（每 10,000 列）
  session_state {autocommit, in_transaction, transaction_failed, transaction_started_at?, idle_seconds?}
  finished    + timed_out: bool = false, skipped: bool = false
```

- `session_state` 在每個 `finished` 後、WS 連上時、交易狀態改變時（commit/rollback/自動 rollback）送出。
- **安全檢查在 worker 執行前做**（`SafetyPolicy`）：
  - 唯讀 profile 執行非唯讀語句 → `error(code="read_only")` + `finished`。
  - 需要確認但 `confirmed=false` → `confirmation_required` + `finished(skipped=true)`。
- **Timeout**：worker 自己計時（跨 engine 通用，DRY），到時呼叫 `connection.cancel()`，結果標成 `timed_out=true`，訊息為「Timed out after N s」。
- **DDL 成功後**通知 `SchemaCache` 讓該 (profile, database) 失效。
- **History**：每個非 `history=false` 的 execute 在 `finished` 時寫一筆。
- **閒置交易**：SessionManager 的 housekeeping 每 15 秒檢查；`in_transaction` 且閒置超過 `idle_transaction_warn_minutes` 時送一次警告 `message`；設定了 `idle_transaction_rollback_minutes` 且超時，就在 worker 執行緒上 rollback，送 `message` 和 `session_state`。
- **匯出**：在同一個 session worker 上重新執行 `sql`（必須是唯讀的回傳列語句，否則 `error(code="export_requires_select")`），不分頁地把**原始值**（未經顯示用截斷）經 formatter 寫入 `path`，每 10,000 列送 `progress`，可取消；完成後送 `finished`。

### 3.4 SQL 智慧

**切分（`split`）**
- 回傳 `StatementSpan(start, end, text)`；`start` 指向第一個非空白、非註解的字元。
- 空語句丟棄。
- `;` 是結束符。以下情況中的 `;` 不切：字串、引號識別字、註解、PG dollar quote（`$tag$…$tag$`，tag 大小寫敏感）、PG `BEGIN ATOMIC … END` 函式本體、SQLite `CREATE TRIGGER … BEGIN … END`。
- PG 的 `/* */` 可以巢狀，SQLite 不行。
- PG 的 `E'...'` 支援反斜線跳脫；SQLite 支援 `[ident]` 與 `` `ident` ``。
- 介面預留 ③ 會加入的 MySQL `DELIMITER` 與 T-SQL `GO`（dialect 規則表）。

**分類（`classify`）** 回傳 `Classification(kind, dangerous_reasons[], tables[])`：
- `kind ∈ {read, write, ddl, transaction, session, unknown}`。
- 危險原因：`update_without_where`、`delete_without_where`、`drop`、`truncate`、`alter_drop`。
- 用 sqlglot（`postgres`、`sqlite`）解析；解析失敗時用 lexer 依第一個關鍵字判斷，判斷不出來就是 `unknown`。
- 特例：`EXPLAIN ANALYZE <寫入語句>` 視為寫入；`SELECT … FOR UPDATE` 視為寫入；`SELECT` 呼叫函式仍視為 read（PG 的唯讀交易在 DB 層會擋住真正的寫入）。

**安全等級**（profile 新欄位 `safety`）：

| 等級 | 行為 |
|---|---|
| `relaxed` | 不確認 |
| `standard`（預設） | 有危險原因就確認 |
| `strict` | 任何非 read 都確認 |

`unknown` 在唯讀連線上一律擋，在 `strict` 下要確認。

**補全（`complete`）**
- 輸入：`text`、`cursor`（UTF-16）、`profile_id`、`database`。dialect 從 profile 取。
- 輸出：`items[{label, kind, detail, apply, boost}]` 與要取代的 `from`/`to`。
- 依游標所在語句的 token 判斷情境：
  - `FROM`/`JOIN`/`INTO`/`UPDATE`/`TABLE` 之後 → relation（不在 search_path 的加 schema 前綴）。
  - `x.` 之後：x 是 alias 或表 → 它的欄位；x 是 schema → 它的 relation。
  - `SELECT`/`WHERE`/`ON`/`GROUP BY`/`ORDER BY`/`HAVING`/`SET` 子句中 → 範圍內 relation 的欄位（依目前語句的 `FROM`/`JOIN`/`UPDATE`，含 alias 解析），加上關鍵字與函式。
  - `JOIN t [alias] ON` 之後 → 依 FK 建議完整的 join 條件（`o.customer_id = c.id`）。
  - 其他 → 關鍵字。
- 需要引號的識別字在 `apply` 裡加上引號。
- 用寫到一半的 SQL 也能運作（lexer-based，不依賴完整 parse）。

**快速 describe（`describe`）**：游標下的識別字解析到 relation，回傳欄位清單（含型別、PK、FK）；找不到就回傳 null。

### 3.5 Frontend 模組

```
src/
├─ features/editor/      # CodeEditor（CM6 包裝；每個 tab 一個 EditorView，切換 tab 時保留 state）
│                        # extensions：sql 語言（依 dialect）、補全來源（/sql/complete，debounce + 取消舊請求）、
│                        # 語句高亮（/sql/split 快取）、錯誤標記、describe popover、vim、keymap
├─ features/query/       # QueryTab：編輯器 + 工具列 + 結果區；執行編排（語句／選取／全部）、確認對話框、
│                        # 交易控制（autocommit 切換、Commit、Rollback、狀態徽章與計時）、timeout
├─ features/grid/        # DataGrid、ValueViewer、RecordView、選取聚合
├─ features/table-data/  # TableDataTab：Data | Columns | DDL；篩選列、排序、計數
├─ features/history/     # HistoryPanel
├─ features/scripts/     # ScriptsPanel（資料夾樹）、tab 與檔案綁定（⌘S／⌘⇧S）
├─ features/palette/     # CommandPalette（cmdk）、QuickOpen
├─ features/settings/    # SettingsDialog（⌘,）
└─ stores/workspace.ts   # 擴充：tab 種類 query／table／object；debounce 500 ms 存到 /workspace
```

① 的 `features/console` 由 `features/query` 取代後刪除；`lib/api/session-socket.ts` 與 execution reducer 延用並擴充（新事件、多個 request 的結果累積）。

**側欄**：上方是 Connections／Scripts／History 三個分頁。

## 4. 功能細節

### 4.1 執行範圍與腳本

| 快捷鍵 | 行為 |
|---|---|
| ⌘↵ | 有選取就跑選取範圍；沒有就依設定 `run.default` 跑游標下語句（預設）或全部 |
| ⌘⇧↵ | 依序跑全部語句 |

- 游標下的語句一直以淡色背景標示；按下執行時閃一下被執行的範圍。
- 執行選取範圍或全部時，先把文字經 `/sql/split` 切成語句，再逐句送 `execute`（每句有自己的 `request_id`）。預設遇到錯誤就停，設定 `run.continue_on_error` 可以改。
- 執行前一次呼叫 `/sql/analyze` 分析所有要跑的語句。需要確認的語句列在同一個確認對話框，確認後才送出，並帶 `confirmed=true`。
- **結果區**：每個有結果集的語句一個分頁（標題是語句前 30 字），加上 Messages 分頁（訊息、affected rows、每句耗時）。
  - 錯誤分頁顯示訊息與位置，點擊跳到編輯器對應位置（語句 offset + 行列）。
  - 編輯器同時用 lint diagnostic 標出錯誤位置。

### 4.2 交易與安全 UI

- Query tab 工具列有 **Auto-commit** 切換（預設依設定為開）。切成手動時送 `set_autocommit(false)`。
- 狀態徽章：
  - 交易中：橘色「Transaction · 02:13」，從交易開始計時。
  - 交易已 abort：紅色「Transaction aborted — Rollback required」。
- Commit、Rollback 按鈕在交易中才可用。
- 閒置警告（backend 送來的 message）顯示成 toast，徽章閃動。
- 關閉有交易未結束的 tab 時跳對話框：Commit／Rollback／取消。
- 有顏色的 profile 會把 tab 標籤與編輯器上緣染成該色；紅色另外在工具列顯示連線名稱的紅底標籤。

### 4.3 Grid

- **欄**：
  - 表頭顯示欄名與型別。
  - 欄寬依表頭與前 100 列內容估算，限制在 60–400 px，可以拖曳調整，雙擊自動符合內容。
  - 列號欄永遠固定，右鍵可以「固定到此欄」。
- **列**：列高 22 px；欄與列都虛擬化。
- **選取**：
  - 單格、矩形範圍（shift 或拖曳）、點表頭選整欄、點列號選整列。
  - 鍵盤：方向鍵、shift+方向、Home/End、PageUp/PageDown、⌘A、⌘C（TSV）、Enter 開值檢視器、Space 開 record view。
- **呈現**：
  - NULL 用斜體灰字。
  - 空字串顯示空白，但 tooltip 標「empty string」。
  - 數字靠右；布林顯示 `true`／`false`。
  - JSON 顯示壓縮後的預覽；bytes 顯示 `0x…` 與大小。
  - 被截斷的文字顯示「…」與大小。
- **右鍵選單**：
  - Copy、Copy as（TSV／CSV／JSON／Markdown／INSERT／IN list／欄名）
  - View value、Record view、Pin column
  - 表格資料檢視中另有「Filter by this value」
- **狀態列**：顯示選取格數；選到數字時加上 sum／avg／min／max。
- **值檢視器**（右側抽屜）：
  - 文字：等寬字、可換行，大小以字元數顯示。
  - JSON：可收合的 tree，可複製路徑。
  - Bytes：hex 預覽與大小。
- **Record view**：單列直式顯示（欄名 | 值），可前後切換列。

### 4.4 複製與匯出格式（`sql/formatting.py`）

| 格式 | 規則 |
|---|---|
| TSV（⌘C 預設） | 值中有 tab 或換行時用 CSV 式的雙引號包起來 |
| CSV | RFC 4180；可選要不要表頭；NULL 輸出為空字串（選項：`NULL`） |
| JSON | 物件陣列；cell 原樣輸出（大數字維持字串） |
| JSONL | 一列一個物件 |
| Markdown | 表格；跳脫 `\|` |
| INSERT | 依 dialect 引號；表名來自表格資料檢視，否則用 `table_name` 佔位；NULL、數字、布林（SQLite 用 1/0）、字串。被截斷的 bytes/文字輸出 NULL，並在回應中附警告 |
| IN list | 單一欄位 → `('a', 'b', 3)` |

- 複製走 `POST /sql/format-rows`：帶入選取的 cell（已編碼值）與欄位 metadata，回傳文字。
- 匯出用 WS `export` 指令，處理原始值，所以 bytes 是完整 hex、長文字不截斷。
- 檔案路徑用 Tauri dialog plugin 的儲存對話框選；瀏覽器模式改成輸入路徑。

### 4.5 表格資料檢視

- 雙擊 table/view/materialized view/partition 會開物件 tab，分頁為 **Data（預設）| Columns | DDL**。
- Data 分頁用自己的 session（第一次顯示才建立）。
- SQL 由 `POST /sql/browse-select {profile_id, path, where, order_by[]}` 產生，例如 `SELECT * FROM sales.orders WHERE <where> ORDER BY …`。
  - 用 dialect 引號，表名來自 path。
  - `where` 是使用者輸入的原始 SQL 片段：使用者自己的查詢，原樣放入。
- 產生的 SQL 顯示在可收合的列上，旁邊有「Open in query tab」。
- 篩選輸入框按 Enter 套用，保留最近 20 筆篩選紀錄。
- 點表頭排序，依序是升冪 → 降冪 → 不排序；shift 可以多欄排序。
- 分頁 500 列，可載入更多；有「Count rows」按鈕（同樣的 WHERE 加上 `count(*)`）和重新整理。

### 4.6 History

- 存在 `history.db`（app data 目錄），用 FTS5 trigram 做子字串搜尋。
- 欄位：`id`、`profile_id`、`profile_name`（快照）、`database`、`sql`、`started_at`、`elapsed_ms`、`rows`、`status`（ok／error／cancelled／timeout）、`error`（截斷到 500 字）。
- 保留最近 50,000 筆，寫入時偶爾清理。
- API：`GET /history?q=&profile_id=&limit=100&before=<id>`、`DELETE /history`（清空，要確認）。
- UI：側欄 History 分頁有搜尋框與清單（時間、連線、第一行、狀態圖示、耗時）。
  - 單擊預覽。
  - 雙擊在目前 query tab 的游標處插入；⌥雙擊開新 query tab。

### 4.7 Scripts

- 根目錄是 `settings.scripts_dir`，預設為 `<data_dir>/scripts`。可以改到使用者的 git repo。
- API：
  - `GET /scripts`（樹狀）
  - `GET /scripts/file?path=`
  - `PUT /scripts/file {path, content}`
  - `POST /scripts/rename {from, to}`
  - `DELETE /scripts/file?path=`
  - `POST /scripts/folder {path}`
- 所有路徑是相對路徑，正規化後必須仍在根目錄內，不允許 `..`、絕對路徑或跳出根目錄的 symlink。
- UI：側欄 Scripts 分頁顯示資料夾樹。
  - 雙擊開到 query tab 並綁定檔案。tab 記住上次使用的連線，第一次開時選目前選中的連線。
  - ⌘S 存回檔案；⌘⇧S 另存到 scripts 目錄。
  - 未存檔時 tab 上顯示圓點。

### 4.8 工作區保存

- `workspace.json`：`{version, tabs[{id, kind, profile_id, database, title, script_path?, object_path?, buffer?, cursor?, selection?, autocommit?}], active_tab_id, sidebar_tab}`。
- 前端在變更後 debounce 500 ms 以 `PUT /workspace` 整份寫入，backend 用 atomic write。緩衝區包含在內，所以當機也不會丟失。
- 啟動時 `GET /workspace` 還原 tab；query session 在 tab 第一次顯示時才建立。

### 4.9 設定（`settings.json`，⌘, 開對話框）

```
editor:  font_size=13, vim=false, word_wrap=false, tab_size=2, keyword_case="upper"|"preserve"
run:     default="statement"|"all", continue_on_error=false, page_size=500, statement_timeout_s=0
safety:  idle_transaction_warn_minutes=3, idle_transaction_rollback_minutes=0（0 = 關閉）
scripts_dir: <data_dir>/scripts
notifications: long_query_seconds=15
```

### 4.10 指令面板與快速開啟

- 用 `cmdk`。
- **⌘P 快速開啟**的來源：
  - 開著的 tab、連線、script（檔名）。
  - 資料庫物件：`GET /metadata/search?q=&profile_id=`，在已載入的 schema snapshot 中做模糊搜尋。
- **⌘⇧P 指令**：
  - New query、Run、Run all、Format、Toggle vim、Toggle auto-commit、Commit、Rollback
  - Refresh schema、Close tab、Settings、Open history、Open scripts

### 4.11 其他

- **Formatter**：用 `sql-formatter`（dialect 對應 postgresql、sqlite），⌘⇧F 格式化選取範圍或游標下語句，只在本地執行。
- **通知**：查詢超過 `long_query_seconds` 才完成、而且視窗不在前景時，用 `@tauri-apps/plugin-notification` 發通知，例如「Query finished in 42 s — 1,234 rows」，失敗時也通知。
- **Describe**：⌥ 點擊或 F1 → 顯示游標下表格的 popover（欄位、型別、PK/FK）。
- **Tauri plugin**：`tauri-plugin-dialog`、`tauri-plugin-notification`，並加入對應 capability。

## 5. 錯誤處理與 log

- 新的 API 沿用 ① 的錯誤格式。`/sql/*` 都是純函式，輸入錯誤回 422。
- Schema snapshot 建立失敗時，補全退化成只有關鍵字，並在 log 記 WARNING，不顯示錯誤給使用者。
- 每次 history 寫入失敗只記 WARNING，不影響執行。
- workspace／settings／scripts 的檔案寫入一律 atomic（暫存檔 + rename）。

## 6. 測試

- **pytest**：
  - `sql/` 全部：切分的各種邊界（dollar quote、BEGIN ATOMIC、trigger、巢狀註解、CJK 與 emoji 的 UTF-16 位移）、分類、補全情境、formatter、select builder。
  - schema snapshot（PG 與 SQLite 整合）。
  - 服務：history、workspace、settings、scripts 的路徑安全。
  - worker 擴充：timeout、commit/rollback/autocommit、確認流程、唯讀阻擋、history 掛鉤、匯出、閒置交易警告與自動 rollback。
  - API。
- **補全效能測試**：產生 5,000 張表的合成 snapshot，斷言 p95 < 50 ms（只在本機量測，不設嚴格門檻以免 CI 抖動）。
- **前端**：typecheck、build；用內建瀏覽器手動走主要流程。不加前端測試框架。

## 7. 風險

| 風險 | 對策 |
|---|---|
| 補全每次按鍵一個 HTTP 往返 | debounce 30 ms、取消舊請求；backend 對 snapshot 預先建索引（名稱小寫 → 物件） |
| UTF-16 與 code point 位移不一致 | 集中在 `sql/offsets.py` 轉換並專門測試 |
| Grid 在大量資料下的效能 | 欄與列都虛擬化；cell 用固定高度；選取狀態只存矩形範圍 |
| PG 手動 commit 模式下的串流 | 沿用 ① 修正 C-1 的作法：交易中的 SELECT 走 server-side cursor |
| 大型緩衝區頻繁寫檔 | debounce 500 ms，只在內容改變時寫入 |
