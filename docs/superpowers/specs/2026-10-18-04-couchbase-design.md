# 子專案 ④：Couchbase 一級支援 — 設計

> 日期：2026-10-18 ｜ 狀態：已定案（2026-10-18 coordinator 審核通過：裁決 R-04-1…R-04-15 與 §12 的細化全部採用；D-04-11 雙擊 collection 開 query tab、D-04-16 拆成三份 plan 照本文；dev 的 Couchbase container 常駐，與 MSSQL 相同，不用 compose profile）
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §2（Q1–Q6）、§3、§4（Couchbase Must 1–8、Won't、SQL++ Should）、§7「Couchbase」、§8（自架 7.x）
> 技術依據：④ 技術調查（入庫為 `docs/research/2026-10-18-04-couchbase-survey.md`，下稱「調查」，引用寫成「調查 §x」）。標 **〔實測〕** 的是調查在本機對 `community-7.6.2`／`enterprise-7.6.12` 量到的事實；標 **〔未實測〕** 的是本設計的假設，§10 彙整並指定由哪個 plan 的第一個 task 先驗證。
> 建立在 ①②③之上：adapter 介面（`adapters/base.py`）、`SessionWorker`（`sessions/worker.py`）、WS 協定（`domain/execution.py`）、`sql/` 套件與 ③ 的 `LexRules`／方言表、匯出 writer（`sql/formatting.py`）

## 1. 目標

讓日常的 Couchbase 工作（連線、找 keyspace 與 index、寫 SQL++、看懂結果、匯出、用 ID 改文件、看 EXPLAIN）在 DbaGuy 裡一次做完，不用再開官方 Web Console、cbq 或 VS Code extension。

| 編號 | 功能 | 對應 |
|---|---|---|
| C1 | 連線：`couchbase://`、`couchbases://`、`http(s)://host:8093` 匯入；節點探索（`nodeServices`）；SSH tunnel 到單一 query node；TLS 與自訂 CA；Keychain；唯讀 profile 由 server 強制（`readonly=true`） | FP §4 Must 1、M1 |
| C2 | Keyspace 樹：bucket › scope › collection › index（狀態、產生的 DDL），數百個 collection 不漏；點節點設定 tab 的 query context | FP §4 Must 2、M2 |
| C3 | SQL++ 執行：切分、分類與安全、⌘↵／選取／全部、取消、tab timeout、query context 選擇器、有型別的具名與位置參數、formatter（含 token 守衛） | FP §4 Must 3、M5、M11、M12、S3、S6 |
| C4 | 結果：串流、依 `signature` 的欄序、`*` 展開（首頁 key 聯集＋溢出欄）、MISSING 與 NULL 分開、int64／CAS／大數字不失真、metrics 與 warnings、載入更多 | FP §4 Must 4、M7、M8 |
| C5 | 完整結果匯出：CSV（巢狀值寫成 JSON 文字）、JSON／JSON Lines（server 原文） | FP §4 Must 5、M9 |
| C6 | 用 ID 開文件：JSON 編輯器、META（cas、expiration、type）、CAS 樂觀檢查、保留 expiry、存檔前預覽 server 會存成什麼 | FP §4 Must 6 |
| C7 | 一鍵 EXPLAIN：operator tree＋反模式標記；EE：ADVISE（可複製 CREATE INDEX）、`profile=timings` 執行後計畫 | FP §4 Must 7、S5 |
| C8 | History：沿用（依連線、可搜尋、最愛），另記 query context | FP §4 Must 8、M13 |
| C9 | 手動「Sample fields」（client 端取樣，不跑 INFER）；keyspace 與欄位路徑補全，特殊名稱自動加 back-tick | FP §4 Won't（不自動 INFER）、SQL++ Should、M6 |
| C10 | 開發環境：`couchbase/server:enterprise-7.6.12` ＋一次性 seed | R-04-3 |

### 成功標準

- 匯入 `couchbases://cb1,cb2/travel-sample` 得到 TLS（verify-full）profile，Test 顯示 `Couchbase Server 7.6.12-…-enterprise`。
- 樹：`dbaguy` › `many` 列出 300 個 collection（本機 < 0.5 秒）；collection 下的 index 顯示 online／deferred 與產生的 DDL。點 `travel-sample` › `inventory` 後，目前 tab 的 context 選擇器變成 `travel-sample.inventory`。
- context `dbaguy.app` 下跑 `SELECT META(d).id, * FROM events AS d`：本機第一頁 < 100 ms，2 萬列逐頁載入；欄位依文件存放順序；文件沒有的欄位顯示灰色 `MISSING`。
- int64 邊界文件（`u_numbers`）每個值與存放原文一致（`9007199254740993`、uint64 max、20 位數、`1.10`、長 π、`1E+3`、`1e400`）。
- 245 秒的聚合查詢按 Cancel：1 秒內結束，server 端該請求為 `closed`，同 tab 立刻可再跑。串流中途關閉結果同樣立即釋放。
- 暫停的結果從第一次暫停起 45–60 秒 → 自動關閉並說明原因（Load more 不重算；7.6.12 的 server 在 77–86 秒自己會砍，見 R-04-6）；已載入的列留著，狀態列顯示 `(partial)`。04b：暫停時預讀（每個結果 8 MiB），放得下就讀到結尾、不關閉；放不下時 45 秒從預讀停下起算，讀 16 MiB 才重算（A23；socket 讀空不算，B42）。
- 唯讀 profile：`INSERT` 被 DbaGuy 擋；繞過分類器的寫入（例如 `EXECUTE` 一個寫入的 prepared statement）被 server 以 1000 拒絕。
- `BEGIN WORK` 被明確拒絕；Couchbase tab 的 toolbar 沒有 Auto-commit／Commit／Rollback／交易徽章。
- 參數：`SELECT $n + 1 AS n1`、`$n = 9007199254740993` → `9007199254740994`；有參數沒給值時不送出並指出是哪個。
- 文件：開 `u_keyorder`、改一個值、⌘S → 預覽列出「key 會排序」並要求確認；文件在別處先改過 → 衝突訊息與 Reload；有 expiry 的文件存檔後 expiry 不變。
- EXPLAIN：IndexScan3 → Fetch 的樹；`#sequentialscan` 標警告；EE 上「Get index advice」列出 CREATE INDEX 並可複製。
- 匯出 50 萬列 JSONL 與 server 送來的原文逐列 token 相同；CSV 的物件欄位是 JSON 文字。
- Q1：sidecar 分頁讀 100 萬列時 RSS 增加 < 30 MB；打包大小增加 ≈ 0。

### 不在範圍內

Capella（DNS SRV、內建 Capella CA、Data API；FP §8）、叢集管理（建立／刪除 bucket、rebalance、XDCR、eventing）、Analytics／Columnar／FTS／vector、Sync Gateway、backup／cbimport／cbexport 包裝、自動 INFER、Couchbase 的 ER 圖、圖表（以上為 FP §4 Won't）；SQL++ 交易（v1 明確拒絕，R-04-4）；KV 與 sub-document API；binary 文件的內容檢視與編輯；文件改名（insert＋remove）；grid 內編輯（⑤；Couchbase 的修改走文件編輯器）；`scan_consistency`（"read your writes"）切換、「用這個陣列欄位篩選 → ANY/UNNEST」、「複製 SQL++ 路徑」、結果自動附上 META（三個 SQL++ Should 留到之後，§11）；Couchbase Server 8.0、ephemeral bucket 的特殊行為（未測）。

## 2. 關鍵決策

R-04-x 是 coordinator 的裁決；D-04-x 是本設計的決定。全文的引用慣例：「§x」指本文，「調查 §x」指技術調查，「FP §x」指 feature-priorities。

| 決策 | 選項 | 結論與理由（含證據） |
|---|---|---|
| **R-04-1** 傳輸 | (A) Python SDK；(B) **Query Service REST + stdlib `http.client` + 增量串流解析**；(C) B＋SDK 只做文件；(D) Console 內部 REST | **(B)**。SDK 4.6.3 不適合 IDE：〔實測〕100 萬列（226 MB）第一列要 5.1 秒、RSS 1.15 GB；每列被 C++ 重新編碼（key 排序、`signature` 也排序，失去欄序；`1.10`→`1.1`）；遇到 `1e400`／400 位整數直接 abort 整個 process（違反 Q2）；signature 與 metrics 在最後一列之後才到（調查 §1.2）。REST：第一列 5–23 ms、27.2 萬列／秒、RSS 平穩約 21 MB、0 新相依（調查 §1.3）。(D) 的 docs endpoint 忽略 `cas`（調查 §6.3）。**CLAUDE.md 技術選型表的「couchbase 4.x」改成「Query Service REST（stdlib `http.client`，無 SDK）」**（§13）。 |
| **R-04-2** 文件存取 | (B) **只用 SQL++**，放在 `DocumentStore` 介面後面；(C) lazy 載入 SDK 做 get/replace | **(B)**。開檔 `SELECT META(d).cas, META(d).expiration, META(d).type, d FROM ks d USE KEYS $id` 拿到存放原文（key 順序、空白、`1e400`），不需 index（調查 §6.2）。存檔 `UPDATE … USE KEYS $id SET d = $doc WHERE META(d).cas = $cas RETURNING META(d).cas` ＋ `preserve_expiry=true`，〔實測〕過期時間保留、CAS 不符回 0 列。副作用（key 排序、空白壓縮、小數正規化、> int64 被改值）由守衛處理：**> int64 擋下**；與編輯內容 token 不同時**先預覽再確認**（細化見 §12）；binary／base64 文件唯讀。`DocumentStore`（get／replace／insert／remove／server_form）讓 (C) 之後能整個換掉（+15 MB、KV 經 SSH 到多節點不通，調查 §6.4）。04c：存檔語句另加 `AND META(d).type = "json"`（A30）；expiration 寫入後另讀（A28）；也擋超出 double 範圍的數字（C4）；`SELECT RAW $doc` 與存放只差字串跳脫寫法（A2）。 | |
| **R-04-3** 開發環境 | CE 7.6.2；**EE 7.6.12** | **EE**。CE 沒有 ADVISE、UPDATE STATISTICS、CBO 估計、`profile` 輸出，RBAC 只有 admin／ro_admin／bucket_full_access（調查 §0.7、§9.1）；公司 cluster 多半是 EE。開發／測試使用免費，repo 只引用 image tag。一次性 `couchbase-init` 依 調查 §9.2–9.3 seed，假帳密、只綁 127.0.0.1。EE 專屬功能偵測 edition，在 CE 上顯示「Community Edition 不提供」而不是失敗。 |
| **R-04-4** 交易 | v1 支援 REST 交易（`txid`＋固定 node）；**v1 不支援** | **不支援**。〔實測〕沒有 `txid` 時 `BEGIN WORK` 開了一個沒人會加入的交易，`COMMIT`／`ROLLBACK`／`SAVEPOINT` 回 17002（調查 §0.5）。TRANSACTION 類語句明確拒絕（`transactions_unsupported`）；新能力 `transactions: bool`（Couchbase 為 False）讓前端隱藏 Auto-commit／Commit／Rollback／交易徽章。之後的 v1.x 可用 `txid`＋同一 node（調查 §10 的表）。 |
| **R-04-5** 取消與 timeout | `DELETE FROM system:active_requests`；SDK cancel；**任一執行緒 shutdown socket** | **shutdown socket**：〔實測〕0 ms、server 立即標成 `closed`，任何使用者都能做；DELETE 需要 admin（CE）或 `query_system_catalog`（EE），一般使用者連自己的請求都看不到（調查 §3.7）。`status:"stopped"`（別人停掉的）視為取消；server 自己的 timeout（1080）原樣顯示。**不送 server `timeout`**：它在 client 暫停時照樣計時，恢復後以 1080 結束（調查 §3.4）；worker 既有的計時器（涵蓋 `run()`＋第一頁）負責 tab timeout。 |
| **R-04-6** 暫停的結果 | 不處理；**暫停 45 秒後自動關閉（從第一次暫停起算）** | **自動關閉**。〔實測〕暫停中的請求在 server 上仍是 `running`，佔住一個 servicer（EE 7.6.12 每 node 32 個）（調查 §3.8）。**A13 不成立**：client 停止讀取的請求，7.6.12 在 77–86 秒後自己結束（1203 `service.slow_client`，狀態 `fatal`，之後的 Load more 只拿到 buffer 裡的列再被截斷），所以 120 秒的原設計會輸給 server；改成 **45 秒**（housekeeping 每 15 秒，實際 45–60 秒關閉，離 server 最早的 77 秒至少 17 秒）。時鐘從結果的**第一次**暫停起算，Load more 不重算（R-04a-T11b）：一次 500 列的 Load more 由 17–37 MB 的 buffer 供應，不會讓 server 的停滯時鐘歸零。關閉時在 Messages 說明「為釋放 query node 已關閉；要更多列請重跑」，已載入的列保留。秒數只定義一次：`adapters/couchbase/adapter.py` 的 `PAUSED_RESULT_MAX_IDLE_S = 45.0`，經能力欄位 `paused_result_max_idle_s` 傳給 worker（§3.7.4）。04b：暫停時預讀（每個結果 8 MiB），放得下就讀到結尾、不關閉；放不下時 45 秒從預讀停下起算，讀 16 MiB 才重算（A23；socket 讀空不算，B42）。 |
| **R-04-7** `*` 的欄位 | (a) 串流中途重送欄位；(b) **signature 順序＋首頁 key 聯集，之後的 key 進溢出欄** | **(b)**，評估見下方「R-04-7 評估」。另加 D-04-1 的 alias 展開。 |
| **R-04-8** MISSING ≠ NULL | 當 NULL；**獨立的 cell 標記** | 獨立標記 `{"$type":"missing"}`，grid 顯示灰色 `MISSING`；copy 與 CSV 為空字串；從 grid 複製成 JSON／JSONL 時省略該 key；JSON／JSONL 匯出本來就是 server 原文（R-04-15）。〔實測〕列裡可以同時有缺的 key 與 `null`（調查 §3.2）。 |
| **R-04-9** Query context | 每個 profile 一個；**每個 tab 一個**，每次請求都送 | **每個 tab**：`{bucket, scope?}` 放在 tab 狀態與 workspace，toolbar 有選擇器，`execute`／`export` 每次帶上；點樹上 bucket／scope／collection 設定目前 tab 的 context（FP §4 Must 2）。Profile 的 database 欄位＝預設 context。〔實測〕`default:bucket.scope`、`bucket.scope`、`default:bucket` 與 back-tick 名稱都可用（調查 §3.4）。 |
| **R-04-10** 參數 | 只在文字裡代換；**有型別的具名／位置參數** | 參數面板，值以 **JSON literal** 輸入（型別＝JSON 型別），名稱 `$x`／`@x`（同一個參數）、位置 `$1`／`?`；放在 `execute`／`export` 指令上，backend 把 literal 原文拼進請求 body（數字不經 float）。協定與面板是通用的（之後 S6 給其他引擎用），④ 只有 Couchbase 開啟（能力 `parameters`）。〔實測〕int64 內的整數精確，小數與 > int64 在 server 變成 float64（調查 §3.4）→ 送出時對這種值加 WARNING。 |
| **R-04-11** INFER 與取樣 | 連線時自動 INFER；**INFER 只由使用者手打＋樹上手動 Sample fields（client 端取樣）** | 〔實測〕7.6.2 CE 對含 binary 文件的 collection 跑 INFER 讓 query service panic 重啟；7.6.12 EE 靜默回 0 列（調查 §5.3）。Sample fields＝`SELECT RAW d FROM ks AS d LIMIT $n`（04b 改成 `SELECT META(d).type AS t, d …`，A24、§3.12），欄位路徑在 Python 推導，不可能弄垮 server。補全的欄位路徑來自取樣結果與本 session 結果裡看過的 key。 |
| **R-04-12** Formatter | **sqlpp → sql-formatter `n1ql`**＋**每個方言都加 token 守衛** | 〔實測〕`n1ql` 處理 `ANY…SATISFIES…END`、`UNNEST`、`USE KEYS`、`$param`、back-tick、`RETURNING`，但把 `'it''s'` 拆成 `'it' 's'`（調查 §8.3）。格式化後用 DbaGuy 的 lexer 重切原文與結果，非空白 token 不同就不替換（比較規則細化見 §3.3.7、§12）。 |
| **R-04-13** EXPLAIN | **Couchbase plan tree＋反模式標記**；EE 加 ADVISE、`profile=timings` | 規則照調查 §7（PrimaryScan、`#sequentialscan`、未涵蓋的 Fetch、IntersectScan）。ADVISE 的 `index_statement` 可複製。`profile=timings` 成本低（多一個 result set，§3.10），放進 ④。 |
| **R-04-14** 連線 | — | URL 匯入（KV port 一律換成 query port）、`nodeServices` 探索（受限使用者可用、匿名 401，調查 §10）、SSH tunnel 到單一 node 的 query port（不探索）、唯讀 → 每個請求 `readonly=true` ＋ SafetyPolicy allow-list（新的 SQL++ 分類器，調查 §8.2）、自訂 CA、Keychain。node 失效 → `connection_lost` ＋ Reconnect（重新探索，試下一個 node）。 |
| **R-04-15** 匯出 | 依欄位重組；**JSON／JSONL 寫 server 原文** | JSON／JSONL 直接寫每列原文（〔實測〕`SELECT *`／passthrough 的文件是存放原文，調查 §4）；CSV 照 grid 欄位，巢狀值寫成 JSON 文字。JSONL 必須一列一行：原文含換行（存放時的空白）時，在字串外壓掉空白，token 不變（§12）。 |
| **D-04-1** `SELECT *` 的 alias 包裝 | 照 server 形狀（一欄＝整份文件）；**展開成文件欄位** | 〔實測〕`SELECT *`（單一 keyspace）每列是 `{"<alias>": {文件}}`，而且文件是存放原文、存放順序；`SELECT u.*` 反而被 server 排序 key，含 `1e400` 的文件整個變成 `{}`（調查 §3.2、§4）。所以「看文件欄位」最忠實的是展開 `SELECT *` 的 alias。條件見 §3.5.3（語句最外層 projection 有不帶 `.` 的 `*`，且首頁每列由 `*` 帶來的 key 都是同一個、值都是物件）。不符合的列整列進溢出欄，無損。 |
| **D-04-2** cell 型別 | 沿用「category 決定怎麼解讀字串」；**每個 cell 自帶型別** | 文件欄位常常這列是字串、那列是物件；`category=json` 會把字串 `"[1]"` 當成陣列匯出、`compactJson` 會吃掉字串裡的空白（`lib/cells.ts`）。改成：字串一律是 str，物件／陣列與無法精確表示的數字是 `{"$type":"json","text":原文}`，缺的 key 是 `{"$type":"missing"}`；新的 `ColumnCategory.MIXED`（「每個 cell 自帶型別，不猜」）。§3.2.4。 |
| **D-04-3** 每次請求的選項 | Couchbase 專用的執行路徑；**`run(sql, options)`** | `DatabaseConnection.run()` 加 `options: RunOptions | None`（query context、參數、`profile_plan`、`retry_safe`）。一個入口，SOLID／DRY；worker 先依能力欄位檢查，不支援的引擎永遠不會收到非空選項（§3.2.1）。 |
| **D-04-4** Plan 的呈現 | 前端猜 cell 內容；**`ResultSetEvent.presentation`** | adapter 知道這是 EXPLAIN／ADVISE／profile 的結果，標成 `presentation="plan"`；前端直接用 plan 檢視（可切回 grid）。通用欄位，之後 PG 的 pev2 也能用。 |
| **D-04-5** Query context 的型別 | 字串 `bucket.scope`；**`{bucket, scope}`** | bucket 名稱可以含 `.`（`my.bucket`），字串會有歧義。協定與 workspace 都用結構；顯示與 history 用 `qualified()` 產生的文字（需要時加 back-tick）。Profile 的 database 欄位是文字，解析規則見 §3.11.1。 |
| **D-04-6** 節點探索的時機 | 每次都探索；**只在預設 port 且沒有 SSH 時探索** | 使用者填了非預設 port（LB、proxy）或用 SSH，就表示「連這個 endpoint」。探索失敗（8091 不通、401、3 秒逾時）立即退回 seed 的 query port，記 INFO。 |
| **D-04-7** TLS | 交給 driver；**自己包 `ssl`** | 自己呼叫 `wrap_socket(server_hostname=…)`，所以經 SSH tunnel 也能做 verify-full（用 `Endpoint.tls_server_name` 檢查），不像 mysql-connector 只能 fail closed。Couchbase 的 TLS 在另一個 port（18093），`prefer` 沒有意義：當成 disable（明文 HTTP），連線對話框標明（§3.6.4）。 |
| **D-04-8** 存檔預覽怎麼算 | 本地模擬 server 的正規化；**問 server：`SELECT RAW $doc`** | 本地模擬要猜 Go 的 float 格式；`SELECT RAW $doc` 走 server 自己的值編碼，與 `SET d = $doc` 存下的應該相同 **〔未實測，A2〕**。04c 第一個 task 驗證；不同就退回本地模擬並標成「近似」。 04c T1：成立（值層級）；差別只在存放時字串裡的 `<>&` 寫成 `\u003c` 等，diff 以字串的值比較。 | |
| **D-04-9** 文件操作的連線 | session 連線；**metadata 連線，寫入不重試** | 文件 tab 不綁 query tab 的 session；metadata 連線本來就是每個 profile 一條、閒置關閉。`MetadataService.with_connection()` 斷線會重試一次，寫入要關掉（`retry=False`）；有 CAS 的 replace／remove 就算重送也只會變成衝突，但 insert 會誤報「已存在」。 |
| **D-04-10** Plan 分析放哪 | 前端 TS；**backend（`adapters/couchbase/explain.py`）** | 反模式規則是「容易出錯的邏輯」，backend 有 pytest；前端只畫樹。`POST /couchbase/plan` 收 plan 原文、回正規化的樹＋標記＋advice。 |
| **D-04-11** 雙擊 collection | 表格資料 tab；detail tab；**開新的 query tab（帶 context 與瀏覽語句）** | 表格資料 tab 的 SELECT／篩選／排序是關聯式的；Couchbase 使用者本來就是寫 SQL++ 看資料。新 tab 的 context 設成該 scope，內容 ``SELECT META(d).id, * FROM `c` AS d``（兩行），不自動執行（「別耍聰明」，M5）。不加 LIMIT：分頁＋R-04-6 已保護 query node。Detail 從右鍵選單開。04b（B18）：back-tick 只在需要時加（同 §3.3.2），例如 `FROM events AS d`。 |
| **D-04-12** 45 秒怎麼計 | 每次暫停一個 Timer；**housekeeping（每 15 秒）** | 沿用閒置交易檢查的作法與 token 防競態（`SessionStateTracker.unchanged_since` 的模式）。從第一次暫停起算、Load more 不重算（R-04a-T11b）；實際在 45–60 秒之間關閉，夠用。 |
| **D-04-13** 交易語句在哪拒絕 | SafetyPolicy；**Couchbase adapter 的 `run()`** | 這不是安全決策而是引擎能力；SafetyPolicy 的 BLOCK 只有「唯讀」語意。adapter 用 `classifier.is_transaction_statement(sql, Dialect.SQLPP)`（與分類器同一張 `_TRANSACTION_STARTS`，DRY）在送出前拒絕。 |
| **D-04-14** keep-alive 重用 | 每句新連線；**閒置 < 30 秒才重用；讀取類可重送一次** | TLS 握手每句一次在遠端 cluster 上明顯變慢。重用的連線在「還沒收到任何回應」就斷，只有語句被分類為 READ 時（worker 傳 `retry_safe`）才換新連線重送一次；其他回 `connection_lost`。〔未實測，A9〕server 的 idle 關閉時間。 |
| **D-04-15** SQL++ 字串 literal | 單引號＋跳脫；**JSON 字串（雙引號）** | SQL++ 的雙引號是字串、跳脫規則與 JSON 相同，所以 `json.dumps(value, ensure_ascii=False)` 就是正確的 literal；物件／陣列 literal 也直接是 JSON。〔實測〕`\'`、`\"`、`\n`、`\t` 與 `\u` 四位 hex 的 Unicode 跳脫可用（調查 §8.1）；其餘跳脫 **〔未實測，A4〕**。 |
| **D-04-16** 計畫拆分 | coordinator 建議兩份；**三份：04a／04b／04c** | 見 §11。 |

