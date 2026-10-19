# 子專案 ⑤：Grid 內編輯 — 設計

> 日期：2026-10-19 ｜ 狀態：已核准（使用者授權 Claude 自主設計與審核）
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §2（Q1–Q6）、§3（S1：修改先暫存、預覽 DML 後一次 commit，依 §8 升為 Must；M7 值的呈現正確；M11 安全與交易；Won't 清單）、§7「型別」、§8（「Grid 內直接編輯：升為 Must」）
> 技術依據：本設計在 dev 資料庫上的探測（2026-10-19；PostgreSQL 17.11／psycopg 3.3.6、MySQL 8.4.11／mysql-connector-python 26.7.0 pure、SQL Server 2022 CU27／pymssql 2.4.4、SQLite 3.53.4），標 **〔實測〕**；標 **〔未實測〕** 的是假設，§10 彙整並指定由哪個 plan task 先驗證。探測只建 `zz_spec05_*` scratch 表（SQLite 用 scratch 目錄裡的檔案），結束時以完整名稱 DROP；紀錄在附錄 A。本文的 A 編號自成一組（⑤ A1…）。
> 建立在 ①–④ 之上：表格資料 tab（[② spec](2026-10-16-02-query-workflow-design.md) §4.5）、grid（§4.3）、`SessionWorker` 與 WS 協定（`domain/execution.py`）、`SafetyPolicy`、`sql/dialects.py` 的引號與 literal、`sql/formatting.py` 的 `value_condition`（Filter by this value）、④ 的 cell 編碼（[④ spec](2026-10-18-04-couchbase-design.md) §3.2.4），以及 04c 存檔流程的教訓（[04c notes](../notes/2026-10-19-04c-couchbase-documents-followups.md) C7、C12、C20、C28、C42、C45、C46、C48，B40）

## 1. 目標

在表格資料 tab 的 grid 裡直接改資料：改 cell、新增列、刪除列都先暫存、看得見；⌘S 顯示 DbaGuy 實際會送出的每一句 DML，確認後在一個交易裡一次 commit。取代使用者目前在 Navicat、Beekeeper、DBeaver 做的「開表 → 篩選 → 改值 → 存」。

| 編號 | 功能 | 對應 |
|---|---|---|
| G1 | 表格資料 tab 的 grid 可編輯：改 cell、新增列、刪除列，全部先暫存，不立即寫入 | S1、M7 |
| G2 | 暫存看得見：改過的 cell、新列、要刪的列各有標記；逐 cell／逐列還原、全部捨棄、⌘Z／⇧⌘Z | S1、Q2、Q6 |
| G3 | 預覽：⌘S 列出 backend 產生、實際會送出的每一句（含交易開頭與結尾），確認後一次 commit | S1、M11 |
| G4 | 值正確：NULL 與空字串分開、數字與大整數以文字傳遞（不經 JS Number）、decimal／日期／UUID／JSON 原樣、binary 用 hex、新列的 DEFAULT | M7、§7「型別」 |
| G5 | 樂觀並行檢查：載入之後別人改了同一個欄位或刪了那列 → 整批 rollback，指出是哪一列；之後可看 server 現在的值、選擇保留自己的 | M11、Q3 |
| G6 | 結果誠實：已 commit／什麼都沒套用／部分套用（MySQL 非交易引擎）／結果不明；從不自動重送 | Q2、Q3 |
| G7 | 暫存跨當機與重開保留；關 tab 前問 | Q2、M4 |
| G8 | 唯讀連線、view、沒有可識別 key 的表：沒有編輯入口並說明原因；backend 也拒絕 | M11、Q3 |

### 成功標準

- PG、MySQL、SQL Server、SQLite 的 dev 表格資料 tab：把一個 bigint cell 改成 `9007199254740993`、一個 cell 設成 NULL、另一個改成空字串，新增一列（identity／auto-increment 用 DEFAULT），刪一列 → ⌘S：預覽依序列出交易開頭、DELETE、UPDATE、INSERT、COMMIT，文字就是送出的文字 → Commit → grid 重讀、捲動位置與已載入列數不變，cell 顯示 server 存下的值（空字串與 NULL 分得出來）。
- 另一條連線先改了同一個 cell → Commit：整批 rollback，對話框指出那一列「載入後被修改或刪除」，其他暫存原封不動；Refresh 後該 cell 有衝突標記並顯示 server 現在的值，「Keep mine」後再 commit 成功。
- 別人改了同一列的**其他**欄位 → 自己的修改照常 commit，別人的值不被蓋掉。
- 沒有 PK／唯一 NOT NULL key 的表、view、唯讀連線、Couchbase：沒有編輯入口，工具列說明原因，Enter 照舊開值檢視器；直接呼叫 API 被拒（403／422）。
- 有暫存的 tab：結束 app（或 app 當掉）再開，暫存還在並綁回對應的列；⌘W 問「Discard 3 staged changes to orders?」。
- 模擬 COMMIT 途中斷線：顯示「可能已套用」，Commit 停用到 Refresh 之後，不會自動重送。
- 注音：在 cell 編輯器組字中按 Enter 選字，不會寫入暫存；組字中的 ⌘S 不開預覽。
- 已載入 10 萬列 × 30 欄時，改一個 cell 只重畫那一列，捲動維持流暢（手動以 React Profiler 確認）。

### 不在範圍內

query tab 結果的編輯（§12；各 engine 的探測結果在附錄 A）、view 與 materialized view、沒有可識別 key 的表（含「只能新增」模式）、多格貼上（TSV）與在 grid 上直接 ⌘V、打字即編輯（type-to-edit）、超過 64 KiB 的值的編輯（只能設 NULL）、enum 與 FK 值的下拉選單、record view 內編輯、從檔案載入 binary、`UPDATE … SET col = DEFAULT`、Couchbase 的 grid 編輯（走 04c 的 Open document，R-05-12）、table designer（FP C9）、FK 導覽（S4）、匯入（C5）。Won't 清單（data compare、fake data 等）照舊不做。

## 2. 關鍵決策

R-05-x 回答 coordinator 列出的設計問題，D-05-x 是其下的設計決定。全文引用：「§x」指本文，「FP §x」指 feature-priorities，「附錄 A」是探測紀錄。

