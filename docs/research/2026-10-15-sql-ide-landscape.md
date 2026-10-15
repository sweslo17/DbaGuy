# SQL IDE 市場調查與實作可行性評估

> 調查日期：2026-10-15
> 目標：macOS 上的個人 SQL IDE，支援 PostgreSQL / MSSQL / MySQL / Couchbase / SQLite
> 方法：4 個平行研究 agent，約 330 次搜尋與抓取（官方文件、GitHub、PyPI/npm、Hacker News、G2/Capterra、官方論壇）
> 限制：Reddit 無法直接存取，使用者評價以 HN、官方論壇、評論網站為主。標示「未驗證」的項目來自二手資料或無法打開的頁面。

難度定義：**低**＝數天；**中**＝1–3 週；**高**＝1 個月以上

> **第二輪更新（2026-10-15）**：已補查 Reddit、Stack Overflow、GitHub 投票、YouTrack、App Store、VS Code 市集、開發者調查、HN 和 Couchbase 社群，並在本機實測 MSSQL driver。**需求排名與 MoSCoW 見 [2026-10-15-feature-priorities.md](2026-10-15-feature-priorities.md)。** 本文已依第二輪結果修正 MSSQL driver、Couchbase 桌面工具、Tauri sidecar 打包方式和簽章這幾處；完整差異見該文件 §6。

---

## 0. TL;DR

### 市場缺口

1. **沒有一款 Mac 上輕量、好用的工具能把 5 種 DB 都做好。** 多 DB 工具不是 JVM（DataGrip、DBeaver），就是 Electron，「吃記憶體、慢」是最常見的抱怨。原生工具（Postico、Sequel Ace、Querious）都只支援單一 DB。
2. **Couchbase 幾乎沒有第三方 GUI 支援。** DataGrip 只能查詢、不能編輯；DBeaver 要付費版才有；Mac 原生工具全部不支援。最完整的體驗是 Couchbase 自家的 Web Workbench 和 VS Code extension；另有 Couchbase Labs 的 Capella DataStudio（非原生，2025-01 後就沒更新）。**這是最明顯的差異化切入點。**
3. **MSSQL 在 Mac 上沒有好選擇。** SSMS 沒有 Mac 版，Azure Data Studio 已在 2026-02-28 退役，只剩 VS Code MSSQL extension。
4. **AI 功能真正被認可的只有幾項：** 「依錯誤訊息修 SQL」「解釋 SQL / 解讀 EXPLAIN」「透過 MCP 讓 Claude Code / Cursor 查 DB」。text-to-SQL 和 AI 行內補全評價兩極。

### 最大技術卡點（依嚴重度）

1. ~~MSSQL 的 Python driver 在 macOS arm64 上不穩~~ → **已解決**。mssql-python 確實不穩（本機重現了 segfault），但 **pymssql 2.4.4 實測沒有卡點**：OpenSSL 內建、伺服器強制加密也能連、8 執行緒並發讀 120 萬列 0 錯誤。剩下的取消查詢要用三層策略。
2. **T-SQL 與 SQL++ 沒有現成的補全或語句切分函式庫**，要自己寫。
3. **取消查詢：五種 DB 五種做法**，Couchbase 和 MSSQL 最麻煩。
4. **在 grid 裡直接編輯並轉成 DML，正確性很難做對**：PK 偵測、identity 欄、併發修改、Couchbase key 不可改。
5. **Python sidecar 打包與簽章。** 如果只是**個人自用**，可以不做公證，但 arm64 上每個 binary 仍需有效的 ad-hoc 簽章（見 §5.5）。

### 建議架構

Tauri 2 + React + Python FastAPI sidecar，driver 抽象成 adapter 層（細節見 §5）。

---

## 1. 老牌 IDE 概覽