### R-04-7 評估（對照實際的 grid 與協定）

| | (a) 中途重送欄位 | (b) 首頁聯集＋溢出欄 |
|---|---|---|
| WS 協定 | 新事件（例如 `result_columns`）；`execution.ts` 要把已到的列補上新欄的 MISSING | 不變：`ResultSetEvent` 仍是「列之前送一次，之後不變」（`worker._pump` 的 `columns_sent`） |
| Grid | 欄位身分 `columnSignature()`（名稱＋型別）一變就：`DataGrid.tsx` 的 `useLayoutEffect(…, [columns])` 捲回頂端、`[signature]` 捲回最左；`useColumnLayout` 丟掉使用者調過的寬度與釘選；`useSelectionState(signature, …)` 清掉選取。使用者捲到第 5 頁時出現一個新 key，畫面跳回左上、寬度重置。要把這 4 處改成「只增不減」的特例，copy／aggregate 的 rect 邊界也要跟著 | 不用改 |
| 匯出 | `_TableWriter._begin()` 在第一列前寫 header、`_ObjectWriter._begin()` 先決定 key；串流匯出不能事後加欄，要兩遍掃描或暫存全部 | 與 grid 同一組欄位，照常串流 |
| 無損 | 是 | 是：首頁之後才出現的 key（以及不是物件的列）完整放在溢出欄，原文、原順序 |
| 代價 | 前端與匯出都要大改 | `*` 結果多一欄；首頁（≤ 500 列／8 MiB）讀完才送第一批列——worker 本來就每 200 列（`_CHUNK`）送一批，PG／MySQL 也要等滿一批，只有慢慢吐列的查詢看得出差別 |

→ **選 (b)**。它守住「欄位在列之前決定」這個所有下游（grid、copy、匯出、aggregate、history）都依賴的不變量，而且無損。之後若 grid 支援「只增不減的欄位」，(a) 可以再加，兩者不衝突。

## 3. 架構

### 3.1 模組總覽

```
backend/dbaguy_backend/
  domain/
    dialect.py            Dialect.SQLPP = "sqlpp"
    execution.py          QueryContext、StatementParams；ExecuteCommand／ExportCommand 新欄位；ResultSetEvent.presentation
    metadata.py           NodeKind.BUCKET／SCOPE／COLLECTION；ServerInfo.edition
    column_category.py    ColumnCategory.MIXED
    schema.py             SnapshotKeyspace、SchemaSnapshot.keyspaces；RelationKind 加 "collection"
    classification.py     DangerReason／GuardReason：SYSTEM_KEYSPACE、MERGE_DELETE
    couchbase.py          API 模型：Keyspace、DocumentView、SavePreview、DocumentChange、FieldSample、PlanView…
    workspace.py          tab 的 query_context、params；kind "document"、document_cas
    settings.py           CouchbaseSettings.sample_size
  adapters/
    base.py               RunOptions、run(sql, options)、RawJsonRows、presentation()、能力欄位、endpoint_target()
    values.py             JsonText、MISSING 與它們的 encode
    sockets.py            enable_keepalive()（從 mysql/adapter.py 搬出來共用）
    couchbase/
      settings.py         profile → 連線參數（seed、port、TLS context、options）、URL 解析、context 文字解析（純函式）
      nodes.py            nodeServices 探索與候選 node 順序
      http.py             一條 keep-alive HTTP 連線：送請求、任一執行緒 shutdown、body 組裝
      stream.py           回應的增量解析器（純函式，無 I/O）
      rows.py             signature → 欄位、`*` 展開、alias 展開、溢出欄、cell 轉換（純函式）
      jsontext.py         無損 JSON 小工具：字串外壓空白、token 比較、保留 literal 的有序解析、存檔差異
      result.py           CouchbaseResult（StatementResult＋RawJsonRows）
      adapter.py          CouchbaseAdapter、CouchbaseConnection（連線、run、cancel、metadata 入口）
      catalog.py          system catalogs → 樹、產生的 DDL、snapshot
      documents.py        DocumentStore（ABC）與 SqlppDocumentStore
      sampling.py         取樣查詢與欄位路徑推導
      explain.py          plan／profile／ADVISE 的正規化與反模式規則（純函式）
  sql/
    dialects.py           SQLPP 規則、保留字、引號與 literal
    lexer.py              新的 LexRules 欄位（具名參數、`?`、back-tick 反斜線跳脫）
    splitter.py           BodyRule.NONE；span 帶參數清單
    classifier.py         SQL++ 關鍵字規則（不走 sqlglot）、is_transaction_statement()
    sqlpp_completion.py   keyspace 路徑與欄位路徑補全（與 completion.py 共用 candidate／排序）
    format_guard.py       格式化前後 token 比較（所有方言）
    formatting.py         新 cell 種類、原文 JSON writer、SQL++ 的 IN list
  services/
    document_service.py   守衛、預覽、確認、呼叫 DocumentStore（經 metadata 連線）
    field_sample_service.py  取樣結果快取（每個 profile＋keyspace），給補全與 detail 用
  sessions/               worker：RunOptions、能力檢查、暫停閒置關閉、原文匯出、history 的 context
  api/
    couchbase.py          /couchbase/contexts、/plan、/documents/*、/fields*
    sql.py                /sql/format-guard；/sql/split 回參數；/sql/complete 收 context 與看過的欄位
```

不變的部分：`SessionWorker` 的分頁、暫停、取消流程與事件順序；`MetadataService`／`SchemaCache` 的結構（Couchbase 是 `per_database_connection=False`，每個 profile 一條 metadata 連線、一份 snapshot）；`ConnectionService` 與 SSH tunnel（只多一個 `endpoint_target()` hook）；Keychain 與 `password_required` 流程。

### 3.2 共用介面與協定

#### 3.2.1 `adapters/base.py`

- `RunOptions`（frozen dataclass，全部有預設值）：
  - `query_context: QueryContext | None`
  - `params: StatementParams | None`
  - `profile_plan: bool = False`（附上執行後計畫；不叫 `profile`，免得和連線 profile 混淆）
  - `retry_safe: bool = False`（worker 依分類結果 READ 設定；見 D-04-14）
- `DatabaseConnection.run(self, sql: str, options: RunOptions | None = None) -> StatementResult`。PG／MySQL／MSSQL／SQLite 與 `tests/fake_db.py` 只改簽章、忽略 `options`。**worker 先檢查**：`query_context` 需要能力 `query_context`、`params` 需要 `parameters`、`profile_plan` 需要 `executed_plan`，不符就回 `unsupported_option`，所以其他 adapter 永遠拿不到非空選項。（03b 正在改的 adapter 檔案在合併時一併改簽章。）
- `RawJsonRows`（ABC mixin，給 `StatementResult`）：`fetch_raw(n) -> list[str]`，每列是 server 送來的 JSON 原文。worker 匯出 JSON／JSONL 時，結果有這個 mixin 就走原文 writer（§3.7.6）。
- `StatementResult.presentation() -> Literal["grid", "plan"]`，預設 `"grid"`。
- `AdapterCapabilities` 新欄位（預設值保持現有引擎行為）：

  | 欄位 | 預設 | Couchbase | 用途 |
  |---|---|---|---|
  | `transactions` | True | False | 前端隱藏交易控制；worker 不套用 tab 的 autocommit |
  | `query_context` | False | True | toolbar 的 context 選擇器、樹點擊設定 context、worker 接受 `query_context` |
  | `parameters` | False | True | 參數面板、worker 接受 `params` |
  | `documents` | False | True | 「Open document」等動作、document tab |
  | `executed_plan` | False | True | 「Run with profile」（EE 才可用，見 §3.10） |
  | `plan_format` | None | `"couchbase"` | Explain 按鈕、plan 檢視 |
  | `paused_result_max_idle_s` | None | `PAUSED_RESULT_MAX_IDLE_S` | worker 的暫停閒置關閉（不進 EngineInfo） |
| `read_ahead_bytes` | None | `READ_AHEAD_BYTES`（8 MiB） | worker 的暫停預讀（`StatementResult.read_ahead(budget)`，不可阻塞；不進 EngineInfo；04b） |

- `EngineInfo` 加上前六個欄位（`info()` 照抄）。
- `WorkerHooks` 新增 `capabilities: AdapterCapabilities`（`SessionHooksFactory` 從 registry 取），worker 的選項檢查、`retry_safe`、暫停閒置關閉都看它。
- `DatabaseAdapter.endpoint_target(profile) -> tuple[str, int]`：`ConnectionService._endpoint()` 用它取代 `profile.host`／`profile.port or default_port`。預設行為不變；Couchbase 回「第一個 seed host、依 TLS 決定的 query port（8093／18093）」，SSH 開啟而 host 是清單時丟 `ValidationFailed`（tunnel 只到一個 node）。
- `DatabaseAdapter.relation_path()`：Couchbase 覆寫成 `[bucket:b, scope:s, collection:c]`。

#### 3.2.2 `domain/execution.py`

```python
class QueryContext(BaseModel):          # extra="forbid"
    bucket: str = Field(min_length=1, max_length=100)
    scope: str | None = Field(default=None, min_length=1, max_length=251)

class StatementParams(BaseModel):       # extra="forbid"
    named: dict[str, str] = {}          # 名稱（不含 $／@）→ JSON literal 原文；最多 200 個
    positional: list[str] = []          # $1、$2…／? 依序；最多 200 個
```

- 名稱需符合 `[A-Za-z_][A-Za-z0-9_]*`；每個 literal 最多 1 MiB，且必須是合法 JSON（`json.loads(parse_int=str, parse_float=str, parse_constant=拒絕)`：只檢查形狀，數值範圍交給 server；`Decimal` 遇到 `1e99999…` 會丟 `InvalidOperation`，04a Task 6 review 修正），否則 worker 回 `invalid_params`（訊息指出哪個參數，不含值）。
- `ExecuteCommand` 新增 `query_context: QueryContext | None = None`、`params: StatementParams | None = None`、`profile_plan: bool = False`。
- `ExportCommand` 新增 `query_context`、`params`（匯出重跑同一句，必須同 context、同參數）。
- `ResultSetEvent` 新增 `presentation: Literal["grid", "plan"] = "grid"`。
- 沒有新的 server 事件：metrics 與 warnings 走 `message`，profile 走額外的 result set（§3.10），欄位不會中途改變（R-04-7）。

#### 3.2.3 其他 domain