| 決策 | 選項 | 結論與理由（含證據） |
|---|---|---|
| **R-05-1** 可編輯的範圍 | (a) 只有表格資料 tab；(b) 加上 query 結果（依 driver 給的來源欄位）；(c) 再加 view | **(a)**。Q4：日常「找到那列 → 改 → commit」用表格資料 tab 加篩選就能做。query 結果的來源資訊各 engine 不一致〔實測，附錄 A〕：PG 的 `ftable`／`ftablecol` 精確（運算式 0／0，view 給 view 的 OID）；MySQL 欄位封包有 `org_table`／`org_name`，但 pure driver 的 `parse_column` 把它們丟掉；SQL Server `sys.dm_exec_describe_first_result_set(…, 1)` 給來源與 `is_part_of_unique_key`，還多出隱藏的 rowversion 欄；SQLite 的 Python 模組沒有 `column_table_name`。四個 engine 只有三個做得到（Q5），而且 query tab 還有使用者自己的手動交易、多結果集、`GO` 批次。表格資料 tab 的 session 永遠是 auto-commit、沒有使用者交易、`SELECT *` 一定含 key。View 的 key 不明、可更新規則各家不同。代價：要改 query 結果裡的值，先開那張表。之後怎麼做見 §12。 |
| **R-05-2** 沒有 key 的表 | (a) 拒絕（整張唯讀並說明）；(b) 用所有原始值比對；(c) 只允許新增 | **(a)**。key＝PK；沒有 PK 時用欄位數最少的 UNIQUE（不是 partial／expression index，欄位全部 NOT NULL）。key 欄也必須能精確比對（不是浮點、不是讀取就失真的型別），否則同樣唯讀；個別的列若 key 在 grid 裡只有預覽（截斷），只有那一列唯讀。(b) 要每個欄位都能比：json／xml／point 沒有 `=`、浮點、SQL Server datetime2(7) 讀進來只剩 6 位、grid 裡的長值與 bytes 只是預覽〔實測〕；重複的列會讓一句改到多列（exactly-1 會擋，但使用者只看到莫名的衝突）。(c) 多一種 UI 狀態，commit 後也看不到新列；留到 §12。 |
| **R-05-3** 暫存模型 | (a) 以 key 為身分、疊在結果上的 overlay；(b) 複製一份結果來改；(c) 以列號為身分 | **(a)**。每個 UPDATE 記 key（原始 cell）、改的欄位與新值、那些欄位改之前的值；DELETE 記 key；新列是自己的清單。顯示時依 key 疊上去，結果本身不複製（Q1）。sort、filter、Load more、Refresh、commit 失敗後的重讀都不會丟失或錯置暫存（列號會變，key 不會）；切換 tab 也不影響（暫存放在 React 之外、依 tab id 的 store）；目前結果裡找不到的 key（被篩掉、已被刪）照樣保留並計數。新列沒有 key，固定顯示在 grid 最上方。 |
| **R-05-4** commit 用哪條連線 | (a) tab 自己的 data session、明確的交易；(b) metadata 連線；(c) 另開一條 | **(a)**。FP §7「不要多開連線」。另一條連線還會被自己暫停的結果擋住：SQLite（rollback journal）有 SELECT 暫停時，另一條連線寫入回 `database is locked`〔實測〕；SQL Server 暫停的 SELECT 在（非 RCSI 的）READ COMMITTED 下，server 等 client 讀取時持有正在讀的那一列／頁的 S lock（已知行為，本機未重現）。(b) 在 SQL Server 設了 `READ UNCOMMITTED`、`LOCK_TIMEOUT 5000`（CLAUDE.md），也被樹與補全共用。代價：commit 先結束暫停中的結果（worker 本來就在新指令前 `_discard_open()`），之後重讀（R-05-10）。表格資料 tab 沒有 auto-commit 切換、篩選只能是單一運算式（`select_builder._single_expression`），所以 session 上永遠沒有使用者交易；worker 仍先檢查 `in_transaction()`，有就拒絕（`transaction_open`）。 |
| **R-05-5** 樂觀並行 | (a) 只比 key；(b) key＋被改欄位的原始值；(c) key＋所有欄位；(d) row version（PG `xmin`、SQL Server rowversion） | UPDATE 用 **(b)**，DELETE 只比 key。(b) 抓的正好是「會蓋掉別人寫入」的衝突（同一欄位）；別人改同一列的其他欄位不算衝突，也不會被蓋掉（只 SET 自己改的欄）。(c) 讓 `updated_at` 之類的欄位造成假衝突，且很多型別比不了。(d) 只有部分 engine 或部分表有（Q5）。DELETE 刪的是那個 key 的實體，比其他欄位會被不相干的修改擋下。比不了的欄位（§3.3.3）不比，預覽逐句列出「沒有檢查並行修改」的欄位。每句 UPDATE／INSERT 必須剛好 1 列，否則整批 rollback：UPDATE 0 列＝載入後被改或刪（PG 也可能是 trigger 跳過了該列，〔實測〕BEFORE trigger 回 NULL 時 rowcount 0）；> 1 列＝key 不唯一。DELETE 0 列不算失敗：該列已不存在（或 key 被改了），結果裡說明「已不存在」（同 04c C46）；> 1 列才失敗。 |
| **R-05-6** 結果分類、不重送 | — | 四種：**committed**；**rolled_back**（什麼都沒套用）；**partial**（只發生在 MySQL 非交易引擎：錯誤之前的句子已生效）；**unknown**（COMMIT 已送出卻沒拿到答覆）。COMMIT 送出之前的任何失敗（錯誤、衝突、取消、斷線）都是 rolled_back（非交易引擎為 partial）：server 在連線結束時 rollback 未 commit 的交易。COMMIT 送出之後 Cancel 不再傳到 driver。COMMIT 自己回錯誤（例如 PG deferred FK，〔實測〕23503 之後狀態 IDLE）也是 rolled_back；只有連線在 COMMIT 途中斷掉（`connection_lost`）或 worker 意外例外才是 unknown。unknown：暫存保留，但 Commit 停用到一次 Refresh 完成；之後的預覽頂端說明「上次結果不明：UPDATE／DELETE 有原始值檢查保護，新增的列會再新增一次」。從不自動重送（04c C7、C45）。前端：apply 請求沒有收到 `edit_outcome` 就結束（socket 斷了、backend 重啟）一律當 unknown；指令根本沒送出（`not_connected`）則什麼都沒發生。 |
| **R-05-7** 預覽即確認；送出的就是預覽的 | (a) 前端組 SQL；(b) backend 產生，apply 時重新產生；(c) backend 產生並存成一次性 plan，apply 只送 plan id | **(c)**。backend 是唯一產生 SQL 的地方（② 決策「SQL 文字智慧放 backend」；引號與 literal 只來自 `sql/dialects.py`）。plan 存在記憶體（單次使用、15 分鐘、綁定 profile＋database＋path），worker 只執行 plan 裡的文字，所以送出的一定是預覽顯示的（04c：確認過的請求帶的正是預覽的文字）。預覽一律出現、不看安全等級：它就是 S1 的「預覽 DML 後 commit」，也是 strict 等級的確認。worker 仍以 `SafetyPolicy` 檢查每一句（唯讀 → BLOCK）；server 端再擋一次：MySQL `SET SESSION TRANSACTION READ ONLY`（〔實測〕明確交易裡 UPDATE 也回 1792）、PG READ ONLY、SQLite `mode=ro`；SQL Server 只有應用層（既有限制）。 |
| **R-05-8** 值的輸入 | — | 一律以文字暫存與傳遞（不經 JS Number），backend 依欄位種類（`EditKind`）產生 literal。**NULL 與空字串**：清空輸入就是空字串；NULL 只能明確設定（⌥⌘N、選單、編輯器的 NULL 按鈕）。**DEFAULT**：只給新列（每格的初始狀態，INSERT 省略該欄；全部 DEFAULT 用 `DEFAULT VALUES`）。**數字**：去掉前後空白後符合 `NUMERIC_LITERAL` 才送，PG 另收 `NaN`、`Infinity`、`-Infinity`。**JSON**：backend 檢查是合法 JSON。**binary**：hex（可帶 `0x`），整個值取代。**bool**：true／false（t、f、1、0、yes、no，不分大小寫）。日期時間、UUID、enum、陣列、interval、money 等：原文送出，由 server 判斷，錯誤原文顯示（M10）。**不能編輯**：generated／computed、PG identity ALWAYS、SQL Server IDENTITY、rowversion、SQL Server 的 sql_variant 與 CLR 型別、MySQL GEOMETRY／VECTOR；grid 裡被截斷的值（> 64 KiB，只有預覽）只能設 NULL。單一值最多 1,048,576 字元。驗證只在 backend 預覽時做（DRY，前端不重寫規則），問題對應到 cell。 |
| **R-05-9** 編輯器與按鍵 | — | 見 §3.6.4 的按鍵表。原則：Return／F2／雙擊開始編輯（Finder 的 Return 改名；F2 與 Scripts 面板的改名一致），Esc 取消，Tab／⇧Tab 換欄，⌘S 開預覽（同 script、document），⌘⌫ 刪列（同 Scripts 面板的 Delete…、Finder），⌥⌘N 設 NULL（DataGrip 的鍵），⌘Z／⇧⌘Z 復原暫存操作。單行值用 inline 輸入框；JSON、含換行或超過 1,000 字元的文字用 drawer 裡的多行編輯器。不做打字即編輯：grid 不是文字欄位，注音的第一個組字鍵會遺失；先按 Return。每個 Enter／Tab／Esc／⌘S 處理先檢查 `isImeKeyEvent`（R-02c-9）。不做多格貼上（§12）。 |
| **R-05-10** commit 之後 | (a) 重跑瀏覽查詢；(b) 依 key 重讀改過的列、就地更新；(c) 不重讀 | **(a)**，載入列數＝原本已載入的列數（最多 10,000，`page_size` 上限），垂直捲動位置與選取保留。commit 一定結束了暫停中的結果（R-05-4），要能繼續 Load more 只能重跑；重跑顯示 server 真正存的值（default、trigger、computed、數字與時間的正規化，Q3）。(b) 需要每個 engine 拿回產生的 key：PG／SQLite 有 RETURNING，MySQL `LAST_INSERT_ID()` 只限 auto_increment，SQL Server 的表有 trigger 時不能 `OUTPUT`（〔實測〕334），而且仍要重跑才能 Load more。rolled_back 且結果原本是暫停的也重跑，暫存依 key 綁回並顯示 server 現在的值。新列 commit 後出現在 server 排序的位置，可能不在已載入範圍：toast 說明。 |
| **R-05-11** 持久化與關閉保護 | — | 暫存存進 workspace（table tab 的 `staged_edits`），每次變更經唯一的 workspace writer 存檔（500 ms），當機或重開都在；還原後依 key 綁回。上限：10,000 個變更、單值 1 MiB、總文字 8 MiB；超過就不寫進 workspace 並在編輯列提示（B40：一個 backend 不收的值不能讓每次存檔都失敗）。關 tab：「Discard N staged changes to <table>?」（Discard／Cancel，沒有 Commit：commit 需要預覽，04c C20）；沒顯示過的還原 tab 也問（C28）；commit 進行中不能關（提示等它結束或在對話框 Cancel）。結束 app 不問（會保存）。還原時 profile 已不存在：tab 照舊被丟，「已關閉」清單註明暫存一起捨棄。 |
| **R-05-12** Couchbase | — | 不做 grid 編輯（新能力 `row_editing`，Couchbase 為 False；Couchbase 也沒有表格資料 tab）。修改文件走 04c 的 Open document（grid 右鍵已有）。理由：沒有交易（R-04-4），SQL++ 存檔會正規化整份文件（R-04-2），一個 cell 的修改等於改整份文件，04c 已有預覽與 CAS。 |
| **D-05-1** 職責分層 | — | engine 的知識（key、generated、型別、能不能比、交易性）在 adapter（`RowEditing.edit_target()`，ISP，同 `FieldSampling`）；SQL 文字在純函式 `sql/dml.py`；cell → literal、比對條件從 `sql/formatting.py` 抽到 `sql/literals.py`，Filter by this value、Copy as INSERT 與 DML 共用（DRY，行為不變）；流程（交易、逐句檢查、結果分類）在 worker，四個 engine 共用。 |
| **D-05-2** literal 與比對規則 | — | 每個 dialect × `EditKind` 一張表（§3.3）。重點〔實測〕：PG 原始值檢查用 `col::text = '原文'`（text-first 載入的就是 server 的文字輸出，同一 session 設定下逐字相同；`json`、`xml`、`point` 沒有 `=` 也能比；`bpchar` 例外用 `=`，因 `::text` 去掉尾端空白）；key 條件一律 `=`（能用 index）。MySQL：JSON 用 `CAST(… AS JSON)`、零日期用 `CAST(col AS CHAR)`（`dialects` 已有）、數字不加引號。SQL Server：非 Unicode 欄位（char／varchar／text 家族）的值是純 ASCII 時不加 `N`——〔實測〕SQL collation 下 `vc = N'k42'` 變成 Clustered Index Scan（每句 UPDATE 掃整張表並加 U lock），Windows collation 才是 seek；非 ASCII 仍用 `N''`（正確優先）。SQLite：原始值依 cell 的儲存型別（數字不加引號、字串加引號；〔實測〕無 affinity 欄位裡的 `'42'` 不等於 `42`）。 |
| **D-05-3** 句子的形狀與順序 | — | 每列一句：UPDATE 只 SET 改過的欄、WHERE key＋原始值；INSERT 只列非 DEFAULT 的欄；DELETE WHERE key。順序 DELETE → UPDATE → INSERT，各組內依暫存先後：先刪可讓 UPDATE／INSERT 重用唯一值。兩列互換唯一值（A→B、B→A）仍會在中途撞唯一鍵 → rollback，要用 SQL 做（已知限制）。 |
| **D-05-4** 交易外框 | — | 開頭與結尾是 dialect 表裡的文字、經 `run()` 送出，所以也出現在預覽：PG `BEGIN`／`COMMIT`；MySQL `START TRANSACTION`／`COMMIT`；SQL Server `SET NOCOUNT OFF;` 加 `BEGIN TRANSACTION`（一個批次）／`COMMIT TRANSACTION`；SQLite `BEGIN IMMEDIATE`／`COMMIT`。`SET NOCOUNT OFF`：server 的 `user options` 若預設開 NOCOUNT，pymssql 讀不到列數（A2）。SQLite 用 IMMEDIATE：寫入鎖一開始就拿，被其他連線擋住時在第一句之前就失敗（busy timeout 內）。失敗時用 adapter 的 `rollback()`（SQL Server 是 `WHILE @@TRANCOUNT > 0 ROLLBACK`，〔實測〕8114 這類錯誤已讓 server 自己 rollback、`@@TRANCOUNT` 為 0，它就什麼都不做）。 |
| **D-05-5** 「影響列數」的來源 | — | 新增 `StatementResult.matched_rows()`，預設回 `affected_rows()`。MySQL 覆寫：UPDATE 讀 OK 封包 info 的 `Rows matched: N`——〔實測〕沒有 `CLIENT_FOUND_ROWS` 時 `affected_rows` 是「改變」的列數，值相同（含 `1.10` 改成 `1.1` 這種 decimal 相等）回 0，`ROW_COUNT()` 也是 0；`_VerbatimCursor` 是 DbaGuy 自己的類別，送出時保留 `cmd_query` 回傳的 dict 即可，不改全域的 client flag（使用者自己的 UPDATE 照舊顯示改變列數）。其他 engine〔實測〕：PG rowcount＝符合的列數（值相同也算）；SQL Server `rows_affected` 是該句自己的列數，AFTER trigger 另外插入 3 列時仍是 1；SQLite `changes()` 不算 trigger。讀不到列數（`None`／-1）→ fail closed：rollback，「無法確認改了幾列，什麼都沒套用」。 |
| **D-05-6** MySQL 非交易引擎 | (a) 拒絕；(b) 允許並警告，回報 partial | **(b)**。target 依 `information_schema.ENGINES.TRANSACTIONS` 標 `transactional`。〔實測〕MyISAM 在交易裡 INSERT 後 ROLLBACK：資料留著、警告 1196。預覽頂端警告「MyISAM 不能 rollback：中途失敗時，前面的句子已經生效」；失敗時結果是 partial，已生效的變更離開暫存、其餘留著。老系統的 log 表常是 MyISAM，拒絕會讓它們完全不能編輯。 |
| **D-05-7** plan 存放 | — | `EditService` 內的 `EditPlanStore`：記憶體、最多 32 份（LRU）、15 分鐘、取出即刪。plan 記 profile id、database、path、交易外框、句子與各句對應的 change id、`transactional`、資料表的顯示名稱、`script()`（給 Copy 與 History 的整份文字，DRY）。worker 經 `WorkerHooks.take_edit_plan(plan_id)` 取得（hooks factory 注入），並檢查 plan 的 profile／database 與 session 相同（`plan_mismatch`）。backend 重啟或逾時 → `plan_expired`，前端重新預覽（使用者再看一次）。 |
| **D-05-8** WS 協定 | — | 新 client 指令 `apply_edits {request_id, plan_id}`；新 server 事件 `edit_outcome {request_id, outcome, applied[], failed_change?, statement?}`。進度沿用 `affected`（index＝第幾句、rows＝符合列數）；錯誤沿用 `error`（server 原文與位置）；之後照舊 `session_state`、`finished`。COMMIT 之前可 Cancel（取消執行中的句子，例如等鎖），之後不行。沒有 timeout（使用者有 Cancel）。 |
| **D-05-9** History | — | 每次 apply（至少送出一句）寫一筆：SQL 是 plan 的 `script()`，status ok／error（unknown 為 error，訊息「Outcome unknown」）。History 本來就存使用者執行過的 SQL（含值）；事後想知道「昨天改了什麼」時有據可查（Q2）。 |
| **D-05-10** 暫存值怎麼顯示 | — | 用既有的 cell 編碼（`lib/cells.ts`）：文字、數字文字、日期、JSON → 字串（數字欄照樣靠右）；bool → boolean；binary → `{$type:"bytes", size, preview}`（由 hex 算出）；NULL → null；DEFAULT → null 加上 `default` 標記（畫成灰色 `DEFAULT`）。所以 copy、aggregate、值檢視器、record view 不必認識新的 cell 種類；複製 DEFAULT 得到空值（文件化）。 |
| **D-05-11** 上限 | — | 一次 commit 最多 10,000 個變更（列）；單值 1,048,576 字元（同參數草稿）；workspace 保存的暫存文字總計 8 MiB。⌘⌫ 一次超過上限：提示改用 SQL。 |
| **D-05-12** 重讀保留位置 | — | commit 後的重跑與一般的 Refresh：同樣的欄位時保留垂直捲動位置與選取，載入列數＝原本已載入的列數（≤ 10,000）。sort 與 filter 照舊從頂端開始。編輯流程常常是「Refresh 看別人的修改 → 繼續改」，跳回頂端會失去位置。 |
| **D-05-13** MySQL 反斜線 | — | target 讀 metadata 連線的 `@@SESSION.sql_mode`（session 與 metadata 連線套用同樣的 profile options）；含 `NO_BACKSLASH_ESCAPES` 時 literal 不把反斜線加倍。否則 `C:\tmp` 會被存成 `C:\\tmp`（靜默改值）。lexer 與 Filter by value 的既有限制不變（CLAUDE.md）。 |
| **D-05-14** 復原 | — | ⌘Z／⇧⌘Z 復原／重做暫存操作（一個使用者動作一步：改一格、對選取設 NULL、刪列、新增列、還原、全部捨棄、Keep mine），最多 100 步，只在記憶體；commit 成功後清空。reducer 是 immutable 的，歷史只是保留舊狀態。WKWebView 會不會把 ⌘Z 交給頁面要在打包版確認（A10）；可靠的路徑是右鍵的 Revert。 |
| **D-05-15** 計畫拆分 | 一份；兩份；三份 | **兩份**：05a backend、05b frontend（§11）。 |