| 工具 | 價格（2026） | PG | MS | MY | CB | SL | 最被稱讚 | 最常被抱怨 |
|---|---|:-:|:-:|:-:|:-:|:-:|---|---|
| **DataGrip** 2026.2 | 個人約 $99/年起；非商用免費 | ✅ | ✅ | ✅ | ⚠️ 僅查詢 | ✅ | SQL 智慧最深：補全、重構、執行游標下語句 | JVM 重（預設 heap 約 750MB）、訂閱制 |
| **DBeaver** CE / 付費版 | CE 免費；付費約 $110–500/年（未驗證） | ✅ | ✅ | ✅ | 💰 付費版才有 | ✅ | 免費、driver 最廣 | UI 雜亂、大資料慢、Eclipse/Java 吃 RAM |
| **Navicat Premium 18** | 永久授權約 $1,599（未驗證）；Lite 免費 | ✅ | ✅ | ✅ | ❌ | ✅ | 資料傳輸/同步、Data Modeler；18 版加入 AI 與 data masking | 價格、授權方案混亂 |
| **TablePlus** | $99 起；續享更新 $59/台 | ✅ | ✅ | ✅ | ❌ | ✅ | 原生、快 | 試用限 2 tab、沒有 DB compare、重度工作偏弱 |
| **Beekeeper Studio** | Community 免費；$9–18/人/月 | ✅ | ✅ | ✅ | ❌ | ✅ | 開源、可先暫存修改再一次套用 | JSON viewer、匯入匯出、ER 圖都要付費 |
| **Postico 2** | $69.99 | ✅ | — | — | — | — | 原生、row 側欄、FK 選擇器 | 只支援 PG |
| **Querious 4** | 買斷 | — | — | ✅ | — | — | table designer、結構比對、server 管理面板 | 只支援 MySQL |
| **DbVisualizer** | Pro 首年 $199 | ✅ | ✅ | ✅ | ❌ | ✅ | 多 DB、Git 整合 | 免費版拿掉編輯、explain、chart |
| **SQLPro Studio** | $99.99 | ✅ | ✅ | ✅ | ❌ | ✅ | 原生、同一視窗開多個 DB | 近年評價資料很少 |
| **Sequel Ace** | 免費 | — | — | ✅ | — | — | 穩定、支援 `mysql://` URL | 只支援 MySQL |
| **DbGate** | 免費（GPL-3） | ✅ | ✅ | ✅ | ❌ | ✅ | schema compare、寬表 form view | — |
| **Antares** | 免費（MIT, Electron） | ✅ | ? | ✅ | — | ✅ | fake data、可搜尋 history、手動 commit | — |
| **SSMS 22** | 免費 | — | ✅ | — | — | — | 最完整的 T-SQL 工具 | **沒有 Mac 版** |
| **Azure Data Studio** | — | — | ✅ | — | — | — | — | **2026-02-28 已退役** |
| **VS Code MSSQL** v1.43 | 免費 | — | ✅ | — | — | — | Schema Designer、SQL notebook、plan visualizer 已 GA；Copilot 整合 | — |
| **pgAdmin 4** 9.x | 免費 | ✅ | — | — | — | — | 官方工具；9.13 加入 AI chat | Web-based，比原生工具慢 |
| **Couchbase Workbench**（Capella / Web Console / VS Code） | 免費 | — | — | — | ✅ | — | 每次查詢自動 EXPLAIN 並視覺化、ADVISE 索引建議、INFER、圖表、notebook | 只支援 Couchbase |

### Couchbase 支援細節

| 工具 | 能做什麼 | 缺口 |
|---|---|---|
| Capella Query Workbench | SQL++ 補全、Format、INFER、query context、自動 EXPLAIN 圖、ADVISE、history、圖表 | 只有雲端版 |
| Web Console Query Workbench（自架） | 語法上色、keyspace 補全、JSON/表格結果、INFER | plan 視覺化資料偏舊 |
| Couchbase VS Code extension | SQL++ notebook、表格/JSON/explain 檢視、history、匯入匯出 | Marketplace 頁面無法驗證 |
| DataGrip | JDBC 接 Query service | 不能編輯資料、不支援 Analytics |
| DBeaver | 瀏覽 bucket、index、cluster、users | 只在付費版 |
| Capella DataStudio（Couchbase Labs） | macOS 和 Windows 桌面版；多 cluster、schema 與 index 檢視、文件檢視、結果匯出、favorites、history、cbimport UI | 非原生；GitHub 上最後一版 1.3.3 是 2025-01；沒有 EXPLAIN |
| LibreDB Studio（Web, MIT） | 只用 REST API：SQL++ IDE、EXPLAIN、keyspace 瀏覽、以 INFER 產生欄位、kill request | 證明了只用 REST 也能做出可用的 client |
| 其他所有工具 | — | 都不支援 |

---

## 2. AI 時代的新興工具