- `NodeKind`：`BUCKET`、`SCOPE`、`COLLECTION`（`INDEX` 沿用）。Couchbase 的路徑：`[bucket:b, scope:s, collection:c, index:i]`。
- `ServerInfo.edition: Literal["enterprise", "community"] | None = None`（其他引擎為 None）。
- `SchemaSnapshot.keyspaces: list[SnapshotKeyspace] = []`；`SnapshotKeyspace(bucket, scope, collection, system: bool)`。關聯式引擎留空。`RelationKind` 加 `"collection"`（給 ⌘P 的 `ObjectSearchHit`）。
- `DangerReason`／`GuardReason` 加 `SYSTEM_KEYSPACE`（寫入 `system:` keyspace：殺請求、刪 prepared statement）與 `MERGE_DELETE`（`MERGE … WHEN MATCHED THEN DELETE`）；前端 `reasons.ts` 加兩句說明。
- `WorkspaceTab`：`kind` 加 `"document"`；新欄位 `query_context: QueryContext | None`、`params: dict[str, str] | None`（參數面板的草稿，鍵為 `$x`／`$1`，值是使用者打的文字，可以是不合法的 JSON）、`document_cas: str | None`（document tab 編輯所根據的 CAS）。Document tab 用既有的 `object_path`（最後一段 `{kind:"document", name:id}`）與 `buffer`（未存的內容）。舊的 `workspace.json` 照樣有效。04b（B14）：`query_context` 是 `QueryContext | "none" | None`——None＝profile 的預設，`"none"`＝No context（一個 `null` 分不出兩者）。
- `AppSettings.couchbase: CouchbaseSettings`（`sample_size: int = 1000`，10–10,000）。
- History：不改 schema。Couchbase 的紀錄把該次請求的 query context 文字寫進既有的 `database` 欄位（`ExecutionRecord` 多帶 context；hooks 對 `query_context` 能力的引擎用它，否則用 session 的 database）。從 History 重開時解析回 `QueryContext`。

#### 3.2.4 Cell 編碼約定（`adapters/values.py`、`lib/cells.ts`、`sql/formatting.py` 三處同步）

| server 的值 | adapter 回傳的 raw 值 | WS cell（`encode_value`） | 前端 kind |
|---|---|---|---|
| 字串 | `str` | 字串（> 64 KiB 用既有的截斷 text cell） | text／empty |
| `true`／`false`／`null` | `bool`／`None` | 原樣 | bool／null |
| 整數，±(2⁵³−1) 內 | `int` | number | number |
| 其他整數、`repr(float(t)) != t` 的小數（`1.10`、`1E+3`、`1e400`、`1e-400`） | `JsonText(t)` | `{"$type":"json","text":t}` | number（text 是數字 literal 時）|
| 可往返的小數（`0.5`） | `float` | number | number |
| 物件、陣列 | `JsonText(原文)` | `{"$type":"json","text":原文}`（> 64 KiB 用截斷 text cell） | json |
| 列裡沒有這個 key | `MISSING`（sentinel） | `{"$type":"missing"}` | missing |

- `JsonText` 是 `values.py` 裡的 frozen dataclass（`text: str`），`MISSING` 是單例。
- 欄位 category：signature 型別 `"number"` → NUMBER、`"string"` → TEXT、`"boolean"` → BOOL，其餘（`"json"`、`"object"`、`"array"`、`*` 展開的欄位、溢出欄）→ **MIXED**。MIXED 的意思是「型別在 cell 上，字串就是字串，不猜 JSON」。
- `sql/formatting.py`：`_normalise_raw` 把 `JsonText` → `_Json(text)`、`MISSING` → `_Missing`；`_normalise_encoded` 認得 `$type` 為 `json`／`missing`。Writer：TSV／CSV／Markdown → `_Json` 寫原文、`_Missing` 寫空字串（不受 `null_text` 影響）；JSON／JSONL 物件 writer → `_Json` 原文嵌入、`_Missing` 省略該 key、MIXED 欄的 str 一律是 JSON 字串；INSERT 對 SQLPP 回 `unsupported_format`（SQL++ 的 INSERT 是 key／value，不是欄位）；IN list 對 SQLPP 產生陣列 `[…]`（`x IN [...]`，**〔未實測，A19〕**），`_Missing` 寫 `MISSING`。
- `aggregate.ts`：MIXED 欄只把 JS number 與數字 literal 的 json cell 算進 sum／avg；`missing` 不算 non-null。
- `value_condition`（SQLPP 的表格篩選，目前沒有入口，但方言表需要）：`MISSING` → `x IS MISSING`、`_Json` → `x = 原文`。

### 3.3 SQL++ 方言（`sql/`）

#### 3.3.1 `LexRules`（`Dialect.SQLPP`）

| 欄位 | 值 | 依據 |
|---|---|---|
| `nested_block_comments` | False | 〔實測〕`/* */` 不巢狀 |
| `line_comment_end` | `[\n\r]` | `--` 到行尾；CR 是否結束 **〔未實測，A6〕**，取安全側（我們看到的程式碼只會比 server 多） |
| `dash_comment_needs_space` | False | 〔實測〕`1--x` 是註解 |
| `hash_comments` | False | 〔實測〕`#` 不是註解 |
| `executable_comments` | False | `/*+ … */` 是 optimizer hint，不影響分類，當一般註解 |
| `dollar_quotes`／`dollar_params` | False／False | 〔實測〕沒有 `$$` 字串 |
| `backslash_escapes` | True | 〔實測〕兩種引號都吃反斜線 |
| `string_quotes` | `' "` | 〔實測〕雙引號永遠是字串 |
| `identifier_quotes` | `` (("`","`"),) `` | |
| 新 `identifier_backslash_escapes` | True | 〔實測〕back-tick 內 ``` `` ``` 與 `` \` `` 都可以；目前的 quoted 掃描只認加倍 |
| 新 `named_param_prefixes` | `$ @` | `$name`、`@name`、`$1` 產生 `TokenKind.PARAM`（不是 WORD） |
| 新 `question_params` | True | `?` 產生 PARAM |
| `variable_prefixes` | 空 | |
| `body_rule` | 新 `BodyRule.NONE` | 〔實測〕沒有程序本體：JS UDF 本體是字串、inline function 用 `{ expr }`，`;` 永遠切句（`_BodyTracker` 對未知規則本來就不開 body） |
| `client_command` | None | 沒有 DELIMITER、`GO` |
| `unicode_spaces` | False | server 是否把 U+00A0 等當空白 **〔未實測，A7〕**；04a 第一個 task 實測，若是空白就改 True（同 T-SQL 的處理） |

`.5` 在 SQL++ 不合法，lexer 照樣當數字：只影響錯誤訊息，不影響切句。

#### 3.3.2 引號、保留字、literal（`sql/dialects.py`）

- `_IDENT_QUOTES[SQLPP] = ("`", "`")`；`force_quote_ident` 對 SQLPP 另外把 `\` 加倍（`identifier_backslash_escapes`），`` ` `` 加倍。**〔未實測，A5〕**：`\\` 代表一個反斜線（依跳脫規則推得）。
- `_SIMPLE_IDENT[SQLPP] = [A-Za-z_][A-Za-z0-9_]*`（〔實測〕`a$b`、`中文` 不行）；`SQLPP_RESERVED` = 7.6 的 225 個保留字（大小寫不敏感）。含 `-` 的名稱（`travel-sample`）與保留字（`value`、`users`、`bucket`）加 back-tick；多加不影響（〔實測〕`FROM dbaguy.app.users` 不加也行）。
- `quote_literal(SQLPP, v) = json.dumps(v, ensure_ascii=False)`（D-04-15）；`bool_literal` → `TRUE`／`FALSE`；`json_literal` → JSON 原文本身；`bytes_literal` → 不適用（SQL++ 沒有 binary literal；呼叫即 `ValueError`，`_BYTES_LITERALS` 仍要有條目給 tripwire）；`_STRING_PREFIXES` ""；`_ISO_DATETIME` False；`_ZERO_DATE_AS_TEXT` None；`_NUL_IN_LITERAL` None（JSON 跳脫成 `\u0000`）。
- `qualified()` 對 SQLPP 支援三段（bucket.scope.collection）：新增 `keyspace_path(bucket, scope, collection)`，每段 `quote_ident`。

#### 3.3.3 切分（`sql/splitter.py`）

- SQL++ 一次請求只能一句（〔實測〕`SELECT 1; SELECT 2` 回 3000，結尾的 `;` 可以），所以一律在 client 切。規則就是一般的 `;`（`BodyRule.NONE`），物件／陣列 constructor 裡的 `{` `}` 不影響。
- `StatementSpan` 新增 `params: tuple[str, ...]`：該句的參數 token，依出現順序，具名的正規化成 `$name`（`@x` 與 `$x` 是同一個），`?` 原樣。只有 `named_param_prefixes` 或 `question_params` 的方言會填。`/sql/split` 的 `StatementRange` 帶出 `params`。

#### 3.3.4 分類（`sql/classifier.py`）

sqlglot 沒有 N1QL／SQL++ 方言（〔實測〕30.23.0 為最新）。SQLPP 只走關鍵字規則：`_from_sqlglot` 對沒有 sqlglot 方言的 dialect 直接回 None（不記 WARNING），`warm_up()` 跳過；`sqlglot_rules.SQLGLOT_DIALECT` 不加 SQLPP，tripwire 測試（`test_every_dialect_has_an_entry_in_every_dialect_table`、`test_every_dialect_parses_through_sqlglot_resolved_by_import`）改成排除一個明確的 `KEYWORD_ONLY_DIALECTS = {SQLPP}`。

新的 `_classify_sqlpp(tokens)`（`_classify_single` 遇到 SQLPP 時呼叫），依調查 §8.2：

| Kind | 語句 |
|---|---|
| READ | SELECT、WITH [RECURSIVE]…SELECT、EXPLAIN（含 EXPLAIN FUNCTION；不會執行，`_EXPLAIN_KEYWORDS[SQLPP] = ()`）、ADVISE、INFER、PREPARE（只準備，不執行） |
| WRITE | INSERT、UPSERT、UPDATE、DELETE、MERGE；`UPDATE STATISTICS`／`ANALYZE`（維護，EE；沒有 WHERE 可言，不給危險理由）；任何位置的 `NEXT VALUE FOR`／`NEXTVAL FOR`（推進 sequence；`PREV VALUE FOR` 是讀）；SELECT／WITH 裡出現 INSERT／UPDATE／DELETE／MERGE 字（保險：〔實測〕SELECT 內的 DML 是語法錯誤） |
| DDL | CREATE／DROP／ALTER／BUILD INDEX、CREATE PRIMARY INDEX、CREATE／DROP SCOPE、CREATE／DROP COLLECTION、CREATE／DROP FUNCTION、CREATE／DROP／ALTER SEQUENCE、GRANT、REVOKE |
| TRANSACTION | `_TRANSACTION_STARTS[SQLPP]`：BEGIN WORK／TRANSACTION／TRAN、START TRANSACTION／WORK、COMMIT、ROLLBACK（含 TO SAVEPOINT）、SAVEPOINT、SET TRANSACTION |
| UNKNOWN（fail closed） | EXECUTE（prepared：server 的唯讀檢查仍會擋寫入；EXECUTE FUNCTION：JS UDF 可能寫入）、其他 SET、無法辨識的開頭 |

危險理由：
- DROP（INDEX／SCOPE／COLLECTION／FUNCTION／SEQUENCE）→ `DROP`。
- UPDATE／DELETE 在括號深度 0 既沒有 WHERE 也沒有 `USE KEYS`（或 `ON KEYS`）→ `UPDATE_WITHOUT_WHERE`／`DELETE_WITHOUT_WHERE`。`DELETE FROM ks USE KEYS 'k'` 是指定目標，不標。實作：把 `_tsql_unfiltered_dml` 一般化成「深度 0 的過濾片語」，T-SQL 傳 `WHERE`，SQL++ 傳 `WHERE`／`USE KEYS`／`ON KEYS`（DRY；子查詢裡的 WHERE 不算）。
- `MERGE … WHEN MATCHED THEN DELETE` → `MERGE_DELETE`。
- 寫入目標是 `system:`（`DELETE FROM system:active_requests …`、`system:prepareds`）→ `SYSTEM_KEYSPACE`。

`is_transaction_statement(sql, dialect) -> bool`：公開函式，給 Couchbase adapter 拒絕交易語句用（D-04-13）。

#### 3.3.5 安全（`services/safety.py`）

- 唯讀：READ 放行；TRANSACTION 在 Couchbase 由 adapter 拒絕；SESSION 沒有 SQLPP 條目 → 擋（SQL++ 本來就沒有 session 設定語句）；其餘擋。
- 第二層：唯讀 profile 的每個請求（含 metadata 與文件的讀取）都送 `readonly=true`。〔實測〕server 拒絕 INSERT／UPSERT／UPDATE／DELETE／MERGE、所有 DDL、BEGIN WORK、CREATE FUNCTION、EXECUTE FUNCTION、`DELETE FROM system:active_requests`、`NEXT VALUE FOR`，以及 `EXECUTE` 一個寫入的 prepared（調查 §3.6）。〔實測，A12〕CTE 與 ADVISE 在 `readonly=true` 下照常執行（EXPLAIN FUNCTION 沒有實測）。**例外（M1）：7.6.12 的 `UPDATE STATISTICS`（含 `ANALYZE` 寫法）在 `readonly=true` 下照樣執行**，會把 optimizer statistics 寫進 `_system._query`；其餘實測過的寫入（DML、index／scope／collection／function／sequence 的 DDL、`EXECUTE FUNCTION`（含會寫入的 JS function）、`NEXT VALUE FOR`、執行寫入的 prepared、`DELETE` 對 `system:`、GRANT／REVOKE、`FLUSH COLLECTION`）都被 server 以 1000 拒絕。所以唯讀 profile 的 `UPDATE STATISTICS` 只有分類器擋（`test_safety.py`）。
- `server_read_only = True`（連線對話框不顯示 SQL Server 那段警語）。

#### 3.3.6 補全的關鍵字（`sql/completion.py`）

`STATEMENT_KEYWORDS[SQLPP]`：SELECT、INSERT、UPSERT、UPDATE、DELETE、MERGE、WITH、CREATE、DROP、ALTER、BUILD、EXPLAIN、ADVISE、INFER、PREPARE、EXECUTE、GRANT、REVOKE。`CLAUSE_KEYWORDS[SQLPP]`：SELECT、RAW、DISTINCT、FROM、USE KEYS、USE INDEX、ON KEYS、JOIN、LEFT JOIN、INNER JOIN、NEST、UNNEST、LET、LETTING、WHERE、GROUP BY、HAVING、ORDER BY、LIMIT、OFFSET、RETURNING、SET、UNSET、VALUES、ANY、EVERY、SATISFIES、IN、WITHIN、END、IS MISSING、IS NOT MISSING、IS VALUED、AND、OR、NOT、LIKE、BETWEEN、CASE、WHEN、THEN、ELSE、ARRAY、FOR、EXISTS、UNION、INTERSECT、EXCEPT、ALL、AS、ASC、DESC（plan 時再補齊）。keyspace 與欄位補全見 §3.12。

#### 3.3.7 Formatter 守衛（`sql/format_guard.py`，所有方言）

`POST /sql/format-guard {dialect, original, formatted} → {same: bool, offset: int | null}`（offset 為 UTF-16，第一個不同的 token 在 original 的位置）。用 `tokenize(dialect)` 切兩邊、丟掉 SPACE 後逐一比較：

- STRING、QUOTED、NUMBER、PARAM、標點：kind 與文字完全相同。`'it''s'` 被拆成兩個字串 → 不同。
- WORD：大小寫不敏感（`keyword_case` 預設 upper 會改關鍵字大小寫）。**例外 SQLPP**：只有保留字大小寫不敏感；其他字（SQL++ 的識別字大小寫敏感）必須完全相同——formatter 若把欄位 `type` 改成 `TYPE`，語意就變了，應該拒絕。
- COMMENT：去掉全部空白後相同（formatter 會重排註解的縮排）。

前端 `format.ts` 在替換前呼叫；`same=false` 就不改文字，toast「Formatter 會改變語句內容（例如字串），已保留原文」。sqlpp → sql-formatter 的 `"n1ql"`。

### 3.4 HTTP 與串流解析（`adapters/couchbase/http.py`、`stream.py`）

#### 3.4.1 連線與請求

- 每個 `CouchbaseConnection` 持有一條 HTTP/1.1 keep-alive 連線（`http.client.HTTPConnection`，TLS 時是覆寫 `connect()` 的子類別：TCP 連到 endpoint，`wrap_socket(server_hostname=tls_server_name or host)`）。session 一條、metadata 一條，不另開側連線（取消不需要）。TCP keepalive 用共用的 `adapters/sockets.enable_keepalive()`（30 秒閒置、10 秒間隔、3 次，與 MySQL 相同；從 `mysql/adapter.py` 搬出）。
- 連線逾時 `CONNECT_TIMEOUT_S = 10`；之後 socket 是阻塞的（不設讀取逾時：串流暫停可以任意久，取消靠 shutdown）。
- `POST /query/service`，header：`Authorization: Basic …`（永不記 log）、`Content-Type: application/json`、`User-Agent: DbaGuy/<版本>`（出現在 server 的 `completed_requests`，DBA 看得出是誰）。
- Body 由程式組字串（不是 `json.dumps` 一個 dict），參數 literal 原文直接嵌入：

  | 欄位 | 值 |
  |---|---|
  | `statement` | `json.dumps(sql, ensure_ascii=False)`；含孤立 surrogate 時 UTF-8 編碼失敗 → `invalid_text_error()`（與 MySQL 相同） |
  | `client_context_id` | `"dbaguy-" + request_id`，只留 `[A-Za-z0-9_-]`，截到 64 字（〔實測〕超過會被截、`/` `"` 回 1110） |
  | `query_context` | `default:` + `quote_ident(bucket)` [+ `.` + `quote_ident(scope)`] |
  | `readonly` | profile 為唯讀時 `true`；metadata 與取樣查詢一律 `true`；文件寫入不送 |
  | `$name` / `args` | 具名參數各一個欄位（名稱加 `$`）；位置參數放 `args` 陣列 |
  | `profile` | `"timings"`（只在 `RunOptions.profile_plan`） |
  | `preserve_expiry` | `true`（只在文件 replace） |
  | `timeout`、`scan_consistency` | 不送（R-04-5；scan consistency 留到之後） |