## 3. 架構

### 3.1 模組總覽

```
backend/dbaguy_backend/
  domain/
    edits.py              EditKind、EditColumn、EditTarget、CellInput、RowUpdate／RowInsert／RowDelete、EditChanges、
                          EditProblem、PlannedStatement、EditPreview
    execution.py          ApplyEditsCommand（client）、EditOutcomeEvent（server）
    profiles.py           EngineInfo.row_editing
    workspace.py          WorkspaceTab.staged_edits（05b）
  adapters/
    base.py               AdapterCapabilities.row_editing；RowEditing（ISP）；StatementResult.matched_rows()
    postgres/editing.py   edit_target：pg_attribute、pg_index、pg_type
    mysql/editing.py      edit_target：information_schema COLUMNS／STATISTICS／TABLES／ENGINES、@@sql_mode
    mssql/editing.py      edit_target：sys.columns／sys.indexes／sys.index_columns／sys.types
    sqlite/editing.py     edit_target：pragma_table_xinfo／index_list／index_info
    mysql/adapter.py      _VerbatimCursor 保留 OK 封包的 info；MysqlResult.matched_rows()
  sql/
    literals.py           從 formatting.py 抽出：cell → literal、比對條件；輸入文字 → literal 與驗證
    dml.py                build_plan(dialect, target, changes) → PlanText（純函式）
    dialects.py           交易外框、DEFAULT VALUES 寫法、quote_literal 的反斜線選項
    formatting.py         改用 literals.py（行為不變，既有測試照跑）
  services/
    edit_service.py       target()、preview()、EditPlanStore
  sessions/
    worker.py             _apply_edits
    hooks.py              take_edit_plan
  api/
    edits.py              POST /edits/target、POST /edits/preview
src/
  stores/stagedEdits.ts   依 tab id 的暫存狀態（React 外，workspace writer 與 close guard 直接讀）
  features/edits/         staging.ts（純 reducer）、overlay.ts（綁定與顯示，純函式）、editsApi.ts、useTableEdits.ts、
                          CellEditor.tsx、ValueEditor.tsx、EditBar.tsx、PreviewDialog.tsx、editKeys.ts
  features/grid/          DataGrid、GridRow／GridCell 收標記；編輯器的位置；GridMenu／GridHeader 的編輯項目
  features/table-data/    TableDataTab 接上編輯；useTableData 的重跑保留位置與列數
  features/session/       SessionController.applyEdits；execution.ts 認得 edit_outcome
  features/workspace/     workspaceFile.ts（savedEdits）、closeGuards.ts（edits 順序）
  features/palette/       表格資料 tab 的編輯指令
```