| 工具 | 類型 | 值得參考的點 |
|---|---|---|
| **TablePro** | macOS 原生（Swift, AGPL） | AI chat、行內建議、Explain/Optimize、Ollama、內建 MCP server、iCloud 同步 |
| **Tabularis** | Tauri + Rust（Apache-2.0） | 內建 MCP server；**plugin 跑在獨立 process（JSON-RPC）**，driver 掛掉不會拖垮整個 app |
| **WhoDB** | 開源（Apache-2.0） | Ollama / LM Studio、`whodb mcp serve` |
| **Chat2DB** | 開源，28k stars | NL-to-SQL、支援 40+ DB；5.3 版起改成 source-available 授權 |
| **DBCode** | VS Code extension | MCP 自動註冊到 VS Code/Cursor；**連線的唯讀設定對 agent 同樣生效**；沒有 WHERE 的 UPDATE/DELETE 會警告 |
| **Beekeeper AI Shell** | 既有工具加 AI | 給 AI 工具讓它**自己探索 schema**；每次執行前要求同意，可設定唯讀查詢自動放行 |
| **DataGrip 2026.2** | 既有工具加 AI | agent skills + MCP tools，可接 Junie / Claude Agent / Codex |
| **DBeaver 26.x** | 既有工具加 AI | 可勾選要送哪些 metadata；sample rows 預設關閉；依語句類型設定「直接執行／確認／不 commit」；MCP allowlist |
| **Neon / Supabase / MotherDuck** | 雲端 DB 的 editor | Fix-with-AI、快捷鍵生成（Cmd+Shift+M）、**生成後不自動執行** |
| **Bytebase SQL Editor** | DB DevOps 平台 | 只送表名和欄位名給 LLM |
| **Paul** | macOS PG（2026 Show HN） | 預設唯讀，要明確切換才能編輯 |
| **Galaxy** | AI-first IDE | saved queries 與 GitHub 雙向同步、可直接開 PR |
| **Harlequin** | TUI | 沒有 AI，但有 headless CLI，提供 `--read-only --timeout` 給 agent 用 |
| **DBHub**（Bytebase） | MCP server | 提供 `search_objects` 讓 agent 分層探索 schema；支援唯讀、row limit、timeout |
| **Microsoft MSSQL MCP / Supabase MCP / Google MCP Toolbox** | MCP server | 都有唯讀模式（Google Toolbox 除外） |
| ~~官方 Postgres MCP~~ | 已封存 | Datadog 證實它的唯讀交易可以用多語句堆疊加上 `COMMIT` 繞過 |
| ~~Vanna~~ | 2026-03 封存 | — |
| **WrenAI** | text-to-SQL | 把 semantic layer 放進 Git |

---

## 3. 最受推崇的功能 × 實作難度

> 「參考」欄位是做得最好、值得模仿的工具。

### A. 編輯器

| # | 功能 | 參考 | 難度 | 卡點與建議做法 |
|---|---|---|---|---|
| 1 | **執行游標下的語句**（不用選取；多個語句時跳出選單讓你選） | DataGrip、Couchbase | 中 | PG 的 `$$` dollar quoting、MySQL 的 `DELIMITER`、T-SQL 的 `GO`（含 `GO n`）、字串和註解裡的 `;`。**每種 dialect 寫一個小型 tokenizer，不要用 regex。** 只在前端用 TS 實作一份（游標移動時即時標示目前語句），後端只接收切好的 batch，符合 DRY。 |
| 2 | **Schema-aware 補全**（含 alias 解析、依 FK 建議 JOIN） | DataGrip、Navicat、TablePlus | 中高 | `@codemirror/lang-sql` 只做基本 schema 補全，而且 **repo 已封存**，要鎖版本或自己 vendor 一份。alias 和 FK join 要自己做：在游標附近做 token-based scope 分析，再搭配 schema cache。**T-SQL、SQL++ 都沒有現成方案。** `sqls` 和 `joe-re/sql-language-server` 可以當行為參考。 |
| 3 | 輸入時即時標出語法錯誤 | Navicat 18、DataGrip | 中高 | 寫到一半的 SQL 會讓嚴格的 parser 直接失敗。PG 可以用 libpg-query（WASM）；其他 DB 建議只在送出前用 sqlglot 檢查。 |
| 4 | 格式化 | 全部 | 低 | `sql-formatter` 支援 tsql、n1ql、postgresql、mysql、sqlite。 |
| 5 | Snippet / saved query（資料夾管理） | Postico、Galaxy（Git 同步） | 低 | 直接存成本地 `.sql` 檔，就能順便用 git 管理。 |
| 6 | 多 tab 與 session 還原 | 全部 | 低中 | CodeMirror 每個 editor 的 state 互相獨立，比 Monaco 的全域 state 好處理。 |