- keep-alive 重用（D-04-14）：上一個回應完整讀完、閒置 < `KEEPALIVE_REUSE_S = 30` 才重用，否則先開新連線。重用的連線在收到任何回應位元組之前出錯（`RemoteDisconnected`、`BrokenPipe`、`ConnectionReset`）且 `retry_safe` → 換新連線重送一次；否則 `connection_lost`。
- 取消（任一執行緒）：持小鎖讀「目前的 socket」，`socket.fromfd(fd)` 複製一份後 `shutdown(SHUT_RDWR)` 再關掉副本。不呼叫 `SSLSocket.shutdown()`：它會在讀取中的執行緒底下把 `_sslobj` 拿掉。純 HTTP 已〔實測〕0 ms；TLS 下的效果 **〔未實測，A10〕**（04a 第一個 task）。連線之後作廢，下個請求開新連線（沒有 session 狀態可遺失）。
- HTTP 狀態碼：query service 錯誤時也回 JSON（400／401／404／500／503）**〔未實測，A14：每個狀態碼都是 JSON〕**；body 不是 JSON（proxy 的 HTML）→ `StatementError(f"HTTP {status} {reason}", code=f"http_{status}")`。

#### 3.4.2 回應解析器（`stream.py`，純函式）

回應是一個串流的 JSON 物件（調查 §3.1）：`requestID`、`clientContextID`、`signature`、`results: [列…]`、`errors`、`warnings`、`status`、`metrics`、`profile`。〔實測〕`signature` 一定在 `results` 之前；**錯誤可能在列之後**（timeout 3 秒的例子：先 53,363 列，再 `errors:[1080]`、`status:"fatal"`）。

以調查的原型（`httpq.py`）為底：
- 增量 UTF-8 decoder（strict；server 送壞的 UTF-8 → `protocol_error`）。
- 最外層物件逐欄掃描的狀態機；未知欄位解析後丟掉。
- 值只在後面接著分隔符時才接受（數字可能被切在兩個 chunk 之間）。
- **大列避免 O(n²)**：一列沒解析完時，記下目前的 buffer 長度，buffer 至少長到兩倍才再試（總成本 ≤ 2 倍）。5 MB 文件被切成 80 個 64 KiB chunk 時，不會每來一個 chunk 就從頭解析一次。
- 每列不先整列 `raw_decode`：直接在 buffer 上逐欄掃描物件的 key（`json.decoder.scanstring`）與值（`JSONDecoder.raw_decode(s, idx)`，C 實作），切出每個值的原文；碰到 buffer 結尾就等更多資料。〔實測〕每欄再 decode 一次的成本：travel-sample routes 2,700 萬位元組 8 萬列／秒（含 HTTP）。
- 每列產出 `RawRow(text, fields)`：`text` 是整列原文（給 `fetch_raw`），`fields` 是 `(key, 原文, 解析後的值)` 的有序清單（物件列）或單一值（RAW 列）。
- 讀取：Content-Length 用 `HTTPResponse.read1`；chunked 由 `http.ChunkedBody` 增量解碼（http.client 的 chunked 讀取停在 chunk 邊界會等下一個 chunk，預讀不能用，04b B33）。

### 3.5 結果塑形（`adapters/couchbase/rows.py`，純函式）

#### 3.5.1 欄位

| signature | 欄位 |
|---|---|
| 沒有（DML 沒有 RETURNING、DDL） | `columns()` → None；`affected_rows()` 見 §3.5.5 |
| 字串（`SELECT RAW …`、EXPLAIN 等：`"json"`、`"number"`…） | 一欄 `value`，type_name＝signature，category 依 §3.2.4 |
| 物件、沒有 `*` | 依 signature 順序；type_name＝各自的 signature 型別 |
| 物件、有 `*`（`{"*":"*"}`、`{"id":"json","*":"*","amount":"json"}`） | signature 順序，`*` 的位置換成首頁裡由 `*` 帶來的 key（不在 signature 其他名稱裡的 key），依首次出現順序；最後加溢出欄 |

- 首頁：最多 `STAR_PEEK_ROWS = 500` 列或 `STAR_PEEK_BYTES = 8 MiB`，至少 1 列；在 `columns()` 第一次被呼叫時讀（在 worker 的 timeout 之內），結果快取（`_pump` 每次迴圈都會呼叫 `columns()`）。讀到的列留在 result 裡，`fetch()`／`fetch_raw()` 先吐這些。沒有 `*` 的結果不預讀。
- `*` 展開欄的 type_name＝首頁觀察到的 JSON 型別，以 `|` 連接（`string`、`number|null`、`object`），category MIXED。
- 溢出欄：名稱 `(other fields)`（與真的 key 撞名時加 ` 2`…），type_name `object`，category MIXED。每列的 cell：不在欄位清單裡的 key 依原順序組成物件 `{"k":原文,…}`（key 用 `json.dumps`、值用原文），沒有就是 MISSING。
- 沒有 `*` 的結果在首頁就出現 signature 以外的 key（〔實測〕的形狀裡沒有）→ 也加溢出欄；首頁之後才出現 → 發一次 WARNING「有些列含欄位以外的 key（第 N 列起）；匯出 JSON 可看到全部」並繼續。UNION 的 signature 是各分支的聯集（〔實測〕），各列只有自己的 key，其餘 MISSING。

#### 3.5.2 Cell

每欄 `row.get(name, MISSING)`，值依 §3.2.4 轉換（數字以原文判斷：整數 literal → `int`，超出 ±(2⁵³−1) 改 `JsonText`；小數 → `repr(float(t)) == t` 才用 `float`，否則 `JsonText`）。不是物件的列（物件 signature 下）整列放進溢出欄。

#### 3.5.3 alias 展開（D-04-1）

以下三點都成立時，`*` 展開的是 alias 物件裡的 key，而不是 alias 本身：
1. 語句最外層的 projection 有一個不接在 `.` 之後的 `*`（lexer 判斷：`SELECT [DISTINCT|ALL]` 或深度 0 的 `,` 之後的 `*`；`u.*` 不算）。
2. signature 只有一個 `*`。
3. 首頁每一列由 `*` 帶來的部分都恰好是同一個 key K（值的型別不限），且至少一列的 K 是有成員的物件（04a Task 8 review 放寬，R-04a-T8a：值不是物件的列，`*` 部分照原樣進溢出欄，例如 `{"d":42}`；全部是純量或 `{}` 時不展開，K 是一般欄位）。

展開後：signature 的其他欄照位置；`*` 的位置換成 K 物件的 key（依首次出現順序：`SELECT *` 是存放順序）；與 signature 欄撞名的顯示成 `K.name`。首頁之後不符合的列（別的 key、值不是物件）把 `*` 帶來的 key／value 原樣放進溢出欄。

雙擊 collection 的瀏覽語句 ``SELECT META(d).id, * FROM `c` AS d`` 依賴「`*` 與其他 projection 並列時，列是 `{"id":…,"d":{文件}}`」**〔未實測，A1〕**；不是的話規則不觸發，畫面退回一般的 `*` 展開，仍然無損（04a 第一個 task 實測，必要時改成 `SELECT META(d).id, d`）。

#### 3.5.4 Metrics、warnings、錯誤

- `warnings[]` → `ServerMessage(WARNING, f"{code}: {msg}")`，隨 `take_messages()` 送出。
- `metrics` 到達時（最後一列之後）→ 一則 INFO：`Elapsed 12.96s · execution 12.95s · 1,000 results · 2.1 MB · 3 mutations · 1 warning`（有的欄位才列；duration 照 server 的 Go 格式原樣顯示，不解析）。
- 參數 WARNING（送出前，Couchbase 特有）：literal 含 > int64 的整數或 float64 存不精確的小數 →「參數 $x：query service 會把 18446744073709551615 讀成 double」。04b（B10）：訊息只寫參數名稱、不顯示值（§4 的原則優先於這個例子）；規則照 server 對 int64 的取整（B11）。
- `errors[]`（在 `run()` 或列之後的 `fetch()` 裡）→ `StatementError`：
  - `code` = `str(code)`；`message` = `msg` 原文（M10），附上 `reason` 的巢狀說明與 `reason.missing_role`（「missing role: query_system_catalog」）；其餘錯誤各一行附在後面。
  - 位置：`line`＋`column`（〔實測〕1-based、column 以 code point 計）→ 該行文字經 `sql/offsets.py` 轉成 UTF-16 → `ErrorPosition`（相對於送出的語句）。
- `status:"stopped"` 且沒有錯誤 → `StatementCancelled("Query was stopped on the server")`。
- 錯誤在列之後：前面的列照常送出，下一次 `fetch()` 才丟（worker `_fail` → 已顯示的列保留）。

#### 3.5.5 Affected rows

- 有 `metrics.mutationCount` → 該數字。
- 沒有，而語句的第一個關鍵字是 INSERT／UPSERT／UPDATE／DELETE／MERGE → 0（〔實測〕`mutationCount` 為 0 時不出現）。
- 其他（DDL 等）→ None。
- DML 帶 RETURNING 時有列也有 mutation：列進 grid，mutation 數在 metrics 訊息裡。

### 3.6 連線（`adapters/couchbase/settings.py`、`nodes.py`、`adapter.py`）

#### 3.6.1 Profile 欄位

| 欄位 | Couchbase 的意義 |
|---|---|
| `host` | 一個 host，或逗號分隔的 seed 清單（`cb1,cb2`；IPv6 用 `[::1]`）；SSH 開啟時只能一個 |
| `port` | query port；None → TLS 時 18093、否則 8093；填了非預設值 → 不探索（D-04-6） |
| `database` | 預設 query context（§3.11.1） |
| `username`／密碼 | 必填（query service 不接受匿名）；密碼在 Keychain，流程同其他引擎 |
| `ssl` | §3.6.4 |
| `options` | `manager_port`（預設 8091／TLS 18091）、`discover_nodes`（`true`／`false`，預設 true）；其他鍵忽略並以 WARNING 記下鍵名 |

`DatabaseAdapter.endpoint_target()` 見 §3.2.1；`default_port = 8093`（EngineInfo 顯示用）。

#### 3.6.2 URL 匯入（`url_schemes = ("couchbase", "couchbases", "http", "https")`）

| URL | 結果 |
|---|---|
| `couchbase://h1,h2[:port][/bucket][?…]` | host `h1,h2`；**port 一律不用**（couchbase:// 的 port 是 KV 11210 或舊的 8091，不是 query port）→ None；ssl `disable` |
| `couchbases://h…` | 同上；ssl `verify-full` |
| `http://h:8093`、`https://h:18093` | host、port 照用（8091／18091 是 Web 管理介面，貼上來的 console URL 不是 query port → 丟掉，None）；ssl `disable`／`verify-full` |
| 路徑 `/bucket[.scope]` | `database`（預設 context） |
| `user:pass@` | username／password（解碼） |
| `?certpath=`、`?truststorepath=` | `ssl.ca_file` |
| `?ssl=no_verify` | ssl `require` |
| 其他參數 | 丟掉，WARNING 只記鍵名（不是單純名稱的鍵記成 `(unnamed)`；訊息永遠是固定文字，不回顯 URL：authority 裡有密碼，同 MySQL） |
| 拒絕 | 冒號後沒有 port；路徑、query、fragment 在 NFKC 正規化後含 `@`（R-04a-T9a：密碼裡未編碼的 `/`、`?`、`#` 會讓 authority 提早結束，密碼尾巴會跑進 database 或被記進 log；certpath 裡的 `@` 請以 `%40` 編碼） |

#### 3.6.3 節點探索與失效轉移

1. 不探索的情況：SSH 開啟、port 非預設、`discover_nodes=false`。→ 候選＝seed 清單，各用 query port。
2. 探索：依序對每個 seed 的 manager port 送 `GET /pools/default/nodeServices`（Basic auth、`DISCOVERY_TIMEOUT_S = 3`，是整個交換（連線、TLS、回應）共用的期限，不是每次 read），第一個成功的回應即可；回應最多 1 MiB。〔實測〕受限使用者可用，匿名 401（調查 §10）。`nodesExt[]` 裡有 `n1ql`（TLS 時 `n1qlSSL`）的就是 query node；node 的 host 只收 `[A-Za-z0-9._:%-]{1,253}`（網路來的資料，不收的整筆丟掉），去重，最多 64 個（R-04a-T9b）。
   - 候選順序：被問的那個 seed 自己（若有 query service），其餘依清單順序。
   - 沒有 `hostname` 的條目＝被問的那個 node **〔未實測，A8〕**；`alternateAddresses.external`：seed host 等於某 node 的 external hostname 時，全部改用 external hostname 與 ports（SDK 的 network=auto 規則）**〔未實測，A8〕**。
   - 探索失敗（連不上、401、逾時、JSON 不對）→ 候選＝seed 清單，INFO 記原因。
3. `open()` 依序試候選：建立連線並送 `SELECT ds_version() AS v, CURRENT_USERS() AS u`（`readonly=true`）。連線被拒／逾時 → 試下一個；401 或 13014（帳密錯）→ 立即 `ConnectionFailed`（帳密錯不是 node 壞）；全部失敗 → `ConnectionFailed`，列出試過的 node 與原因。
4. 成功後記下 version（`Couchbase Server 7.6.12-8946-enterprise`）、edition（`-enterprise`／`-community` 字尾）、current user（`CURRENT_USERS()` 的第一個 **〔未實測，A17：受限使用者能否呼叫〕**；不行就用 profile 的 username）。
5. Session 期間 node 掛掉：取消／關閉後的「換新連線」連不上 → `connection_lost`，前端照常顯示 Reconnect；Reconnect 建新 session → 重新探索 → 試下一個可用 node。沒有 session 狀態會遺失（沒有交易、沒有暫存表），訊息照樣說明連線已重建。

#### 3.6.4 TLS

| `ssl.mode` | 行為 | 連線對話框標籤 |
|---|---|---|
| `disable` | HTTP（8093） | disable (HTTP) |
| `prefer` | 同 disable（Couchbase 的 TLS 在另一個 port，無法「嘗試」） | prefer (= disable: HTTP) |
| `require` | HTTPS（18093），不驗證憑證 | require (HTTPS, no certificate check) |
| `verify-ca` | HTTPS，用 `ca_file`（或 OpenSSL 預設 CA）驗證，不檢查 host name | verify-ca |
| `verify-full` | 再檢查 host name；經 SSH 時檢查 `tls_server_name`（真實 host） | verify-full |

- `create_default_context(cafile=…)`；client 憑證（`cert_file`＋`key_file`）用 `load_cert_chain`（傳了就用，沒有實測 x.509 驗證）。
- 沒有 CA 檔時只信任 OpenSSL 預設的 CA bundle，不是 macOS Keychain；打包版可能找不到 → fail closed（與 MySQL 同樣的已知限制）。公司 cluster 通常本來就要給 cluster CA。
- 新的 Couchbase profile 預設 `disable`；明文 HTTP 且 host 不是 loopback 時，對話框提示「密碼以 Basic auth 明文送出」。
- 〔實測〕18093＋cluster CA 走 TLS 1.3 可用（調查 §1.3）；dev cluster 的 node 憑證是否含 127.0.0.1 SAN（verify-full）**〔未實測，A11〕**。

### 3.7 Session 行為

#### 3.7.1 執行流程

`CouchbaseConnection.run(sql, options)`：
1. `is_transaction_statement(sql, SQLPP)` → `StatementError("SQL++ transactions (BEGIN WORK, COMMIT, ROLLBACK, SAVEPOINT) are not supported in DbaGuy yet; every statement commits on its own", code="transactions_unsupported")`。
2. 組 body、送出（keep-alive 規則 §3.4.1）。
3. 讀到 `signature`、`results` 開頭，或物件結束為止：錯誤 → 丟 `StatementError`；否則回 `CouchbaseResult`。`run()` 很快返回（server 先送 signature）。
4. `presentation()`：第一個關鍵字是 EXPLAIN／ADVISE → `"plan"`；這種結果的形狀固定為一欄 `plan`（MIXED）、一列、cell 是整列原文的 `JsonText`（不管 signature 是字串還是物件 **〔A15〕**）。

#### 3.7.2 取消、強制取消、關閉