不變的部分：`SessionWorker` 的分頁、暫停、取消與事件順序；`MetadataService` 與 metadata 連線；表格資料 tab 的 Count rows session；`SafetyPolicy` 的規則；cell 編碼；Couchbase 的一切。

### 3.2 可編輯的判斷：`EditTarget`

#### 3.2.1 介面

```python
EditKind = Literal["number", "text", "bool", "datetime", "binary", "json", "other"]

class EditColumn(BaseModel):
    name: str
    type_name: str                # 宣告的型別，例如 numeric(10,2)、varchar(20)、datetime2(7)
    kind: EditKind                # 由各 engine 既有的 category 對照表推得（DRY），再加 engine 的例外
    nullable: bool
    has_default: bool             # default、identity、auto_increment、generated 都算：INSERT 可以省略
    writable: bool
    read_only_reason: str | None  # 例如 "generated column"、"IDENTITY column"、"rowversion"
    compare: Literal["equals", "as_text", "none"]  # 原始值檢查的寫法；none：不檢查（§3.3.3）
    unicode: bool = True          # SQL Server：nchar／nvarchar／ntext 為 True，char／varchar／text 為 False

class EditTarget(BaseModel):
    path: list[PathSegment]
    editable: bool
    reason: str | None            # editable=False 時給使用者看的原因
    key: list[str]                # key 欄名，依 index 順序
    key_source: Literal["primary_key", "unique"] | None
    columns: list[EditColumn]
    transactional: bool = True    # MySQL：引擎支援交易
    backslash_escapes: bool = True  # MySQL：sql_mode 沒有 NO_BACKSLASH_ESCAPES

class RowEditing(ABC):            # 關聯式的 DatabaseConnection 實作；Couchbase 不實作
    @abstractmethod
    def edit_target(self, path: list[PathSegment]) -> EditTarget: ...
```

- `AdapterCapabilities.row_editing: bool = False`（進 `EngineInfo`）；PG、MySQL、SQL Server、SQLite 為 True。
- path 不是 table／partition（view、materialized view、其他物件）→ `editable=False`，`reason="Views are read-only here"` 等，不是錯誤（工具列要顯示原因）。
- 找不到 key → `editable=False`，`reason="No primary key or unique key on NOT NULL columns: a row cannot be identified"`；key 欄的 `compare` 是 `none`（浮點、讀取就失真的型別）→ `editable=False`，原因指出是哪一欄、為什麼（`as_text` 的欄當 key 沒問題：key 條件一律寫 `=`，§3.3.3）。個別的列若 key cell 在 grid 裡不完整（截斷的文字、只有預覽的 bytes），只有那一列唯讀（`key_incomplete`）。
- target 走 metadata 連線（`MetadataService.with_connection`，讀取，可重連），固定數量的 catalog 查詢；不快取在 backend（每次預覽重讀，DDL 之後不會用到舊資訊），前端以 TanStack Query 快取到 Refresh。

#### 3.2.2 各 engine

| | PostgreSQL | MySQL | SQL Server | SQLite |
|---|---|---|---|---|
| 可編輯的物件 | `relkind` r、p（含 partition） | `TABLE_TYPE = 'BASE TABLE'` | user table（`type = 'U'`） | `type = 'table'`，不含 virtual table |
| key | PK；否則 `indisunique`、無 `indpred`／`indexprs`、欄位全 NOT NULL、欄位最少的 index | `PRIMARY`；否則 `NON_UNIQUE = 0`、欄位全 NOT NULL、最少欄 | `is_primary_key`；否則 `is_unique`、`has_filter = 0`、欄位全 NOT NULL | `pk > 0` 的欄；否則 origin u／c、`partial = 0`、欄位全 NOT NULL 的 index。rowid 不用：`SELECT *` 不含它 |
| 不能寫 | `attgenerated <> ''`（〔實測〕428C9）、`attidentity = 'a'` | EXTRA 含 `GENERATED`（〔實測〕3105）、GEOMETRY／VECTOR | `is_computed`（271）、`is_identity`（8102）、rowversion（272）、`generated_always_type <> 0`（temporal）、sql_variant、hierarchyid、geometry、geography（〔實測〕三個錯誤碼） | `hidden` 2／3（generated，〔實測〕「cannot UPDATE generated column」） |
| `compare = none` | float4、float8 | FLOAT、DOUBLE | float、real、datetime2／datetimeoffset／time 的 scale > 6、text、ntext、image、xml（〔實測〕402）、sql_variant、CLR 型別 | REAL affinity |
| `compare` 其他 | `as_text`，`bpchar` 為 `equals` | `equals` | `equals` | `equals` |
| 交易 | 永遠是 | `ENGINES.TRANSACTIONS = 'YES'` | 永遠是 | 永遠是 |
| 特例 | money → `other`（輸入依 lc_monetary） | `@@sql_mode` → `backslash_escapes` | `unicode` 依型別 | kind 依宣告型別的 affinity：INTEGER／REAL／NUMERIC → number、TEXT → text、BLOB → binary、沒有宣告 → other |

浮點一律不比（四個 engine 同一條規則，Q5）：〔實測〕MySQL `FLOAT = 1.1` 對不上；PG 的 `real = '1.1'` 其實對得上，但規則一致比多省一次比較重要。SQL Server datetime2(7)〔實測〕：driver 只到微秒，拿讀到的值比對是 0 列。

### 3.3 DML 產生（`sql/dml.py`、`sql/literals.py`）

#### 3.3.1 輸入

```python
class CellInput(BaseModel):          # 一個 cell 的新值
    kind: Literal["value", "null", "default"]
    text: str | None = Field(default=None, max_length=1_048_576)   # kind="value" 時必填

class RowUpdate(BaseModel):  id: str; key: dict[str, JsonValue]; set: dict[str, CellInput]; original: dict[str, JsonValue]
class RowInsert(BaseModel):  id: str; values: dict[str, CellInput]
class RowDelete(BaseModel):  id: str; key: dict[str, JsonValue]
class EditChanges(BaseModel): updates: list[RowUpdate] = []; inserts: list[RowInsert] = []; deletes: list[RowDelete] = []
```

- `key` 與 `original` 是 grid 手上的編碼 cell（`encode_value` 的輸出），由 `literals.py` 的 normaliser 讀（與 `value_condition` 同一個）。被截斷的文字、不完整的 bytes 不能當 key 或原始值（`key_incomplete`；原始值則該欄 `compare` 視為 none 並列入未檢查）。
- 變更總數 ≤ 10,000；`id` 由前端產生（`u:`／`i:`／`d:` 前綴），backend 只回傳不解讀。

#### 3.3.2 句子

```
UPDATE <table> SET <c1> = <新值>, <c2> = <新值> WHERE <k> = <key> [AND <k2> = <key2>] AND <c1 的原始值檢查> AND <c2 的原始值檢查>
INSERT INTO <table> (<c1>, <c2>) VALUES (<新值>, <新值>)        -- 全部 DEFAULT：DEFAULT VALUES（MySQL：() VALUES ()）
DELETE FROM <table> WHERE <k> = <key> [AND …]
```

`<table>` 是 `qualified(dialect, schema, name)`（只在需要時加引號，與瀏覽的 SELECT 相同）；PG 與 SQL Server 是 session 的 database 內的 schema.name。原始值是 NULL 時檢查寫 `c IS NULL`。

#### 3.3.3 literal 與比對（`EditKind` × dialect）

新值（SET、VALUES）：

| kind | PostgreSQL | MySQL | SQL Server | SQLite |
|---|---|---|---|---|
| number | 數字原樣；`NaN`／`Infinity`／`-Infinity` → `'NaN'` 等 | 數字原樣 | 數字原樣 | 數字原樣 |
| text、other | `quote_literal` | `quote_literal`（反斜線依 `backslash_escapes`） | `N'…'` | `'…'`；other（無 affinity）的值是數字字樣 → 數字 |
| bool | `TRUE`／`FALSE` | `TRUE`／`FALSE` | `1`／`0` | `1`／`0` |
| datetime | `'…'` | `'…'` | `datetime_literal`（`N'…T…'`，dmy 語系不誤讀） | `'…'` |
| binary | `'\x…'::bytea` | `X'…'` | `0x…` | `X'…'` |
| json | `'…'`（json／jsonb 由欄位解析） | `'…'`（指定給 JSON 欄時 server 解析） | `N'…'` | `'…'` |

原始值檢查與 key 條件（由 `value_condition` 的同一套 normaliser 與 literal，`literals.match_condition`）：

| | PostgreSQL | MySQL | SQL Server | SQLite |
|---|---|---|---|---|
| key | `k = <literal>` | `k = <literal>` | `k = <literal>`；非 Unicode 欄且 ASCII → `'…'` | `k = <literal>` |
| 原始值 `equals` | （bpchar）`c = <literal>` | `c = <literal>`；JSON `c = CAST('…' AS JSON)`；零日期 `CAST(c AS CHAR) = '…'` | 同 key 的寫法 | 依原始 cell 型別：數字不加引號、字串加引號 |
| 原始值 `as_text` | `c::text = '<原文>'`；bool 寫 `'true'`／`'false'`、bytea 寫 `'\x…'` | — | — | — |

〔實測〕PG：除了 `json`、`xml`、`point`（`=` 回 42883），每種型別 `c = '原文'` 都對得上；`::text` 對每種型別都對得上，`bpchar` 除外（`'ab   '::text` 去掉尾端空白）。`as_text` 比 `=` 嚴格：citext 的大小寫、interval 的 `1 day` 與 `24 hours`（`=` 認為相等，〔實測〕）、jsonb 的重排都會被當成修改，這是並行檢查要的方向。MySQL 與 SQL Server 的 `=` 依欄位 collation：大小寫不分的 collation 下，只改大小寫的並行修改抓不到（與 Filter by value 相同的已知限制）。