### B. 結果檢視

| # | 功能 | 參考 | 難度 | 卡點與建議做法 |
|---|---|---|---|---|
| 7 | **虛擬化的大結果 grid**（10 萬到 100 萬列） | TablePlus 快；DBeaver 慢 | 中 | 用 RevoGrid（MIT，可編輯）或 TanStack Table + Virtual。**超過 2^53 的 BIGINT/DECIMAL 必須以字串傳遞**，否則會失去精度。後端做 streaming 加分頁。 |
| 8 | **多種檢視：grid / 直式 record / JSON / tree** | Querious、Navicat、Postico、Couchbase | 低中 | Couchbase 的巢狀 JSON 一定要有這個。 |
| 9 | 複製為 CSV / JSON / Markdown / INSERT | 多數 | 低（INSERT 為中） | INSERT 要處理各 dialect 的 quoting、identity 欄（`IDENTITY_INSERT`）、binary、時區。 |
| 10 | 從結果快速畫圖 | Couchbase、DbVisualizer Pro | 低中 | 用 ECharts；聚合交給 DB 做，不要在前端算。 |
| 11 | MSSQL 多結果集與 PRINT 訊息 | SSMS | 中 | 必須呼叫 `nextset()` 把所有結果集讀完，否則後續行為不可預期。 |
| 12 | 可搜尋的 query history | Antares、Couchbase | 低 | 用 SQLite FTS5 的 trigram tokenizer。 |

### C. 資料編輯

| # | 功能 | 參考 | 難度 | 卡點與建議做法 |
|---|---|---|---|---|
| 13 | **在 grid 裡編輯並轉成 DML**，修改先暫存、預覽後一次 commit | Beekeeper、VS Code MSSQL Edit Data | **高** | 要偵測 PK（四種 DB 各有查法），沒有 PK 就禁止編輯。還要處理 identity 和 generated 欄、併發修改（用舊值做樂觀檢查）。Couchbase 的 `META().id` 不能就地修改，只能「新增 + 刪除」。 |
| 14 | FK 導覽與 FK 值選擇器 | Postico、Querious | 中 | Couchbase 沒有 FK。 |
| 15 | Table designer 加即時 DDL 預覽 | Postico、VS Code MSSQL Schema Designer | 高 | 各 DB 的 ALTER 語法差異很大。 |
| 16 | 交易模式（auto / manual commit，並明顯標示尚未 commit） | Antares、Beekeeper | 低中 | — |

### D. 安全

| # | 功能 | 參考 | 難度 | 卡點與建議做法 |
|---|---|---|---|---|
| 17 | **Production 連線色彩標記與唯讀模式** | Postico、Paul、DBCode | 中 | **T-SQL 沒有唯讀交易**，只能靠唯讀登入帳號。PG 和 MySQL 有 `READ ONLY` transaction；SQLite 可用 `mode=ro`。 |
| 18 | **危險語句確認**（沒有 WHERE 的 UPDATE/DELETE、DROP、TRUNCATE） | DataGrip、DBCode | 中 | 用 sqlglot 分類語句。注意 `SELECT nextval()` 和 `EXPLAIN ANALYZE` 也會寫入。**無法解析的語句一律要求確認。** Navicat 曾有使用者回報被註解掉的區塊還是被執行、結果 drop 了一張表，所以切分器一定要正確處理註解。 |

### E. 效能分析

| # | 功能 | 參考 | 難度 | 卡點與建議做法 |
|---|---|---|---|---|
| 19 | **Visual EXPLAIN**（Couchbase 每次查詢都會自動跑） | Couchbase、VS Code MSSQL、DBeaver | PG 低中／MS 中／MY 中／CB 低中 | PG 用 **pev2**（Vue 3 元件，要用 iframe 嵌入；**不要用它的託管網站，plan 會被存下來**）。MSSQL 用 html-query-plan（已停止更新）。MySQL 和 Couchbase 要自己畫（JSON + React Flow）。**MySQL 的 EXPLAIN ANALYZE 會真的執行 DML**，要包在交易裡再 rollback。 |
| 20 | 索引建議 | Couchbase `ADVISE` | 低 | 直接執行 ADVISE，把結果畫出來。 |
| 21 | Server 監控面板（process list、kill、slow log） | Querious、pgAdmin | 中 | — |