- `cancel()`／`force_cancel()`／`abort()`：設旗標並 shutdown 目前的 socket（§3.4.1）。讀取端因此出錯時，旗標在 → `StatementCancelled`；旗標不在 → `connection_lost`。沒有請求在跑 → 只設旗標（worker 的 `_cancel_requested` 會在 `run()` 之前處理）。
- `StatementResult.close()`：回應已讀完 → 連線保留重用；沒讀完 → shutdown（server 立即停止，〔實測〕`closed`），連線作廢。不像 MySQL 要 drain。
- `status:"stopped"` → 取消（§3.5.4）。

#### 3.7.3 Timeout

不送 server 的 `timeout`（R-04-5）。worker 既有的計時器在 `run()`＋第一頁期間到期 → `cancel()` → 「Timed out after N s」。server 端若設了節點／cluster timeout（預設 0），1080 照原文顯示。

#### 3.7.4 暫停的結果閒置關閉（R-04-6、D-04-12）

- `SessionWorker._pause()` 在 `_state_lock` 下記 `paused_since` 與一個遞增的 token；`_set_running(request)`、`_finish()` 清掉。
- `WorkerHooks.paused_result_max_idle_s` 來自 adapter 能力（`SessionHooksFactory` 從 registry 取）。
- `SessionManager.check_paused_results()` 由 container 的 housekeeping（每 15 秒）呼叫 → 每個 worker 的 `check_paused_result()`：距結果的**第一次**暫停超過上限（45 秒，R-04-6）就 `_put(close_idle_paused(token))`；Load more 不重算時鐘（它由 server 的 buffer 供應，不會讓 server 的停滯時鐘歸零；7.6.12 在 77–86 秒自己砍掉停滯的請求，A13）。worker 執行緒上再確認 token 沒變、仍是同一個暫停的結果，才送 WARNING `Closed this result 45 s after it first paused, to free the query node — run the statement again to load more, or export it to get every row`（文字由 `paused_result_closed_text(limit_s)` 組：整分鐘寫 `N min`，其餘寫 `N s`）並 `_finish(ex)`。
- 前端不用改：`finished` 會把暫停中的 set 標成 `partial`，已載入的列留著；之後的 Load more 拿到 `no_open_result`（已結束的 execution 本來就不會再要）。
- 04b：有 `read_ahead_bytes` 的引擎，暫停中 worker 在指令之間預讀 server 已送出的列（`StatementResult.read_ahead()`；只讀 socket 上已有的、不等待）。「第一次暫停起算」改成由 `sessions/read_ahead.py` 的 `StallClock` 決定停滯何時開始：預讀讀滿的那一刻起算；讀到結尾（`DONE`）就沒有停滯、不關閉；停滯開始後再讀了 16 MiB（預讀上限的 2 倍，`STALL_RESET_FACTOR`；Load more 讀的也算）才重算；socket 讀空不算（B42）。沒有預讀的引擎維持第一次暫停起算。關閉訊息改成 `Closed this result 45 s after the server was left waiting for it, to free the query node — …`（B7）。

#### 3.7.5 沒有交易（R-04-4）

`in_transaction()` → False、`autocommit()` → True、`transaction_failed()` → False；`set_autocommit(True)` 無動作，`set_autocommit(False)` → `transactions_unsupported`；`commit()`／`rollback()` 無動作。`cancel_aborts_transaction = False`。前端在 `transactions=false` 時不呼叫 `setAutocommit`（`useTransactionControls` 直接跳過），toolbar 隱藏控制與徽章。

#### 3.7.6 匯出

- worker `_write_export`：`connection.run(cmd.sql, options)`（同 context、同參數）。
- JSON／JSONL 且結果是 `RawJsonRows` → 新的原文 writer（`formatting.open_raw_json_writer(kind, fp)`）：JSONL 每列一行，原文含 `\n`／`\r` 時用 `jsontext.one_line()`（字串外壓空白，token 不變）；JSON 寫 `[\n  列,\n  列\n]\n`，列原文不動。RAW 結果就是一列一個純量。
- CSV／TSV／Markdown：照 §3.5 的欄位（與 grid 相同），`_Json` 寫原文。INSERT → `unsupported_format`。
- 進度事件、取消、暫存檔與原本相同。

### 3.8 物件樹與 snapshot（`adapters/couchbase/catalog.py`）

用 system catalogs（〔實測〕依使用者權限過濾、經單一 query port 就能用，300 個 collection 18 ms；`/pools/default/buckets/<b>/scopes` 對受限使用者 403，調查 §5.1）。全部用具名參數，不拼字串；`readonly=true`。

| 節點 | 子節點的查詢 |
|---|---|
| root | ``SELECT RAW b.name FROM system:buckets AS b ORDER BY b.name`` |
| bucket | ``SELECT RAW s.name FROM system:scopes AS s WHERE s.`bucket` = $b ORDER BY s.name``；另加 `_default`（system:scopes 不列它）；`_system` 標 `system=True` 排最後 |
| scope | ``SELECT RAW k.name FROM system:keyspaces AS k WHERE k.`bucket` = $b AND k.`scope` = $s ORDER BY k.name``；`_default` scope 另查 bucket 層的 keyspace（沒有 `bucket` 欄位、`name = $b` 的那列）→ `_default` collection |
| collection | ``SELECT RAW i FROM system:indexes AS i WHERE i.bucket_id = $b AND i.scope_id = $s AND i.keyspace_id = $c ORDER BY i.name``；`_default._default` 用 `i.keyspace_id = $b AND i.bucket_id IS MISSING` |

- 節點：bucket／scope `has_children=True`；collection `has_children=True`（index 直接掛在下面，不另設資料夾——只有一種子物件）；index `detail`＝`online`／`deferred`／`primary · online`…（〔實測〕看過 online、deferred；文件另列 building、pending、offline、scheduled for creation、error）。
- `object_detail`（全部 `ddl_is_generated=True`）：
  - scope：``CREATE SCOPE `b`.`s`;``
  - collection：``CREATE COLLECTION `b`.`s`.`c`;`` ＋各 index 的 DDL；取樣欄位由前端另外取（§3.12），不在這裡合併。
  - index：``CREATE [PRIMARY] INDEX `name` ON `b`.`s`.`c`(key, …) [PARTITION BY …] [WHERE cond] [WITH {"defer_build": true, "num_replica": n}]``；`index_key`／`condition` 是 server 給的表達式字串，照貼；`defer_build` 只在 state 為 deferred 時；`num_replica` > 0 才寫；`comment` 放 state、`last_scan_time`、`using`。`/indexStatus` 的完整 definition 對受限使用者 403（調查 §5.2），不用。
  - bucket：沒有 DDL（叢集管理不在範圍），`comment` 說明。
- `schema_snapshot()`：buckets ＋ ``SELECT k.`bucket`, k.`scope`, k.name FROM system:keyspaces AS k ORDER BY k.`bucket`, k.`scope`, k.name``（04a：不加 namespace 條件——列裡有 `namespace` 與 `namespace_id`，`namespace` 是保留字，要用時加 back-tick；bucket 層那列換成 `_default._default`）→ `SchemaSnapshot(database=None, default_schema=None, search_path=[], schemas=[], keyspaces=[…])`。數量固定、兩個查詢，不設上限。
- 受限使用者看到的 index 是否也依權限過濾 **〔未實測，A18〕**；只看得到自己的 keyspace 是〔實測〕。

### 3.9 文件（R-04-2；`adapters/couchbase/documents.py`、`services/document_service.py`、`api/couchbase.py`）

#### 3.9.1 介面

```python
class DocumentStore(ABC):
    def get(self, keyspace: Keyspace, doc_id: str) -> StoredDocument | None
    def server_form(self, text: str) -> str          # 這個 store 會存成的文字；SDK store 回傳原文
    def blocked_numbers(self, text: str) -> list[str]  # 會被改值、必須擋下的數字的 JSONPath；SDK store 回 []
    def replace(self, keyspace, doc_id, text: str, cas: int) -> WriteResult   # DocumentConflict / DocumentMissing
    def insert(self, keyspace, doc_id, text: str) -> WriteResult              # DocumentExists
    def remove(self, keyspace, doc_id, cas: int) -> None                      # DocumentConflict / DocumentMissing
```

`CouchbaseConnection` 另外實作小介面 `DocumentCapable.documents() -> DocumentStore`（ISP；`DocumentService` 檢查 `isinstance` 或能力 `documents`）。

04c：ABC 放在 `adapters/documents.py`（與 engine 無關，裁決 C8；`DocumentService` 依賴它而不是 Couchbase 的套件），`SqlppDocumentStore` 在 `adapters/couchbase/documents.py`；`is_server_error` 因此移到 `adapters/couchbase/messages.py`。`blocked_numbers` 另放在不用連線的 `DocumentRules`（`DocumentEngine.document_rules()`），讓只看文字的檢查在 request 執行緒做、不佔 metadata worker（C43）。

#### 3.9.2 `SqlppDocumentStore`

| 操作 | SQL++ |
|---|---|
| get | `SELECT META(d).cas AS cas, META(d).expiration AS expiration, META(d).type AS type, d FROM ks AS d USE KEYS $id` → 0 列＝不存在；`d` 是存放原文（〔實測〕key 順序、空白、`1e400`）；type `base64` → binary，`d` 是 `"<binary (9 b)>"`，取出大小，不給內容 |
| server_form | `SELECT RAW $doc`（`readonly=true`）→ 該列原文 **〔A2〕** |
| replace | `UPDATE ks AS d USE KEYS $id SET d = $doc WHERE META(d).cas = $cas AND META(d).type = "json" RETURNING META(d).cas AS cas`，`preserve_expiry=true`；1 列 → 新 CAS，expiration 寫入後另讀 `SELECT META(d).cas, META(d).expiration, META(d).type FROM ks AS d USE KEYS $id`（〔實測〕RETURNING 的 expiration 一律是 0，A28）；0 列（〔實測〕CAS 不符時 success＋0 列）或 12009（〔實測〕中途被改）→ 再讀一次 META 分辨：沒有 → `document_missing`；type 不是 json → `binary_document`；CAS 變了 → `document_conflict`（帶目前的 CAS）；CAS 沒變的 12009 是 server 自己的拒絕（durability 做不到、暫時失敗、值太大），原文顯示（`QueryFailed`）。沒有 type 條件時 UPDATE 會把 binary 文件改成 JSON（A30） |
| insert | `INSERT INTO ks AS d (KEY, VALUE) VALUES ($id, $doc) RETURNING META(d).cas AS cas`；重複 key → server 回 12009（`reason.code` 17012，A29；和並發修改同一個 code），讀 key 確認存在 → `document_exists`，不存在就是 server 自己的拒絕，原文顯示 |
| remove | `DELETE FROM ks AS d USE KEYS $id WHERE META(d).cas = $cas AND META(d).type = "json" RETURNING RAW META(d).id`；0 列 → 同 replace 的分辨 |
| blocked_numbers | 以 `parse_int` 檢查：超出 int64（±9,223,372,036,854,775,807）的整數 → 路徑清單（〔實測〕`12345678901234567890` 存成 `12345678901234567000`；int64 max 精確） 04c（C4）：也包含超出 double 範圍的數字（`1e400`、`2e308`，server 以 1040 拒絕）；精度損失（π 剩 16 位）與下溢（`1e-400`→`0`）不擋，在預覽標成改值。實作是 `jsontext.number_paths`，前面加一個預先篩選（19 位以上的數字、三位數的正指數），幾乎所有存檔都不必走完整個文件。 | |

CAS 以 JSON 整數 literal 送（uint64；〔實測〕目前 < 2⁶³，約到 2262 年都在 int64 內）；API 與前端一律用字串傳 CAS（> 2⁵³）。非物件文件（字串、陣列、數字）能否用 `SET d = $doc` 取代：〔實測，04c T1：成立，A3〕。04c：CAS 一律用字串傳、store 拒絕超過 int64 的值（SQL++ 會把它當 double 比，可能對上別的 CAS，C11；〔實測〕CAS 超過 2⁵³ 時 `META(d).cas = $cas` 仍精確比較，A27）。

#### 3.9.3 存檔流程（`DocumentService`）

1. 唯讀 profile → 直接拒絕（`read_only`）；binary 文件 → `binary_document`。
2. 本地檢查：不是合法 JSON → `invalid_json`（含位置）；`blocked_numbers` 有 → `number_out_of_range`（列出路徑，說明「Couchbase 會把超過 int64 的整數存成 double 而改值；請改成字串」），不送出。04c：巢狀超過 100 層（`MAX_DOCUMENT_DEPTH`）也是 `invalid_json`（C38：diff 與 `number_paths` 每層都重讀一次，100 層是它們的時間上限）；確認過的存檔也擋 `blocked_numbers`；這些只看文字的檢查在 request 執行緒做，還沒用到任何連線（C43）。
3. `server_form(text)`；用 `jsontext.diff(edited, server_form)` 比較：兩邊 token 相同（只差空白）→ 直接存；不同 → 回 `SavePreview{server_text, changes, counts}`，`changes` 是 `DocumentChange{path, kind: key_order|number|duplicate_key|other, value_changed, before, after}`（例如 `$.address` key 重排、`$.price` `1.10 → 1.1`、重複的 key 被丟掉；`other` 讓沒預料到的差異不會被靜默吞掉；`value_changed`：數字以 `Decimal` 比，key 重排與只換寫法的數字不算改值），改值的項目排在前面（每一類、改值與否各自最多保留 100 個，列出最多 200 個），`counts` 是每一類的完整數量（C2）；前端顯示後由使用者確認（`confirmed=true` 再送一次）。
4. `replace`／`insert`，經 metadata 連線、`retry=False`（D-04-9）。成功回新 CAS 與 expiration（寫入後另讀，A28）。server 的錯誤回應是最終答案；其他失敗（連線斷了、拿不到回應、server 的 1080 timeout、寫入成功後讀 META 失敗）→ `write_outcome_unknown`（502，「可能已寫入，Reload 看」），絕不重送（C7）。
5. 文件 API 不經 SafetyPolicy（不是使用者打的 SQL；它本身就是明確的寫入動作，有自己的確認）。

04c 實作時的細化：
- ⌘S 在文字與載入或上次存的相同時什麼都不送（前端以 baseline 判斷，C12、C32）；只有 Save、新文件的第一次存、Delete… 會寫入。這回答了使用者在 04a 的問題（byte-exact 存檔重不重要）：SQL++ 存不出 byte-exact，所以 DbaGuy 永遠不重新編碼使用者沒編輯的文件、每一個 token 的改變都在發生之前顯示。
- 確認過的存檔之後，編輯器顯示 server 的形式（原文有換行就排版）並以它為新的 baseline（C13）；直接存之後保留使用者的文字。
- 預覽用的 `server_form` 是一次短的 metadata 呼叫，比較（diff）在 request 執行緒，寫入是另一次短的 metadata 呼叫，所以一份要幾秒才比得完的文件不會擋住這個 profile 的樹、補全與取樣（C43）。寫入的呼叫帶「已放棄」旗標：在佇列裡就逾時的寫入永遠不會執行，開始後才逾時算 unknown（C42）。
- 寫入前閒置超過 2 秒的連線先做一次可重送的檢查，讓被 server 關掉的 keep-alive 在寫入之前就換掉（C40）；request body 的每個原文 literal 組裝時檢查剛好是一個 JSON 值，否則一句都不送（C39）。
- 刪除時 server 說文件已不存在＝使用者要的結果（C46）；密碼不在時（後端重啟後）寫入回 `password_required`，前端開 unlock 對話框，取消則這次存檔結束（C31、C48）。

#### 3.9.4 API（`/couchbase/documents/*`，全部帶 `profile_id` 與 `keyspace{bucket, scope, collection}`）

| endpoint | 輸入 | 輸出 |
|---|---|---|
| `POST /get` | `id` | `DocumentView{id, cas: str, expiration: int（Unix 秒，0＝沒有）, type: "json"\|"base64", text: str \| None, size: int}`；不存在 → 404 `document_missing` |
| `POST /save` | `id, cas, text, confirmed` | `DocumentSaved{status:"saved", cas, expiration, size}` 或 `SavePreview{status:"needs_confirmation", server_text, changes, counts}`（以 `status` 區分）；`size` 是存檔的文字去掉 token 之間空白的 UTF-8 長度，到下次載入前是估計（C21） |
| `POST /insert` | `id, text, confirmed` | 同 `/save` |
| `POST /remove` | `id, cas` | 204 或錯誤 |
| `POST /couchbase/statement-keyspace` | `sql, query_context` | 該語句讀的 collection（只有一個、完整寫出或照執行時的 context 解析）或 `null`；純函式，不連線；grid 的「Open document…」預填用（C44） |

CAS 是 `^[1-9][0-9]{0,18}$` 的十進位字串且 ≤ int64（C11）；ID 1–250 UTF-8 bytes；文字最多 20 MiB。錯誤（C11）：`document_missing` 404、`document_conflict` 409（`detail.cas`）、`document_exists` 409、`binary_document` 422、`invalid_json` 422（`detail.offset` 是 UTF-16 位移、`detail.reason`）、`number_out_of_range` 422（`detail.paths`，路徑只在 detail，不進訊息與 log）、`read_only` 403、`write_outcome_unknown` 502。

### 3.10 EXPLAIN、ADVISE、profile（R-04-13；`adapters/couchbase/explain.py`）

- **分析**（純函式，pytest）：`analyze(text) -> PlanView`，依形狀判斷：
  - EXPLAIN 列 `{plan, text}`（EE 另有頂層 `cost`、`cardinality`）→ `kind="plan"`；
  - profile `{executionTimings…}` → `kind="profile"`；
  - ADVISE 列 `{"#operator":"Advise", advice:{adviseinfo:{current_indexes, recommended_indexes:{indexes, covering_indexes}}}, query}` → `kind="advice"`。