驗證（預覽時，`EditProblem{change_id, column, code, message}`）：`invalid_number`、`invalid_json`、`invalid_hex`、`invalid_bool`、`invalid_text`（PG 的 U+0000；任何 engine 的孤立 surrogate；`quote_literal` 已把其他 engine 的 NUL 寫成跳脫）、`value_too_large`、`read_only_column`、`unknown_column`、`not_nullable`（對 NOT NULL 欄設 NULL）、`key_incomplete`。有任何問題就不建 plan（`plan_id=null`），問題回到對應的 cell。

預覽警告（不擋）：`unchecked_columns`（逐句，列出沒有檢查並行修改的欄與原因）、`non_transactional`、`missing_required`（新列缺了 NOT NULL、沒有 default、不是 generated 的欄：server 多半會拒絕，但 trigger 可能補上，所以只警告）。

### 3.4 預覽（`services/edit_service.py`、`api/edits.py`）

| endpoint | 輸入 | 輸出 |
|---|---|---|
| `POST /edits/target` | `profile_id, path` | `EditTarget` |
| `POST /edits/preview` | `profile_id, path, changes` | `EditPreview{plan_id \| null, begin, statements: PlannedStatement[], commit, problems, warnings, counts{updates, inserts, deletes}, script}` |

- `PlannedStatement{change_id, kind: update\|insert\|delete, sql, unchecked: [{column, reason}]}`；`begin`／`commit` 是交易外框的原文；`script` 是 Copy 用的整份文字（句子之間 `;` 與換行；T-SQL 用 `GO` 行）。
- 唯讀 profile → 403 `read_only`；engine 沒有 `row_editing` 或 target 不可編輯 → 422 `not_editable`（訊息是 target 的原因）；密碼不在 → `password_required`（前端開 unlock 對話框後重試預覽，同 04c C31）。
- target 讀取（I/O）在 metadata worker，產生與驗證（純函式）在 request 執行緒（同 04c C43：一萬個變更的檢查不擋住樹與補全）。
- 預覽不寫入任何東西，可以重複呼叫。

### 3.5 套用（`sessions/worker.py`）

`_apply_edits(cmd)`，一個 worker task，依序：

1. `_discard_open()`：結束暫停中的瀏覽結果（它的 `finished` 照常送出，grid 會標 partial 直到重跑）。`_start_request`。
2. `plan = hooks.take_edit_plan(plan_id)`；沒有 → `plan_expired`；profile 或 database 不符 → `plan_mismatch`。
3. 每句 DML 經 `hooks.check(sql, confirmed=True)`：BLOCK → `read_only`，什麼都不送。
4. `connection.in_transaction()` 為真 → `transaction_open`（表格資料 tab 不會發生，防禦用）。
5. 送 `begin`（經 `_run_statement`，關掉它的結果）。
6. 逐句：`_run_statement` → `matched_rows()` → 送 `affected(index=i, rows=n)` → 依 R-05-5 判斷；句子之間 `_raise_if_cancelled()`。
7. COMMIT 前：`_set_running(None)`（之後的 Cancel 不再碰 driver；它回傳的「途中有沒有 Cancel」為真就改成 rollback），再確認 `in_transaction()` 仍為真（不是的話交易已被 server 結束而沒有錯誤——不該發生——結果算 unknown）。
8. 送 `commit`。

結果分類：

| 情況 | outcome | 暫存 |
|---|---|---|
| COMMIT 成功 | committed，`applied`＝全部 | 全部移除，歷史清空，重跑瀏覽 |
| 第 i 句錯誤、衝突（UPDATE／INSERT ≠ 1、DELETE > 1）、讀不到列數、取消、COMMIT 之前斷線 | rolled_back，`failed_change`、`statement`＝i | 全部保留，失敗的那個標紅 |
| COMMIT 回錯誤（不是斷線） | rolled_back（`statement`＝null） | 全部保留 |
| `transactional=False`（MySQL MyISAM 等）：第 i 句失敗（錯誤、衝突、讀不到列數、取消） | partial，`applied`＝i 之前的 | `applied` 移除，其餘保留 |
| `transactional=False`：第 i 句執行中斷線 | unknown，`applied`＝i 之前的（第 i 句不明） | `applied` 移除，其餘保留，Commit 停用到 Refresh |
| COMMIT 送出後斷線或 worker 例外 | unknown | 保留，Commit 停用到 Refresh |

- 失敗時用 `connection.rollback()`；rollback 本身因斷線失敗仍是 rolled_back（server 在連線結束時 rollback）。
- 句子的錯誤經 `error` 送出（server 原文、code、位置；位置相對於那一句），再送 `edit_outcome`。
- `_listen(request_id)`：trigger 的 NOTICE／PRINT 照常進這個請求的 messages，對話框顯示。
- History：D-05-9。
- 事件順序：`started` → `affected`×n → [`error`] → `edit_outcome` → `session_state` → `finished`。

### 3.6 Frontend

#### 3.6.1 暫存狀態（`features/edits/staging.ts`，純 reducer，Vitest）

```ts
type CellInput = { kind: "value"; text: string } | { kind: "null" } | { kind: "default" };
interface StagedUpdate { id: string; key: Record<string, unknown>; set: Record<string, CellInput>; original: Record<string, unknown>; seq: number }
interface StagedInsert { id: string; values: Record<string, CellInput>; seq: number }
interface StagedDelete { id: string; key: Record<string, unknown>; seq: number }
interface StagedEdits {
  keyColumns: string[];
  updates: Record<RowKey, StagedUpdate>;   // RowKey：key cell 依 key 順序的 JSON 文字
  inserts: StagedInsert[];
  deletes: Record<RowKey, StagedDelete>;
  problems: Record<string, EditProblem[]>; // 上次預覽或 apply 指出的，依 change id
  outcomeUnknown: boolean;                 // R-05-6：Commit 停用到 Refresh
  past: Snapshot[]; future: Snapshot[];    // D-05-14，最多 100
}
```

操作：`edit`（輸入等於原始值時移除該欄的修改：沒有 no-op 的 UPDATE）、`setNull`（對選取的多格）、`useDefault`（新列）、`revertCell`、`revertRow`、`deleteRows`（新列直接移除；已全部標刪的選取再按一次 → 還原）、`restoreRows`、`addRow`、`discardAll`、`keepMine`（把原始值換成 server 現在的值；換完若與新值相同就變成 no-op 而移除）、`applyOutcome`（移除 `applied`、記 problems、unknown）、`refreshed`（清 unknown）、`undo`、`redo`。標刪的列保留它的 UPDATE（隱藏），還原時回來；plan 只送 DELETE。

`stores/stagedEdits.ts`：依 tab id 存放（同 `editorBuffers` 的理由：workspace writer 與 close guard 不必等元件掛上就能讀）；變更時通知 workspace writer。

#### 3.6.2 overlay 與標記（`features/edits/overlay.ts`，純函式，Vitest）

- `bindRows(rows, columns, keyColumns)` → RowKey → 列號：只在有暫存時算；新結果算一次，Load more 只算新加的頁（O(頁)）。
- `overlay(rows, staged, binding)` → 顯示用的 `rows`（新列在最前；有修改的列換成合併後的新陣列，其他列沿用原本的物件，所以 `GridRow` 的 memo 照樣命中）、每列的標記、每格的標記。只在 `rows` 或暫存改變時重算；只複製 row 參照的陣列，不複製 cell。
- 標記（顏色 token，深淺主題都有）：改過的 cell → `warning` 淡底，tooltip「Was: <原始值>」；新列 → `success` 淡底，列號顯示 `+`；標刪的列 → `danger` 淡底加刪除線，列號 `−`；DEFAULT → 灰色斜體 `DEFAULT`；預覽或 apply 指出的問題 → `danger` 外框；衝突（目前的值 ≠ 暫存的原始值，Refresh 後才看得到）→ `danger` 角標，tooltip「Changed on the server: now <值>」，選單「Keep mine」；key 欄的表頭有鑰匙圖示，不能寫的欄表頭有說明 tooltip。
- 編輯列（`EditBar`）的計數包含不在目前結果裡的暫存（「5 changes · 2 in rows not shown」）。

#### 3.6.3 編輯器

- **inline**（`CellEditor`）：真正的 `<input>`，蓋在 cell 上，寬度不小於 cell、最多 480 px；number、text（單行且 ≤ 1,000 字元）、datetime、bool、binary（hex）、other。開始時是目前顯示的文字（NULL 為空並以 placeholder `NULL` 提示，DEFAULT 同）。
- **drawer**（`ValueEditor`：可編輯的 grid 裡，Return 對這類 cell 開 drawer 的編輯模式；唯讀的值檢視器仍在選單）：CodeMirror，JSON 欄用 `@codemirror/lang-json`（04c 已有），其他是純文字；給 json kind、含換行或超過 1,000 字元的文字。按鈕：Apply（⌘↵）、Set NULL、Cancel（Esc）；顯示「Was: …」。
- 讀不出完整值的 cell（截斷的文字）：Return 提示「This value is too large to edit here (N characters); Set NULL or use SQL」。不能寫的欄：Return 提示原因。
- 失焦（點別處）時，有改就暫存（同 Numbers）；Esc 是唯一的取消。
- 新列：Add row 之後焦點在第一個可寫的欄並開啟編輯器。