### F. Schema 與資料工具

| # | 功能 | 參考 | 難度 | 卡點與建議做法 |
|---|---|---|---|---|
| 22 | ER 圖自動產生 | Querious、Navicat、pgAdmin | 中 | React Flow + elkjs，layout 放在 Web Worker；大型 schema 要 lazy render。 |
| 23 | Schema compare / sync | Navicat、DbGate、Querious | 高（跨 DB） | 可以直接呼叫 **Atlas** 或 **sqldef** 的 CLI（支援 PG/MySQL/MSSQL/SQLite）。migra 已 deprecated。Couchbase 只能比 collection 和 index。 |
| 24 | 匯入匯出（CSV/Excel/JSON），附預覽 | Navicat 18 | 中 | CSV 用 PapaParse streaming；GB 級資料用 DuckDB；SheetJS 的 npm 版已不再更新。 |
| 25 | Data compare | Navicat | 高 | data-diff 已封存，只能自己做。 |
| 26 | Notebook 模式（SQL 加 Markdown cell） | VS Code MSSQL、Couchbase VS Code | 中 | 沒有成熟的開源元件。 |
| 27 | Fake data / data masking | Antares、Navicat 18 | 中 | 優先度低。 |

### G. 其他

| # | 功能 | 參考 | 難度 | 卡點與建議做法 |
|---|---|---|---|---|
| 28 | 多台 Mac 同步設定 | Postico、TablePro（iCloud） | 低 | 設定放在 iCloud Drive 資料夾即可；密碼不同步，留在 Keychain。 |
| 29 | Schema 載入效能 | Beekeeper | 低 | 用固定數量的查詢一次列出所有表，不要每張表各發一次查詢。 |

---

## 4. AI 功能評估

| 功能 | 社群評價 | 難度 | 做法 |
|---|---|---|---|
| **依錯誤訊息修 SQL** | 高（HN 與廠商宣傳）；**第二輪修正：Reddit 上有經驗的使用者偏好原樣錯誤加精確位置，GitHub 幾乎沒人投票** | 低 | 只送 SQL、錯誤訊息和相關表的 schema。送出的資料少，效益立即可見。 |
| **解釋 SQL / 解讀 EXPLAIN** | 高 | 低 | **一定要附上真實的 EXPLAIN 輸出**；沒有 plan 的優化建議只是猜測。 |
| **MCP server**（讓 Claude Code / Cursor 直接用 IDE 已設定好的連線） | 高（對已經在用 agent 的開發者） | 中 | Python 的 MCP SDK 可以直接掛在 sidecar 上，**預設唯讀**。這項 CP 值很高。 |
| Text-to-SQL chat（agent 式探索 schema） | 中 | 中高 | 給模型 list / describe / search 工具，**不要把整份 DDL 塞進去**。讓使用者確認模型選了哪些表。 |
| Optimize query | 中 | 中 | 要搭配真實的 plan。 |
| AI 行內補全（ghost text） | 低中 | 高 | 延遲太高，而且會編造欄位名。確定性的 schema 補全比較可靠，**建議不做**。 |

### 讓 LLM 了解 schema 的策略（業界做法）

- **控制送出內容。** 只送名稱、型別、PK/FK，每項都可以關閉。隱藏的表直接排除。sample rows 預設關閉（DBeaver、Bytebase 都這樣做）。
- **讓 agent 自己探索，不要一次 dump。** 給模型 list / describe / search 工具（Beekeeper、DBHub、DataGrip）。大型 schema 最適合這個做法。
- **縮小範圍。** Uber 的 QueryGPT 先挑 workspace，再挑表（使用者可以修改），再修剪欄位。單一 schema 有 40–60K tokens，修剪後才不會爆 context。
- **業務詞彙與驗證過的查詢範例。** 例如 Snowflake 的 semantic model、WrenAI 的 MDL。

### 安全策略

1. **只有資料庫層級的唯讀帳號真正靠得住。** 客戶端包一層唯讀交易，會被多語句堆疊加 `COMMIT` 繞過。
2. 在 driver 層**拒絕多語句輸入**。
3. SELECT 自動執行，但設 timeout 和列數上限；DML 和 DDL 一律要求確認。
4. 查詢結果送回 LLM 前先包上警告，防止 prompt injection（Supabase 的做法）。
5. 提供本地模型（Ollama）選項，並清楚標示哪些資料會離開本機。