- **樹**：子節點在 `~children`（Sequence）、`~child`（Parallel、Authorize）、`scans`（IntersectScan、UnionScan）、`first`／`second`（join）、`~subqueries`；另外任何值是「帶 `#operator` 的物件或其陣列」的欄位也當子節點（容錯）。`PlanNode{operator, title（例如 "IndexScan3 · def_inventory_airport_faa"）, details: [(label, value)]（keyspace、index、spans 摘要、condition、covers、limit…）, estimates: {cost, cardinality} | None（EE CBO 的 `optimizer_estimates`）, stats: {items_in, items_out, exec_time, serv_time, kern_time} | None（profile 的 `#stats`）, badges, children}`。
- **反模式標記**（調查 §7，四種都〔實測〕看過）：

  | 標記 | 規則 | 嚴重度 |
  |---|---|---|
  | `primary_scan` | `PrimaryScan`／`PrimaryScan3` 且 `index` 不是 `#sequentialscan` | warning |
  | `sequential_scan` | `PrimaryScan3` 且 `index:"#sequentialscan"` 或 `using:"sequentialscan"` | warning |
  | `not_covered` | `IndexScan3` 沒有 `covers`，同一個 Sequence 裡之後有同 keyspace 的 `Fetch` | info（`SELECT *` 本來就要 Fetch） |
  | `intersect_scan` | `IntersectScan` | warning |
  | `slowest`（只 profile） | `execTime`＋`servTime` 佔全部 > 50% 的 operator | info |

- **API**：`POST /couchbase/plan {text}` → `PlanView{kind, root, findings: [{badge, path, text}], advice: [{statement, keyspace_alias, rule, kind: recommended|covering|current}], statement_text}`。04b：findings 以 node id 定位（`path` 無法指到 `~subqueries` 底下的節點）；operator 上限 10,000（`MAX_PLAN_NODES`）、深度 200，超過 → `unknown`；findings 最多 100 則。
- **流程**：
  - toolbar「Explain」（⌘E；plan 時以 `lib/shortcuts.ts` 確認未佔用）→ 對游標下的語句或選取送 `EXPLAIN <語句>`（`history=false`）；選取多句 → 提示「Explain 一次一句」。結果 `presentation="plan"` → 結果區直接是 plan 檢視（可切回 grid）。使用者自己打的 EXPLAIN 也一樣。
  - plan 檢視的「Get index advice」：用 plan 裡的 `text` 送 `ADVISE <text>`（`history=false`；04b B12：走 metadata 連線的 `POST /couchbase/advise`、唯讀、帶 tab 的 context，不經 session，免得結束同一次執行暫停中的結果），結果也是 plan presentation，顯示在同一面板，每個 `index_statement` 有 Copy。CE 上停用並提示「Community Edition 不提供 index advice」；萬一仍收到 3230，原文顯示。〔實測〕7.6.12 對含 binary 文件的 collection 可能回 5001 panic → 原文顯示。
  - 「Run with profile」（Explain 旁的選單，EE 才可用）：照常執行（照常過 SafetyPolicy：它真的會執行），`ExecuteCommand.profile_plan=true` → body `profile:"timings"`；回應的 `profile` 在 metrics 之後到達，result 讀完最後一列時 `next_result()` 回 True 一次，切到一個 presentation `plan` 的額外 result set（欄 `plan`、一列 `JsonText(profile 原文)`）。提早關閉結果就沒有 profile（plan 檢視說明）。〔實測〕EE `timings` 給 `executionTimings` 與每個 operator 的 `#stats`；CE 靜默不給（按鈕本來就停用）。

### 3.11 Query context 與參數（R-04-9、R-04-10）

#### 3.11.1 Query context

- 型別 `QueryContext{bucket, scope?}`（D-04-5）。顯示文字：`keyspace_path`（`travel-sample.inventory`、`` `my.bucket`.app ``）。
- Profile 的 `database` 解析：含 back-tick → 依 SQL++ 路徑解析；否則在**最後一個** `.` 切成 bucket／scope（scope 名稱不能含 `.`，bucket 可以），沒有 `.` → 只有 bucket。名稱含 `.` 且沒有 scope 的 bucket 要寫成 `` `a.b` ``（對話框 hint 說明）。
- 前端：`QueryTabState.queryContext`；新 tab 預設＝profile 的預設 context；從樹或 ⌘P 開的瀏覽 tab＝該 scope；從 History 重開＝紀錄裡的 context。
- toolbar 的 `ContextPicker`（`query_context` 能力才顯示）：列出 `GET /couchbase/contexts?profile_id` 回傳的 `{bucket, scope}`（來自 snapshot 的 keyspaces，含只有 bucket 的項目）＋「No context」。04b（B13）：端點收 tab 目前的 context，snapshot 沒有列它時（來自 History 或 profile 預設）仍列在第一個（`listed: false`）；每個顯示文字都來自 `QueryContext.text()`，前端不自己加引號。
- 點樹上的 bucket／scope／collection／index（`Explorer.selectRow`）：目前的 tab 是同一個 profile 的 query tab 時，把它的 context 設成該節點的 bucket＋scope（bucket 節點 → 只有 bucket，即 `default:bucket`，`users` 解析成 `bucket._default.users`）。其他情況不動（選取 profile 的行為照舊）。執行中的語句不受影響（context 每次請求才送）。
- `_default._default`：瀏覽語句用 context `(b, "_default")` ＋ ``FROM `_default` ``。

#### 3.11.2 參數面板

- 顯示條件：能力 `parameters`，且目前編輯器文字的 `/sql/split` 結果有參數。位置在編輯器與結果之間，可收合（「Parameters (3)」）。
- 每列：名稱（`$x`；`@x` 併入 `$x`）、值（monospace 輸入，JSON literal）、由值推出的型別徽章（string／number／boolean／null／object／array／invalid）；位置參數 `$1…$n`，n＝各句「最大的 `$k`」與「`?` 的個數」中最大者。輸入不合法時提示「字串要用雙引號："abc"」（前端用 `json.ts` 的無損 parser 檢查）。
- 值存在 tab 狀態與 workspace（`params` 草稿），重開 app 仍在（Q2）。
- 執行：每一句送它用到的具名參數＋前 k 個位置參數（k＝該句需要的個數）。有用到卻沒值或不合法 → 不送出任何一句，toast 指出是哪個並展開面板。匯出帶同樣的參數。
- History 只存 SQL（含參數 placeholder）與 context，不存參數值（v1）。

### 3.12 欄位取樣與補全（R-04-11、SQL++ Should）

- **取樣**：`POST /couchbase/fields/sample {profile_id, keyspace, size}`（size 預設 `settings.couchbase.sample_size`）→ metadata 連線送 `SELECT META(d).type AS t, d FROM ks AS d LIMIT $n`（`readonly=true`；binary 文件以 `META().type` 判斷，A24），最多 `SAMPLE_MAX_BYTES = 32 MiB`（超過就關閉請求，用已讀的）。在 Python 推導 `FieldPath{path: [segment…], types: {json 型別: 次數}, docs: int, percent: float}`：物件往下到 `FIELD_PATH_MAX_DEPTH = 6` 層，陣列記成 `[]` 段（元素是物件時往下），最多 `FIELD_PATHS_MAX = 2000` 條；非物件文件計成文件本身的型別。沒有 index 的 collection 會走 sequential scan（EE 受限使用者缺 `query_use_sequential_scans` 回 13014，原文顯示）。
- **快取**：`FieldSampleService`（記憶體，每個 profile＋keyspace 一份，直到重新取樣、profile 變更或 app 結束）；`GET /couchbase/fields?profile_id&bucket&scope&collection` 取快取（沒有回 null）。
- **入口**：collection 右鍵「Sample fields…」（可改 size）；collection detail tab 的「Fields (sampled)」區塊（路徑、型別、% 文件、「Sample again」）。INFER 仍可以自己打（READ）。
- **補全**（`sql/sqlpp_completion.py`；`/sql/complete` 對 SQLPP 分派過來）：
  - `CompleteRequest` 新增 `query_context: QueryContext | None`、`seen_fields: list[str]`（最多 500 個；前端給目前 tab 最近結果的欄名，不含 `value`、溢出欄與帶 `.` 的名稱（B22：`*` 展開的衝突欄 `d.id` 若當成欄位會插入 `` `d.id` ``，是另一個 key））。
  - keyspace 位置（FROM、JOIN、UPDATE、INSERT／UPSERT INTO、DELETE FROM、MERGE INTO、NEST）：第一段＝context scope 下的 collection＋所有 bucket；`b.` 之後＝scope；`b.s.` 之後＝collection；插入文字用 `quote_ident(SQLPP)`（自動 back-tick，SQL++ Should）。
  - 欄位位置：`alias.` → 該 alias 的 keyspace 的取樣頂層欄位；`alias.a.` → 巢狀；不帶前綴且 FROM 只有一個 keyspace → 該 keyspace 的欄位；另加 alias、`seen_fields`（detail「seen in results」，排在取樣欄位後、關鍵字前）、關鍵字。alias 解析沿用 `sql/scope.py` 能用的部分；UNNEST 的 alias 之後再做。
  - 共用：`_Candidate`、過濾與排序從 `completion.py` 抽成共用 helper（DRY）。`/sql/describe` 對 SQLPP 回 None（之後）。
- **⌘P**：`SchemaCache.search` 也搜 `snapshot.keyspaces`，命中 kind `collection`；選了就開瀏覽 tab（D-04-11）。

### 3.13 Frontend

| 範圍 | 檔案 | 內容 |
|---|---|---|
| 型別 | `npm run gen:api` | `Dialect` 加 `sqlpp`、`ColumnCategory` 加 `mixed`、能力欄位、`ServerInfo.edition`、新指令欄位、`presentation` |
| 編輯器 | `features/editor/extensions.ts` | `SQL_DIALECTS.sqlpp = SQLDialect.define({keywords: 保留字, builtin: 常用函式, doubleQuotedStrings: true, backslashEscapes: true, identifierQuotes: "`", hashComments: false, spaceAfterDashes: false, specialVar: "?$@"})`（選項名稱 plan 時對照 lang-sql 6.10） |
| Formatter | `features/editor/format.ts` | `sqlpp → "n1ql"`；替換前呼叫 `/sql/format-guard`（所有方言） |
| Cell | `lib/cells.ts`、`features/grid/aggregate.ts`、`ValueViewer.tsx`、`RecordView.tsx` | kind `missing`（灰色 `MISSING`、`plainValue` ""、viewer 說明「欄位不存在」）；`$type:"json"`（text 是數字 → number，否則 json；`plainValue`＝text；viewer 用無損 parser 畫樹）；MIXED 欄的字串不猜 JSON；aggregate 規則 §3.2.4 |
| Copy | `features/grid/copy.ts`、`GridMenu.tsx` | SQLPP 不提供 INSERT；IN list 由 backend 產生陣列 |
| 交易 | `QueryToolbar.tsx`、`useTransactionControls.ts`、`TransactionBadge` | `transactions=false` 時隱藏控制與徽章、不同步 autocommit |
| 連線 | `ConnectionDialog.tsx` | URL 範例 `couchbase://cb1,cb2/bucket · couchbases://… · http://host:8093`；host hint（逗號清單、SSH 只能一個）；port placeholder `8093 (18093 with TLS)`；database 標籤「Default query context」（placeholder `bucket or bucket.scope`）；SSL 標籤 §3.6.4、預設 disable、明文提示；CA／client cert 欄位 |
| 樹 | `features/explorer/kinds.tsx`、`Explorer.tsx`、`ObjectDetailView.tsx` | bucket／scope／collection 圖示與標籤；`CONTAINER_KINDS` 加 bucket、scope（雙擊展開）；選取設定 context；collection 右鍵：Browse documents、Open document by ID…、New document…、Sample fields…、Details；雙擊＝Browse；detail 的 Fields (sampled) 區塊 |
| Context | 新 `features/query/ContextPicker.tsx`、`stores/workspace.ts`、workspace 同步 | §3.11.1 |
| 參數 | 新 `features/query/ParamsPanel.tsx`、`params.ts`、`useQueryRunner.ts`、`SessionController.execute/export` | §3.11.2 |
| Plan | 新 `features/explain/PlanView.tsx`、`ResultsArea.tsx`、`QueryToolbar.tsx` | Explain 按鈕與選單、`presentation="plan"` 的結果、advice 與 Copy、EE 判斷（`server.edition`） |
| 補全 | `features/editor/completion.ts` | SQLPP 時帶 `query_context` 與 `seen_fields` |
| ⌘P、History | `features/palette/*`、`features/history/*` | collection 命中開瀏覽 tab；重開時設定 context |
| 文件 | 新 `features/documents/`（`documentState.ts`、`useDocument.ts`、`useDocumentSave.ts`、`documentApi.ts`、`openDocument.ts`、`previewSummary.ts`、`DocumentTab.tsx`、`DocumentBar.tsx`、`OpenDocumentDialog.tsx`、`SavePreviewDialog.tsx`、`ConflictDialog.tsx`）、`features/editor/startingPoint.ts`、`stores/workspace.ts` | 見下 |

Document tab：
- 標題＝ID（tooltip 是 keyspace）；META 列：CAS、expiration（本地時間＋相對時間，或「none」）、type、大小。04c：tab 的所有狀態轉換（載入、Retry、Reload、dirty／baseline、衝突、已不存在、Create it、確認過的存檔、unlock）放在純函式 reducer `documentState.ts`，由 Vitest 測（C35）；`CodeEditor.setText` 一律把新文字當新的起點、清空 undo 歷史（`startingPoint.ts`）。
- CodeMirror＋`@codemirror/lang-json`（新相依，約 10 KB，相依的 `@lezer/json` 很小、`@codemirror/language` 已有；plan 時確認最新版與發佈日期）。內容是存放原文；「Format」用 `json.ts` 的 `formatJson`（保留數字 literal 與 key 順序）。
- ⌘S 存檔 → 預覽對話框（有 `changes` 時）→ 確認；`number_out_of_range` → 錯誤面板列出路徑、不可存。衝突 → 「文件在你開啟後被改過／被刪除」：Reload（放棄修改，先確認）、Copy my version、Cancel。04c：預覽的第一行先講改值的項目（「N values change」，列表不完整時是「at least N」），再講其他類別；沒改過的文件按 ⌘S 只提示 "No changes to save"（C12）；確認過的存檔之後編輯器顯示 server 的形式（C13）；存檔、新增、刪除進行中編輯器唯讀（C14）。衝突時若文件已被刪除，提供「Save as new document」（連同 Copy my version、Cancel）而不是 Reload（C22）。
- Delete…（不可復原 → 確認對話框）、Reload、Copy。唯讀 profile → 編輯器唯讀＋提示。binary → 只顯示 META 與「binary document (N bytes) — view only」。04c：binary 文件顯示 META 與 "view only"（沒有 Save、Delete、編輯器）；文件已不存在時顯示「Create it」（tab 有未存文字時保留編輯器，唯讀，只能 Create it 或在衝突對話框選 Save as new document，C30、C47）；關閉有未存修改的 document tab 問「Discard unsaved changes?」（Discard／Cancel，沒有 Save，C20；沒顯示過的還原 tab 也問，C28）。
- 未存的內容與所根據的 CAS 存進 workspace（`buffer`、`document_cas`），當機重開還在；存檔時照 CAS 檢查。04c：只保存新文件或有未存修改的文件（`buffer`＋`document_cas`），乾淨的文件重開時從 server 讀、tab 第一次顯示才讀（C15、C16）。
- 開啟：樹的「Open document by ID…」、grid 的「Open document…」（ID 預填該列名為 `id` 的欄或被點的字串 cell，keyspace 預填 tab 的 context＋上次用的 collection，對話框可改）。04c：新增「New document…」（樹，唯讀 profile 沒有）：ID 在對話框決定，內容從 `{}` 開始，第一次存照樣預覽，ID 已存在時提供「Open the existing document」（確認後捨棄草稿，C19）；grid 的預填改用該語句自己的 keyspace（`/couchbase/statement-keyspace`），其次才是 tab 的 context 與上次用的 collection（C44）。

## 4. 錯誤處理

| 情況 | 型別／code | 訊息 |
|---|---|---|
| 所有候選 node 都連不上 | `ConnectionFailed` | `Could not reach a query node: cb1:8093 (refused), cb2:8093 (timed out)` |
| 401／13014（連線時） | `ConnectionFailed` | server 原文＋「check the user name and password」 |
| TLS 驗證失敗 | `ConnectionFailed` | ssl 錯誤原文＋「set the cluster's CA file in the SSL tab」 |
| SSH 開啟但 host 是清單 | `ValidationFailed` | `An SSH tunnel reaches one node: give a single host` |
| 請求中 socket 出錯（非取消） | `connection_lost` | `Connection to the query node was lost: …`（前端 Reconnect） |
| 取消、worker timeout | `cancelled` | 既有 |
| `status:"stopped"` | `cancelled` | `Query was stopped on the server` |
| server 錯誤 | `str(code)`＋position | `msg` 原文＋reason／missing_role；多個錯誤各一行 |
| 交易語句、`set_autocommit(False)` | `transactions_unsupported` | §3.7.1 |
| 不支援的選項（非 Couchbase 帶 context／參數／profile） | `unsupported_option` | `This engine does not take a query context / parameters / an executed plan` |
| 參數不合法 | `invalid_params` | 指出參數名，不含值 |
| body 不是 JSON | `http_<status>` | `HTTP 502 Bad Gateway` |
| 回應 JSON 壞掉 | `protocol_error` | `The query service sent a malformed response`（WARNING log 只記 offset） |
| 文件 | `document_missing`、`document_conflict`、`document_exists`、`number_out_of_range`、`binary_document`、`invalid_json`、`read_only` | §3.9 |
| 寫入的結果不明 | `write_outcome_unknown`（502） | `The connection to the query node was lost during the write: the document may or may not have changed. Reload it to see what the server holds`（逾時等其他原因用該原因的固定文字；絕不重送，C7） |
| 匯出 INSERT 格式 | `unsupported_format` | `INSERT statements are not offered for Couchbase` |