#### 3.6.4 按鍵（可編輯的表格資料 grid；唯讀 grid 與 query 結果不變）

| 鍵 | 動作 |
|---|---|
| Return、F2、雙擊 | 編輯目前的 cell（inline 或 drawer）；唯讀 grid 照舊 Return 開值檢視器 |
| （編輯中）Return | 暫存並關閉編輯器，目前的 cell 不移動 |
| （編輯中）Tab／⇧Tab | 暫存並編輯同一列下一個／上一個可寫的欄 |
| （編輯中）Esc | 放棄這次輸入 |
| （drawer 中）⌘↵ | 暫存（Return 是換行） |
| ⌥⌘N | 選取範圍內可寫的 cell 設 NULL（NOT NULL 欄提示並略過）；編輯中也可用 |
| ⌘⌫ | 選取範圍碰到的列標記刪除；全部已標刪時再按一次 → 還原 |
| ⌘Z／⇧⌘Z | 復原／重做暫存操作（不在編輯器內時） |
| ⌘S | 有暫存：開預覽；沒有：toast「No changes to commit」 |
| Space | 照舊開 record view |

所有 Return／Tab／Esc／⌘S 處理先過 `isImeKeyEvent`：組字中的 Enter、WebKit 晚到的 keyCode 229 Enter 都不暫存、不開預覽。右鍵選單：Edit value（↵）、Set NULL（⌥⌘N）、Use default（新列）、Revert cell、Revert row、Delete rows（⌘⌫）／Restore rows、Keep mine（衝突）、Add row；View value 留在選單（不再綁 ↵）。⌘⇧P 指令：Add row、Delete selected rows、Set NULL、Commit changes…、Discard all changes。Add row 沒有預設快捷鍵：編輯列的按鈕與 ⌘⇧P 已涵蓋（M3「每個指令都能用鍵盤」），快捷鍵留到 §12。

#### 3.6.5 編輯列與預覽對話框

- 編輯列放在 Data 檢視的工具列右側：可編輯時有「+ Row」；有暫存時加上「N changes」（`warning`）、「Commit… ⌘S」、「Discard…」（超過一個變更時先確認，⌘Z 可復原）。不可編輯時是灰色的「Read-only · <原因>」（tooltip 為完整原因）。正在匯出、瀏覽中（stale）、session 不是 open：Commit 停用並說明。
- **PreviewDialog**：標題「Commit changes to <schema.table>」；頂端是連線名稱與顏色（紅色連線的 Commit 按鈕用 danger 樣式）、計數（刪除排第一、`danger` 色）、警告（非交易引擎、未檢查的欄、上次結果不明、缺少必填欄）；中間是唯讀 CodeMirror（SQL 上色），內容就是 `begin`、各句、`commit` 的原文，每句前的 gutter 標 U／I／D；Copy（`script`）。底部說明「Runs in one transaction: if any statement fails or does not match exactly one row, nothing is applied」（非交易引擎改成對應的說明）。Esc 取消；Commit 是預設按鈕。有 `problems` 時不顯示 Commit，只列問題並可「Show in grid」。
- 按下 Commit：對話框變成進度（「Committing… statement 3 of 12」，來自 `affected`）與 Cancel（COMMIT 送出後隱藏）。
- 結果：committed → 關閉對話框、toast「Committed 4 changes to orders」（含 DELETE 0 列的「1 row was already gone」、新列可能不在已載入範圍的說明），重跑瀏覽（D-05-12）；rolled_back → 對話框顯示 server 錯誤原文與失敗的那一句（標紅）、「Nothing was applied」，按鈕 Show in grid（那一列不在目前結果裡時 toast 說明）、Close；partial → 說明哪些已生效；unknown → 「The connection was lost while committing: the changes may or may not have been applied. Refresh to see the table as it is now」，按鈕 Refresh、Close。trigger 的訊息列在下方。
- 重跑時機：committed、partial 一定重跑；rolled_back 只在瀏覽結果原本是暫停的（被 apply 結束）時重跑，暫存依 key 綁回；unknown 等使用者按 Refresh（重跑完成後才清掉 `outcomeUnknown`）。

#### 3.6.6 跟既有行為的差異

- 可編輯的 grid 裡 Return 改成編輯（值檢視器移到選單）。
- Refresh 保留位置與已載入列數（D-05-12）。
- `SessionController.applyEdits(planId)`，`ExecutionKind` 加 `"edits"`，`busy()` 涵蓋它；`execution.ts` 收 `edit_outcome`。
- `CLOSE_GUARD_ORDER.edits = 5`，只在關閉時問（斷線時 tab 與暫存都留著）。
- 匯出對話框在有暫存時多一行「Staged changes are not committed and are not in the export」。
- Filter by this value 用在改過的 cell 上時，用原始（server）的值；新列沒有這個項目。

### 3.7 Workspace（05b）

- `WorkspaceTab.staged_edits: StagedEditsFile | None`（只給 kind "table"）：`{version: 1, key_columns, updates[{id, key, set, original}], inserts[{id, values}], deletes[{id, key}]}`，`extra="forbid"`，上限同 D-05-11；cell 是任意 JSON 值（有大小上限）。
- 前端 `savedEdits()`（同 `savedParams`）只寫 backend 會收的內容；超過上限就整個不寫，編輯列提示「Too large to keep across a restart」。
- 還原：放進 `stores/stagedEdits.ts`；Data 檢視載入後依 key 綁回；欄位已不存在的修改在下次預覽變成 `unknown_column`，使用者還原它即可。

## 4. 錯誤處理

| 情況 | 型別／code | 訊息 |
|---|---|---|
| 唯讀連線（預覽） | 403 `read_only` | `This connection is read-only` |
| 唯讀連線（apply，policy） | `read_only` | 既有的 `_read_only_message` |
| 表不能編輯 | 422 `not_editable` | target 的原因（view、沒有 key、key 欄比不了…） |
| 值不合法 | 預覽的 `problems` | 例如 `Not a number`、`Not valid JSON at line 3, column 7`、`Not hex digits`；固定文字，不回顯 SQL |
| plan 過期／不符 | `plan_expired`／`plan_mismatch` | `The preview has expired: preview the changes again` |
| 已有交易 | `transaction_open` | 既有文字 |
| UPDATE 0 列 | `edit_conflict` | `Row <key> was changed or deleted since it was loaded (or a trigger skipped it): nothing was applied` |
| > 1 列 | `edit_conflict` | `Row <key> matched N rows: its key is not unique. Nothing was applied` |
| 讀不到列數 | `row_count_unknown` | `Could not confirm how many rows changed: nothing was applied` |
| server 錯誤 | server 的 code 與位置 | server 原文（M10），對話框標出那一句 |
| 取消 | `cancelled` | `Cancelled: nothing was applied` |
| COMMIT 失敗 | server 的 code | 原文＋「The transaction was rolled back: nothing was applied」 |
| 結果不明 | `outcome_unknown`（`edit_outcome`） | §3.6.5 的文字 |

訊息裡的 `<key>` 是 key 欄的名稱與值（給使用者看，不進 log）。DbaGuy 自己的錯誤是固定文字，不回顯 SQL。

## 5. Log（loguru）

| 等級 | 內容 |
|---|---|
| INFO | 預覽：profile、資料表（schema.name）、計數、問題數、ms；apply：計數、outcome、ms；衝突：第幾句、類別（不含 key 值） |
| WARNING | unknown、partial、讀不到列數 |
| DEBUG | 每句 SQL（含值，同 worker 既有規則：只有明確開 DEBUG 才進檔案） |
| 永不 | INFO 以上的值、key 值、server 錯誤原文（會回顯常值；同 `QueryFailed` 的規則只記 code） |

## 6. 測試（依「只測容易出錯的邏輯」）

- **單元（pytest）**：`sql/dml.py` 與 `sql/literals.py` 每個 dialect × kind 的 literal、key 與原始值條件（PG `::text`／bpchar／bool／bytea；MySQL JSON CAST、零日期、`backslash_escapes`；SQL Server `N` 與 ASCII 規則、`T` 日期；SQLite 依儲存型別）、DEFAULT VALUES、句子順序、每種 problem 與 warning、上限；`formatting.py` 既有測試不改照過（抽出的回歸保護）；plan store（單次、TTL、LRU、profile／database 綁定）；MySQL info 的 `Rows matched` 解析；worker 的結果分類（用 fake connection：句子錯誤、0 列、> 1 列、讀不到列數、COMMIT 前取消、COMMIT 回錯誤、COMMIT 斷線 → unknown、非交易 → partial、非交易中途斷線 → unknown、唯讀 BLOCK、plan 過期、事件順序）。
- **整合（dev DB，每個 engine 自己的 `zz_` scratch 表，測完以名稱 DROP）**：`edit_target`（PK、唯一鍵、沒有 key、view、generated／identity／computed／rowversion、各種 `compare`、MyISAM、`@@sql_mode`）；往返（大整數、decimal、日期與零日期、datetime2(7) 不檢查、PG json／xml／point 的 `::text`、bytea／varbinary hex、NULL 與空字串、DEFAULT）；衝突與 rollback（另一條連線先改）；中途錯誤 rollback；MyISAM partial；PG deferred FK 在 COMMIT 失敗；SQL Server AFTER trigger 的列數、tx-dooming 錯誤（8114）；PG BEFORE trigger 跳過；唯讀（worker 與 MySQL 1792）；等鎖時 Cancel → rollback；暫停的瀏覽結果被結束後 apply。
- **前端（Vitest）**：reducer 每個操作與 undo／redo、keepMine、applyOutcome；overlay 的綁定（含 Load more 只算新頁）、標記、不複製 cell；`savedEdits` 的上限；close guard 的計數。
- **手動（`scripts/dev.sh --web` 與內建瀏覽器）**：每份 plan 的清單；注音與打包版的項目列進給使用者的最終測試清單（§11）。