---

## 5. 技術選型與卡點

### 5.1 建議架構

```
┌────────────────── Tauri 2 (.app) ──────────────────┐
│  React + CodeMirror 6 + RevoGrid                    │
│     │  HTTP / WebSocket（127.0.0.1 + 每次啟動的 token）│
│  Python sidecar（FastAPI · pydantic v2 · loguru）   │
│   ├─ ConnectionManager（asyncssh tunnel、keyring）  │
│   ├─ DatabaseAdapter 介面                           │
│   │    introspect / execute / stream / cancel /     │
│   │    messages                                     │
│   │    ├─ PostgresAdapter   (psycopg 3)             │
│   │    ├─ MySQLAdapter      (mysql-connector-python)│
│   │    ├─ MSSQLAdapter      (pymssql)               │
│   │    ├─ CouchbaseAdapter  (couchbase 4.x)         │
│   │    └─ SQLiteAdapter     (sqlite3)               │
│   ├─ sqlglot（語句分類、危險語句偵測）               │
│   ├─ Local store：SQLite（history + FTS5）+ JSON 設定│
│   └─ MCP server（預設唯讀）                         │
└─────────────────────────────────────────────────────┘
```

### 5.2 外殼選擇

| 選項 | 評價 | 說明 |
|---|---|---|
| **Tauri 2 + Python sidecar** | ✅ 推薦 | 使用系統 WebView，app 體積小，原生選單、快捷鍵、檔案對話框都有。**PyInstaller onedir 要放在 `bundle.resources`，用絕對路徑啟動**；`externalBin` 只能帶單一檔案。Rust 端程式碼很少（啟動 sidecar、傳 port 和 token）。**官方沒有保證 app 結束時會關掉 sidecar**，要自己做 parent-death watchdog。 |
| pywebview + Python | 簡單的替代方案 | 全部用 Python，沒有 sidecar 生命週期問題，但原生整合較弱。 |
| Electron + Node drivers | 備案 | MSSQL 用純 JS 的 `tedious`，可以**完全避開 ODBC 和 native driver 的問題**；Beekeeper、DbGate 都是這個架構。缺點是偏離 Python stack，也用不到 sqlglot。 |

**為什麼還是用 Python？** sqlglot（解析與分類語句，T-SQL 是官方支援的 dialect）、DuckDB（匯入）、MCP SDK，而且符合你的既有 stack。換語言也沒辦法解決 MSSQL 的問題，除非選純 TDS 的 driver（tedious、tiberius，或 Python 的 pymssql）。

### 5.3 各 DB 的 driver

| DB | 建議 driver | 難度 | 取消查詢 | 卡點 |
|---|---|---|---|---|
| PostgreSQL | psycopg 3.3 | 低 | `cancel_safe()`（libpq 17 以上才是非阻塞） | 取消不保證查詢一定沒完成；schema 資訊用 pg_catalog；一條連線只能連一個 database |
| MySQL | mysql-connector-python 26.7（備案 PyMySQL） | 低中 | 另開一條連線執行 `KILL QUERY <connection_id>` | aiomysql 還是 Alpha，別用；mysqlclient 沒有 Mac wheel；database 等同 schema |
| **MSSQL** | **pymssql 2.4.4**（已實測：FreeTDS 1.5.19 與 OpenSSL 都打包在 wheel 裡，不需要 ODBC 或 Homebrew） | 中 | 三層策略：fetch 迴圈檢查旗標 → `set_wait_callback` 在同一執行緒呼叫 `cancel()` → 關閉 socket 並重連。**不能跨執行緒呼叫 cancel**；一般帳號沒有 `KILL` 權限 | `autocommit=True`（預設值會偷送 `BEGIN TRAN`）；設定 `min_error_severity = 11`；app 自己產生 `freetds.conf` 來設定 CA 和 strict；`GO` 由客戶端切分；sys.* 是每個 database 各一份，要 lazy 載入。細節見 [feature-priorities §7](2026-10-15-feature-priorities.md)。（已排除 mssql-python：本機重現 segfault #668/#679，TLS 依賴 Homebrew OpenSSL #826；pyodbc 需要 unixODBC + msodbcsql18） |
| Couchbase | couchbase 4.6 + acouchbase | 中 | 沒有用 handle 取消的 API。**做法：執行時自己指定 `client_context_id`，取消時執行 `DELETE FROM system:active_requests WHERE clientContextID = '...'`**（我的補充，待驗證） | INFER 是抽樣（預設 1000 份文件），schema 只是近似值；結果只能迭代一次；沒有 FK；階層是 bucket > scope > collection |
| SQLite | stdlib sqlite3 | 低 | `connection.interrupt()` | 系統內建的 Python 不支援 load extension，要打包有支援的 Python |