原則：server 錯誤原文優先（M10）；DbaGuy 自己的錯誤是固定文字，不回顯 SQL、文件內容、參數值或密碼。

## 5. Log（loguru）

| 等級 | 內容 |
|---|---|
| INFO | 連線成功（host:port、選中的 node、TLS 模式、探索結果：N 個 query node 或退回 seed 的原因、edition、版本）；session 開關；取消（方式、ms）；暫停結果自動關閉；文件存檔／新增／刪除（keyspace，不含 ID）；取樣（keyspace、文件數、ms） |
| WARNING | 連線中途斷開；回應格式錯誤（只記 offset 與欄位名）；忽略的 profile option 鍵名；丟掉的 URL 參數鍵名；首頁之後出現欄位以外的 key |
| DEBUG | SQL 文字（前 2000 字，同 worker）；request id、client_context_id；參數**名稱**；文件 ID；HTTP 狀態與 metrics |
| 永不 | `Authorization` header、密碼、參數值、文件內容、列內容 |

既有規則照舊：檔案 log 跟著 `DBAGUY_LOG_LEVEL`（預設 INFO），SQL 只有明確開 DEBUG 才寫進檔案。

## 6. 開發與測試環境

### 6.1 `dev/docker-compose.yml`

- `couchbase`：`couchbase/server:enterprise-7.6.12`（arm64），ports **同號對應**且只綁 127.0.0.1：`8091`、`8093`、`18091`、`18093`。同號是因為 `nodeServices` 回報的是 server 自己的 port，換號會讓探索連錯 port；與本機另裝的 Couchbase 衝突的機會很小（其他服務換號是為了避開本機常見的 PG／MySQL）。不開 KV（11210）到主機：DbaGuy 不用 KV。記憶體約 1.2 GiB（〔實測〕EE＋travel-sample＋1 bucket＋300 collection 1.17 GiB）。
  - healthcheck＝「已 seed」：用 curl 對 8093 查 `dbaguy._default._default` 的 `seed_done` 文件（同 `mssql` 的作法，因為 `up --wait` 不等一次性服務）。
- `couchbase-init`：同一個 image，`entrypoint: ["/bin/bash", "/couchbase/seed.sh"]`，掛 `./couchbase:/couchbase:ro`，`restart: "no"`；seed 完閒置（同 `mssql-init` 的 R-03b-P1：`up --wait` 遇到已退出的一次性服務會失敗）。
- `bastion` 加 `depends_on: couchbase`，tunnel 目標 `couchbase:8093`。

### 6.2 Seed（`dev/couchbase/seed.sh` ＋ `dev/couchbase/init/*.n1ql`）

假帳密：cluster admin `dbaguy`／`dbaguy`；受限使用者 `dbaguy_reader`／`dbaguy`（`query_select[dbaguy:app:users]`、`data_reader[dbaguy:app:users]`，EE 角色；不給 `query_use_sequential_scans`，用來測 13014）。

1. 等 `http://couchbase:8091/ui/index.html` 200；已有 `seed_done` → 閒置。
2. `POST /clusterInit`：hostname 127.0.0.1、services `kv,n1ql,index`、memoryQuota 512、indexMemoryQuota 256、plasma（〔實測〕0.2 秒）。
3. 安裝 travel-sample（〔實測〕含 index 約 40 秒）；輪詢到 index 都 online。
4. bucket `dbaguy`（100 MB）；SQL++（`cbq` 執行 `init/*.n1ql`）：scope `app`、`many`；collection `app.users`、`app.orders`、`app.events`、`` app.`odd-name` ``、`` app.`value` ``、`app.blobs`、`app.noindex`；`many.c001…c300`（〔實測〕約 22 秒）。
5. index：`app.users`／`orders`／`events` primary；`idx_orders_amount ON orders(amount DESC)`；`idx_users_deferred … WITH {"defer_build":true}`（deferred 狀態）；`app.noindex` 不建（sequential scan）。
6. 資料：`app.events` 2 萬筆（`INSERT … SELECT` 自 `ARRAY_RANGE` UNNEST）；用 Console docs endpoint（`POST /pools/default/buckets/dbaguy/scopes/app/collections/users/docs/<id>`，〔實測〕原文照存）寫入 `u_numbers`（int64 邊界：2⁵³+1、int64 max／min、uint64 max、20 位數、`1.10`、長 π、`1E+3`、`1e-400`、`1e400`）、`u_keyorder`（`{"zeta":…,"alpha":{"z":1,"a":2}}`）、形狀不一的 `u1…u5`（缺欄位、巢狀陣列與物件、null）、非物件文件 `s1`（字串）、`a1`（陣列）、`n1`（數字）；`u_ttl`（一年 expiry，`INSERT … VALUES (…, {…}, {"expiration": 31536000})` **〔未實測，A21〕**）；`u_big`（5 MB，`REPEAT`）；`app.blobs` 的 binary 與 text 文件用 image 內的 `cbc create` 加旗標 **〔未實測，A20〕**（這兩份會觸發調查 §5.3 的 INFER／ADVISE bug，正好用來驗證 Sample fields 不受影響）。
7. RBAC 使用者；最後寫 `dbaguy._default._default::seed_done`。

TLS 測試用的 CA 不入庫：測試執行時從 `GET /pools/default/trustedCAs` 取得寫到暫存檔。

### 6.3 測試（依「只測容易出錯的邏輯」）

- `tests/support.py`：`requires_couchbase`（port 8093 開著且 `seed_done` 存在）、`make_couchbase_profile(...)`、取 CA 的 helper。
- **單元**：
  - lexer／splitter：back-tick 的兩種跳脫、兩種引號的反斜線跳脫、`--` 不需空白、`#` 不是註解、不巢狀的 `/* */`、`$x`／`@x`／`$1`／`?` 是 PARAM、物件與陣列 constructor（`{…}`、`[…]`）不影響切句、span 的 `params`。
  - classifier：§3.3.4 每一列（含 `UPDATE STATISTICS` 不標危險、`DELETE … USE KEYS` 不標、子查詢裡的 WHERE 不算、`NEXT VALUE FOR`、`system:`、MERGE…DELETE、`is_transaction_statement`）；tripwire 更新。
  - dialects：`quote_ident`（保留字、`-`、非 ASCII、`` ` ``、`\`）、`quote_literal`＝JSON、`keyspace_path`。
  - format guard：`'it''s'`、關鍵字大小寫、SQL++ 識別字大小寫、註解重排。
  - stream 解析器：同一份回應在**每一個 byte 位置**切成兩個 chunk 都得到相同結果；多位元組 UTF-8 被切開；數字跨 chunk；錯誤在列之後；未知欄位；大列的倍增重試。
  - rows：調查 §3.2 每種 signature、`*` 展開、alias 展開與三個條件、溢出欄（撞名、非物件列）、MISSING、數字規則（§3.2.4 每一種 literal）。
  - URL 解析、context 文字解析、參數驗證與 body 組裝（literal 原文進 body、名稱正規化）、keep-alive 重送規則（用假的 socket）。
  - explain：用調查的 `explain_sample.json` 與合成的 plan 驗四種標記、profile 的 slowest、ADVISE 解析。
  - 文件：`blocked_numbers`、`jsontext.diff`（key 重排、數字、重複 key、只差空白）、`one_line`。
  - formatting：`_Json`／`_Missing` 在每種 writer、原文 JSON writer、SQLPP 的 IN list 與 INSERT 拒絕。
  - worker：`unsupported_option`、`invalid_params`、暫停閒置關閉（假時鐘、token 競態）、原文匯出走 `RawJsonRows`。
- **整合（dev cluster）**：連線與版本／edition、帳密錯、探索（單節點）、樹（`_default`、`_system`、300 個 collection、受限使用者只看到自己的）、index DDL 與 deferred、snapshot；2 萬列分頁與暫停後繼續、暫停閒置關閉；執行中取消（245 秒聚合，之後查 `system:completed_requests` 的 state）、串流中關閉、worker timeout；唯讀 profile 的 server 拒絕（直接呼叫 adapter 繞過分類器）；錯誤位置（中文＋emoji 那句，column 轉 UTF-16）；warnings 與 metrics 訊息；參數型別；每次請求的 context；交易語句拒絕；文件 get／save／衝突／expiry 保留／int64 擋下／binary 唯讀／insert 重複／remove；ADVISE 與 profile（EE）；SSH 經 bastion；TLS 18093＋CA（verify-ca，verify-full 視 A11）；JSONL 匯出與 server 原文 token 相同；取樣不受 binary 文件影響。
- **前端**：手動，用 `scripts/dev.sh --web` 與內建瀏覽器（CLAUDE.md）；清單放進各 plan 與最後給使用者的測試清單。04c 起純模組有 Vitest（`npm test`，dev 相依 Vitest 5.0.3；`documentState`、`startingPoint`、`previewSummary`、`documentApi`、`useDocumentSave` 的重送規則、`openDocument`、`workspaceFile`，C35）；React 元件沒有測試。

## 7. 打包與量測

- 不加 Python 相依：只用 stdlib（`http.client`、`ssl`、`socket`、`json`、`codecs`、`decimal`）。PyInstaller 不需要 hidden import，sidecar 大小增加 ≈ 0（〔實測〕對照：SDK 會 +15 MB）。sqlglot 沒有新方言。
- 前端只加 `@codemirror/lang-json`（04c，6.0.2；JS 主 chunk +33 kB／gzip +12 kB 含 04c 的前端）；dev 相依另有 Vitest（不進 bundle）。
- 打包後驗證：用凍結的 sidecar 連 dev cluster 跑一句查詢（HTTP 與 TLS 各一次）、開一份文件（04c）。04c 的結果（`src-tauri/resources/backend` 與 `.app` 裡的 sidecar 各跑一次）：`u_numbers` 原文相同、`u_big`（5 MiB）0.06 秒、新增（預覽 → 確認）、CAS 過期的 save（409）、直接存、移除、讀回 404、TLS（verify-ca）讀文件，都符合預期；sidecar 49,100 → 49,148 KB，沒有 hidden import。
- 量測（04a 收尾）：`scripts/measure-startup.sh` 的冷啟動與記憶體不應變化；另外記錄 sidecar 在 100 萬列分頁（暫停 10 秒、繼續）期間的 RSS（目標增加 < 30 MB；〔實測〕原型約 21 MB 平穩）。

## 8. 痛點對照

| 需求 | 這個子專案怎麼解 | 踩到／退步 |
|---|---|---|
| Q1 輕快低記憶體 | 0 新 Python 相依；串流＋TCP backpressure，100 萬列 RSS 平穩（SDK 是 1.15 GB）；預讀上限 8 MiB、取樣 32 MiB | 每個 `*` 結果先預讀首頁（≤ 500 列）；dev 環境多 1.2 GiB（只影響開發機） |
| Q2 穩定不丟工作 | 沒有 C++ abort 路徑（不用 SDK）；INFER 不自動跑、取樣不會弄垮 server；解析錯誤只會變成該句的錯誤；文件 tab 的未存內容進 workspace | 暫停 45 秒自動關閉會「拿走」使用者的暫停狀態——有訊息說明、列保留（server 自己 77–86 秒也會砍，所以不關也留不住） |
| Q3 可信任 | 數字與 key 順序原文保留、MISSING ≠ NULL、server 強制唯讀、錯誤原文＋位置、存檔前顯示 server 會怎麼改、> int64 擋下、明文 HTTP 提示；密碼只在 Keychain、log 不含 header 與值 | 存檔仍會被 server 排序 key、正規化小數（B 方案的本質；有預覽與確認，SDK 方案可換） |
| Q4 聚焦 | 遵守 FP §4 Won't 全部；三個 SQL++ Should 只做最便宜的（自動 back-tick），其餘列為之後 | — |
| Q5 多引擎 | 走同一個 adapter／worker／協定；新增的都是通用能力（`RunOptions`、`presentation`、參數協定、`transactions`、MIXED cell），PG 之後可用參數與 plan 檢視 | 其他 adapter 要改 `run()` 簽章（一行）；tripwire 要承認「沒有 sqlglot 的方言」 |
| Q6 macOS 慣例 | ⌘S 存文件、⌘E Explain、⌘W 關 document tab | — |
| FP §4 Must 1 連線 | URL 匯入、多 seed、探索、SSH、TLS／CA、Keychain、多 profile | Capella 項目依 FP §8 不做 |
| FP §4 Must 2 Keyspace 樹 | system catalogs，300 個 collection 18 ms，不漏；index 狀態與 DDL；點節點設定 context | DDL 是產生的（受限使用者拿不到 `/indexStatus`），標明 generated |
| FP §4 Must 3 編輯器 | 切分、⌘↵、取消（0 ms）、tab timeout、有型別參數、context 選擇器 | server 自己的 timeout 只能原文顯示 |
| FP §4 Must 4 結果 | 串流、signature 欄序、key 聯集、int64／CAS 精確、metrics、載入更多 | 首頁之後才出現的 key 在溢出欄，不是獨立欄（R-04-7） |
| FP §4 Must 5 匯出 | CSV／JSON／JSONL 串流到檔案；JSON 系列是原文 | — |
| FP §4 Must 6 用 ID 開文件 | META、CAS 檢查、保留 expiry、衝突處理 | 存檔正規化（見 Q3）；binary 只能看 META |
| FP §4 Must 7 EXPLAIN | 樹＋四種反模式；EE ADVISE＋profile | CE 上 ADVISE／profile 停用（server 不提供） |
| FP §4 Must 8 History | 沿用，另記 context | 不存參數值 |
| FP §8 自架 7.x | 以 7.6 為目標，EE dev 環境；edition 偵測 | 7.0–7.2 沒有 sequential scan，舊規則（沒有 primary index 就不能掃）照 server 原文；8.0 未測 |
| M5 執行範圍 | SQL++ 一律 client 切句 | — |
| M6 補全 | keyspace 路徑＋取樣欄位＋結果看過的欄位，自動 back-tick | 欄位要先取樣（不自動 INFER，依 FP §4） |
| M7／M8 grid 與 JSON | MIXED cell 不猜型別、JSON 樹用無損 parser、MISSING 灰字 | — |
| M10 錯誤 | 原文＋行列（code point → UTF-16） | — |
| M11 安全 | SQL++ 分類器、危險理由（含 system: 與 MERGE…DELETE）、雙層唯讀、沒有交易所以沒有閒置交易 | — |
| M12 取消與 timeout | socket shutdown 立即停止 server 端 | — |

## 9. 風險

| 風險 | 處理 |
|---|---|
| 公司 cluster 的 edition／版本未知（INFER／ADVISE 的 bug、CBO、profile 都看它） | edition 偵測；EE 功能在 CE 停用；INFER 不自動跑；最終測試清單第一項就是看版本 |
| 多節點、NAT（`alternateAddresses`）完全沒在真的 cluster 上測 | 探索失敗就退回 seed；SSH 永遠只連一個 node；R-04-14 的 Reconnect；列入最終測試清單 |
| 很多 tab 同時暫停，佔滿 query node 的 servicer（每 node 32 個） | 45 秒自動關閉（第一次暫停起算）；`client_context_id`＋`User-Agent` 讓 DBA 看得出是 DbaGuy |
| 打包版沒有 OpenSSL 預設 CA bundle，公開憑證的 cluster 驗證失敗 | fail closed＋提示設定 CA 檔；與 MySQL 同一個已知限制；之後可考慮 macOS trust store |
| 未實測的回應形狀（A1、A15）讓 alias 展開或 plan 偵測不觸發 | 規則都退回無損的一般路徑；04a 第一個 task 先實測 |
| `SELECT RAW $doc` 與實際存放不同（A2） | 04c 先實測；不同就改本地模擬並標「近似」。〔實測，04c T1〕值層級相同，只差字串跳脫的寫法，diff 以值比較，不需要「近似」 |
| TLS 下的跨執行緒取消（A10） | 04a 先實測；不行就改呼叫 `SSLSocket.shutdown()`，讀取端拿到的任何例外（含 unwrap 造成的 `ValueError`）在取消旗標下都視為取消 |
| keep-alive 被 server 提前關閉（A9） | 30 秒重用窗、READ 才重送；其他回 `connection_lost` |
| 很大的文件（接近 20 MB）在 CodeMirror 裡卡 | 只在 document tab 出現；先接受，量測後再決定是否加大小提示 |
| dev 環境多 1.2 GiB、seed 約 90 秒 | 與 MSSQL 同樣常駐；若太重，改成 compose profile（只需改測試的 skip 條件） |

## 10. 未實測的假設

| # | 假設 | 驗證 |
|---|---|---|
| A1 | `SELECT META(d).id, * FROM ks AS d` 的列是 `{"id":…,"d":{文件}}` | 04a T1 |
| A2 | `SELECT RAW $doc` 的輸出＝`SET d = $doc` 存下的文字 | 04c T1；〔實測〕成立（值層級）：key 順序（排序）、空白（壓縮）、重複 key（留最後一個）、數字（`1.10`→`1.1`、`-0`／`0.0`／`1.0`→`0`／`0`／`1`、`1e2`→`100`、`1E+3`→`1000`、`1.5e300` 展開成 301 位、`12345678901234567890.0`→`12345678901234567000`、`1e-400`→`0`、π 剩 16 位）與非物件文件都一致；只差字串跳脫：存放時物件／陣列裡字串的 `<`、`>`、`&` 寫成 `\u003c`、`\u003e`、`\u0026`（`SELECT RAW` 只在 key 裡逃脫 `<`），兩者都把 `é`、`\/`、surrogate pair 的 escape 轉成字元；兩者都以 1040 拒絕 `1e400`、`2e308`；超出 int64 的整數兩邊都被改值（`12345678901234567890`→`12345678901234567000`、`9223372036854775808`→`9223372036854775807`）；受限使用者可以執行 → 裁決 C1：diff 以字串的值比較 |
| A3 | 非物件文件可用 `SET d = $doc` 取代；§3.9.2 的 insert（`INSERT INTO ks AS d (KEY, VALUE) …`）與 remove 寫法 | 04c T1；〔實測〕成立：字串、陣列、數字、`null`、`true` 可互相取代物件；`INSERT … RETURNING META(d).cas AS cas` 可插入它們；`DELETE … WHERE META(d).cas = $cas … RETURNING RAW META(d).id` 在 CAS 對時回 id，CAS 錯或沒給時沒有列 |
| A4 | SQL++ 字串跳脫與 JSON 完全相同（`\/`、`\b`、`\f`、`\uXXXX` surrogate pair） | 04a 單元＋整合 |
| A5 | back-tick 名稱裡 `\\` 是一個反斜線 | 04a 整合 |
| A6 | `--` 註解在 CR 結束 | 04a T1（只影響安全側） |
| A7 | server 是否把 U+00A0、U+3000 等當空白 | 04a T1 |
| A8 | `nodeServices` 沒有 hostname＝被問的 node；`alternateAddresses` 規則；IPv6 | 單節點 04a；多節點列入最終測試清單 |
| A9 | query service 讓閒置的 keep-alive 連線活 ≥ 30 秒 | 04a 整合（閒置 35 秒後再查） |
| A10 | TLS 下對 fd 副本 shutdown 能立即中斷讀取 | 04a T1 |
| A11 | dev cluster 的 18093 憑證含 127.0.0.1（verify-full） | 04a T1 |
| A12 | `readonly=true` 對 CTE、EXPLAIN FUNCTION、ADVISE 的行為 | 04a 整合；〔實測〕CTE、ADVISE 照常執行，EXPLAIN FUNCTION 未測；**`UPDATE STATISTICS`／`ANALYZE` 在 `readonly=true` 下照樣執行**（M1，只有分類器擋），其餘寫入都被 1000 拒絕（§3.3.5） |
| A13 | 暫停中的請求在 120 秒內不會被 server 自己關掉 | 04a 整合；〔實測〕**不成立**：77–86 秒後 server 自己結束（1203，`fatal`）→ 上限改 45 秒（R-04-6） |
| A14 | query service 的錯誤回應（4xx／5xx）都是 JSON | 04a 整合 |
| A15 | EXPLAIN 的 signature 形狀（字串或物件；兩者都處理） | 04a T1 |
| A16 | `profile` 欄位在 metrics 之後、形狀 `executionTimings` | 04b T1；〔實測〕成立（7.6.12 EE，T1 重跑相同）：頂層鍵 `requestID, signature, results, status, metrics, profile`；`profile`＝`{phaseTimes, phaseCounts, phaseOperators, cpuTime, requestTime, servicingHost, executionTimings}`；`executionTimings` 是從 `Authorize` 起的 operator 樹，帶 `#stats`（`#itemsIn`、`#itemsOut`、`#phaseSwitches`、`execTime`、`kernTime`、`servTime`，Go duration 如 `34.611027ms`、`4.792µs`）；有成本估計時 `profile` 另帶 `optimizerEstimates {cardinality, cost}` |
| A17 | 受限使用者能呼叫 `CURRENT_USERS()` | 04a 整合（`dbaguy_reader`） |
| A18 | `system:indexes` 也依權限過濾 | 04a 整合 |
| A19 | IN list 用 `[…]` 陣列 | 04a 整合 |
| A20 | image 內的 `cbc create` 能寫 binary／text 旗標 | 04a dev 環境 |
| A21 | SQL++ `INSERT … VALUES (k, v, {"expiration": n})` | 04a dev 環境 |
| A23 | client 讀了一個大回應後停止，7.6.12 在停止後固定時間結束請求；之後的小讀取不重新計時，大讀取會 | 04b T1；〔實測〕成立：讀 8 MiB 後停止 → 77.5 秒後消失（`fatal`，1203 `service.slow_client`）；40 秒後讀 128 KiB → 仍是第一次停止後 77.4 秒；讀 8 MiB → 第二次停止後 77.4 秒；16 × 512 KiB 在 32 秒內 → 99.6 秒（讀約 5.5 MiB 後重置）；每 2 秒讀 128 KiB 撐了 200 秒；被砍後剩下的回應讀不到。T1 重跑：三個請求同時，V1 停止後 76.7 秒、V3（40 秒後 128 KiB）77.3 秒（未重置）、V2（40 秒後 8 MiB）重置後 76.1 秒 → `STALL_RESET_FACTOR = 2`（16 MiB） |
| A24 | 取樣時能分辨 binary 文件 | 04b T1；〔實測〕`SELECT RAW d` 給 `"<binary (9 b)>"`（和字串文件無法分辨）；`SELECT META(d).type AS t, d` 的 `t` 對 binary 是 `"base64"`、其餘 `"json"` → §3.12 用後者（B16） |
| A25 | plan 與 advice 的形狀（plan 檢視依賴） | 04b T1；〔實測〕EXPLAIN 列 `{plan, text}`（CBO 另有 `cost`、`cardinality`、`optimizer_hints`）；`~subqueries` 在列的頂層（不在 `plan` 裡），各 `{correlated, plan, subquery}`；`IntersectScan.scans[]`；join 是 Sequence 裡平的 `Join`（`on_keys`）；每個 operator 可有 `optimizer_estimates {cardinality, cost, fr_cost, size}`；ADVISE 列 `{#operator: Advise, advice: {#operator: IndexAdvice, adviseinfo: {current_indexes[], recommended_indexes}}, query}`，`recommended_indexes` 是 `{indexes[], covering_indexes[]}` 或字串 `"No index recommendation at this time."`；有 query context 時另有 `index_statement_relative`、`query_context`；十份答案存成 `backend/tests/fixtures/couchbase/` |
| A26 | 參數綁定 | 04b T1；〔實測〕`@n` 綁 `$n`；`?` 依 `args` 順序、`$1` 是 `args[0]`（可混用）；`$n + 1` 加 9007199254740993 → 9007199254740994 精確；`18446744073709551615` → `18446744073709552000`；`3.141592653589793238462643383279` → `3.141592653589793`；`1.10` → `1.1` |
| A27 | `META(d).cas = $cas` 在 CAS 超過 2⁵³ 時仍精確比較 | 04c T1；〔實測〕成立：CAS ±1、+255 都不符 |
| A28 | `preserve_expiry` 的 UPDATE 之後 `RETURNING META(d).expiration` 給保留的 expiration | 04c T1；〔實測〕**不成立**：回 0，但存放的 expiration 保留（4102444800、2000000000）；沒有 `preserve_expiry` 時存放的 expiration 變成 0 → 寫入後另讀（C5） |
| A29 | 重複的 INSERT 回 12009、`reason.code` 17012（`dml.statement.duplicatekey`） | 04c T1；〔實測〕成立（HTTP 200，`errors` 在 body）；12009 與並發修改是同一個 code → 讀 key 分辨（C6）；04c 審查另發現 12009 也用於 durability 做不到、暫時失敗、值太大，所以 CAS 沒變的 12009 不算衝突 |
| A30 | `AND META(d).type = "json"` 讓 UPDATE 不動 binary 文件；沒有它 UPDATE 會把 binary 文件改成 JSON | 04c T1；〔實測〕兩半都成立（有條件：沒有列、仍是 `base64`；沒有條件：一列、type 變 `json`）→ 裁決 C5 |
| A31 | cluster manager 的 docs endpoint 逐 byte 存文字、非 JSON 的值存成 binary、`expiry` 是絕對時間且 ≤ 2147483647 | 04c T1；〔實測〕成立（`expiry=4102444800` 被拒絕：必須是 0–2147483647；2000000000 可以；`flags` 被忽略）；DELETE 一個已不存在的 key 回 **400** `Key doesn't exist`，404 是 collection 不存在（C23）；測試的 helper 用 |
| A32 | dev bucket 能放數 MiB 的測試文件 | 規劃時；〔實測〕**不成立**：100 MB 的 `dbaguy` bucket 放兩份約 18 MB 的文件後，讀與 SQL++ DELETE 回 TMPFAIL（12008）超過一分鐘，其中一份只能用 KV docs endpoint 刪掉 → 寫入的測試文件 ≤ 64 KiB；大文件的檢查讀 `u_big`（5 MiB）或合成的回應 |
| A33 | 對同一個 CAS 的並發 DELETE 與 save，只有一邊成功 | 04c Task 4 審查；〔實測〕**不成立**：80 次裡 21 次兩邊都成功、文件被刪掉（server 對 DELETE 的 CAS 檢查在它自己的讀取上）→ 已知限制 C41 |
| A34 | server 的 1080（timeout）之後，寫入沒有生效 | 04c Task 4 審查；〔實測〕**不成立**：192 次裡 45 次寫入已生效 → 寫入的 1080 回 `write_outcome_unknown`（C7） |