## 7. 效能、打包與量測

- 改一格：reducer O(1)、overlay O(列數) 的參照陣列加上被改的那一列；`GridRow` 的 memo 讓只有被改的列重畫（Q1）。綁定只在有暫存時算，Load more 只算新的一頁。
- 預覽：1,000 個變更的產生與驗證 < 300 ms（本機量）。
- apply：每句一個往返（SQL Server 的 `run()` 另有狀態讀取與 describe，CLAUDE.md 已知）。05a 量本機 1,000 句 UPDATE 的時間（A7），預算 PG／MySQL／SQLite ≤ 3 秒、SQL Server ≤ 10 秒；進度在對話框。
- 打包：沒有新的 Python 或前端相依（`@codemirror/lang-json` 04c 已有）；sidecar 大小不變。

## 8. 痛點對照

| 需求 | 這個子專案怎麼解 | 踩到／退步 |
|---|---|---|
| Q1 輕快低記憶體 | overlay 不複製結果；只重畫被改的列；0 新相依 | commit 後重跑瀏覽（最多 10,000 列）；SQL Server 每句多兩個往返 |
| Q2 穩定不丟工作 | 暫存進 workspace（當機、重開都在）；close guard；commit 失敗與不明時暫存都留著；不自動重送 | 超過 8 MiB 的暫存不跨重開（有提示）；undo 歷史不跨重開 |
| Q3 可信任 | 送出的就是預覽的；樂觀並行檢查；exactly-1；結果四分類；commit 後顯示 server 真正存的值 | 浮點、datetime2(7)、text／xml 等不檢查（預覽逐句列出）；MySQL／SQL Server 依 collation 比較 |
| Q4 聚焦 | 只做表格資料 tab；不做 query 結果、view、貼上、type-to-edit、只能新增的模式 | query 結果裡的值要先開表 |
| Q5 多引擎 | 四個關聯式 engine 同一套流程與規則；engine 差異只在 `edit_target` 與 literal 表 | Couchbase 走 04c（不同的資料模型） |
| Q6 macOS 慣例 | Return 編輯、Esc 取消、⌘S 預覽、⌘⌫ 刪除、⌘Z 復原 | ⌘Z 在打包版要確認（A10） |
| S1（Must，FP §8） | 暫存 → 預覽 DML → 一次 commit | — |
| M7 值的正確 | NULL ≠ 空字串、數字以文字、大整數／decimal／日期／UUID／JSON 原樣、binary hex、DEFAULT | 超過 64 KiB 的值不能編輯 |
| M11 安全與交易 | 一個交易、唯讀雙層、production 顏色進對話框、刪除排在預覽第一 | SQL Server 唯讀只在應用層（既有） |
| M10 錯誤 | server 原文、標出是哪一句、哪一格 | — |

## 9. 風險

| 風險 | 處理 |
|---|---|
| 公司的 MySQL 用 `NO_BACKSLASH_ESCAPES` 或非預設 sql_mode | D-05-13；A9 驗證；Filter by value 的既有限制照舊 |
| SQL Server server 預設 NOCOUNT ON、INSTEAD OF trigger | `SET NOCOUNT OFF` 外框；讀不到或不對的列數 fail closed（A2、A3） |
| 大表的 MySQL 暫停結果結束前要 drain，apply 前等好幾秒 | 對話框顯示「Committing…」；A6 量；與 sort／filter 現在的等待相同 |
| 一萬句在 SQL Server 上很慢 | 進度與 Cancel；A7 量；之後可合併批次（§12） |
| 唯一值互換、key 欄互換 | 已知限制，預覽前無法預知；錯誤原文＋rollback |
| WKWebView 把 ⌘Z／⌘⌫／⌥⌘N 先交給 Edit 選單 | A10；選單與 ⌘⇧P 是可靠的路徑 |
| collation 讓只改大小寫的並行修改漏掉（MySQL、SQL Server） | 文件化；§12 留 binary collation 比較 |
| 使用者在 grid 外（別的工具）同時改同一列的同一欄 | 這正是 R-05-5 要抓的；抓到就整批 rollback，Refresh 後 Keep mine |

## 10. 未實測的假設

| # | 假設 | 驗證 |
|---|---|---|
| A1 | `_VerbatimCursor` 送出時保留 `cmd_query` 的 dict，就拿得到 `info_msg` 的 `Rows matched`（〔實測〕OK 封包裡有：`Rows matched: 1  Changed: 0`） | 05a T1 |
| A2 | SQL Server 的 session 被 `user options` 設成 NOCOUNT ON 時 `rows_affected` 是 -1，`SET NOCOUNT OFF;` 外框讓它恢復 | 05a T1 |
| A3 | SQL Server INSTEAD OF trigger 的表的列數（AFTER trigger〔實測〕正確） | 05a T1；不對就列為已知限制並 fail closed |
| A4 | PG 每種 text-first 型別的 `::text` 與載入的文字逐字相同（range、timetz、money、interval、composite 陣列、domain、citext、時區設定） | 05a 整合 |
| A5 | `begin`／`commit` 經各 adapter 的 `run()` 後狀態一致：psycopg 的 transaction status、SQL Server 的 `@@TRANCOUNT` 快取、MySQL 的 flag、SQLite 的 cancel gate | 05a T1 |
| A6 | 結束大表（100 萬列）的暫停瀏覽結果再 apply 的等待時間，各 engine | 05a 量 |
| A7 | 本機 1,000 句 UPDATE 的時間（§7 的預算） | 05a 量 |
| A8 | SQL Server SQL collation 下 varchar key 用 `'…'`（不加 N）是 seek（〔實測〕`N'…'` 是 scan；plain 的 SHOWPLAN_TEXT 只有一個 Clustered Index Update 運算子，沒看到 Scan） | 05a T1 以 SHOWPLAN_XML 確認 |
| A9 | `NO_BACKSLASH_ESCAPES` 下不加倍反斜線的 literal 存回原值 | 05a 整合（session 設 sql_mode 的 scratch 測試） |
| A10 | 打包版 WKWebView 在預設的 Edit 選單下，grid 有焦點時 ⌘Z、⇧⌘Z、⌘⌫、⌥⌘N、F2 會到頁面 | 05b 打包版檢查（給使用者的清單） |
| A11 | inline `<input>` 在 WKWebView 的注音：選字的 Enter 帶 `isComposing` 或 keyCode 229（R-02c-9 的規則涵蓋） | 05b 手動（使用者清單） |
| A12 | 以「已載入列數」重跑並保留捲動位置時畫面不閃、選取保留 | 05b |
| A13 | PG serialization／deadlock 在 COMMIT 回錯誤後是 rolled back（deferred FK〔實測〕23503 後狀態 IDLE） | 05a 整合 |
| A14 | SQLite `BEGIN IMMEDIATE` 遇到 Count rows session 正在讀：busy timeout 內等到或失敗為 rolled_back | 05a 整合 |
| A15 | SQL Server 讓交易結束的錯誤（〔實測〕8114 後 `@@TRANCOUNT` 0）之後 adapter 的 `rollback()` 不出錯、outcome 為 rolled_back | 05a 整合 |

## 11. 執行計畫拆分

一個 branch `feat/05-grid-editing`，兩份 plan 依序執行、各自可合併驗收：

1. **05a：backend（編輯的契約）**
   - T1 probe：A1、A2、A3、A5、A8。
   - `sql/literals.py` 從 `formatting.py` 抽出（既有測試全過）；`sql/dml.py`；dialects 的交易外框、DEFAULT VALUES、反斜線選項。
   - `domain/edits.py`；`RowEditing` 與能力 `row_editing`；四個 engine 的 `edit_target`；`matched_rows()`（MySQL 的 info）。
   - `EditService`、plan store、`/edits/target`、`/edits/preview`。
   - worker `_apply_edits`、`ApplyEditsCommand`、`EditOutcomeEvent`、hooks、History；`npm run gen:api`。
   - 單元與整合測試；A4、A6、A7、A9、A13–A15 的量測與驗證；打包後用凍結的 sidecar 對每個 engine 跑一次預覽＋apply。
   - **結束時**：用 API 與 WS 能對四個 engine 預覽並 commit，結果四分類都有測試；前端還沒有入口。
2. **05b：frontend（編輯體驗）**
   - reducer 與 store（Vitest）、overlay 與綁定（Vitest）。
   - DataGrid／GridRow／GridCell 的標記、編輯器的位置、表頭、選單；inline 與 drawer 編輯器；按鍵與 IME。
   - 編輯列、唯讀原因、⌘⇧P 指令；PreviewDialog、apply、結果與 Show in grid；重跑保留位置與列數。
   - workspace 的 `staged_edits`（backend model 與 `savedEdits`）、還原綁定、close guard、匯出對話框的提示。
   - 手動檢查（每個 engine）、給使用者的最終測試清單（含注音、打包版 A10）。

**為什麼兩份**：backend 的風險是各 engine 的 SQL 與交易語意，全部能用整合測試驗，不需要 UI，先定下 API 讓前端在穩定的契約上做（同 04a／04b 的順序）；前端的風險是 grid 的互動與 IME，需要手動檢查。05b 預估 10–12 個 task（與 04c 相當）；若 plan 時超過 12 個，把「workspace、還原、close guard、undo／redo」拆成 05c。

## 12. 細化與意見