### 5.4 前端函式庫

| 用途 | 選擇 | 注意 |
|---|---|---|
| Editor | **CodeMirror 6** | 比 Monaco 輕（Sourcegraph 換掉 Monaco 後，JS 從 6MB 降到 3.4MB）；`lang-sql` 已封存，要鎖版本或 vendor |
| Grid | **RevoGrid** 或 TanStack Table + Virtual | 避開 Handsontable（商用要授權）；AG Grid 的 clipboard 和 Excel 匯出是 Enterprise 功能 |
| 格式化 | sql-formatter | 支援 tsql、n1ql |
| EXPLAIN | pev2（PG）、html-query-plan（MSSQL） | pev2 是 Vue 元件，要用 iframe 嵌入 |
| ER 圖 | React Flow + elkjs | elkjs 授權可能是 EPL-2.0，要確認 |
| 圖表 | ECharts | — |
| CSV | PapaParse | — |
| **避開** | sql-parser-cst（GPL-2）、pgsql-ast-parser（沒有授權） | — |

### 5.5 主要風險與對策

| # | 風險 | 對策 |
|---|---|---|
| 1 | ~~MSSQL 在 arm64 上並發時不穩~~ | **已解決**：改用 pymssql，本機實測並發無誤。剩下的請用公司的 SQL Server 跑一次實機檢查清單（[feature-priorities 附錄 A](2026-10-15-feature-priorities.md)） |
| 2 | ~~MSSQL 加密連線依賴 Homebrew OpenSSL~~ | **已解決**：pymssql 內建 OpenSSL；CA 和 strict 模式透過 app 自己產生的 `freetds.conf` 設定 |
| 3 | 巢狀 native code 的簽章與公證 | **自用：本機 build 的 .app 不會被加上 quarantine 屬性，Gatekeeper 不會擋，可以不做公證。** 但 arm64 上每個 Mach-O 仍需有效的 ad-hoc 簽章，而 PyInstaller 在本機 build 也可能簽章失敗。要發佈時才需要 Developer ID |
| 4 | App 結束後 sidecar 變成孤兒 process | sidecar 監聽 stdin，pipe 關閉就自動結束；再加 single-instance lock |
| 5 | Couchbase 取消查詢不乾淨 | 用 `client_context_id` 標記查詢，再用 `system:active_requests` 刪除；Stop 按鈕上線前先在真實 cluster 驗證 |

### 5.6 個人自用帶來的簡化

- 不需要做 Apple 公證，但仍需要 ad-hoc 簽章（見風險 3）。
- 只支援 arm64，最低 macOS 15。
- 可以依賴 Homebrew。
- 不用擔心授權限制，例如 AGPL 的元件可以拿來參考。

---

## 6. 建議 Roadmap

> ⚠️ 第一輪的 Phase 1–3 分法**已被 [feature-priorities §3 的 MoSCoW](2026-10-15-feature-priorities.md) 取代**。例如圖表和 schema diff 已移到 Won't，交易狀態指示升為 Must。下方只保留 Phase 0 作為參考。

### Phase 0：技術 spike（約 1 週）

- 用公司實際的 SQL Server 跑一次 pymssql 實機檢查清單（約 10 分鐘，見 [feature-priorities 附錄 A](2026-10-15-feature-priorities.md)）。
- 驗證 Couchbase 的取消流程。
- 建立 Tauri + Python sidecar 骨架：啟動、port 與 token、watchdog、PyInstaller onedir。

### Phase 1：MVP（能取代日常使用的工具）

- 連線管理：Keychain、SSH tunnel、production 色彩標記
- 5 種 DB 的 schema 樹（lazy 載入、可搜尋）
- CodeMirror 多 tab、執行游標下語句、基本 schema 補全、格式化
- 虛擬化 grid、多種檢視、各種複製格式、MSSQL 多結果集與訊息
- 取消查詢、交易模式、危險語句確認
- 可搜尋的 query history