## 11. 執行計畫拆分

一個 branch `feat/04-couchbase`，三份 plan 依序執行、各自可合併驗收：

1. **04a：SQL++ 方言＋HTTP 引擎＋dev 環境**（backend 為主，加上讓查詢能用的最少前端）
   - T1 probe（A1、A6、A7、A10、A11、A15）與 dev 環境（compose、seed、`requires_couchbase`）。
   - `Dialect.SQLPP`：LexRules 新欄位、`BodyRule.NONE`、span 的 `params`、SQL++ 分類器與危險理由、safety、引號／保留字／literal、tripwire 更新、`/sql/format-guard`。
   - 共用介面與協定：`RunOptions` 與各 adapter 的 `run()` 簽章、能力欄位、`endpoint_target()`、`presentation`、`RawJsonRows`、cell 編碼（`JsonText`、`MISSING`、MIXED）、formatting 的新 cell 與原文 JSON writer、worker 的選項檢查／暫停閒置關閉／原文匯出／history context、`sockets.enable_keepalive()`。
   - Couchbase adapter：settings／URL／TLS／探索、http、stream、rows、result、取消、錯誤、metrics、唯讀、拒絕交易、catalog（樹、DDL、snapshot）。
   - 前端最少：`gen:api`、CodeMirror 方言、formatter 對應＋守衛、cell 種類與 aggregate、viewer、copy 格式、隱藏交易控制、連線對話框、樹的圖示。
   - 打包驗證與量測。
   - **結束時**：能建立／匯入 Couchbase profile、瀏覽樹、執行 SQL++（串流、分頁、取消、timeout）、看到精確值與 MISSING、匯出。
2. **04b：Couchbase 查詢工作流**
   - Query context（選擇器、tab 狀態與 workspace、樹點擊、History、瀏覽語句與 ⌘P）。
   - 參數面板（split 的參數、執行與匯出帶參數、workspace、數字 WARNING）。
   - EXPLAIN／ADVISE／profile（`/couchbase/plan`、PlanView、Explain 按鈕、A16）。
   - 取樣（`FieldSampleService`、API、collection detail）與 SQL++ 補全（keyspace、欄位、看過的欄位）。
3. **04c：文件**
   - T1 probe（A2、A3）。
   - `DocumentStore`／`SqlppDocumentStore`、`DocumentService`、`/couchbase/documents/*`、`MetadataService.with_connection(retry=False)`。
   - Document tab、預覽／衝突對話框、開啟對話框、grid 的「Open document…」、workspace 還原、`@codemirror/lang-json`、打包後開一份文件。

**為什麼是三份而不是兩份（D-04-16）**：coordinator 建議的 04b 包含樹／context／文件／EXPLAIN／參數五個跨 backend 與前端的功能，約是 03b 的兩倍。文件是獨立的垂直切片（自己的 backend 介面、API router、tab 種類、衝突 UX），也是 UX 風險最高的一塊；拆開後每份 plan 維持 03a／03b 的大小，04b 不必等文件編輯器就能合併。樹的 backend（列出 bucket／scope／collection／index）放 04a，因為它是 adapter 工作、可以用整合測試驗；樹上設定 context 屬於 04b。若 coordinator 仍要兩份，04b 與 04c 可直接合併（兩者只在樹的右鍵選單有交集）。

## 12. 對裁決的細化與意見

- **R-04-2 細化**：「與編輯內容不同就預覽並確認」改成「**token** 不同才確認」。只差空白（server 一定會壓縮）也確認的話，每次存一份排版過的文件都要按一次確認，確認會變成反射動作而失去意義。
- **R-04-12 細化**：token 比較不能逐字相同——`keyword_case` 預設 upper 會改關鍵字大小寫，逐字比較會拒絕所有格式化。規則：WORD 大小寫不敏感（SQLPP 只限保留字，因為 SQL++ 識別字大小寫敏感）、註解忽略空白，其餘逐字（§3.3.7）。
- **R-04-15 細化**：JSONL 要一列一行，passthrough 文件的原文可能帶存放時的換行；寫入前在字串外壓掉空白，token 不變。JSON 陣列匯出則保留原文 byte。
- **R-04-6 意見**：原先認為 120 秒合理，但 A13 實測不成立（server 在 77–86 秒自己砍停滯的請求），改為 45 秒、從第一次暫停起算；housekeeping 每 15 秒檢查，所以實際是 45–60 秒（D-04-12）。若要精確可改成每次暫停一個 Timer，但目前沒有必要。
- **R-04-8**：CSV 裡 NULL 與 MISSING 都是空字串（`null_text` 預設也是空），兩者在 CSV 無法區分——這是 CSV 的限制，需要分辨就用 JSON 匯出。
- 其餘裁決照單全收，沒有發現錯誤。
- **04c 執行中追加的裁決**（C1–C34 在 plan `2026-10-19-04c-couchbase-documents.md` 的 Planning／Pre-flight rulings，之後的記在這裡）：C35 document tab 的狀態轉換放在純函式 reducer，用 Vitest 測；C36 一次只讓一個人跑完整的 backend 測試（scratch 文件 vs `app.orders` 剛好 500 份）；C37 `diff` 的 offset 存成緊湊陣列（寬物件的暫時記憶體 ≤ 約 10 倍）；C38 巢狀超過 100 層的文件拒絕存；C39 request body 的每個原文 literal 必須剛好是一個 JSON 值（避免多出頂層欄位而繞過 `readonly`）；C40 寫入前閒置超過 2 秒的連線先做一次可重送的檢查；C41 DELETE 與同一個 CAS 的並發 save 會競爭，列為已知限制（A33）；C42 寫入的 metadata 呼叫帶「已放棄」旗標，在佇列裡就逾時的寫入永遠不會執行；C43 只看文字的檢查與 diff 在 request 執行緒、metadata 呼叫只做 I/O；C44 grid 的預填用語句自己的 keyspace（`/couchbase/statement-keyspace`）；C45 寫入的回應拿不到＝結果不明、絕不重送；C46 刪除時 server 說文件已不存在＝已達成；C47 不存在的文件有未存文字時保持唯讀，出口是 Create it 或 Save as new document；C48 取消 unlock 對話框＝這次存檔結束（什麼都沒送，編輯器可編輯）。

## 13. 收尾時要更新的文件

- `CLAUDE.md`：技術選型表的 Drivers 列把「couchbase 4.x（Couchbase）」改成「Couchbase：Query Service REST（stdlib `http.client`，無 SDK）」；Repo 結構加 `adapters/couchbase/` 的模組說明；開發指令與測試 DB 加 Couchbase（ports、帳密、seed、記憶體）；架構重點加 Couchbase 段落（REST＋增量解析、socket shutdown 取消、不送 server timeout、暫停 45 秒關閉、`*` 展開與溢出欄、MISSING、每 tab 的 query context、參數協定、沒有交易、文件經 SQL++ 與正規化預覽）；已知限制（存檔正規化、binary 唯讀、首頁之後的 key 在溢出欄、CA bundle、多節點未實測）；進度表 ④。
- feature-priorities §7「Couchbase」的「建議用 Python SDK」與「hidden import `pycbc_core`」已過時：在該段加註指向本 spec（不改寫研究文件本文）。
- 每份 plan 的 followups note（`docs/superpowers/notes/`），含打包版待驗清單：真實 cluster 的版本與 edition、多節點探索與 Reconnect、TLS 與 CA、受限使用者的樹與 13014、取消與暫停關閉、文件存檔預覽與衝突。