對 coordinator 問題的細化：
- **exactly-1 的例外**：DELETE 0 列不算失敗（R-05-5，同 04c C46）；MySQL 的「列數」是符合的列數，不是改變的列數（D-05-5）。
- **影響列數讀不到時 fail closed**，不猜。
- **INSERT 之後顯示 server 的 default 與 identity**：不用 RETURNING／OUTPUT，而是重跑瀏覽（R-05-10）；理由是 SQL Server 有 trigger 時不能 `OUTPUT`、MySQL 只拿得到 auto_increment，而且重跑本來就必要。
- **原始值比對的方向**：PG 用 `::text` 是刻意選「比 `=` 嚴格」：寧可多一次假衝突（Refresh、Keep mine），也不漏掉一次真的覆蓋。

之後（不在 ⑤）：
- **query 結果的編輯**：PG 用 `ftable`／`ftablecol`；MySQL 在 `MySQLProtocol.parse_column` 掛 hook 取 `org_table`／`org_name`／`db`；SQL Server 用 `sys.dm_exec_describe_first_result_set(…, 1)` 的來源與 `is_part_of_unique_key`（注意它加上的隱藏 rowversion 欄）；SQLite 沒有來源資訊，只能從單表 SELECT 推斷或不支援。前提：結果含完整 key、只有一個來源表、query tab 在手動交易中的語意要另外決定。可沿用 ⑤ 的 plan／apply／結果分類。
- 多格貼上（TSV）、grid 上 ⌘V；打字即編輯（隱藏的 input 接住 IME）；Add row 的快捷鍵。
- 沒有 key 的表的「只能新增」模式；讀完整值以編輯超過 64 KiB 的值；enum／bool／FK 的選單；record view 內編輯；檔案載入 binary。
- 更強的並行檢查：SQL Server rowversion、PG `xmin`；MySQL／SQL Server 用 binary collation 比較（需依欄位字元集選 collation）。
- SQL Server 合併批次以加快大量變更（每句仍要自己的列數）。

## 13. 收尾時要更新的文件

- `CLAUDE.md`：Repo 結構加 `features/edits/`、`stores/stagedEdits.ts`、`sql/dml.py`、`sql/literals.py`、`adapters/*/editing.py`、`services/edit_service.py`、`api/edits.py`；架構重點加「Grid 內編輯」一段（表格資料 tab、key、overlay、plan、tab 自己的 session 與明確交易、exactly-1、結果四分類、PG `::text`、MySQL 符合列數、SQL Server 外框與 ASCII literal、重跑保留位置、workspace 暫存與 close guard）；已知限制（浮點等不檢查、collation、唯一值互換、MyISAM partial、64 KiB、不做 query 結果）；進度表 ⑤。
- 每份 plan 的 followups note（`docs/superpowers/notes/`），含給使用者的最終測試清單：四個 engine 的改、增、刪與 commit；衝突與 Keep mine；Refresh 保留位置；唯讀與沒有 key 的表；重開 app 後暫存還在；注音；打包版的 ⌘Z／⌘⌫／⌥⌘N；公司資料庫的 sql_mode、collation、trigger、MyISAM。

## 附錄 A：探測紀錄（2026-10-19，dev 資料庫）

腳本在本次 session 的 scratch 目錄（session 結束後可能清除），結論如下。

**PostgreSQL 17.11／psycopg 3.3.6**
- 結果的來源：`SELECT * FROM t` 每欄 `(ftable=t 的 OID, ftablecol=欄號)`；`t.i4 + 1 AS e` 為 `(0, 0)`；別名 `t.txt AS alias` 指回 `txt`；JOIN 兩張表各自正確；從 view 讀為 view 的 OID。
- text-first 載入的值（與 adapter 相同的 loader）：`int8`、`numeric`、`timestamptz`、`date`、`json`（保留原文空白）、`jsonb`（server 正規化）、`uuid`、陣列、`xml`、`interval`、`point`、`char(5)`（`'ab   '`）都是字串；`bool`、`int4`、`float4`／`float8` 是原生值；`bytea` 是 bytes。
- 比對：`c = '原文'` 對上所有型別，除了 `json`、`xml`、`point`（42883）；`c::text = '原文'` 對上所有型別，除了 `char(5)`（尾端空白被去掉）與 bool（literal 要寫成文字）。`real = 1.1`（numeric literal）0 列、`real = '1.1'` 1 列；`float8 = 0.1` 1 列。`'1 day'::interval = '24 hours'` 為真。
- `UPDATE … SET g`（generated）→ 428C9；值相同的 UPDATE rowcount 1；`INSERT … RETURNING` 拿到 identity 與 generated 值；BEFORE trigger 回 NULL 時 rowcount 0。
- autocommit 連線送 `BEGIN` → INTRANS；交易中錯誤 → INERROR；之後 `commit()` 實為 ROLLBACK（前面的 UPDATE 沒有生效）。DEFERRABLE INITIALLY DEFERRED 的 FK 違反在 `commit()` 回 23503，之後 IDLE。

**MySQL 8.4.11／mysql-connector-python 26.7.0（pure）**
- 預設 client flags 沒有 `FOUND_ROWS`。欄位封包裡有 `db`、`table`、`org_table`、`name`、`org_name`（別名指回原欄、運算式為空），pure driver 的 `parse_column` 不保留。
- 值相同（含 decimal `…1234567890` 與 `…123456789`）的 UPDATE：`affected_rows` 0、`info_msg` `Rows matched: 1  Changed: 0  Warnings: 0`、`ROW_COUNT()` 0。
- `id = '9007199254740993'`（加引號）與不加引號都是 1 列，不存在的 `…994` 是 0 列（8.4 依欄位型別轉換常數）。
- 比對：FLOAT `= 1.1` 0 列；DOUBLE `= 0.1` 1 列；JSON `= '…'` 0 列、`= CAST('…' AS JSON)` 1 列；DATETIME(6)、TIME `838:59:59`、BINARY(16)、BIT、ENUM、generated 欄都對得上；`utf8mb4_0900_ai_ci` 下 `'HELLO'` 對上 `'Hello'`。
- 預設 sql_mode（STRICT_TRANS_TABLES、NO_ZERO_DATE…）：設 `'0000-00-00'` → 1292；比對 `d = '0000-00-00'` → 1525；`CAST(d AS CHAR) = '0000-00-00'` 1 列。
- generated 欄 UPDATE → 3105；auto_increment INSERT 的 OK 封包帶 `insert_id`。
- `START TRANSACTION` 後 `in_transaction` 為真；1366、1452 錯誤後交易仍開著；ROLLBACK 生效。MyISAM：交易中 INSERT 後 ROLLBACK → 警告 1196、資料留著；`ENGINES.TRANSACTIONS` 只有 InnoDB 為 YES。
- `information_schema.COLUMNS.EXTRA`：`auto_increment`、`VIRTUAL GENERATED`、`DEFAULT_GENERATED`。
- `SET SESSION TRANSACTION READ ONLY` 的連線：UPDATE（含明確交易內）→ 1792。

**SQL Server 2022 CU27／pymssql 2.4.4（`_mssql`）**
- `sys.dm_exec_describe_first_result_set(N'…', NULL, 1)`：每欄的 `source_database／schema／table／column`、`is_part_of_unique_key`、`is_identity_column`、`is_updateable`；運算式為 NULL；多出 `is_hidden = 1` 的 rowversion 欄。
- 讀到的值：decimal、datetime（3 位）、uniqueidentifier、varbinary 完整；datetime2(7)／datetimeoffset(7) 只到微秒；sql_variant 為 bytes。
- 比對：datetime2(7)、datetimeoffset(7)、sql_variant 0 列；text、ntext、xml → 402；其他（decimal、float、real、datetime、uniqueidentifier、varbinary、varchar、nvarchar、bit、computed、rowversion）1 列。
- 有 AFTER UPDATE trigger（插入 3 列）時 `rows_affected` 1、`@@ROWCOUNT` 1；0 列的 UPDATE 為 0；有 trigger 的表用 `OUTPUT`（沒有 INTO）→ 334；值相同的 UPDATE 為 1。
- 更新 identity → 8102、computed → 271、rowversion → 272；`SCOPE_IDENTITY()` 可用。
- `BEGIN TRANSACTION` 後：2627（重複 key）→ `@@TRANCOUNT` 1、`XACT_STATE()` 1；8114（轉換）→ `@@TRANCOUNT` 0（server 已 rollback 整個交易）。
- `SQL_Latin1_General_CP1_CI_AS` 的 varchar PK：`WHERE vc = N'k42'` 的 UPDATE 是 Clustered Index Scan；`Latin1_General_CI_AS`、`Chinese_Taiwan_Stroke_CI_AS` 為 seek；`'k42'` 三者都只有一個 Clustered Index Update 運算子（A8）。

**SQLite 3.53.4（Python `sqlite3`）**
- `cursor.description` 只有欄名；模組沒有 `column_table_name`。
- AFTER UPDATE trigger 另外插入兩列時 rowcount 1；值相同的 UPDATE rowcount 1；`RETURNING` 可用。
- INTEGER affinity 欄 `= '42'` 對上 42；無 affinity 欄存 `'42'` 時 `= 42` 0 列、`= '42'` 1 列；`SET i = '43'` 在 INTEGER 欄存成 integer、無 affinity 欄存成 text。
- generated 欄 UPDATE → 「cannot UPDATE generated column」；`pragma_table_xinfo` 的 `hidden` 為 2。
- `isolation_level=None` 時明確 `BEGIN` → `in_transaction` 真；`commit()` 有效；交易中 UNIQUE 錯誤後交易仍開著，`rollback()` 生效。
- rollback journal：一條連線的 SELECT 讀了 10 列後暫停，另一條連線的寫入（timeout 0.5 秒）→ `database is locked`。