### Phase 2：差異化

- **Couchbase 一級支援**：SQL++ 上色與補全、keyspace 樹、INFER、巢狀 JSON 檢視、EXPLAIN 視覺化、ADVISE
- **AI**：依錯誤修 SQL、Explain（附真實 plan）、agent 式 text-to-SQL、MCP server（唯讀）
- Visual EXPLAIN（先做 PG pev2，再做 MSSQL 和 MySQL）
- alias 與 FK-aware 補全

### Phase 3：進階

- Grid 內編輯、暫存修改、DML 預覽
- ER 圖、匯入匯出、結果圖表、notebook
- Schema diff（包裝 Atlas 或 sqldef）、table designer

---

## 7. 待決策問題

1. ~~公司 MSSQL 用哪種驗證方式？~~ → **已回答：SQL 帳密**。MSSQL 在公司較少用，遇到卡點可以擱置；實測沒有卡點，所以照做。
2. **外殼：** Tauri（推薦）、pywebview（較簡單），還是 Electron（MSSQL 最穩）？
3. **AI provider：** Claude API、本地 Ollama，還是兩者都要？
4. **未來是否打算給別人用？** 這會影響是否需要簽章與公證，以及第三方元件的授權選擇。

---

## 8. 主要參考來源

**市場與產品**
- Azure Data Studio 退役：https://devblogs.microsoft.com/azure-sql/azure-data-studio-retirement/
- VS Code MSSQL 2026-06 更新：https://devblogs.microsoft.com/azure-sql/vscode-mssql-june-2026/
- DBeaver 26.1（MCP）：https://dbeaver.com/dbeaver-ultimate-26-1/
- DBeaver AI 設定：https://dbeaver.com/docs/dbeaver/25.2/AI-Assistance-settings/
- DataGrip 執行語句：https://www.jetbrains.com/help/datagrip/query-execution.html
- DataGrip 的 Couchbase 限制：https://www.jetbrains.com/help/datagrip/couchbase-query.html
- Couchbase Query Workbench：https://docs.couchbase.com/cloud/clusters/query-service/query-workbench.html
- Couchbase VS Code：https://www.couchbase.com/developers/integrations/vs-code-plugin/
- Postico：https://eggerapps.at/postico/
- Querious：https://araelium.com/querious
- Beekeeper AI Shell：https://docs.beekeeperstudio.io/user_guide/sql-ai-shell/
- TablePro：https://github.com/TableProApp/TablePro
- Tabularis：https://tabularis.dev
- DBCode：https://dbcode.io
- DBHub：https://github.com/bytebase/dbhub

**AI 與安全**
- Postgres MCP SQL injection：https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server.md
- Supabase MCP 縱深防禦：https://supabase.com/blog/defense-in-depth-mcp
- Uber QueryGPT：https://www.uber.com/blog/query-gpt/
- Spider 2.0：https://www.arxiv.org/pdf/2411.07763

**技術**
- Tauri sidecar：https://v2.tauri.app/develop/sidecar/
- PyInstaller macOS 注意事項：https://pyinstaller.org/en/stable/feature-notes.html
- mssql-python：https://github.com/microsoft/mssql-python（issues #679、#668、#685、#826）
- pymssql：https://pypi.org/project/pymssql/
- psycopg 3 取消：https://www.psycopg.org/psycopg3/docs/api/connections.html
- Couchbase 終止查詢：https://docs.couchbase.com/server/7.6/n1ql-rest-admin/index.md
- Couchbase INFER：https://docs.couchbase.com/server/current/n1ql/n1ql-language-reference/infer.html
- T-SQL GO：https://learn.microsoft.com/en-us/sql/t-sql/language-elements/sql-server-utilities-statements-go
- CodeMirror lang-sql（已封存）：https://github.com/codemirror/lang-sql
- Monaco 換成 CodeMirror（Sourcegraph）：https://sourcegraph.com/blog/migrating-monaco-codemirror
- sqlglot：https://github.com/tobymao/sqlglot
- sql-formatter：https://github.com/sql-formatter-org/sql-formatter
- pev2：https://github.com/dalibo/pev2
- RevoGrid：https://github.com/revolist/revogrid
- Atlas：https://atlasgo.io/
- SQLite FTS5：https://www.sqlite.org/fts5.html
