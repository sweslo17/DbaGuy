# 子專案 ⑥：資料工具（SQL dump、執行 SQL 檔、跨連線複製資料）— 設計

> 日期：2026-10-20 ｜ 狀態：coordinator 審核通過（2026-10-20，見 §12 的 CR-1…CR-6）；06a（backend）已實作（2026-10-21，〔06a〕註記在 §7、§10、§11 與 §12 的 CR-6 之後）；06b（dump 與 Run SQL file 的前端，加上 backend 的 `resume_at`、`/data/run-file/excerpt`、`from_line`）已實作（2026-10-23，〔06b〕註記在 R-06-7、R-06-15、§3.1、§3.2、§3.6、§4、§9、§10 的 A12、§11 與 §12 的 CR-5；最終審查與它的修正波已做（2026-10-25，〔06b 修正波〕註記在 R-06-7、§3.3.1、§3.3.2、§3.3.4、§3.6 與 §4）；plan 是 `docs/superpowers/plans/2026-10-23-06b-data-tools-frontend.md`，規劃時的裁決 V1–V20 與 spec 的問題 B1–B14 在它的「Planning rulings」與「Spec issues found while planning」）；06c（Copy data：backend、對話框與入口）已實作（2026-10-25，〔06c〕註記在 R-06-8、R-06-9、R-06-10、R-06-12、D-06-8、§3.5.1–§3.5.5、§3.6、§4、§7、§10 的 A9／A11／A16 與 §11；plan 是 `docs/superpowers/plans/2026-10-25-06c-data-tools-copy.md`，規劃時的 spec 問題 C1–C18 在它的「Spec issues found while planning」，給使用者的測試清單在它的 Appendix A；06c 的最終審查與修正波還沒做，⑥ 之後才合併回 `main`）
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §2（Q1–Q6）、§3（M9 完整結果匯出、M11 安全、M7 值的呈現；Won't 清單：「備份、還原、dump：直接用 pg_dump 等 CLI；DBeaver 依賴外部 binary 的問題反而是抱怨大戶（約 310 👍）」、schema compare／sync、data compare、fake data）、§7（串流、型別、MSSQL）、§8（「實際常用的進階功能：**SQL dump**、**跨 DB 傳輸或複製資料**」升為 v1）。§8 推翻了 Won't 清單的「dump」一列，但沒有推翻它的理由：使用者不要的是**包外部程式**，所以 ⑥ 全部原生（D-06-1），不呼叫 `pg_dump`、`mysqldump`、`sqlcmd`、`bcp`，也不帶它們的 binary。
> 技術依據：本設計在 dev 資料庫上的探測（2026-10-20；PostgreSQL 17.11／psycopg 3.3.6、MySQL 8.4.11／mysql-connector-python 26.7.0 pure、SQL Server 2022 CU27（16.0.4305.5）／pymssql 2.4.4、SQLite 3.53.4、sqlglot 30.23.0、Python 3.13.5；dev 容器裡的 `pg_dump` 17.11），標 **〔實測〕**；標 **〔未實測〕** 的是假設，§10 彙整並指定由哪個 plan task 先驗證。探測只建 `zz_06_*` scratch 物件（SQLite 用 scratch 目錄裡的檔案），以完整名稱 DROP；Couchbase 沒有寫入。紀錄在附錄 A。本文的 A 編號自成一組（⑥ A1…）。
> 建立在 ①–⑤ 之上：`SessionWorker` 與 WS 協定（`domain/execution.py`；匯出的串流、取消、`ProgressEvent`、`atomic_text_file`）、⑤ 的 `sql/literals.py`（`CellLiterals`、`LiteralStyle`）、`sql/dialects.py`（引號、literal、`transaction_frame`、`script_text`）、各 engine 的 DDL 產生（`adapters/postgres/ddl.py`、MySQL `SHOW CREATE`、`adapters/mssql/introspection.py`、SQLite `sqlite_master`）、⑤ 的 `RowEditing.edit_target`（欄位能不能寫、literal 的寫法）與 `EditPlanStore`、`SafetyPolicy`、匯出對話框（`src/features/grid/ExportDialog.tsx`）的進度與取消模式，以及 ⑤ 的教訓：每個請求恰好一個結果事件（E1）、不明就說不明、絕不重送（R-05-6、C45）。

## 1. 目標

三個功能，取代使用者在 Navicat／DBeaver 裡的「Dump SQL File」「Execute SQL File」「Data Transfer」：

| 編號 | 功能 | 對應 |
|---|---|---|
| G1 | **Dump**：把一張表、勾選的幾張表、一個 schema 或整個 database 寫成 `.sql` 檔：結構（DDL）、資料（多列 INSERT），或兩者；原生，不呼叫外部程式 | §8 SQL dump、M9 |
| G2 | **Run SQL file**：在一條連線（及 database）上串流執行一個 `.sql` 檔：進度、出錯時停或跳過、全有或全無（一個交易）、從第 N 句重跑；⑥ 自己的 dump、`mysqldump`、`pg_dump --inserts`、SSMS 的 `GO` 腳本、`sqlite3 .dump` 都能跑 | §8、Q2 |
| G3 | **Copy data**：把表（或 view、query 結果）從連線 A 複製到連線 B，可以跨 engine：建新表、附加到既有表、或先清空再附加；型別對照列出且可改；分批、進度、取消、結果誠實 | §8 跨 DB 傳輸 |
| G4 | 值不失真：大整數、decimal、日期時間（含 SQL Server 第 7 位小數）、binary、JSON、NULL 與空字串、U+0000；跨 engine 的每一種轉換都在執行前列出 | Q3、M7 |
| G5 | 安全：唯讀連線不能當目標；寫入經 `SafetyPolicy`；不悄悄覆蓋（建表遇到既有表就停、dump 預設不寫 DROP）；執行前顯示目標連線（顏色）與會做的事 | M11、Q3 |
| G6 | 輕：每條路徑都串流，記憶體有固定上限；以 100 萬列量測 dump、run、copy | Q1 |
| G7 | 不丟工作：取消、出錯、當機之後，檔案與目標的狀態說得清楚；不自動重送；History 記下 run 與 copy | Q2 |
| G8 | 入口：物件樹右鍵、表格資料 tab、query 結果、Scripts 面板、⌘⇧P；Tauri 檔案對話框（瀏覽器模式打路徑）；job 在背景跑，側欄的 Jobs 可以回到它 | Q6、M3 |

### 成功標準

- PG、MySQL、SQL Server、SQLite：把 dev 的 seed schema（結構＋資料）dump 成檔 → 以「全有或全無」在一個 scratch database 上 Run SQL file → 每張表的列數與逐表 checksum 相同；序列／identity／AUTO_INCREMENT 的下一個值接在最大值之後；view、routine、trigger 都在；檔頭寫明沒有包含的東西。
- 100 萬列的表：dump、run、copy 都在 §7 的預算內，sidecar RSS 增加不超過 §7 的上限，重複跑不累積。
- 一張型別齊全的 PG 表（`bigint` 2⁶³−1、38 位 `numeric`、`timestamptz`、`jsonb`、`bytea`、`uuid`、陣列、NULL 與空字串）以「建新表」複製到 MySQL、SQL Server、SQLite：預覽列出每欄的型別與轉換（例如 `timestamptz → datetime(6)：轉成 UTC`）；複製後的值依列出的轉換逐一相同。
- 取消：dump 途中取消 → 沒有檔案，原本的同名檔不變；全有或全無的 run 途中取消 → 目標不變；逐句模式 → 「停在第 N 行；之前的語句已生效」；copy 途中取消 → 逐表的結果（已複製／已 rollback／未開始）。
- 唯讀的目標：Run 與 Copy 在連線之前就被拒（403），worker 也再擋一次。
- `pg_dump --inserts` 的檔案跑得完（跳過 `\restrict`／`\unrestrict` 並說明）；含 `COPY … FROM stdin` 的 `pg_dump` 檔案在第一個 COPY 停下，訊息指向 `--inserts` 或 psql；`mysqldump` 的檔案以逐句模式跑完。
- 執行途中結束 backend：History 那一筆顯示 Interrupted；全有或全無的 run 什麼都沒套用（server 在斷線時 rollback）。
- 關掉對話框 job 照跑；側欄的 Jobs 可以回到它；重新載入視窗後仍列出它（`GET /data/jobs`），可以看結果或取消。

### 不在範圍內

Couchbase 的 dump 與 copy（R-06-14）；跨 engine 的 dump（dump 是來源 engine 的 SQL；跨 engine 用 Copy）；把 dump 還原到另一個 schema 名稱（PG／SQL Server 的 dump 保留 schema 名稱，改名用 Copy）；schema compare／sync、複製後的資料比對（只報列數）、fake data（Won't）；copy 的 upsert／略過既有列、可續傳的 copy；`COPY`／`LOAD DATA`／BCP 等批次協定（§12）；壓縮檔（`.sql.gz`）；權限、owner、角色、使用者、tablespace、SQL Server 的 partition scheme 與 filegroup；extension 自己的物件；CSV／Excel 匯入（C5）；排程 dump；psql／sqlcmd 的 meta-command；非 UTF-8 的檔案。

## 2. 關鍵決策

R-06-x 回答 coordinator 列出的設計問題，D-06-x 是其下的設計決定。「§x」指本文，「FP §x」指 feature-priorities，「附錄 A」是探測紀錄，「⑤ R-05-x／E／G」指 ⑤ 的 spec 與 05a 的裁決。

| 決策 | 選項 | 結論與理由（含證據） |
|---|---|---|
| **R-06-1** v1 的範圍與 engine | — | Dump：PG、MySQL、SQL Server、SQLite。Run SQL file：五個 engine（Couchbase 只有逐句模式，沒有交易）。Copy data：來源與目標都是四個關聯式 engine（含跨 engine）；來源可以是表、view、materialized view、或關聯式 query tab 的結果。Q4：三個功能都只做「搬資料、重建一份」的 80% 情境，不做同步、比對與排程。Q5：三個功能對每個 engine 用同一套流程與對話框，engine 的差異只在 adapter 的 catalog 讀取與 `sql/` 的規則表（D-06-1）。 |
| **R-06-2** Dump 包含哪些物件 | (a) 只有表與資料；(b) 加上 view、序列、型別；(c) 再加 routine、trigger、event（Navicat 的 Dump SQL File）；(d) 全部，含權限與 owner | **(c)**，routine 與 trigger 是一個勾選項（schema 與 database 範圍預設勾，單一表的範圍只帶那張表的 trigger 與它們呼叫的 PG function）。§8：使用者拿它取代 Navicat 的 dump，(a)(b) 重建出來的 schema 會少東西。各 engine 已經有每種物件的 DDL（`object_detail`），⑥ 加的是**順序**（§3.3.3）：PG 的 function／procedure 在檔頭 `SET check_function_bodies = false`（`pg_dump` 也這樣做，〔實測〕附錄 A P8）之後放在表之前——〔實測〕P9：SQL function 的 body 引用還沒建的表，檢查開著是 42P01、關掉就可以——但簽名用到某張表 row type 的 function（`RETURNS SETOF orders`）關掉檢查也是 42704，所以依 `pg_depend` 放在那張表之後；MySQL 的 routine 在表之後、view 之前（〔實測〕M10：procedure 的 body 引用不存在的表可以建，view 呼叫不存在的 function 是 1305）；trigger 一律在資料之後（載入時不觸發）；view 依 engine 的相依資訊排序。不做 (d)：還原到另一台 server 時角色多半不存在（`pg_dump` 的 `ALTER TABLE … OWNER TO` 就是常見的失敗原因，〔實測〕P8），檔頭寫明「沒有 owner 與權限」，並逐類列出沒有包含的物件與數量（§3.3.1）。 |
| **R-06-3** Dump 的檔案格式 | (a) 可攜的 SQL（一份檔案能跑在任何 engine）；(b) 來源 engine 自己的 SQL | **(b)**。DDL 本身就不可攜（型別、identity、引號），跨 engine 是 Copy 的工作（R-06-8）。資料是多列 INSERT，欄位明確列出（`INSERT INTO t (a, b) VALUES (…), (…);`），每句最多 1,000 列（SQL Server 100 列）且最多 1 MiB 的文字（D-06-5）；literal 只從 `sql/literals.py` 與 `sql/dialects.py` 來（D-05-1）。名稱照各 engine DDL 產生器的寫法：PG 與 SQL Server 帶 schema（DDL、FK 的 REFERENCES、view 的本文都帶，去掉它不可靠），MySQL 與 SQLite 不帶（`SHOW CREATE TABLE` 與 `sqlite_master` 本來就不帶，還原到 tab 選的那個 database）。UTF-8、LF、沒有 BOM。T-SQL 每個批次後一行 `GO`（`script_text`）。 |
| **R-06-4** Dump 的一致性 | (a) 每張表各自讀；(b) 整份 dump 在一個讀取交易裡 | **(b)**，各 engine 做得到的程度：PG `BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY`（MVCC，不擋寫入）；MySQL `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY`（InnoDB；〔實測〕附錄 A M9 交易裡 `SHOW CREATE TABLE` 照常）；SQLite 一個讀取交易（WAL 不擋寫入；rollback journal 模式會擋住其他連線的寫入直到 dump 結束，對話框說明，A5）；SQL Server 只有 database 開了 `ALLOW_SNAPSHOT_ISOLATION` 才用 `SNAPSHOT`，否則每張表在輪到它時以 READ COMMITTED 讀（〔實測〕dev 的 database 是 OFF，公司的多半也是），檔頭與對話框寫明「不是一致的快照」。Dump 跑在**自己的 job session**（D-06-10），不是 tab 的：不會結束 tab 暫停中的結果，也不受使用者改過的 session 設定影響（D-06-7 設定輸出格式）。 |
| **R-06-5** 還原：用 query tab 還是專用的執行器 | (a) 開進 query tab 再 Run all；(b) 專用的串流執行器 | **(b)**。Scripts 只收 10 MB 以內的檔案（`scripts_service._MAX_SCRIPT_BYTES`），CodeMirror 開不了幾百 MB；Run all 是前端逐句經 WS 送，每句一筆 History、一列訊息（02c：2,000 句 INSERT 修好之後仍要約 33 秒）。專用的執行器在 backend 從磁碟分塊讀、切句、送出（D-06-3），進度以 bytes 計，History 只記一筆（D-06-9）。跑在**自己的 job session**：檔案裡的 `SET search_path`、`USE`、`SET FOREIGN_KEY_CHECKS = 0`、`PRAGMA foreign_keys = OFF` 不會留在使用者的 tab 裡，結束就關。小檔案仍可以照舊開進 query tab。 |
| **R-06-6** 還原的逐句安全檢查 | (a) 每句完整的 `SafetyPolicy.check`；(b) INSERT 只用關鍵字規則；(c) 整份確認一次，逐句只做唯讀閘門 | **(c)**。〔實測〕附錄 A X2：在 split 之外每句再跑 `check()`，整體只剩 0.30–0.50 MB/s（sqlglot）或 0.47–0.64 MB/s（關鍵字規則：`check()` 會再切一次句、再 tokenize 一次），只切句是 2.1–3.0 MB/s——1 GB 的檔案是 33–55 分鐘對 6–8 分鐘。對話框就是確認（同 ⑤ 的「預覽即確認」，R-05-7）：使用者確認了整份檔案，`decide()` 對「已確認、連線可寫」的語句一律 RUN、與分類無關，所以分類是白做。`SafetyPolicy` 新增 `gate_confirmed(sql, dialect, read_only)`：可寫的連線直接 RUN（不分類），唯讀的連線照 `check()` 判斷（RUN 以外就 BLOCK）——規則仍只在 policy 裡（DRY）。唯讀的目標在開始前就被拒（403 `read_only`），worker 逐句的閘門擋的是執行中途被改成唯讀的 profile：閘門每秒最多重讀一次 profile（`ProfileStore.get` 每次讀檔，一百萬句就是一百萬次讀檔）。server 端另有一層：PG READ ONLY、MySQL `SET SESSION TRANSACTION READ ONLY`、SQLite `mode=ro`、Couchbase `readonly=true`（SQL Server 只有應用層，既有限制）。 |
| **R-06-7** 還原的模式與錯誤策略 | — | 兩種模式。**全有或全無**：先送 dialect 的交易開頭（⑤ 的 `transaction_frame`：PG `BEGIN`、MySQL `START TRANSACTION`、SQL Server `SET NOCOUNT OFF;`＋`BEGIN TRANSACTION`、SQLite `BEGIN IMMEDIATE`），出錯或取消就 rollback、什麼都沒套用；檔案自己的交易語句（`BEGIN`、`COMMIT`…，`is_transaction_statement`）在送出前就停（「The file controls its own transactions (line 12: COMMIT): run it statement by statement」）；每句之後確認 `in_transaction()` 仍為真，否則交易已被結束（MySQL 的 DDL 隱含 commit〔實測〕附錄 A M8，或 T-SQL 批次裡的 COMMIT），立刻停、結果是 partial（R-06-12）。**逐句**：auto-commit，每句各自生效；出錯時「停」或「跳過並繼續」。PG 的交易遇錯就 abort（後面每句都是 25P02），所以「跳過並繼續」只在逐句模式。預設：PG、SQL Server、SQLite 是全有或全無；MySQL 是逐句（DDL 會隱含 commit，對話框說明全有或全無只適用沒有 DDL 的檔案）；Couchbase 只有逐句。**從第 N 句開始**：逐句模式停下之後，結果提供「從第 N 句重跑」（修好再跑）與「跳過它、從 N+1 繼續」；前面的語句仍會切句（DELIMITER 等狀態要對），但不送出。〔06b：B1——停在檔案自己的交易裡時 server 已 rollback 整段，從失敗的那句重跑會永遠漏掉那段，所以 `JobOutcomeEvent.resume_at` 由 backend 說從哪一句開始（被 rollback 的交易的第一句，或停下的那句；說不準時是 null，結果就不提供「Run again from line N」與「Skip it」）；「跳過它」只在 `resume_at` 就是停下的那句時提供。兩個動作都回到預先填好的表單、不直接執行（B5、V2）；重跑帶 `from_line`，檔案在兩次之間改過就拒絕（`file_changed`）。見 §3.2、§3.6 的〔06b〕〕〔06b 修正波：逐句停下（`stopped`）而 `resume_at` 是 null 時連「Run again…」也不提供（它會從原本的起點重跑已生效的語句，最終審查 I1），改提示「Check the target, then choose where to start in a new run (Run SQL file… from the tree).」；partial 的「Run the rest statement by statement…」與全有或全無的結果照舊〕 |
| **R-06-8** Copy data 的架構 | (a) 一條 job thread 同時開兩條連線；(b) 兩個 worker（來源一個、目標一個）之間一條有上限的管線；(c) 先 dump 成暫存檔再 run | **(b)**。每條連線只在自己的 worker thread 上被呼叫（adapter 的契約；SQL Server 的 `GuardedConnection` 拒絕別的執行緒），取消、arm 視窗、`_run_statement` 全部沿用 worker 的機制（DRY：(a) 要重寫一份）。來源 worker 跑 `TransferSource`（讀 1,000 列一塊），目標 worker 跑 `TransferSink`（組多列 INSERT、送出），中間的 `RowPipe` 最多放 2 塊（Q1）。(c) 要兩倍的磁碟與時間，而且 literal 要依**目標**的欄位寫（R-06-10）。來源 session：表從物件樹來時是新開的 job session；query 結果來自那個 tab 自己的 session（重跑那一句，同匯出：同一個交易、temp table 看得到；只收 READ，同 `_EXPORT_SELECT_ONLY`）。命令送在目標 session 的 WS（`TransferCommand{plan_id, source_session_id}`），sink 經 hooks 在來源 worker 上啟動 source；任一邊失敗或取消，另一邊跟著停。〔06c：命令是 `CopyDataCommand{plan_id, source_session_id: str | None}`（`type` 是 `copy_data`，第三種 `JobKind`；C2）：表的來源是 None，backend 自己為來源開一個 session、copy 結束時關掉（`SessionManager._source`、`SourceLease`；前端不必開、也不會在重新載入時漏關）；query 結果帶它的 tab 的 session，必須屬於 plan 的來源 profile（`plan_mismatch`）。`TransferSource` 是 helper job（`Job.helper`，以 `PreparedJob` 提交：不是 client 的命令、不進 job 清單、在 session 開著的交易裡跑，C3），它的事件以 `<request id>:source:<k>` 送到來源 session 的 socket，query tab 不認得那個 request id、不理它。「另一邊跟著停」只有一個例外：sink 先停時關掉管線，source 在下一次放入時停；copy 自己的來源 session 上的讀取會被取消，**query tab 上的讀取從不取消**（PG 上取消它會 abort tab 開著的交易，pre-flight H2），還在 tab 的佇列裡就撤回（`SourceWorker.withdraw`）。source 從沒開始（session 先關、佇列裡被取消）時管線也會失敗（`Job.not_run`，pre-flight M1），copy 不會一直等〕 |
| **R-06-9** Copy 的目標表與衝突 | — | 三種模式：**建新表**（表已存在就不做：預覽時是問題，執行時 CREATE TABLE 失敗也什麼都沒做）、**附加**（依欄名對應，見 §3.5.4）、**先刪除既有的列再附加**（`DELETE FROM t`，危險語句，預覽以 danger 色列出，與資料同一個交易）。建出來的表只有欄位、NOT NULL 與 primary key：預設值、check、index、FK、identity 都是 engine 專屬的運算式，翻譯不了（要一模一樣的表：同 engine 用 Dump＋Run 建結構，再以附加模式 Copy）。key 衝突一律失敗、整張表 rollback（v1 不做 upsert 或略過既有列：那是資料同步，Won't 的鄰居，§12）。多張表依 FK 排序（父表先，SchemaSnapshot 的 `foreign_keys`）；環狀的在預覽警告；MySQL 與 SQLite 的目標 session 關掉 FK 檢查（`SET FOREIGN_KEY_CHECKS = 0`；`PRAGMA defer_foreign_keys = ON`，交易內有效），PG 與 SQL Server 做不到 session 層級的關閉（`session_replication_role` 要 superuser、`NOCHECK CONSTRAINT` 會改到表的定義），所以只靠排序。identity：預設保留來源的值（PG `OVERRIDING SYSTEM VALUE`、SQL Server `SET IDENTITY_INSERT … ON`），附加模式可逐欄改成「讓目標產生」（不寫那一欄）；PG 寫入之後以 `setval` 把序列推到最大值之後（〔實測〕附錄 A P2：明確寫入 identity 之後序列沒有前進，下一個 DEFAULT 是 1）。來源與目標是同一張表、或同一個 SQLite 檔案（rollback journal 下讀取擋住寫入）：預覽就拒絕，後者建議改用 `INSERT … SELECT`。〔06c：FK 檢查不是另外關的，是目標 job session 在第一張表之前照 dump 的還原設定跑一次（`transfer_sql.TARGET_SETUP`＝06a 的 `RESTORE_SETTINGS`，SQL Server 前面加 `SET IMPLICIT_TRANSACTIONS OFF`；結束後 `TARGET_RESET`，C4）：MySQL `FOREIGN_KEY_CHECKS = 0` 與嚴格的 `sql_mode`；SQLite 是 `foreign_keys = OFF`（在任何交易之外，所以生效；每張表各自 COMMIT，`defer_foreign_keys` 幫不了後面的表）。附加或取代進 MySQL、SQLite 的既有表、而它有 FK（或取代時別的表參照它）時，預覽警告 FK 在 copy 中不檢查（L2，讀 completion 的 snapshot：snapshot 舊了只會少警告、不會誤拒）。表的順序（C11）：建新表依**來源**的 FK 父表先（新表沒有 FK）；附加與取代依**目標自己的** FK（PG、SQL Server 以一個 catalog 查詢即時讀，含 ON DELETE 的動作，`adapters/references.py`、`base.ReferenceReading`；MySQL、SQLite 讀 snapshot，它們的檢查與 cascade 在 copy 中都是關的），環狀的只在附加或取代進 PG、SQL Server 時警告（`FK_CYCLE`：它們逐列檢查）。取代時目標的 `ON DELETE CASCADE`／`SET NULL`／`SET DEFAULT` 會改到另一張表的列：那張表在 copy 裡就一定排在它之後（否則它剛複製的列會被 cascade 掉，Task 2 fix round 4），連順序都做不到（cascade 的環）是問題 `cascade_loop`、沒有 plan；copy 之外的表會被改到時警告（`CASCADES_OUTSIDE`）。key 衝突：SQLite 的目標寫 `INSERT OR ABORT`（`InsertBatcher(abort_on_conflict=True)`），表上 `ON CONFLICT REPLACE`／`IGNORE` 的 key 也是失敗、整張表 rollback（C8，⑤ I1）。建新表的 primary key 每次從 catalog 讀（`TableColumn.key_position`，和欄位同一個查詢；不讀 completion 的快取，Task 2 fix round 3）；目標 engine 做不了 key 的型別（MySQL 的 text／blob、SQL Server 的 max 型別）換成有長度的（MySQL `varchar(768)`、`varbinary(3072)`，複合 key 平分 3,072 bytes；SQL Server `nvarchar(450)`／`varchar(900)`／`varbinary(900)`）並註明「longer values fail their row」；json、xml 這類 key 不了的、或使用者自己打的型別做不了 key 時不建 key 並警告（`KEY_NOT_CREATED`），從不換掉使用者的型別。identity 的 `setval` 只給整數欄（pre-flight H1：numeric 的 identity 會 42883）。同一張表的判斷用解析過的 database（請求沒帶 database 時讀連線的 `current_database()`，Task 2 I1、pre-flight M2）；同一個目標表收兩個來源是 `duplicate_target`〕 |
| **R-06-10** 跨 engine 的型別對照 | (a) 一律 text；(b) 每個 engine 的型別家族對照表，預覽列出、可逐欄修改 | **(b)**。各 engine 的 catalog 讀出欄位的家族（`TypeFamily`：bool、int16…、decimal(p,s)、timestamp_tz、json、uuid…，D-06-1），純函式 `sql/type_map.py` 對照出目標型別與轉換（§3.5.2）。同 engine 照抄宣告的型別（使用者自訂型別除外：PG enum／domain／composite 換成基底型別或 text，因為目標 database 不一定有那個型別）。會失真或改寫的對照標 ⚠（例如 `timestamptz → MySQL datetime(6)：轉成 UTC`、`numeric 沒有精度 → MySQL decimal(65,30)：超過 30 位小數會被四捨五入`、SQLite 的 decimal 存成 TEXT 才保住每一位）；沒有可攜寫法的（跨 engine 的 spatial、SQL Server `sql_variant`、binary 對 text）是問題，改了型別或排除該欄才能執行。使用者改的型別經 lexer 檢查只是一個型別片段（沒有 `;`、註解、未關閉的引號或括號，同篩選列的 `_single_expression` 規則，DRY），才拼進 CREATE TABLE。〔06c：「使用者自訂型別」是 `TableColumn.custom_type`（C13）：PG 的 domain、enum、composite 與任何不在 `pg_catalog` 的型別，SQL Server 的 alias type；它們即使同 engine 也依家族對照、附說明；spatial 在同 engine 照抄宣告的文字；PG 之間的 `money` 照舊是 `money`。宣告的型別 → `TypeFamily` 的規則每個 engine 只有一份（`adapters/families.py`：catalog 讀出的欄、query 結果的 `type_name` 與使用者打的型別（`of_name`）都用它）。「排除該欄」是請求的 `TargetSpec.left_out`（C1）〕 |
| **R-06-11** 值的保真（Q3） | — | 讀：沿用各 adapter 的無損載入（PG text-first、MySQL text protocol、SQL Server 的文字轉換、SQLite 每個值自己的型別），只加兩處：SQL Server 的 `datetime2`／`datetimeoffset`／`time` scale 7 在 SELECT 裡 `CAST(c AS varchar(…))`（〔實測〕附錄 A S7：driver 只到微秒，CAST 的文字保住第 7 位且寫回相等）〔06a：見 S4，改用 `CONVERT(…, 126)`：CAST 的 datetimeoffset 文字寫不回（A15）〕；`sql_variant` 只拿得到原始 bytes（〔實測〕`42` 讀成 `b'*\x00\x00\x00'`），dump 與 copy 都不寫那一欄並在預覽與檔頭列出（還原後是 NULL 或預設值）。寫：`CellLiterals` 依目標欄位的種類與 `LiteralStyle`（SQL Server 的 `national` 逐欄、MySQL 的反斜線、PG 的 standard strings，⑤ D-05-2）；另補 SQLite 的非有限浮點（寫 `9e999`／`-9e999`，〔實測〕附錄 A L3：`'inf'` 會存成文字）與時區位移的正規化（`±HH` → `±HH:MM`，〔實測〕附錄 A X5、S10：MySQL 與 SQL Server 不收 PG 輸出的 `+00`）；PG 的 `money` 以 `::numeric` 讀、寫成數字（〔實測〕P10：文字輸出跟著 `lc_monetary` 是 `$1,234.56`，換一台 server 會讀錯；數字寫進 money 欄是精確的）。不能寫的欄：generated／computed（〔實測〕PG 428C9、MySQL 3105、SQL Server 271、SQLite 錯誤）與 rowversion（273）不寫，還原時由 server 重算；identity 見 R-06-9。NUL：PG 的文字裝不下 U+0000（⑤ E16），跨到 PG 的那一列失敗並指出行與欄。編碼：檔案一律 UTF-8；SQL Server 的非 Unicode 欄（附加模式）以外的字元會被 server 換成 `?`（⑤ 已知），預覽警告。大值：一列超過 1 MiB 的文字自成一句；超過 64 MiB 的一句（MySQL `max_allowed_packet` 預設〔實測〕67,108,864）在 dump 時警告〔06a：見〔06a〕執行時的其他決定的「一句太長」：每張表超過 Run SQL file 的 256 MiB（每個 dialect）或 MySQL 來源的 `@@max_allowed_packet` 時警告一次〕。 |
| **R-06-12** 結果、取消與當機（Q2） | — | 每個 job 恰好一個 `job_outcome`（⑤ E1），從不重送；排隊時就被取消、或 session 在開始前關閉的 job 是 `not_started`。**Dump**：寫在 `.part` 暫存檔、全部寫完才換名（`atomic_text_file`，同匯出）；失敗或取消不留檔、原本的同名檔不變；當機只會留下一個隱藏的 `.<名>.<亂數>.part`（已知限制）。**Run**：全有或全無 → `completed`／`rolled_back`（COMMIT 前的任何失敗，含斷線）／`partial`（交易被檔案或 DDL 結束）／`unknown`（COMMIT 途中斷線）；逐句 → `completed`（可帶「N errors skipped」）／`stopped`（停在第 N 行：之前的已生效，它本身沒有或被 rollback）／`unknown`（第 N 句執行中斷線：它可能已執行）。**Copy**：逐表一個交易，逐表的結果 `copied`／`rolled_back`／`not_started`／`unknown`，整體是 `completed`／`partial`／`rolled_back`／`unknown`；不能 rollback 的目標表（MyISAM，⑤ 的 `transactional=False`）照 ⑤ G10：送出之後的失敗是 `unknown`；MySQL 建新表時 CREATE TABLE 隱含 commit，失敗後表還在（空的），結果明說「已建立、沒有資料」，不自動 DROP（Q3：不做沒問過的破壞性動作）。當機：server 在斷線時 rollback 未 commit 的交易，所以全有或全無與 copy 的那張表什麼都沒套用；History 那一筆是 Interrupted（D-06-9）。〔06c：copy 在第一張沒複製成的表就停（它與後面的表 `not_started`）。MySQL 的表能不能 rollback 以表開始時 server 說的為準，不只看 plan（Task 4 審查 I-1）：建新表在 CREATE 之後讀一次 engine（它拿到的是 server 的預設 engine），附加與取代在 BEGIN 之前讀（plan 可能是 15 分鐘前的）；每次 rollback 之後讀 `SHOW WARNINGS`，1196 就是 unknown（與 Run SQL file 共用 `sessions/jobs/kept_changes.py`）。所以「已建立、沒有資料」（`CREATED_EMPTY`）只在那張表真的 rollback 時說，不能 rollback 的建新表說 `CREATED_KEPT`（表留著、帶著失敗前寫進去的列）。取代進不能 rollback 的表：DELETE 一送出，之後的失敗就是 unknown（「deleted at once… stay deleted」，DELETE 中途被停時說刪掉的那些留著，pre-flight H3）。unknown 的表不算列數（`rows` 0），警告「Up to N rows written to <table> may stand」。取消（C6）：COMMIT 那段 worker 取消不了語句，那時的 Cancel 被留著（`_held_cancel`），下一張表開始前 `JobContext.resume_cancels()` 照辦：那張與後面的表 `not_started`、結果帶 `cancelled`；在一張表的第一句之前到的 Cancel 讓那張表 `not_started`（不是 rolled_back）；profile 中途改成唯讀時下一張表不開始（pre-flight L-b）。History：取消而 partial／unknown 的是 error 加「(cancelled)」（不是 cancelled：要使用者看目標；Run SQL file 一樣）〕 |
| **R-06-13** 串流與記憶體（Q1） | — | Dump：來源讀 1,000 列一塊（匯出的 `_EXPORT_CHUNK`）〔06a：見〔06a〕執行時的其他決定的「Dump 的讀取」：先讀 1 列，之後依目前最大的一列決定，最多 1,000 列〕，PG 在交易裡走 server-side cursor（DECLARE／FETCH）、MySQL unbuffered、SQL Server 與 SQLite 本來就串流；寫檔前的單句最多 1 MiB。Run：一次讀 1 MiB〔06a：見 S5，512 KiB〕，切句器不保留 token（D-06-3），單句上限 256 MiB（超過就停並指出行號）。Copy：管線最多 2 塊，每塊最多 1,000 列且其值的文字最多 4 MiB。量測目標見 §7。 |
| **R-06-14** Couchbase | (a) 三個功能都做；(b) 只做 Run SQL file；(c) 完全不做 | **(b)**。Run SQL file 對 SQL++ 是通用的（切句、逐句送、`readonly=true`，帶 tab 的 query context），沒有新的 Couchbase 程式碼。Dump 與 Copy 不做：文件沒有 DDL；SQL++ 寫入會正規化整份文件（04c C12：`1.10` → `1.1`、key 排序、重複 key），批次寫回做不到 byte-exact，要 KV／SDK 的路徑（R-04-2 選項 C）；cbexport／cbimport 的包裝是 FP §4 的 Won't；「把資料拿出來」已有 JSON／JSONL 的完整匯出（伺服器原文，R-04-15）。唯一合理的候選是「把 Couchbase query 結果複製到關聯式的表」（分析用），列在 §12 並留給使用者決定。 |
| **R-06-15** UI 與 job 的生命週期 | (a) 模態對話框，跑完才能關（同匯出）；(b) 對話框可以關，job 在背景跑 | **(b)**。一個 1 GB 的 run 要好幾分鐘，擋住整個 app 不合理（Q1 的「快」包含不卡住使用者）。對話框關掉 job 照跑，側欄底部的 **Jobs** 顯示進行中的數量，點開列出這次執行的 job（跑完的也在，直到清除），每一個都能重開它的對話框（進度、結果、取消）。job 由 backend 列出（`GET /data/jobs`），所以重新載入視窗後仍看得到、能取消；WS 重連時 worker 重送該 job 最後的進度與結果（D-06-10）。跑超過 `notifications.long_query_seconds` 且 app 不在前景時發通知（S8 的既有設定）。job 進行中關閉視窗或 ⌘Q 先問（Keep running／Quit anyway，A12；02c 的 carry 說明 quit 目前略過所有 close guard）。〔06b：job 在 React 外（`jobsStore`＋每個 job 一個 `JobRunner`，B4、V1），對話框、socket 與視窗重新載入都帶不走它；Jobs 按鈕只在這個視窗有 job 時出現（V3）；重新載入後列出的 job 沒有命令（`JobInfo` 不帶），只有結果與 Cancel、沒有 run again（B8、V6）；view 沒開時結束另有 toast（B14、V11）；刪除有 job 在跑的連線會被拒（B13：`DELETE /profiles/{id}` 會關掉它的 job session）；⌘Q 與關視窗的問法見 CR-5 的〔06b〕〕 |
| **D-06-1** 職責分層 | — | engine 的知識（catalog 有哪些物件、欄位家族、相依、DDL、序列的值、hint）在 adapter 的新 ISP 介面 `DataTools`（同 `RowEditing`、`FieldSampling`）：`table_columns(path)`（欄位描述，⑤ 的四個 `edit_target` 改成讀它，DRY）與 `dump_catalog(scope)`。SQL 文字在純函式：`sql/statement_stream.py`（檔案 → 語句）、`sql/bulk_insert.py`（多列 INSERT，dump 與 copy 共用）、`sql/dump_script.py`（檔頭、區段、結尾）、`sql/type_map.py`（對照與轉換）、`select_builder.build_copy_select`（讀資料的 SELECT）。流程在 `sessions/jobs/`（`dump.py`、`run_file.py`、`transfer.py`），經 `JobContext` 使用 worker 的能力（D-06-2）。預覽與檢查在 `services/data_service.py`（metadata 連線；純函式的部分在 request 執行緒，同 04c C43）。API 在 `api/data.py`。沒有新的 Python 或前端相依。 |
| **D-06-2** Worker 的 job 框架 | (a) 每個 job 各寫一份請求流程（同 `_export`、`_apply_edits`）；(b) 一個通用的 `_run_job` | **(b)**。`submit()` 收 `DumpCommand`、`RunFileCommand`、`TransferCommand`，都走 `_submit_request(cmd, self._run_job)`；`_run_job` 負責 `_discard_open()`、`_start_request`、`_set_running(None)`、例外分類、`job_outcome` → `session_state` → `finished`，job 物件只做自己的事。`JobContext`：`run(sql, options)`（經 `_run_statement`：arm／disarm、取消檢查、`describe=False`，同 ⑤ M1）、`close(result)`、`raise_if_cancelled()`、`progress(...)`（節流，D-06-11）、`message(...)`、`connection`（catalog 與 dump 的方法在同一條 thread 上呼叫）、`hooks`。事件順序：`started` → `job_progress`×n → [`message`] → [`error`] → `job_outcome` → `session_state` → `finished`；排隊時被取消或 session 已關閉的也送一個 `job_outcome`（`not_started`，被取消的帶 `cancelled`）。worker.py 不再為每個 job 長一段（OCP）。 |
| **D-06-3** 串流切句 | — | `sql/splitter.py` 的切句改成單趟、不保留 token 的 `iter_spans`（目前每句的 token 都留在 `current` 清單裡：一句 50 MB 的 INSERT 約 1,200 萬個 token），`split()` 改用它、結果不變（既有測試照過）。`StatementStream`（`sql/statement_stream.py`）：一次讀 1 MiB，只處理到最後一個換行（`GO` 與 `DELIMITER` 行要整行才能判斷），完整的語句往外送，未完成的那句留到下一塊；一句一直不完整時下一次讀兩倍（避免大句被反覆重掃），單句超過 256 MiB 就停。帶出每句的起始行號（錯誤訊息與「從第 N 句」用）、MySQL 的 DELIMITER 狀態跨塊延續。PG 的 psql meta-command（行首的 `\`，在語句開始處）是獨立的一種 span：`\restrict`／`\unrestrict`（〔實測〕附錄 A P8：pg_dump 17.11 的純文字輸出第一行與最後一行）跳過並送一則訊息，其他（`\connect`、`\i`…）停下並說明；編輯器的 `/sql/split` 不變。檔案必須是 UTF-8（開頭的 BOM 去掉），解碼失敗停在那一行。 |
| **D-06-4** classifier 的 sqlglot 上限（02a carry） | — | 〔實測〕附錄 A X1：sqlglot 分類一句 1,000 列的 INSERT 要 154–212 ms，記憶體尖峰約是文字的 113 倍（0.84 MB 的語句 95 MB）：query tab 裡一句 10 MB 的 INSERT 會吃掉約 1.1 GB。超過 64 KiB 的 INSERT（MySQL 另含 REPLACE）只用關鍵字規則：〔實測〕sqlglot 對每一種 INSERT（含 ON CONFLICT、ON DUPLICATE KEY、INSERT … SELECT）的答案都是 WRITE、沒有危險理由，和關鍵字規則相同；T-SQL 批次照舊逐段分類（`INSERT … EXEC` 的 UNKNOWN 來自字詞下限，不受影響）。這也是 ⑥ 以外的路徑（query tab、⌘E 的 analyze）的修正；Run SQL file 本身不分類（R-06-6）。 |
| **D-06-5** 多列 INSERT 的批次 | — | `InsertBatcher(dialect, table_sql, columns, styles)`：每句最多 1,000 列（SQL Server 100 列）、文字最多 1 MiB，一列超過就自成一句。〔實測〕附錄 A S5、X4：SQL Server 一句 1,001 列是 10738；同樣 2 萬列，每句 1,000 列 7,678 列／秒、100 列 21,847 列／秒（編譯成本）。PG、MySQL、SQLite 每句 1,000 列（10 萬列：PG 126k–191k 列／秒、MySQL 61k–120k、SQLite 519k–615k）。identity 的寫法（PG `OVERRIDING SYSTEM VALUE`、SQL Server 前後的 `SET IDENTITY_INSERT`）由它包，dump 與 copy 共用（DRY）。 |
| **D-06-6** 讀資料的 SELECT | — | `select_builder.build_copy_select(dialect, path, columns, filter)`：欄位明確列出、略過不寫的欄（generated、rowversion、sql_variant）；PG 一般表用 `FROM ONLY t`（繼承的子表不重複），partitioned 表從父表讀一次、partition 不另讀（`pg_dump` 也是），`money` 欄讀 `m::numeric`；SQL Server scale 7 的時間型別 `CAST` 成文字（R-06-11）〔06a：見 S4，`CONVERT(…, 126)`〕；表格資料 tab 的篩選可帶過來（「只有符合目前篩選的列」，沿用 `_single_expression`）。 |
| **D-06-7** 各 engine 的檔頭與 session 設定 | — | 見 §3.3.3 的表。原則：dump session 先設輸出格式（PG `DateStyle = ISO`、`IntervalStyle = postgres`、`extra_float_digits = 3`、`search_path = ''` 讓 `format_type` 與預設值全部帶 schema〔實測〕附錄 A P5；MySQL `USE <db>` 讓 `SHOW CREATE VIEW` 不帶 database 名〔實測〕附錄 A M6）；檔頭寫還原需要的設定（PG `client_encoding`、`standard_conforming_strings = on`、`check_function_bodies = false`；MySQL `SET NAMES utf8mb4`、`SET FOREIGN_KEY_CHECKS = 0`、`SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO'`〔實測〕附錄 A M1：預設模式拒絕 `0000-00-00`（1292），這個模式收下零日期、保留 id 0、仍是嚴格；SQL Server `SET NOCOUNT ON`、`SET QUOTED_IDENTIFIER ON`、`SET ANSI_NULLS ON`；SQLite `PRAGMA foreign_keys = OFF` 與 `PRAGMA defer_foreign_keys = ON`）。literal 依這些設定寫（MySQL 反斜線跳脫、PG standard strings），所以檔頭固定它們，還原時就不看 server 的預設。MySQL 物件的 `DEFINER=…` 拿掉（還原的帳號成為 definer；沒有 SUPER／SET_USER_ID 時保留它會是 1227），以 splitter 既有的 `_after_account` 找出帳號的範圍，檔頭說明。 |
| **D-06-8** 一次性 plan | — | ⑤ 的 `EditPlanStore` 抽成 `services/plans.py` 的 `OneTimePlanStore`（uuid4、取走即刪、15 分鐘 `CLOCK_MONOTONIC`、最多 32 份、文字總量上限），edit 與 transfer 共用。Copy 的預覽存成 plan（對照、建表與 DELETE 的文字、批次大小、來源的描述），`TransferCommand` 只帶 `plan_id`：執行的就是預覽的（R-05-7）；plan 綁目標 profile＋database 與來源 profile，來源 session 必須屬於來源 profile（`plan_mismatch`）。INSERT 的文字不在 plan 裡（在執行時依目標 session 實際的設定產生，見 §3.5.5）。〔06c：`services/plans.py` 的 `OneTimePlanStore` 是泛型的，edit 與 copy 各一個 store（`EditPlanStore`、`TransferPlanStore`，上限各自算）；copy 的過期與不符說「The preview has expired: preview the copy again」（`COPY_PLAN_EXPIRED`，⑤ 的文字不變，pre-flight L-a）。命令是 `CopyDataCommand`（見 R-06-8 的〔06c〕）〕Dump 是讀取，不存 plan：`/data/dump/plan` 只給對話框看，`DumpCommand` 帶勾選的物件與選項，在快照裡重讀 catalog。Run 不存 plan（串流）。 |
| **D-06-9** History | — | Run 與 Copy 各寫**一筆**（Dump 是讀取，同匯出不寫）：開始時寫入（新狀態 `running`），結束時補上結果、列數、時間與錯誤；backend 啟動時把還是 `running` 的改成 `interrupted`（單一 process，不會誤改）。⑤ M9（apply 途中死掉沒有紀錄）之後也能改用它。SQL 欄是摘要與 plan 的 DDL／DELETE（`-- Run SQL file /path/x.sql (148.2 MB), all or nothing`；`-- Copy data: Local PG public.orders → Dev MySQL orders (create)` 加上 CREATE TABLE），不含 INSERT。`HistoryService` 加 `begin()`／`finish()`。 |
| **D-06-10** job 的 session 與清單 | — | 前端為 job 開 session（`POST /sessions`，unlock 流程照舊），job 結束就關（`DELETE /sessions/{id}`）。`SessionManager` 不再 reap 有請求正在跑的 session（目前只看有沒有 WS，10 分鐘：一個超過 10 分鐘、視窗剛好斷線的匯出今天也會被殺）。`JobRegistry`（manager 內）記每個 job 的 session、request、種類、標題、開始時間、最後的進度與結果：`GET /data/jobs`、`POST /data/jobs/cancel`；WS attach 時 worker 重送該 session 上 job 的最後一個 `job_progress` 與 `job_outcome`（同 `replay_state`）。跑完的 job 留在清單裡到使用者清除或 backend 結束；沒有人 attach 的 job session 在 job 結束 10 分鐘後照一般規則被 reap。 |
| **D-06-11** 進度、訊息與錯誤的上限 | — | `job_progress` 每 250 ms 最多一次（另在每個物件開始與結束時），內容：階段、目前的物件、列數、bytes、總 bytes（run）、語句數、錯誤數、行號。逐句模式「跳過並繼續」的錯誤保留前 100 則（行號、code、server 原文），之後只計數；server 訊息（NOTICE／PRINT）保留前 200 則。 |
| **D-06-12** Log | — | 同 ⑤ §5：INFO 記 job 的種類、profile、物件數、列數、bytes、ms、結果與檔案路徑（同匯出），不記值；WARNING 記 partial、unknown、interrupted；DEBUG 才有語句（每句只記起始行號與前 200 字元：一百萬句的 run 不能把 log 灌滿）；server 的錯誤原文只在 DEBUG（會回顯 literal）。 |
| **D-06-13** 能力欄位 | — | `AdapterCapabilities` 與 `EngineInfo` 加 `dump`、`run_file`、`copy_data`（PG、MySQL、SQL Server、SQLite 三個都是 True；Couchbase 只有 `run_file`）。前端只依它們顯示入口；API 對沒有能力的 engine 回 422 `not_supported`。 |
| **D-06-14** 計畫拆分 | 一份；兩份；三份 | **兩份**：06a backend、06b frontend（§11）。06a 規劃時若超過 13 個 task，把 Copy 的 backend（§11 的 06a T10–T11）連同它的對話框拆成 06c，dump 與 run 先端到端完成。 |

## 3. 架構

### 3.1 模組總覽

```
backend/dbaguy_backend/
  domain/
    data_jobs.py          DumpScope、DumpOptions、DumpPlan、FileInspection、TransferRequest、TransferPlan、TableTransfer、
                          ColumnMapping、JobOutcome 與各自的結果欄位
    execution.py          DumpCommand、RunFileCommand、TransferCommand（client）；JobProgressEvent、JobOutcomeEvent（server）
    history.py            HistoryStatus 加 running、interrupted
    profiles.py           EngineInfo.dump、run_file、copy_data
  adapters/
    base.py               AdapterCapabilities.dump／run_file／copy_data；DataTools（ISP）
    columns.py            TableColumn、TypeFamily（engine 無關的欄位描述；⑤ 的 edit_target 與 ⑥ 共用）
    postgres/dump.py      catalog：物件、相依、序列與它們的值、extension、partition、trigger 的 function
    mysql/dump.py         catalog：SHOW CREATE、VIEW_TABLE_USAGE、DEFINER
    mssql/dump.py         catalog：sys.sql_expression_dependencies、snapshot 是否可用、identity、scale 7 的欄
    sqlite/dump.py        catalog：sqlite_master、virtual table 與 shadow table
    */editing.py          改讀 table_columns（行為不變，⑤ 的測試照過）
    postgres/ddl.py、mssql/introspection.py   table_ddl 可以不含 FK（FK 另成 ALTER TABLE）
  sql/
    splitter.py           iter_spans（單趟、不留 token）；split() 改用它
    statement_stream.py   StatementStream（D-06-3）
    classifier.py         超過 64 KiB 的 INSERT／REPLACE 只用關鍵字規則（D-06-4）
    bulk_insert.py        InsertBatcher（D-06-5）
    dump_script.py        檔頭、session 設定、區段順序、結尾（純函式）
    type_map.py           TypeFamily → 目標型別、轉換、⚠ 與問題（純函式）
    select_builder.py     build_copy_select（D-06-6）；型別片段的檢查與篩選共用 _single_expression
    literals.py           SQLite 非有限浮點、時區位移的正規化
    dialects.py           各 engine 的檔頭設定表、FK 檢查的開關
  services/
    safety.py             gate_confirmed（R-06-6）
    plans.py              OneTimePlanStore（由 edit_plans.py 抽出）
    data_service.py       dump plan、inspect、transfer plan
    history_service.py    begin()、finish()；啟動時 running → interrupted
  sessions/
    jobs/context.py       JobContext
    jobs/dump.py          DumpJob
    jobs/run_file.py      RunFileJob
    jobs/transfer.py      TransferSource、TransferSink、RowPipe
    worker.py             _run_job；job 最後的進度與結果在 attach 時重送
    manager.py            忙碌的 session 不 reap；JobRegistry
    hooks.py              gate、take_transfer_plan、source_worker
  api/data.py             /data/dump/plan、/data/run-file/inspect、/data/transfer/plan、/data/jobs、/data/jobs/cancel、/data/reveal
src/
  features/data-tools/    jobState.ts（純 reducer）、jobsStore.ts、useJobSession.ts、DumpDialog.tsx、RunFileDialog.tsx、
                          CopyDataDialog.tsx、MappingTable.tsx、JobsButton.tsx、entryPoints.ts、fileNames.ts、mappingRows.ts
  lib/native.ts           chooseOpenPath
  features/explorer/      關聯式節點的右鍵選單；features/table-data/、features/query/、features/scripts/、features/palette/ 的入口
  app/                    Sidebar 底部的 Jobs；視窗關閉與 ⌘Q 的 job 守衛
src-tauri/src/menu.rs     Quit 改成先問前端（A12）
```

〔06b：前端照實作是 `features/data-tools/` 的純模組 `jobState`、`jobReport`、`jobRunner`、`jobsStore`、`jobEndings`、`entryPoints`、`dumpForm`、`runForm`、`fileNames`（Vitest），`jobsApi`、`dataDialogs`，元件 `DataToolsHost`、`DumpDialog`、`RunFileDialog`、`JobDialog`、`JobViews`、`TargetPicker`、`FileLines`、`JobsButton`、`useJobEndings`；沒有 `useJobSession`（B4、V1）；`CopyDataDialog`、`MappingTable`、`mappingRows` 是 06c。守衛在 `src-tauri/src/quit.rs`（＋`menu.rs`、`lib.rs`）與 `src/app/{quitQuestion.ts, QuitGuard.tsx}`。backend 另加 `JobOutcomeEvent.resume_at`、`StoppedAt.exact`、`RunFileCommand.from_line`（`sessions/jobs/run_file.py`）與 `POST /data/run-file/excerpt`（`DataService.excerpt`）〕

不變的部分：`SessionWorker` 既有請求（execute、export、apply_edits、交易指令）的行為與事件；匯出對話框；⑤ 的 edit 流程（只換 plan store 的位置與 `edit_target` 的讀法）；Couchbase 的 adapter。

### 3.2 Job 框架（`sessions/jobs/`、`api/data.py`）

**WS 指令**（送在 job 的 session 上）：

```python
class DumpCommand(BaseModel):
    type: Literal["dump"] = "dump"
    request_id: str
    path: str                                  # 絕對路徑、資料夾已存在；經 .part 寫入
    scope: DumpScope                           # database | schema | tables（含勾選的名稱）
    content: Literal["schema", "data", "both"] = "both"
    objects: DumpObjectKinds = DumpObjectKinds()   # views、sequences、types、routines、triggers 各自的勾選
    drop_first: bool = False                   # 每個物件之前寫 DROP … IF EXISTS（檔頭記下，R-06-12）
    filter: str | None = None                  # 只在單一表：表格資料 tab 的篩選

class RunFileCommand(BaseModel):
    type: Literal["run_file"] = "run_file"
    request_id: str
    path: str
    mode: Literal["all_or_nothing", "statements"]
    on_error: Literal["stop", "continue"] = "stop"     # continue 只在 statements
    from_statement: int = Field(default=1, ge=1)
    search_path: str | None = None             # PG：從 schema 節點開時預填
    query_context: QueryContext | None = None  # Couchbase

class TransferCommand(BaseModel):
    type: Literal["transfer"] = "transfer"
    request_id: str
    plan_id: str = Field(min_length=1, max_length=64)
    source_session_id: str
```

**WS 事件**：`JobProgressEvent{request_id, stage, object, rows, bytes, total_bytes, statements, errors, line}`；`JobOutcomeEvent{request_id, job, outcome, cancelled, rows, bytes, statements, path?, objects: [{name, outcome, rows}], errors: [{line, statement, code, message}], error_count, stopped_at: {line, statement}?, warnings}`。`outcome` 依 job 不同（R-06-12）；`cancelled` 是一個旗標，不是另一種結果（取消的 run 是 `rolled_back` 或 `stopped`，帶 `cancelled=true`）。〔06b：`JobOutcomeEvent.resume_at: StoppedAt | None`（B1）——只有逐句的 run：從哪一句重跑才不會漏掉或重跑任何一句（被 rollback 而從計數拿掉的檔案交易的第一句，否則停下的那句；`COMMIT AND CHAIN` 之後從 chain 的下一句）；前面的語句做了什麼說不準時是 null（每一則「may」的警告、交易被 rollback 之後跳過的語句已生效、`GO n` 跑了一部分、MySQL 不能 rollback 的表或 Couchbase 上失敗的寫入、N1、N2、R2、R3、R10），斷線的 `unknown` 是唯一留著它的例外（那句可能已生效）。`StoppedAt.exact`：行號是不是那一句的開頭（不是 UTF-8 的行在語句中間、psql 指令、太大的語句、讀檔錯誤時是 false）。`RunFileCommand.from_line: int | None`：重跑時第 `from_statement` 句開始的行，不符就在送出它之前拒絕（`not_started`、`file_changed`，§4），只比那一行。N1 也套用到 server 把檔案交易 rollback 的路徑，06a 的 MySQL CALL 有少數結果從 stopped／completed 變 unknown〕

**REST**：

| endpoint | 輸入 | 輸出 |
|---|---|---|
| `POST /data/dump/plan` | `profile_id, path, scope, content, objects` | `DumpPlan{objects: [{kind, schema, name, rows_estimate, notes}], not_included: {kind: count}, consistency: "snapshot" \| "per_table", warnings}`（metadata 連線） |
| `POST /data/run-file/inspect` | `profile_id, path` | `FileInspection{size, header: DumpHeader?, preview: str（前 200 行、最多 64 KiB）, warnings}`；檔頭由 ⑥ 的 dump 寫，用來警告 dialect 不符與「會先 DROP」——只是提示，不是許可 |
| 〔06b〕`POST /data/run-file/excerpt`（B2） | `path, line`（1…2³¹−1） | `FileExcerpt{path, first_line, text, found, truncated}`：一個錯誤附近的行（inspect 的預覽只有前 200 行，錯誤多半在很後面）；行以 LF 數（同 run），從前 20 行起、最多 60 行與 64 KiB，前面的長行佔掉一半以上時從那一行本身開始（那一行一定看得到）；不連線、不看 profile（run 已經讀過這個檔）；不是檔案 404 `file_not_found`，檔案比那一行短是 `found: false` |
| `POST /data/transfer/plan` | `TransferRequest{source: SourceSpec, target: TargetSpec}`（§3.5.1） | `TransferPlan{plan_id?, tables: [TableTransfer], order, warnings, problems}`（有 problem 時 `plan_id` 是 null，同 ⑤ E8） |
| `GET /data/jobs`、`POST /data/jobs/cancel` | — ／ `session_id, request_id` | JobRegistry 的清單（D-06-10）；取消同 WS 的 `cancel` |
| `POST /data/reveal` | `path` | 只對這次 backend 執行裡 job 寫出的檔案執行 `/usr/bin/open -R`（Show in Finder；Tauri 要另裝 opener plugin，這樣沒有新相依） |

唯讀 profile：`/data/run-file/inspect` 與 `/data/transfer/plan` 對目標是 403 `read_only`（不連線）；worker 收到命令時再查一次。Dump 在唯讀 profile 照常可用。

### 3.3 Dump

#### 3.3.1 範圍與物件

| 入口 | 範圍 | 包含 |
|---|---|---|
| database 節點（PG、SQL Server） | database | 每個非系統 schema 的全部 |
| schema 節點（MySQL 的 database 也是 schema 節點）、SQLite 的 main | schema | 表、view（含 PG materialized view）、PG 序列與型別、routine、trigger、MySQL event、SQL Server synonym、安裝在這個 schema 的 PG extension（`CREATE EXTENSION IF NOT EXISTS … SCHEMA …`，plpgsql 除外） |
| Tables 資料夾 | 勾選的表 | 預設全勾 |
| 表節點、表格資料 tab | 一張表 | 那張表、它用到的 PG 序列與型別、它的 trigger（PG 另帶 trigger 呼叫的 function）；可帶目前的篩選 |
| view 節點 | 一個 view | 只有 DDL |

不包含（檔頭逐類列出數量）：權限、owner、角色；PG extension 自己的物件（`pg_depend` deptype `e`）與單一表範圍裡的 extension（檔頭列出表用到的 extension 型別）；SQLite 的 virtual table 與它的 shadow table（⑤ G4，寫入會弄壞索引）；SQL Server 的 filegroup／partition scheme（還原成沒有分割的表）。表或勾選的表的範圍裡，PG 與 SQL Server 的 FK 若指向不在這份 dump 裡的表就不寫（資料之後的 `ALTER TABLE … ADD CONSTRAINT` 在空的 database 上會失敗，全有或全無時整份 rollback），檔頭逐條列出；MySQL 與 SQLite 的 FK 在 CREATE TABLE 裡，還原時 FK 檢查是關的，照留。`drop_first` 的 DROP 不加 CASCADE（不碰這份 dump 以外的物件：有別的物件依賴時那一句失敗）。〔06b 修正波（最終審查 I2）：所以 plan 與結果事先說——catalog 在同一次讀取裡讀出不在檔案裡、卻依賴檔案會 DROP 的物件（`DumpCatalog.dependents`：範圍外的，或範圍內沒選的種類，例如 view 沒勾），`dump_script.drop_warnings` 每個被擋的物件一則「Drop objects first: <依賴者> (<種類>, not in this dump) depends on <物件>, so restoring where it exists fails at that DROP. Dump it too, or turn Drop objects first off.」（名稱照 `shown_names` 的上限），對話框排第一、紅色。PG：`pg_depend` deptype n 的依賴者（view、materialized view、rule、FK、trigger、別的表的欄位與預設值、operator、event trigger…，2BP01；一句查詢；表的 row type 的依賴者不說：以它為參數或回傳的 function、型別是它的別的表的欄位，還原時照樣 2BP01、誠實地 rollback，06b 複審的 Low）；SQL Server：FK（3726）與 schema-bound 的參照（3729；不是 schema-bound 的 view 不擋；在 catalog 的第一批裡）；MySQL 與 SQLite 不說：還原時 FK 檢查關著、DROP 不會失敗，view 讀得到重建的表（實測）〕

#### 3.3.2 檔案的樣子

```
-- DbaGuy SQL dump · format 1
-- Engine: postgresql 17.11 · database dbaguy · schema public
-- Created: 2026-10-20T12:34:56+08:00
-- Content: schema and data · 12 tables, 2 views, 3 sequences, 4 functions, 2 triggers
-- Snapshot: one consistent snapshot (REPEATABLE READ)
-- Not included: owners and privileges · 1 extension's objects
-- Options: drop objects first: no
<還原用的 session 設定（§3.3.3）>
<pre-data：schema、extension、型別、序列、routine（PG）、CREATE TABLE（PG／SQL Server 不含 FK）>
-- Data: public.orders
<INSERT …>
-- public.orders: 12,345 rows
<post-data：PG setval 與 OWNED BY、index（PG／SQL Server）、FK（PG／SQL Server）、view、routine（MySQL、SQL Server）、trigger、MySQL event、SQL Server synonym、設定還原（MySQL FOREIGN_KEY_CHECKS = 1、SQLite foreign_keys = ON）>
-- Dump complete: 12,345 rows in 12 tables
```

`drop_first` 時每個 CREATE 前寫 `DROP … IF EXISTS`（表依 FK 反序；SQL Server 用 `IF OBJECT_ID(N'…') IS NOT NULL DROP …`）。〔06b 修正波（F-a）：PG 父表也在 dump 裡的 partition 是 `partition` 種類的物件——沒有自己的 DROP（跟著父表的），列經父表讀；檔頭的 Content 另計（「6 tables, 1 partition」），plan、檔頭與結果的表數一致（以前 plan 把它算成表、多一張）〕內容只有 data 時沒有 pre／post-data，但仍有設定、identity 的處理與 PG 的 setval。

#### 3.3.3 各 engine

| | PostgreSQL | MySQL | SQL Server | SQLite |
|---|---|---|---|---|
| dump session 的設定 | `DateStyle = ISO, MDY`、`IntervalStyle = postgres`、`extra_float_digits = 3`、`search_path = ''`、`TimeZone = 'UTC'` | `USE <db>`、`SET NAMES utf8mb4`、`time_zone = '+00:00'` | — | — |
| 快照 | REPEATABLE READ, READ ONLY | CONSISTENT SNAPSHOT, READ ONLY | SNAPSHOT（允許時），否則逐表 | 一個讀取交易 |
| 檔頭的還原設定 | `SET client_encoding = 'UTF8'`、`SET standard_conforming_strings = on`、`SET check_function_bodies = false` | `SET NAMES utf8mb4`、`SET FOREIGN_KEY_CHECKS = 0`、`SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO'`、`SET time_zone = '+00:00'` | `SET NOCOUNT ON`、`SET QUOTED_IDENTIFIER ON`、`SET ANSI_NULLS ON`，`GO` | `PRAGMA foreign_keys = OFF`、`PRAGMA defer_foreign_keys = ON` |
| 表的 DDL | `table_ddl`（不含 FK）；partition 是 `PARTITION OF` | `SHOW CREATE TABLE`（FK 在裡面，靠 FOREIGN_KEY_CHECKS；保留 `AUTO_INCREMENT=n`〔實測〕） | `_table_ddl`（不含 FK） | `sqlite_master.sql`（FK 在裡面，靠 PRAGMA） |
| 讀資料 | `SELECT … FROM ONLY t`；partitioned 從父表 | `SELECT …` | `SELECT …`，scale 7 的時間 CAST | `SELECT …` |
| 不寫的欄 | generated | VIRTUAL／STORED GENERATED | computed、rowversion、sql_variant | generated（hidden 2／3） |
| identity | ALWAYS：`OVERRIDING SYSTEM VALUE`；資料後 `setval` | 明確寫入（`NO_AUTO_VALUE_ON_ZERO` 保住 0） | `SET IDENTITY_INSERT … ON／OFF`（〔實測〕之後的值自動接在最大值後） | 明確寫入（〔實測〕`sqlite_sequence` 跟著更新） |
| 序列 | standalone 與 serial 的 `CREATE SEQUENCE` 在表之前；資料後 `SELECT setval('s', last_value, is_called)`、`ALTER SEQUENCE … OWNED BY` | — | `CREATE SEQUENCE … START WITH <下一個值>`：`last_used_value` 是 NULL（從沒用過）時是 `start_value`，否則 `current_value + increment`（〔實測〕附錄 A S11） | — |
| index、FK | 資料之後 | 在 CREATE TABLE 裡 | 資料之後 | index 在資料之後 |
| view 的順序 | `pg_depend`（經 `pg_rewrite`） | `VIEW_TABLE_USAGE`（〔實測〕8.4 可用） | `sys.sql_expression_dependencies` | `sqlite_master` 順序（〔實測〕建 view 時不檢查表在不在） |
| routine | 表之前（`check_function_bodies = false`）；簽名用到表的 row type 的在那張表之後（`pg_depend`，〔實測〕P9）；extension 的不寫 | 表之後、view 之前（〔實測〕M10），`DELIMITER ;;` 包起來，去掉 DEFINER | 不 schema-bound 的 function 在表之前（延遲名稱解析）；schema-bound 的與 view 一起依相依排序；procedure 在 view 之後 | — |
| trigger | 資料之後 | 資料之後，`DELIMITER` | 資料之後（各自一個批次） | 資料之後 |

依相依排序時，相依資訊不完整（環、動態 SQL）就照 catalog 建立順序（`oid`／`object_id`／rowid）排在已排好的之後；還原時某個物件失敗，「逐句＋跳過並繼續」可以完成其餘的，結果列出失敗的物件。

#### 3.3.4 資料與檔案

- 每張表：`build_copy_select` → `ctx.run` → 每次 `fetch(1000)`〔06a：見〔06a〕執行時的其他決定的「Dump 的讀取」〕 → `InsertBatcher` → 寫檔；每塊之後檢查取消、節流送進度。表的列數估計（PG `reltuples`、MySQL `TABLE_ROWS`、SQL Server `sys.partitions`、SQLite 沒有）只用於進度，寫明 ≈。〔06b 修正波（F-e）：MySQL 8.x 把 `TABLE_ROWS`／`DATA_LENGTH` 快取 `information_schema_stats_expiry`（預設一天），10 萬列的表顯示「≈ 41 rows」，CR-4 的空間檢查也讀它：dump 的 session 送 `/*!80003 SET SESSION information_schema_stats_expiry = 0 */`，plan 在共用的 metadata 連線上只為 TABLES 那一句設、之後 `= DEFAULT`（5.7 跳過 version comment）；dev 上 plan 的時間不變（7–11 ms）〕
- 寫檔沿用 `atomic_text_file`；`OSError`（磁碟滿等）→ `dump_io`，`.part` 刪掉。
- 檔名預設 `<database>_<schema 或表>_<yyyyMMdd-HHmm>.sql`（去掉 macOS 不收的 `/`、`:`）。

### 3.4 Run SQL file

#### 3.4.1 流程（`RunFileJob`）

1. 開檔（不是一般檔案、讀不到 → `file_not_found`）；以 `StatementStream(dialect)` 讀。
2. PG 有 `search_path` 時送 `SET search_path TO …`（引號由 `quote_ident`）；全有或全無時送交易開頭。
3. 每句：`hooks.gate(sql)`（R-06-6）→ 全有或全無時先擋檔案自己的交易語句 → `ctx.run(sql, RunOptions(describe=False, query_context=…))` → 讀完並丟掉它的結果（不顯示）、轉送訊息 → 全有或全無時確認 `in_transaction()`。語句數小於 `from_statement` 的只切不送。
4. 錯誤：`stop` → 停；`continue` → 記下（行號、code、server 原文，D-06-11），下一句。
5. 全有或全無：COMMIT 之前 `_set_running(None)`（之後的 Cancel 到不了 driver，同 ⑤ §3.5）、確認仍在交易中，送 dialect 的 COMMIT；COMMIT 被拒先 rollback 再 `rolled_back`（同 ⑤ E10）。
6. 結束時讓這個 profile 的 schema 快取失效（不逐句分類，所以不知道哪句是 DDL：一律失效）。

#### 3.4.2 各 engine

| | PostgreSQL | MySQL | SQL Server | SQLite | Couchbase |
|---|---|---|---|---|---|
| 切句 | `;`、`$$`、`BEGIN ATOMIC`；psql meta 行（D-06-3） | `;`、`DELIMITER`、版本註解原樣送出 | `GO [n]`（n 次） | `;`、trigger 的 BEGIN…END | `;` |
| 預設模式 | 全有或全無 | 逐句 | 全有或全無 | 全有或全無 | 逐句（只有它） |
| 全有或全無的限制 | `CREATE INDEX CONCURRENTLY` 等不能在交易裡的語句會失敗 → rolled_back | DDL 隱含 commit → 停、partial | `CREATE DATABASE` 等 → 失敗 → rolled_back | 檔案的 `BEGIN TRANSACTION`（`sqlite3 .dump`）→ 停，改逐句 | — |
| 不支援 | `COPY … FROM stdin`（adapter 的 `unsupported_copy`；訊息加上「pg_dump：用 `--inserts`，或用 psql」） | — | sqlcmd 的 `:r`、`:setvar`（送到 server 是語法錯誤） | `.` 開頭的 sqlite3 指令（同上） | — |

#### 3.4.3 別的工具寫的檔案

- `mysqldump`：`/*!40101 SET … */` 版本註解（server 會執行，DbaGuy 不分類所以不影響）、`LOCK TABLES … WRITE`、`DELIMITER ;;` 包 trigger 與 routine：逐句模式跑完（A8）。
- `pg_dump --inserts`：`\restrict` 跳過、`SET`、`SELECT pg_catalog.set_config('search_path', '', false)`、`ALTER … OWNER TO`（角色不在就失敗：用「跳過並繼續」或 `pg_dump --no-owner`）。純文字的預設格式用 COPY，跑不了（上表）。
- SSMS「Generate Scripts」：`GO` 分隔、`SET ANSI_PADDING` 等：可以。
- `sqlite3 .dump`：`PRAGMA foreign_keys=OFF; BEGIN TRANSACTION; … COMMIT;`：逐句模式可以，全有或全無在它的 BEGIN 停下並說明。

### 3.5 Copy data

#### 3.5.1 請求與預覽

```python
class SourceSpec(BaseModel):       # tables 與 query 其一
    profile_id: UUID; database: str | None
    tables: list[list[PathSegment]] | None = None   # 表、view、materialized view
    query: QuerySource | None = None                # sql、params、query_context、結果的欄位（前端已有，預覽不重跑）
    filter: str | None = None                       # 只在單一表

class TargetSpec(BaseModel):
    profile_id: UUID; database: str | None; schema: str | None
    table: str | None = None                        # 單一來源時可改名；多表時同名
    mode: Literal["create", "append", "replace_rows"]
    overrides: dict[str, dict[str, str]] = {}       # 表 → 欄 → 目標型別（R-06-10）
    identity: dict[str, dict[str, Literal["keep", "generate"]]] = {}
```

`TransferRequest{source: SourceSpec, target: TargetSpec}`；`TableTransfer{source, target, mode, columns: [ColumnMapping{source_name, source_type, family, target_name, target_type, conversion, note, skipped}], before: [sql], after: [sql], rows_estimate, warnings, problems}`。`before` 是建表或 DELETE、PG 的 `OVERRIDING` 與 SQL Server 的 `IDENTITY_INSERT` 的說明，`after` 是 PG 的 `setval`；對話框顯示原文（同 ⑤ 的預覽）。上限：一次最多 500 張表，型別片段最多 200 字元。

〔06c：照上面做，改了這些（C1，`domain/transfer.py`）：`TargetSpec.schema`（會遮住 pydantic 的 `BaseModel.schema`）改名 `schema_name`；加 `TargetSpec.left_out`（每張來源表 → 不複製的來源欄，R-06-10 的「排除該欄」）；`QuerySource{sql, columns, title}`（`params`、`query_context` 只有 Couchbase 有，而 Couchbase 不 copy，R-06-14；`title` 是訊息裡怎麼稱呼它：tab 的名稱）；`TableTransfer` 加 `source_key`（`overrides`／`identity`／`left_out` 以它為 key）與 `source_path`、拿掉 `rows_estimate`（進度說已讀的列數，不說比例）；`ColumnMapping` 加 `type_editable`、`overridden`、`identity`、`problem`。`TransferPlan{plan_id, source_profile_id, tables, warnings, problems}`：任何一張表或整個 copy 有問題時 `plan_id` 是 null（⑤ E8）。請求的上限：query 的語句 1 MiB（`MAX_QUERY_CHARS`）、4,096 欄、篩選 65,536 字元（同 dump）、型別文字收 1,024 字元，超過 200 字元（`TYPE_CHARS`）是 `invalid_type`。`POST /data/transfer/plan`：唯讀的目標 403、沒有 `copy_data` 的 engine（Couchbase）422 `not_supported`，都在連線之前；兩邊各在自己的 metadata 連線上讀（每張表一次 `table_columns`，加上目標的 edit target，不做 ⑤ 的 EXPLAIN：`edit_target(order=False)`）；缺密碼時 `password_required` 的 `detail.profile_id` 說是誰的（pre-flight M3）；PG／SQL Server 的連線沒指定 database 時讀它的 `current_database()`（pre-flight M2）。預覽不寫任何東西、不跑 query〕

#### 3.5.2 型別對照（建新表、跨 engine；同 engine 照抄宣告的型別）

| 家族 | PostgreSQL | MySQL | SQL Server | SQLite |
|---|---|---|---|---|
| bool | boolean | tinyint(1) | bit | INTEGER |
| int16／int32／int64 | smallint／integer／bigint | smallint／int／bigint | smallint／int／bigint | INTEGER |
| uint64（MySQL BIGINT UNSIGNED） | numeric(20) | bigint unsigned | decimal(20,0) | TEXT ⚠ |
| decimal(p,s) | numeric(p,s) | decimal(p,s)，p > 65 或 s > 30 是問題 | decimal(p,s)，p > 38 是問題 | TEXT ⚠ |
| decimal 沒有精度（PG `numeric`） | numeric | decimal(65,30) ⚠ | decimal(38,10) ⚠ | TEXT ⚠ |
| money（PG 以 `::numeric` 讀，D-06-6） | money（PG 來源）／numeric(19,4) | decimal(19,4) | money | TEXT |
| float32／float64 | real／double precision | float／double | real／float | REAL |
| char(n) | char(n) | char(n)（n > 255 → varchar） | nchar(n) | TEXT |
| varchar(n) | varchar(n) | varchar(n)（n > 16,383 → longtext） | nvarchar(2n)（> 4,000 → max）⚠ | TEXT |
| text | text | longtext | nvarchar(max) | TEXT |
| binary | bytea | varbinary(n)／longblob | varbinary(n)／varbinary(max) | BLOB |
| date | date | date | date | TEXT |
| time | time | time(6) | time(7) | TEXT |
| duration（MySQL `TIME`：可以超過 24 小時、可以是負的） | interval | time(6) | time(7) ⚠ 超過 24 小時或負的那一列失敗 | TEXT |
| year（MySQL） | smallint | year | smallint | INTEGER |
| timestamp | timestamp | datetime(6) | datetime2(7) | TEXT |
| timestamp_tz | timestamptz | datetime(6) ⚠ UTC | datetimeoffset(7) | TEXT（ISO，±HH:MM） |
| time_tz（PG timetz） | timetz | time(6) ⚠ 去掉位移 | time(7) ⚠ | TEXT |
| interval | interval | varchar(64) ⚠ 文字 | nvarchar(64) ⚠ | TEXT |
| uuid | uuid | char(36) | uniqueidentifier | TEXT |
| json | jsonb（PG `json` 來源 → json；SQL Server 與 SQLite 的 JSON 是文字，進 jsonb 會重排 key、去掉重複的 key ⚠） | json | nvarchar(max) | TEXT |
| xml | xml | longtext | xml | TEXT |
| enum／set | text | 同定義（MySQL 來源）／varchar(255) | nvarchar(255) | TEXT |
| array、range、inet、hstore 等（PG） | 同型別 | longtext ⚠ PG 的文字 | nvarchar(max) ⚠ | TEXT |
| spatial | 同 engine：宣告的型別；跨 engine：**問題** | | | |
| sql_variant、hierarchyid（跨 engine） | **問題** | | | |
| rowversion | 不複製（目標自己產生；建表時不建這一欄） | | | |

⚠ 在預覽逐欄說明。SQLite 的 TEXT ⚠：NUMERIC／INTEGER affinity 會把像數字的文字轉成 INTEGER／REAL，超過 15 位有效數字或尾端的 0 就不見（⑤ G1 的同一類問題），存成 TEXT 才保住每一位。SQL Server 的 `nvarchar(n)` 以 UTF-16 code unit 計：PG `varchar(10)` 的 10 個 emoji 要 20 個單位。位元型別：MySQL `BIT(n)` 由 adapter 讀成整數（`_bit`，〔實測〕附錄 A M7），家族是 int64；PG `bit`／`varbit` 讀成 `'1010'` 文字，跨 engine 是 text。query 結果的來源沒有精度資訊時（ColumnMeta 只有 `type_name` 與 category），家族由型別名推，推不出來就依 category（NUMBER → decimal 沒有精度、TEXT → text、DATETIME → timestamp、BINARY → binary、BOOL → bool、JSON → json、其他 → text）；預覽可改。

〔06c：表照上面建（`type_map.target_type`；每一格都是 `test_type_map.py` 的一個 case），另外：PG `timetz` → MySQL `time(6)` 去掉位移（規劃時的 4×4 探測找到的，A9）；同 engine 的使用者自訂型別見 R-06-10 的〔06c〕（C13）；SQL Server 第 7 位小數進 PG、MySQL 會被 server 四捨五入、MySQL 的 `json` 會重排 key、留一個重複的 key、改寫小數（`1.10` → `1.1`）與超過 64 位的整數：都是說明（⚠），不是問題（Task 1 審查 I1、I2；要原文就選 longtext）；PG `jsonb` 從別的 engine 來也有說明。附加或使用者打的型別也照這些說明（`conversion` 對實際的目標欄說，不只建表時）。文字與 binary 互不進對方的欄（`incompatible`：連附加進既有的 binary 欄也是問題，因為 PG 的 bytea 會把文字當成跳脫讀；Task 1 審查之後的裁決）。建新表的 key 欄型別見 R-06-9 的〔06c〕〕

#### 3.5.3 值的轉換（`type_map.convert`，純函式；其餘都是「原值，依目標欄位種類寫 literal」）

| 來源 → 目標 | 轉換 |
|---|---|
| bool → 整數／bit | 1／0 |
| bool → 文字 | `true`／`false` |
| timestamp_tz → 沒有時區的型別 | 轉成 UTC 再去掉位移（PG 來源 session 設 `TimeZone = 'UTC'`；SQL Server `datetimeoffset` 由 Python 換算）；〔實測〕附錄 A S10：SQL Server `CAST(N'…+00:00' AS datetime2)` 直接丟掉位移、不換算，所以不能交給 server |
| timestamp_tz → datetimeoffset／TEXT | 位移寫成 `±HH:MM` |
| uint64、decimal → SQLite TEXT | 加引號的文字 |
| 文字 → binary、binary → 文字 | 問題（不轉） |
| PG `infinity`、西元前的日期、MySQL `0000-00-00` → 其他 engine | 那一列失敗，訊息指出表、行與欄（不改成 NULL：Q3；篩掉或把目標型別改成文字） |

〔06c：位移一律在 Python 換成 UTC（`type_map.to_utc`，每一位小數都留著，SQL Server 的第 7 位也是），來源 session 不改 `TimeZone`（C5：改 query tab 的 session 會改到使用者的；一個轉換只有一條規則）。來源 session 自己的設定在每張表的讀取前讀、設成 copy 要的、讀完設回（`transfer_sql.SOURCE_SETTINGS`）：PG `DateStyle = 'ISO, MDY'`、`IntervalStyle = 'postgres'`；MySQL `time_zone = '+00:00'`（TIMESTAMP 讀成 UTC）與 `net_write_timeout = 3600`（A11）。不能裝的值（C16）：PG 的 NaN 進 SQLite 是那一列失敗（06a 的 dump 寫 NULL 是對的：dump 的是 SQLite 自己，沒有 NaN）、±infinity 寫 `9e999`；「NaN 與 infinity 失敗」的說明只給 PG 或 SQLite 的 float、decimal 或沒有型別的欄進 MySQL、SQL Server；整數進 bool 欄只收 0 與 1，其他那一列失敗。失敗的列是 `RowFailure` → job 的 `value_not_copyable`：「Row N of <source>, column <c>: <原因>」，整張表 rollback。轉換函式每張表每對欄選一次（`type_map.conversion`；`None`＝driver 讀的原值直接以目標欄的 literal 寫）〕

#### 3.5.4 目標表

- **建新表**：`CREATE TABLE <schema.name> (<欄> <型別> [NOT NULL], …, PRIMARY KEY (…))`（來源沒有 PK 就沒有）。PG、SQL Server、SQLite 與資料同一個交易；MySQL 隱含 commit（R-06-12）。
- **附加**：目標欄位由 `table_columns` 讀（⑤ 的拒絕條件照用：PG `standard_conforming_strings` off、SQLite shadow table、SQL Server history／graph／ledger 表、view）；依名稱對應（完全相同優先，其次不分大小寫且唯一）。來源有、目標沒有的欄：略過（警告）；目標有、來源沒有的欄：用目標的預設值，NOT NULL 又沒有預設值是問題；目標的 generated、rowversion 欄不寫。型別不同時照 §3.5.3 轉換或是問題。MySQL 目標的 `sql_mode` 沒有 `STRICT_TRANS_TABLES` 時警告（太長的字串會被截斷，⑤ L16）；SQL Server 非 Unicode 欄警告（R-06-11）；不能 rollback 的表警告（⑤ `non_transactional`）。
- **先刪除既有的列**：附加，加上交易開頭之後的 `DELETE FROM <t>`（`DELETE_WITHOUT_WHERE`，預覽 danger 色）。

〔06c：目標 session 跑 dump 的還原設定（R-06-9 的〔06c〕，C4），MySQL 的 session 一律是嚴格的 `sql_mode`：太長的值讓那句失敗、整張表 rollback，不會被截斷，所以沒有「不是嚴格模式」的警告。表的順序與環狀的警告、cascade 見 R-06-9 的〔06c〕（C11）。來源裡 dump 不寫的欄（`TableColumn.copied` 是 false：generated、computed、rowversion、`sql_variant`、period 欄）不複製，對照表列出原因（「not copied: …」）；附加從不寫目標的 computed／generated 欄（「the target computes it」）；兩者都不是問題（C12）。PG 繼承的父表照表格資料 tab 的讀法、不加 `ONLY`：父表的 copy 含子表的列；partition 照 dump 經它的 root 讀（C15）。依名稱對應之外：兩個來源欄進同一個目標欄、或建表時兩個欄名在 MySQL、SQLite、SQL Server 只差大小寫，是 `duplicate_column`。不能 rollback 的表：附加警告 `NON_TRANSACTIONAL`，取代另有 `NON_TRANSACTIONAL_REPLACE`（DELETE 立刻生效、失敗後刪掉的列不會回來，pre-flight H3）。建新表在 MySQL 失敗後表留著：預覽說「(empty, unless the server's default engine cannot roll back)」（`CREATED_STAYS`）〕

#### 3.5.5 執行（`TransferSink` 在目標 worker、`TransferSource` 在來源 worker）

1. sink 取 plan（`hooks.take_transfer_plan`，`plan_expired`／`plan_mismatch`）、讀目標 session 的實際設定（MySQL 的反斜線模式由交易開頭的 OK 封包得知，同 ⑤ M2；PG 的 standard strings）——INSERT 的 literal 依這個 session 寫，不依預覽時的 metadata 連線，所以 ⑤ M2 那種不一致不會發生。
2. 每張表依 plan 的順序：交易開頭 → `before` → 經 `hooks.source_worker(source_session_id)` 在來源 worker 提交 `TransferSource`（`build_copy_select`；query 來源重跑那一句，欄位與 plan 不同就 `columns_changed`）→ 從 `RowPipe` 取塊 → `InsertBatcher` → `ctx.run` 每句（`gate_confirmed`，同 R-06-6：完整的 `check()` 分類一句 1,000 列的 INSERT 要 154–212 ms；確認符合的列數等於這句的列數，不符就失敗：trigger 跳過或改寫，同 ⑤ D-05-5 的 fail closed）→ `after`（PG `setval`）→ COMMIT。
3. MySQL 與 SQLite 在第一張表之前關 FK 檢查（R-06-9），結束後恢復；SQL Server 的 `IDENTITY_INSERT` 每次只能開一張表，所以每張表自己開、自己關。
4. 取消 sink → 取消 source（RowPipe 關閉，source 在下一次放入時以 `StatementCancelled` 結束）→ rollback 這張表 → 後面的表 `not_started`。source 出錯（server 錯誤、斷線）→ 錯誤經管線到 sink → rollback，訊息標明是來源的錯誤（`source_failed`，帶來源的原文）。
5. 管線滿時 source 在自己的 thread 等待（每 100 ms 看一次取消）；這段時間 MySQL 來源的 server 也在等，超過 `net_write_timeout`（預設 60 秒）會結束讀取（A11）。

〔06c：照上面做（`sessions/jobs/transfer.py` 的 `TransferSink`、`transfer_source.py`、`pipe.py`），細節：
- 1：plan 在 `hooks._copy` 取走一次（只有 plan 的目標 profile 與 database 的 session 取得到），之後依序：唯讀的拒絕、來源的 lease（C2）、一筆 History（`running`）、目標的 `TARGET_SETUP`（安靜地送，C4）。MySQL 的反斜線模式在每張表的 BEGIN 之後讀。
- 2：MySQL 的 CREATE TABLE 在 BEGIN 之前（它隱含 commit），其他在交易裡；每張表開始時再讀一次目標欄（`table_columns`；預覽之後被 DROP 的表是 `columns_changed`，pre-flight L-c）。source 經 `JobHooks.source` 拿到的 worker 跑 `TransferSource`（C3），每塊最多 1,000 列（`FETCH_ROWS`）且列的文字最多 4 MiB（`PIPE_CHUNK_CHARS`），讀的大小依目前最大的一列（`fetch_size`，從 dump 移到 `context.py` 共用），所以記憶體跟著列的大小走。query 來源：計畫時與執行時都只收單純的讀取（`plain_read`，與 dump 的篩選同一個檢查；`query_not_select`，文字是預覽的那一句），欄位要和預覽的同名（`columns_changed`），只收一個結果（T-SQL 批次的第二個結果會被當成第一個，pre-flight M4），tab 暫停中的結果先結束（同匯出），預覽警告 query 會重跑（`QUERY_RUNS_AGAIN`，C18）。每句 INSERT 過 `gate_confirmed`，符合的列數經 `read_matched`（⑤ 的 apply 與 copy 共用）比對：不符是 `row_count_mismatch`、讀不到（trigger 回傳結果集）是 `row_count_unknown`，整張表 rollback（A16）。COMMIT 之前交易已不在是 `transaction_ended`（rolled_back）；COMMIT 回來了交易卻還開著是 `commit_unconfirmed`；COMMIT 途中斷線、SQL Server trigger 自己 COMMIT（3609）是 unknown。表裡的程式錯誤只讓那張表失敗（「Internal error」，traceback 不帶 locals）。MySQL 每張表多兩句：表開始時讀 engine、rollback 之後的 `SHOW WARNINGS`（R-06-12 的〔06c〕）。
- 3：見 R-06-9 的〔06c〕；SQL Server 的 `IDENTITY_INSERT` 每張表自己開、在它的 `after` 裡關。
- 4：sink 先停時關管線（`RowPipe.close`），source 下一次放入時停（`PipeClosed`），結果提早關閉（每個 engine 都安全）；query tab 的讀取從不取消（R-06-8 的〔06c〕，pre-flight H2）。source 的錯誤經管線到 sink（`SourceFailed`，code `source_failed`，帶來源的原文與 code），開不了來源 session（密碼、server、tab 不見了）是「The source connection could not be opened: …」帶那個錯誤的 code，什麼都還沒送（C2）；來源 profile 在預覽之後被刪掉是 `SOURCE_UNAVAILABLE` 帶 `profile_not_found`。Cancel 見 R-06-12 的〔06c〕（C6）。
- 5：MySQL 來源 session 設 `net_write_timeout = 3600`、讀完設回（A11，C5）。copy 自己開的來源 session 在最後一次讀取把設定設回之後才關（最多等 `SOURCE_END_WAIT_S` = 10 秒）；query tab 的 lease 立刻放掉。
- Log：INFO 記 copy 的表、列數、每張表的結果與 code（worker 記 job 的結束）；unknown 的表、rollback 失敗、來源的設定沒設回是 WARNING；從不記值〕

### 3.6 Frontend（06b）

- **DumpDialog**：標題「Dump <schema> to a SQL file」；連線名稱與顏色；物件清單（表的 ≈ 列數、view、序列、routine、trigger，可勾選）；內容（Schema and data／Schema only／Data only）；選項（Drop objects first、Only rows matching the filter）；`DumpPlan` 的警告（「Not a consistent snapshot」、「sql_variant column x is not written」、「routines are not included」…）；檔案（Choose… 的 save panel，副檔名 `.sql`；瀏覽器模式打絕對路徑，同匯出）。執行中：目前的物件、第幾個、列數、MB、經過時間；可以關閉（job 照跑）、Cancel。結果：路徑、大小、物件數、警告；Show in Finder。
  〔06b：「可勾選」只在 Tables 資料夾逐表勾（預設全勾，最多 500 張，超過不讀 plan 並說明）；其他範圍勾的是物件種類：schema 與 database 是 engine 的種類，表、partition、Tables 資料夾只有 Triggers，view 沒有（B6、V14：`DumpScope` 只收 database、schema 或同一 schema 裡指名的表，逐物件挑會把 schema 的 dump 變成 tables 的 dump、丟掉它的 routine）。不適用的選項灰著、不勾（送出的就是畫面上的），原因寫在下面。「第幾個」來自開始時 plan 的表順序（`JobProgressEvent` 只帶物件名），重新載入後列出的 job 沒有（B7、V15）。plan 是 `useQuery`（`retry: false`、`gcTime: 0`：PG 的 plan 可能等鎖 5 秒，不自動重試），整個請求停 400 ms 才讀，讀新的時舊的 plan 與警告變淡、它的表不傳給 job（V16）；篩選加 Drop objects first 的 F2 警告排第一、紅色〔06b 修正波：被擋的 DROP（I2，見 §3.3.1）也是〕。檔案一開始是空的（前端不知道使用者的資料夾，`~` 不是絕對路徑），save panel 預設 `dumpFileName`（V20、B10：MySQL 的 database 不寫兩次、SQLite 用檔名的主幹、`main` 不寫、Tables 資料夾加上 `<n>-tables`（〔06b 修正波 F-f〕一張都沒勾時不寫）；去掉 `/`、`:`、控制字元，主幹最多 200 bytes 的 UTF-8）。Show in Finder 在瀏覽器模式也有（backend 在這台 Mac 上 `open -R`，V12）；結果另有 Dump again…（回到表單，V2）；〔06b 修正波〕大小不到 1 KiB 時寫 bytes（F-f），自己按的 Cancel 的錯誤框是 muted、不重複「Query cancelled」（F-d）。那個表格資料 tab 有暫存時說「Staged changes are not in the dump: it reads what the server holds」。匯出對話框改用同一個路徑列（`FilePathField`、`safeFileStem`、`pathProblem`、`formatElapsed`，經過時間寫成「2 min 05 s」，V19）〕
- **RunFileDialog**：目標（連線、database；PG 的 search path）；檔案（Choose… 的 open panel；`lib/native.ts` 新增 `chooseOpenPath(filters)`，`dialog:allow-open` 已有權限）；inspect 的結果（大小、DbaGuy 檔頭的 engine 與選項、前 200 行的唯讀預覽、警告：「written for PostgreSQL, the target is MySQL」、「drops and recreates objects」）；模式（All or nothing／Statement by statement＋Stop／Skip and continue），不適用的選項灰著並說明；Start at statement N；Run（紅色連線用 danger 樣式，同 ⑤）。執行中：MB／總 MB、語句數、錯誤數、目前的行號、ETA。結果：outcome 的說明、錯誤清單（行號、server 原文；點了以唯讀預覽顯示那一段）、「Run again from line N」與「Skip it and continue」。
  〔06b：目標是 engine 有 `run_file` 的連線，唯讀的灰著並說明（backend 在送出前就拒絕）；database 的選單在 MySQL 列 schema；Couchbase 的 query context 來自 bucket／scope 節點或連線的預設（undefined＝預設、null＝不帶、物件＝指定），對話框裡沒有 picker（B11、V13）。路徑欄的 Return 只讀檔、從不執行，讀完焦點到 Run（紅色連線先到勾選），下一個 Return 才執行（V5）。預覽與錯誤附近的行是有行號的唯讀行（`FileLines`），不是 CodeMirror（V10：CodeMirror 的行號從 1 開始）；預覽先被 64 KiB 截斷時寫「The first N lines (the first 64 KiB)」。選了的模式變成不適用（換到 Couchbase、DbaGuy 的檔少了檔尾）就換成逐句；全有或全無時「Skip it and continue」灰著、不勾。錯誤清單（行號、label、server 原文的第一行；backend 保留前 100 則）每一則都可以 Show：讀 `/data/run-file/excerpt`（B2），那一行上色、捲到中間。「Run again from line N」只依 `resume_at`（B1），「Skip it and continue」只在 `resume_at` 就是停下的那句、沒有取消、code 可以跳過時；這兩個與 Run statement by statement…、Run the rest statement by statement…、Run again… 都回到預先填好的表單，不直接執行（B5、V2：CR-3 的勾選再問一次、檔案再讀一次）；預設的起點只在原本的連線、database、search path 與檔案有效，換了就回到第 1 句、不帶 `from_line`，回到原處就恢復。重跑帶 `from_line`（`exact` 不是 false 時），Skip 與 Run the rest… 不帶（不知道下一句的行）。ETA 在 3 秒、讀了 1 % 之後才出現〕〔06b 修正波：逐句停下而沒有 `resume_at` 的不提供任何 run again（R-06-7 的〔06b 修正波〕，I1）；`file_changed` 的 Run again… 回到的表單在檔案上方說「The file changed since the last run: check where to start.」（起點是被拒的那一句的號碼）；預覽與錯誤附近的行底下留水平捲軸的位置（F-c）；自己按的 Cancel 的錯誤框是 muted（F-d），run 的 Show line N 留著〕
- **CopyDataDialog**：來源（表、勾選的表、query 結果：tab 名稱與語句的第一行）；目標（連線、database、schema、表名、模式）；預覽：每張表一段，欄位對照表（來源欄／型別 → 目標欄／型別，建新表時型別可改、附加時 identity 可選 keep／generate，⚠ 與問題標在那一格），`before`／`after` 的 SQL 原文，警告與問題；有問題時沒有 Copy 按鈕。執行中：逐表的列數與狀態；Cancel。結果：逐表 copied／rolled back／not started／unknown 與說明。〔06b：不在 06b，連同 MappingTable、`mappingRows` 與 Copy 的入口是 06c〕〔06c：建成（`CopyDataDialog.tsx`、`MappingTable.tsx`；表單與對照的規則在純模組 `copyForm.ts`、`mappingRows.ts`，Vitest 在 `copy.test.ts`）：From（來源連線、表或勾選的表；query 結果是 tab 的名稱與語句的第一行（`statementFirstLine`，hover 看前 1,000 字元））；To（`TargetPicker` 的 `tool="copy_data"`：Couchbase 不在清單、唯讀的灰著並說明；database、PG／SQL Server 的 schema（不列系統的 schema；目標還沒有 database 時灰著並說明）、表名（單一來源，query 預設 `query_result`）、三種模式與它們的說明）；Schema 或 Tables 資料夾的來源逐表勾選（`TableChecks`，與 dump 共用，預設全勾，最多 500 張）；預覽是 `useSettledRead`（與 dump 共用：選擇停 400 ms 才讀，舊的變淡、從不拿來執行）。每張表一段：對照表（來源欄／型別 → 目標欄／型別，建新表的型別可以改（離開欄位或按 Return 才送）、可以取消勾選排除那一欄、附加時 identity 選 keep 或 generate；⚠ 與問題在那一格），`before` 的語句、「— the rows are copied here —」、`after` 的語句（`DELETE` 是 danger 色）；超過 40 張表時先畫有問題的表與前面的，「Show all」畫全部；問題在上面的紅框、警告各一次（兩者都有高度上限）；有問題時 Copy 停用並說原因（不是沒有按鈕）。紅色（production 色）的目標讓 Copy 是 danger 鈕，**不另外要勾選**（C9：copy 的每一句在預覽裡都看得到，INSERT 只加列，同 ⑤ 紅色連線的 Commit）；按住的 Return 不會按到它（`ignoreHeldReturn`）。執行中**不是**逐表的列數與狀態（pre-flight L-h）：是目前的表與第幾個（「Copying <table> (k of n)」）、讀了的列數（「N rows read」：那張表還可能 rollback），逐表的結果只在結束時列出（`CopiedTables`，unknown 的表不印列數）；Jobs 的那一列也有「(k of n)」（這個視窗開始的 job，dump 也一樣）。結果的動作（C17）：什麼都沒套用（rolled back、not started）是「Copy again…」（原本的表單）；做到一半（partial、failed、取消）是「Copy the rest…」（只勾沒複製的表：沒有一張會複製兩次）；copied 與 unknown（先看目標）沒有；重新載入後列出的 job 沒有（沒有命令，06b V6）；都回到表單（06b V2）。Cancel 的說明、⌘Q 的問句與模式的說明都說「rolls back… unless the table cannot roll back」（結果之前不說得比 backend 肯定，Task 7 審查）〕
- **入口**：物件樹右鍵（關聯式的 database、schema、Tables 資料夾、表、view 節點：Dump…、Copy data to…、Run SQL file…；profile 節點：Run SQL file…）；表格資料 tab 的選單（Dump table…、Copy rows to…，帶目前的篩選）；query 結果的選單與匯出旁邊（Copy result to a table…）；Scripts 面板的檔案（Run on connection…）；⌘⇧P（Dump…、Run SQL file…、Copy data to…，預設是作用中 tab 的連線與表）。能力欄位決定顯示（D-06-13）。〔06b：Copy 的項目是 06c。表格資料 tab 沒有選單，「Dump table…」在工具列最右的 ⋯（B12、V4；06c 加 Copy rows to…）。樹：連線列的 Run SQL file… 排第一，database、schema、Tables 資料夾、表、view 有 Dump…（SQLite 只有 `main`）與 Run SQL file…（database、schema；唯讀連線的灰著並說明），Couchbase 的 bucket、scope 有 Run SQL file…（帶它的 query context）。Scripts 的 Run on connection… 跑存著的檔（綁著它的 tab 有未存的改動時說明），連線是樹選著的、否則作用中 tab 的。⌘⇧P：Dump…（有 `dump` 的 engine；table tab 經它自己的 handler、帶它的篩選；query tab 是它的 database、MySQL 的 schema、SQLite 的 main）、Run SQL file…（一律）、Show jobs（V17）〕〔06c：Copy 的入口（依 `copy_data`）：樹的 schema、Tables 資料夾、表、view、materialized view 有「Copy data to…」（在 Dump… 之後；SQLite 只有 `main`），**database 節點沒有**（C10：copy 的目標是一個 schema，一個 database 的好幾個 schema 裡同名的表會撞在一起：一次複製一個 schema）；表格資料 tab 的 ⋯ 有「Copy rows to…」（在 Dump table… 之後，帶目前的篩選，view 的篩選也帶，pre-flight L-d；不能從這裡複製時灰著並說明）；query 結果的 status bar「Copy to table…」與 grid 選單「Copy result to a table…」（在 Export… 之後；執行結束、結果有欄位、engine 能 copy 時；Couchbase 沒有）；⌘⇧P 的「Copy data to…」（`dataTools.copy`：table tab 經它自己的 `copyRows`，query tab 是它預設 schema 的表；PG、SQL Server 的 tab 沒有 database 時灰著，同 Dump…）。刪除連線的守衛（B13）也算 copy 的**來源**連線（`jobsStore.runningOn`；只有讀取時說「a copy is still reading from it」）；copy 正在讀的 query tab 不能關、也不能換連線或 database（`CLOSE_GUARD_ORDER.copy` = 1，只在關閉時問、是拒絕，pre-flight L-f；`copyReadsSession`）〕
- **Jobs**：側欄底部的按鈕（有 job 在跑時顯示 spinner 與數量）；popover 列出 job（種類、標題、進度或結果、經過時間）；點一下重開對話框；Clear finished。資料來自 `GET /data/jobs` 與各 job session 的 WS（`useJobSession`：開 session、送命令、收事件、結束後關 session）。〔06b：沒有 `useJobSession`（hook 會跟著它的元件消失，B4、V1）：`jobsStore`（zustand，在 React 外）每個 job 一個 `JobRunner`：開 job 自己的 session（unlock 流程照舊），backend 送來第一個 `session_state` 時送命令、只送一次（重連拿 backend 的重送），結果一到（即時或重送，有沒有 `finished` 都算）就關 socket 與 session、從不重用（V7）；Couchbase 的預設 query context 在 session 開了以後填。按鈕只在這個視窗有 job（跑著或結束、到 Clear 為止）時出現（V3）。`syncJobs`（app 開始時、打開 Jobs 時、session 不見之後、刪除連線之前）以 `GET /data/jobs` 補上重新載入後的 job（沒有命令：不能 run again，B8、V6），接上跑著的、關掉結束的 session；送出 10 秒後 backend 沒列的是 `not_listed`（猜的，socket 或之後的清單說的取代它，V9）。popover 是 `data-non-modal`（開著時視窗的快捷鍵照常）；清單讀來的結果沒有經過時間（`JobInfo` 沒有結束時間）。Cancel 在 view 裡按過 1 秒後可以再按（SQL Server 的第二次 Cancel 會斷掉卡住的連線，同匯出），run 送出 COMMIT 之後停用（V18）〕〔06b 修正波：popover 開著時用快捷鍵開的 modal 關掉後，焦點回到 Jobs 按鈕（`focusToReturn`：opener 在 `data-non-modal` 的面板裡時改記 `aria-controls` 指向它的按鈕），不是 body〕
- **純模組（Vitest）**：`jobState.ts`（事件 → 對話框的狀態：進度、結果、可不可以重跑或跳過）、`mappingRows.ts`（`TableTransfer` → 對照表的列、⚠ 與問題的位置、覆寫）、`entryPoints.ts`（樹的節點 → 範圍與預設）、`fileNames.ts`（預設檔名、副檔名）。〔06b：另有 `jobReport.ts`（job 的文字與安全的動作；§4 的每個 code 有 label；SQLite 忙碌用共用的 `lib/sqliteBusy.ts`，⑤ 的文字不變，B9）、`jobRunner.ts`、`jobsStore.ts`、`jobEndings.ts`、`dumpForm.ts`、`runForm.ts`，與 `app/quitQuestion.ts`；`mappingRows.ts` 是 06c〕〔06c：`copyForm.ts`（表單、請求、命令、標題、`restForm`、`passwordOwner`）與 `mappingRows.ts`（對照表的列、表單的改動、語句的行、先畫哪些表）；`entryPoints.ts` 加 Copy 的入口，`jobReport.ts`、`jobsStore.ts`、`jobState.ts` 加第三種 job（copy 的文字、`CODE_LABELS`、`copyLostHint`、`runningOn`、`copyReadsSession`、`deleteBlockedMessage`）；Vitest 在 `copy.test.ts` 與既有的測試檔〕
- **macOS**：Return 是主要按鈕、Esc 關閉（執行中也可以，job 照跑）；檔案對話框是系統的；完成通知沿用 S8；視窗關閉（⌘⇧W、紅燈）用 `onCloseRequested` 問；⌘Q 的 predefined Quit 改成自訂項目，先發事件給前端，前端確認後才結束（A12）。〔06b：結束時 view 沒開的 job 另有 toast（帶 Open），通知照 S8（app 在背景、超過門檻）；取消而結束的（還要使用者看目標的 partial、unknown 除外）與沒送出的都不說（B14、V11）。視窗的關閉不是 JS 的 `onCloseRequested`（它要 `core:window:allow-destroy`，而且頁面卡住時紅燈與 ⌘⇧W 會什麼都不做）：shell 擋下每一次關閉、問頁面，2 秒內沒有回答就照以前關（pre-flight M4）；細節見 CR-5 的〔06b〕〕

## 4. 錯誤處理

| 情況 | code | 訊息（固定文字；server 的錯誤原文另外顯示） |
|---|---|---|
| 唯讀的目標 | 403 `read_only` | `This connection is read-only` |
| engine 沒有這個功能 | 422 `not_supported` | `<Engine> connections cannot <dump / copy data>` |
| 檔案不存在或不是檔案 | `file_not_found` | `<path> is not a file` |
| 不是 UTF-8 | `file_not_utf8` | `Line N is not UTF-8 text` |
| 單句太大 | `statement_too_large` | `The statement starting at line N is larger than 256 MiB` |
| psql meta-command | `psql_meta_command` | `Line N is a psql command (\connect): run this file with psql`（`\restrict`／`\unrestrict` 是訊息，不是錯誤） |
| 全有或全無遇到檔案自己的交易語句 | `transaction_in_file` | `The file controls its own transactions (line N: COMMIT): run it statement by statement` |
| 交易被結束 | `transaction_ended` | `The transaction ended at line N (MySQL commits around DDL): the statements before it are applied; run the rest statement by statement` |
| COPY FROM stdin | `unsupported_copy` | adapter 的文字＋`pg_dump: use --inserts, or run this file with psql` |
| 寫檔失敗 | `dump_io` | `Cannot write <path>: <原因>` |
| plan 過期／不符 | `plan_expired`／`plan_mismatch` | 同 ⑤：`The preview has expired: preview the copy again` |
| 建新表但表已存在 | 預覽的 problem `target_exists` | `<schema.table> already exists: append to it, or choose another name` |
| 型別對照不了 | problem `unmappable_type` | `<column> (<type>) has no <Engine> type: choose one, or leave the column out` |
| 來源與目標相同 | problem `same_table`／`same_sqlite_file` | `The source and the target are the same table` ／ `… the same SQLite file: use INSERT … SELECT in a query tab` |
| 型別片段不合法 | problem `invalid_type` | `Not a single type: <原因>` |
| query 的欄位變了 | `columns_changed` | `The query's columns changed since the preview: preview the copy again` |
| 符合的列數不對 | `row_count_mismatch` | `An INSERT of N rows reported M rows (a trigger may have skipped or changed them): table rolled back` |
| 來源出錯 | `source_failed` | `Reading <source> failed: <來源的原文>` |
| 斷線 | `connection_lost` | adapter 的文字；COMMIT 途中 → unknown 的說明 |
| 〔06b〕重跑時檔案變了（`from_line` 不符） | `file_changed`（`not_started`） | `The file changed: statement N now starts at line M`（或 `…it has K statements now, fewer than N`、`The file changed: <讀不下去的原因>`）；前端的 label「The file changed since the run before」，Run again… 不帶行〔06b 修正波：回到的表單說「The file changed since the last run: check where to start.」〕 |

〔06c：copy 另有的 code（C7）。job：`value_not_copyable`（「Row N of <source>, column <目標欄>: <原因>」，原因不引用值，只有 `infinity`、零日期這類特定字）、⑤ 的 `row_count_unknown`、run 的 `transaction_ended`（copy 的：那張表的交易在 COMMIT 前就不在了）與 `commit_unconfirmed`、`query_not_select`（執行時 query 不是單純的讀取，或回傳不只一個結果；文字和預覽的同一句）；`row_count_mismatch` 的文字是「An INSERT of N rows reported M rows (a trigger may have skipped or changed them): the table was rolled back」。預覽的 problem 另有 `target_missing`、`target_not_writable`（⑤ 的拒絕條件）、`source_missing`、`missing_column`、`unconvertible`、`no_columns`、`duplicate_column`、`invalid_filter`、`query_not_select`、`duplicate_target`、`cascade_loop`（R-06-9 的〔06c〕）。前端每個都有 label（`CODE_LABELS`；`transaction_ended` 依 job 的種類說，`connection_lost` 的提示依結果說：unknown、沒開始、rolled back，partial 時加「the tables before it stay copied」）〕

結果的說明（R-06-12）由前端依 `job_outcome` 組成，例如 rolled back：「Nothing was applied: the transaction was rolled back」；stopped：「Stopped at line 5,001. The 1,233 statements before it are applied」；unknown：「The connection was lost while line 5,001 ran: it may or may not have taken effect」。

## 5. Log（loguru）

| 等級 | 內容 |
|---|---|
| INFO | job 開始與結束：種類、profile、範圍或檔案路徑、物件數、列數、bytes、語句數、錯誤數、ms、outcome |
| WARNING | partial、unknown、interrupted；跳過的物件；dump 的 consistency 是 per_table |
| DEBUG | 每句的起始行號與前 200 字元；server 的錯誤原文 |
| 永不 | INFO 以上的值、literal、server 錯誤原文（會回顯 literal，同 `QueryFailed`） |

## 6. 測試（依「只測容易出錯的邏輯」）

- **單元（pytest）**：`iter_spans` 與既有 `split()` 的結果相同（現有的全部切句測試，加一組每個 dialect 的語料）；`StatementStream` 在**每一個位置**切塊時與一次讀完的結果相同（字串、註解、`$$`、`DELIMITER`、`GO n`、CRLF、BOM、psql meta 行、UTF-8 的多 byte 字元剛好在塊邊界）、行號、256 MiB 上限、加倍讀取；classifier 的 64 KiB 上限（之前與之後的答案相同）；`gate_confirmed`；`InsertBatcher`（每個 dialect 的列數與 1 MiB、單列超過、identity 的包法、略過的欄）；`dump_script`（各 engine 的檔頭、區段順序、drop_first、只有資料）；`type_map`（§3.5.2 每一格、⚠、問題、覆寫的檢查、§3.5.3 每一種轉換含 `±HH`、UTC、超出範圍的日期）；`OneTimePlanStore`（⑤ 的測試移過來）；`JobRegistry`、忙碌的 session 不 reap；History 的 `begin`／`finish`／`interrupted`；worker 的 job 事件順序與每個 outcome（fake connection：出錯、取消在語句之間與執行中、COMMIT 被拒、COMMIT 途中斷線、`in_transaction()` 變假、排隊時取消）。
- **整合（dev DB；scratch 物件一律 `zz_06_*`，在 `finally` 以完整名稱 DROP；還原進 scratch database `zz_06_restore`（PG、MySQL 以 root、SQL Server 以 sa 建立與 DROP）；SQLite 用 `tmp_path`；Couchbase 的 run 測試只讀，加一組 `dbaguy.app.orders` 的 `zz_06_run_1` 小文件 UPSERT 與以 exact key 的 DELETE，`app.orders` 保持 500 份）**：每個 engine 把 seed schema dump 成檔 → run 進 `zz_06_restore` → 逐表列數與 checksum 相同、序列與 identity 接得上、view／routine／trigger 存在；兩種模式的出錯、取消、從第 N 句；MySQL 的 DDL 隱含 commit → partial；`mysqldump`、`pg_dump --inserts`、SSMS 風格、`sqlite3 .dump` 的檔案（由 dev 容器裡的工具產生，A8）；copy 的 4×4 矩陣（一張型別齊全的 `zz_06_types` 表，建新表與附加，值依轉換相同）、key 衝突 rollback、先刪除、MyISAM 目標的 unknown、取消、來源出錯、identity keep／generate 與 setval、FK 排序。
- **前端（Vitest）**：§3.6 的純模組。
- **手動（`scripts/dev.sh --web` 與內建瀏覽器）**：三個對話框在每個 engine 的流程、Jobs 與重新載入；打包版的檔案對話框、Show in Finder、通知、視窗關閉與 ⌘Q 的守衛、注音（檔名與表名輸入框）進給使用者的最終測試清單。
- 規則同 05a：scratch 物件存在時不跑完整的 backend 測試，一次只讓一個人跑 live 測試。

## 7. 效能、打包與量測

預算（06a 以凍結的 sidecar 量，A10）。基準：〔實測〕附錄 A X3 的格式化速度（6 欄 147k–167k 列／秒）、X4 的載入速度、X2 的切句速度。

| 項目（100 萬列、每列約 230 bytes、6 欄） | PG | MySQL | SQL Server | SQLite | 記憶體 |
|---|---|---|---|---|---|
| Dump（結構＋資料；欄是來源） | ≤ 30 秒 | ≤ 30 秒 | ≤ 60 秒 | ≤ 30 秒 | sidecar RSS 增加 ≤ 30 MB，重複跑不累積 |
| Run 那份 dump（全有或全無；欄是目標） | ≤ 2 分鐘 | ≤ 2 分鐘 | ≤ 4 分鐘 | ≤ 2 分鐘 | ≤ 30 MB |
| Copy 自另一個 engine（欄是目標） | ≤ 60 秒 | ≤ 60 秒 | ≤ 2.5 分鐘 | ≤ 60 秒 | ≤ 40 MB |

- Run 受切句限制（約 2–3 MB/s，R-06-6）：100 萬列的 dump 約 150 MB，切句約 60 秒，再加 server 的時間。
- SQL Server 的載入是瓶頸（每句 100 列 21,847 列／秒；同一批次放多句可到 26,931，§12）。
- 〔06a 實測〕凍結的 sidecar、100 萬列、每列約 230 bytes 的 6 欄表（dump 是 231–233 MB，比估計的 150 MB 大），load average 4–9：dump PG 9.8–12.2 秒、MySQL 14.9–16.0 秒、SQL Server 17.7–19.1 秒、SQLite 9.1–10.1 秒；run PG 56.2–59.0 秒、MySQL 68.8–69.5 秒（逐句）、SQL Server 97.5–108.5 秒（10,003 句）、SQLite 46.5–50.3 秒，2.1–5.0 MB/s（SQL Server 的 2.1–2.4 MB/s 在估計的 2–3 MB/s 之內，PG、MySQL、SQLite 比估計快）：時間都在預算內（最慢的 SQL Server run 是 41–45%），還原後的聚合與來源相同。記憶體：dump 的尖峰 54–97 MB、從被 macOS 收回的基準線（44–89 MB）起算 +4–47 MB，重複跑不累積；run 的尖峰 SQLite 87–94、MySQL 79–80、SQL Server 69–76 MB（+9–31 MB），PG 101–146 MB（+34–86 MB，**超過預算**，`MallocLargeCache=0` 與非凍結的 sidecar 一樣），同一個 sidecar 的第二次不比第一次高。〔06a 修正波 F1〕PG 的原因是 psycopg 3 的 prepared-statement 快取：沒有 prepare 也留著最近 100 句不同語句的編碼文字，到 session 關閉為止；每條 PG 連線改成 `prepare_threshold=None`。in-process 的 A/B（100 萬列的 dump、233 MB、1,000 句，各 2 次）：RSS 尖峰 +40／+45 → +17／+19 MB，結束後 90–101 → 60–64 MB（快取裡 23.1 MB 的語句文字 → 0），時間不變（56–57 秒）；上面的打包版數字是修正之前量的（這一波沒有重新 build）。sidecar +300 KB（下一行估 < 200 KB：審查的修正波讓 06a 的 backend 變成約 8,700 行），沒有新相依、不需要 hidden import；打包後的檢查是 dump 兩次與 run（每個 engine）、Couchbase run 一次（probe 逐項 assert），copy 的一列 → 06c。
- 〔06c 實測〕Copy 那一列（凍結的 sidecar、probe 自己啟動、暫存的 data dir；一張 100 萬列、每列約 230 bytes、6 欄（bigint key、含 `it's`／中文／emoji 的 text、numeric(20,2)、timestamp、double、10% NULL 的 text）的 PG 表以建新表 copy 到 MySQL、SQL Server、新的 SQLite 檔，再把 SQLite 的那張 copy 回 PG，每次都經 `POST /data/transfer/plan` 與目標 session 上的 `copy_data`；跑 2 次，load average 4–14（第一次剛 build 完）與 4）：PG → MySQL 19.3–19.9 秒、PG → SQL Server 87.9–90.1 秒（10,000 句、每句 100 列）、PG → SQLite 12.8–12.9 秒、SQLite → PG 19.9 秒：都在預算內（最慢的 SQL Server 是 2.5 分鐘的 59–60%）；每個目標的列數、id 的總和、非 NULL 的數目、一列的文字與金額都和來源相同。sidecar RSS 從每次 copy 之前的 74–95 MB 起算（同一個 sidecar 依序跑四次），尖峰 93–108 MB，增加 +3–22 MB（預算 40 MB；規劃時非凍結的 sidecar +2–12 MB，時間 19.2／87.2／12.3／19.6 秒）。stderr 沒有 `ModuleNotFoundError`，不需要 hidden import。同一個 build 重量 06a F1 之後 PG 的 run（06a Task 10 的 probe 只跑 PG）：100 萬列的 dump（232 MB）以全有或全無還原 62.7–63.1 秒，RSS 尖峰 94–95 MB、+22–23 MB（2 次），在 30 MB 的預算內（F1 之前 +34–86 MB）；`MallocLargeCache=0` 時 +12 MB、尖峰 82 MB。
- 打包：沒有新的 Python 或前端相依（stdlib 與既有 driver），不需要 hidden import；sidecar 預估增加 < 200 KB（新的純 Python 模組）。打包後的檢查：凍結的 sidecar 對四個 engine 各 dump 一次、run 一次、copy 一次（probe 逐項 assert），Couchbase run 一次。

## 8. 痛點對照

| 需求 | 這個子專案怎麼解 | 踩到／退步 |
|---|---|---|
| Q1 輕快低記憶體 | 每條路徑串流、固定上限的塊與管線；切句不留 token；0 新相依；順手修掉 sqlglot 在大 INSERT 上 113 倍的記憶體（D-06-4） | Run 的速度受 Python 切句限制（2–3 MB/s；〔06a 實測〕2.1–5.0 MB/s）；SQL Server 的載入慢（約 2 萬列／秒；〔06a 實測〕100 萬列 97.5–108.5 秒）；〔06a〕PG 的 run 的 RSS 尖峰超過 30 MB 的預算：psycopg 的 prepared-statement 快取，修正波 F1 修掉（in-process +40–45 → +17–19 MB；〔06c〕打包版 +22–23 MB，§7）；〔06c〕Copy：凍結的 sidecar 的 100 萬列 copy 都在預算內、RSS +3–22 MB（§7），SQL Server 的目標仍最慢（87.9–90.1 秒） |
| Q2 穩定不丟工作 | dump 原子寫入；run 與 copy 的結果分類、不重送；History 開始就寫、當機後是 Interrupted；job 在背景、重新載入視窗後仍找得到；關視窗與 ⌘Q 先問 | 當機留下隱藏的 `.part`；MySQL 的建表失敗後留下空表（不自動刪） |
| Q3 可信任 | literal 共用 ⑤ 的規則；SQL Server 第 7 位小數保住；跨 engine 的每一種轉換執行前列出；預覽即確認、執行的就是 plan；唯讀雙層 | `sql_variant` 不寫；MySQL 非嚴格模式下的截斷只能警告；SQL Server 唯讀只在應用層（既有） |
| Q4 聚焦 | 只做搬與重建；不做同步、比對、upsert、排程、壓縮、外部程式 | 要一模一樣的跨 engine 表結構做不到（只建欄位、NOT NULL、PK） |
| Q5 多引擎 | 同一套 job 框架、對話框與規則表；engine 差異在 adapter 的 catalog 與 `sql/` 的表；Couchbase 有 Run file | Couchbase 沒有 dump 與 copy（R-06-14） |
| Q6 macOS 慣例 | 系統的存檔與開檔面板、Show in Finder、背景完成通知、Return／Esc | ⌘Q 守衛要改 Rust 的選單（A12） |
| FP §8 SQL dump、跨 DB 傳輸 | G1、G3；原生，不靠外部 binary（Won't 的理由仍成立） | — |
| M9 匯出 | 整個 schema 或 database 的結構與資料 | — |
| M11 安全 | 唯讀不能當目標、production 顏色進對話框、危險語句（DROP first、DELETE）以 danger 色列出 | Run 不逐句分類，危險在確認時以「會執行檔案裡的每一句」說明 |

## 9. 風險

| 風險 | 處理 |
|---|---|
| 公司 DB 的物件型態超出 dev 的 seed（PostGIS、分割表、加密的 SQL Server module、MySQL 的 DEFINER 是不存在的帳號） | 物件逐一處理、讀不到 DDL 的列在檔頭「Not included」（SQL Server 沒有 VIEW DEFINITION 時 `ddl=None` 的既有行為）；公司資料庫的檢查清單 |
| 相依排序不完整（動態 SQL、跨 schema 的 view） | catalog 的建立順序兜底；「逐句＋跳過並繼續」完成其餘的並列出失敗的物件 |
| 100 萬句以上的檔案在 Python 切句很慢 | 預算以 MB/s 寫明（§7）；之後讓 classifier 與切句共用 token、或給 ⑥ 自己寫的 INSERT 一條快速路徑（§12） |
| MySQL 來源在管線滿時被 `net_write_timeout` 結束 | 管線只有在目標比來源慢時才會滿；量測（A11），訊息說明 |
| SQL Server 的大型 VALUES 編譯慢 | 每句 100 列（〔實測〕3 倍快）；之後一個批次放多句 |
| WKWebView 的 `onCloseRequested` 與 Quit 的改法 | A12 先驗證；做不到就退回「關閉時 job 會被中斷」的說明與 History 的 Interrupted〔06b：做得到，但不是這個改法：見 §10 的 A12 與 CR-5 的〔06b〕；Dock 的 Quit、登出、關機仍不問〕 |
| SQLite rollback journal 模式下 dump 擋住寫入 | 對話框依 `PRAGMA journal_mode` 說明；WAL 不受影響 |
| 使用者把別的工具的 dump 跑在全有或全無模式 | 交易語句與交易結束都有明確的停止與說明（R-06-7） |

## 10. 未實測的假設

| # | 假設 | 驗證 |
|---|---|---|
| A1 | MySQL GEOMETRY 與 VECTOR 的 bytes 以 `X'…'` 寫回不變（mysqldump `--hex-blob` 的做法）；SQL Server hierarchyid 同 geography（〔實測〕geography 的 `0x…` 可以） | 06a T1 → 〔06a Task 4，實測〕MySQL 8.4.11 的 GEOMETRY 以 `X'…'` 寫回相同（SRID 4326 保留）；SQL Server hierarchyid（`/1/2/3/`、`/1/2.5/3/`）、geography、geometry 以 `CAST(… AS varbinary(max))` 讀、`0x…` 寫回相同；VECTOR 不在 MySQL 8.4，沒有測（公司是 9.x 時再驗） |
| A2 | PG 在 REPEATABLE READ READ ONLY 交易裡：DDL 輔助函式（`pg_get_*`）與 server-side cursor 交錯使用沒有問題，100 萬列的串流記憶體平穩 | 06a T1 → 〔06a Task 5，實測〕REPEATABLE READ READ ONLY 交易裡 server-side cursor 讀 100 萬列、FETCH 之間穿插 20 次 `pg_get_*`：都正常，交易仍是 repeatable read、read only，client RSS 68 → 70 MB；凍結的 sidecar dump 100 萬列時 RSS 尖峰 61–66 MB，第二次不高於第一次（Task 10，§7） |
| A3 | MySQL CONSISTENT SNAPSHOT 交易裡：unbuffered 串流讀完再 `SHOW CREATE` 下一張表（〔實測〕交易裡 `SHOW CREATE TABLE` 可以） | 06a T1 → 〔06a Task 6，實測〕CONSISTENT SNAPSHOT 交易裡讀整份 catalog 之後，別的連線新增的列看不到（snapshot 的 `COUNT(*)` 仍是 2）；`SHOW CREATE TABLE` 的 AUTO_INCREMENT 是讀到那一刻的值，不在 snapshot 裡（S10）；catalog 讀的是 data dictionary，同時有 DDL 時讀列會遇到 1412（dump 說那張表被改了，請重新 dump） |
| A4 | SQL Server 開了 `ALLOW_SNAPSHOT_ISOLATION` 的 database 用 SNAPSHOT 讀，DDL 讀取不受影響 | 06a T1（scratch database 裡打開） → 〔06a Task 7，實測〕scratch database 開了 `ALLOW_SNAPSHOT_ISOLATION`：SNAPSHOT 交易裡 catalog 與 DDL 的讀取正常；`BEGIN TRANSACTION` 之後、第一次讀取之前別的連線新增的列看得到，catalog 讀過之後新增的看不到：SNAPSHOT 從第一次讀取開始（S13）；catalog view 不分版本，snapshot 之後才建的表讀列時是 3961。沒開時（dev 的 database）dump 逐表讀（per_table），檔頭與預覽說明 |
| A5 | SQLite rollback journal 模式下，dump 的讀取交易擋住其他連線的寫入（`database is locked`），WAL 不擋 | 06a T1 → 〔06a Task 6，實測〕rollback journal（`delete`）時 dump 的讀取交易讓別的連線寫入 `database is locked`；WAL 時寫得進去；預覽在不是 WAL 時警告 |
| A6 | `iter_spans` 與既有 `split()` 在全部語料上相同；`StatementStream` 在每個切塊位置相同 | 06a T2 → 〔06a Task 1，實測〕`split()` 改成 `list(iter_spans())`：五個 dialect 18,000 段隨機 SQL 與舊的 `split()` 逐欄相同（0 差異；審查另跑約 30,000 段），既有的切句測試全過，舊 `split()` 的 span 寫成測試裡的 literal；`StatementStream` 在隨機切點、1–200 與 1 MiB 的塊大小都和一次讀完相同 |
| A7 | 四個 engine 的 seed schema 能 dump 並在 `zz_06_restore` 裡重建，逐表 checksum 相同 | 06a T9 → 〔06a Task 9，實測〕PG、MySQL、SQL Server 的 `dbaguy` 與 `analytics` seed，與一個 SQLite 的 shapes 檔（AUTOINCREMENT、untyped 的 `'007'`／`1.10`、±9e999、−0.0、blob、STRICT、WITHOUT ROWID、CJK 的表名），以 `drop_first` dump、在 `zz_06_restore_*` 還原兩次：獨立的 inventory（不經 dump 自己的 reader）與逐列的 hash 相同，序列、identity、AUTO_INCREMENT、`sqlite_sequence`、view、routine、trigger、event 都在；MySQL 逐句（全有或全無在第 5 句被拒，S6、S22）；SQL Server 的 `sql_variant` 欄是 NULL（不寫，檔頭列出）；沒有可寫欄的表還原同樣的列數 |
| A8 | dev 容器裡的 `mysqldump`、`pg_dump --inserts`、`sqlite3 .dump` 的輸出與 SSMS 風格的 GO 腳本能跑（逐句） | 06a T4 → 〔06a Task 3，實測〕dev 容器的 `pg_dump --inserts`（`\restrict` 跳過並說明）跑完、COPY 形式在第一個 COPY 停（`unsupported_copy` 加提示）；`mysqldump --hex-blob` 逐句跑完、全有或全無在它的 `DROP TABLE` 前就被拒（S22，表不動）；沒有 `--hex-blob` 時二進位欄是原始 bytes，前面 17 句照送、在那一行以 `file_not_utf8` 停並提示 `--hex-blob`（S11）；`sqlite3 .dump` 逐句跑完、全有或全無在它的 `BEGIN TRANSACTION` 停；SSMS 風格的 CRLF、`GO` 腳本跑完 |
| A9 | Copy 的 4×4 矩陣上，`zz_06_types` 的每個值依 §3.5.3 相同 | 06a T11 → 06c（S1） → 〔06c Task 5，實測〕型別齊全的 `zz_06c_*` 表（PG 24 欄、MySQL 23、SQL Server 23、SQLite 13，各三列，加上第 7 位小數的列）以建新表複製 16 對 engine：讀回的每個值依它的意義相同（`tests/copy_matrix.py`、`test_copy_matrix.py`）：位移依目標比（PG `timetz` → MySQL `time(6)` 去掉位移，其他保留）；JSON 進文字欄以原文比，MySQL `json` 照它記載的正規化（說明 `MYSQL_JSON_NOTE`）；突變測試（丟掉位移、`1.10`、key 順序、NULL 對 `null`）讓對應的組合失敗。找到 SQL Server 讀 `\` 加換行是續行（⑤、06a、06c 都會悄悄改值），以 Task 5b 修掉（CLAUDE.md 的 SQL Server 一節）。只測了建新表：附加的轉換走同一條 `_writer` 路徑、對著真的目標欄，附加的規劃由 Task 2、4 的測試涵蓋（pre-flight L-h） |
| A10 | §7 的預算 | 06a T12 → 〔06a Task 10，實測〕見 §7 的〔06a 實測〕 |
| A11 | MySQL 來源在管線滿超過 `net_write_timeout` 時的行為與訊息 | 06a T11 → 06c（S1） → 〔06c 規劃時與 Task 3，實測〕`net_write_timeout = 2`、讀的一方停 8 秒：100 萬列的讀取在 66,001 列後以 `connection_lost`（2013「Lost connection to MySQL server during query」）結束，從不悄悄少列；dev 的登入可以 `SET SESSION net_write_timeout = 3600`，同樣停 8 秒讀完 100 萬列。copy 在來源 session 設 3600、每張表讀完設回 session 自己的值（C5；live 測試在 `test_copy_framework.py`） |
| A12 | Tauri 2 的 `onCloseRequested` 在 WKWebView 可用；predefined Quit 換成自訂項目後 ⌘Q 仍是系統的鍵、確認後能正常結束 | 06b 第一個 task → 〔06b Task 1，讀 tauri 2.12.4／tao 0.37.1／muda 0.20.0 的原始碼並 `cargo check`、`cargo clippy -D warnings`〕predefined Quit 送 `terminate:`，tao 沒有 `applicationShouldTerminate`，所以 ⌘Q 從不經過 `RunEvent::ExitRequested`（只有最後一個視窗被 destroy 與 `AppHandle::exit` 會）：`prevent_exit()` 擋不住它。自訂的「Quit DbaGuy」（⌘Q，標籤與按鍵同 muda 的 predefined 項目）先問頁面，結束走同一個 `AppState::exit`（sidecar 照以前停掉）。JS 的 `onCloseRequested` 自己 destroy 視窗、要 `core:window:allow-destroy`（`core:default` 沒有），頁面卡住時紅燈與 ⌘⇧W 會什麼都不做：改成 shell 在 `on_window_event` 擋下每一次關閉再問頁面（沒有新權限）。執行中的行為（⌘Q、紅燈、⌘⇧W、最小化的視窗被帶回前面、重載時的 2 秒、注音）只在打包版驗得到：在使用者的測試清單，06b 沒有實測 |
| A13 | PG 的 `money` 以 `::numeric` 讀（〔實測〕P10）對 `money[]` 與 domain over money 也適用；做不到的寫進已知限制 | 06a T6 → 〔06a Task 4，實測〕`money`、`money[]` 與 domain over money 經 `numeric`／`numeric[]` 讀出、寫回都相同（`-1234567.89`、`{1.50,-2.00,NULL}`、`7.25`；±2^63 也精確） |
| A14 | MySQL 的 `DEFINER=` 移除對各種帳號寫法都正確（`user@host`、`'u'@'h'`、`` `u`@`h` ``、`CURRENT_USER`） | 06a T7 → 〔06a Task 6，實測〕`user@host`、`'u'@'%'`、`` `u`@`%` ``、`"u"@"h"`（ANSI_QUOTES 下建立的 program）、名稱裡雙寫的 back-tick、`CURRENT_USER`、`CURRENT_USER()`、`@` 兩邊有空白：八種寫法都拿掉 DEFINER、其餘原文不變；本體裡的 `definer = …` 不動 |
| A15 | SQL Server 的 `CAST(… AS varchar)` 對 datetime2(7)、datetimeoffset(7)、time(7) 在 British 與 us_english 都寫回相同（〔實測〕us_english 可以） | 06a T8 → 〔06a Task 4，實測〕CAST 的文字在 datetimeoffset 的位移前有空格（`… 12:34:56.1234567 +08:00`），接上 `T` 之後 us_english 與 British 都拒絕（241）；改用 `CONVERT(…, 126)`（`2026-10-05T12:34:56.1234567+08:00`），兩種語系、六種 language／DATEFORMAT 設定都寫回相同（S4） |
| A16 | `INSERT` 符合列數的讀法（⑤ 的 `matched_rows()`）對多列 INSERT 正確：MySQL 多列 INSERT 的 OK 封包是 `Records: n`（〔實測〕⑤ A1），`affected_rows` 即列數 | 06a T11 → 06c（S1） → 〔06c 規劃時與 Task 4，實測〕四個 engine 的 4 列 INSERT 都是 4（MySQL 的 OK 封包 `Records: 4`）。PG 的 `BEFORE INSERT` trigger 對兩列回 NULL：2（抓得到，`row_count_mismatch`）；SQLite 的 `RAISE(IGNORE)` 兩列：2（抓得到）；**SQL Server 的 `INSTEAD OF INSERT` 只寫四列中的兩列：4**（外層的數，抓不到：C14，已知限制，同 ⑤ A3）；寫別的表的 `AFTER` trigger 在每個 engine 都不改列數；SQL Server 的 trigger 回傳結果集：讀不到（`row_count_unknown`）；交易裡的 MyISAM：ROLLBACK 警告 1196、列留著（所以那裡失敗的寫入是 unknown）；重複的 key：23505、1062、2627、`SQLITE_CONSTRAINT_PRIMARYKEY`。trigger 跳過列的 live 測試在 `test_transfer_job.py` |

## 11. 執行計畫拆分

一個 branch `feat/06-data-tools`，兩份 plan 依序執行、各自可合併驗收：

1. **06a：backend（約 12 個 task）**〔06a：規劃成 10 個 task，Copy（下面的 T10–T11）連同 CopyDataDialog 拆成 06c（D-06-14、CR-6；S1）。T1 的 probe 分到用到它們的 task：A1、A13、A15 → Task 4，A2 → Task 5，A3、A5、A14 → Task 6，A4 → Task 7；A6 → Task 1、A8 → Task 3、A7 → Task 9、A10 → Task 10；plan 見 `docs/superpowers/plans/2026-10-20-06a-data-tools-backend.md`，06b、06c 之後另寫 plan〕
   - T1 probe：A1–A5。
   - T2 `iter_spans`、`StatementStream`、classifier 的 64 KiB 上限（純函式，A6）。
   - T3 job 框架：指令與事件、`JobContext`、`_run_job`、`JobRegistry`、忙碌的 session 不 reap、History 的 `begin`／`finish`、`gate_confirmed`、能力欄位；`npm run gen:api`。
   - T4 `RunFileJob` 與 `/data/run-file/inspect`；每個 engine 的 live 測試；別的工具的檔案（A8）。
   - T5 `DataTools.table_columns` 與 `TypeFamily`（四個 engine）；⑤ 的 `edit_target` 改讀它。
   - T6 PG 的 `dump_catalog`（型別、序列與值、extension、partition、routine、trigger 的 function、相依；A13）。
   - T7 MySQL 與 SQLite 的 `dump_catalog`（DEFINER，A14；virtual table）。
   - T8 SQL Server 的 `dump_catalog`（相依、snapshot、scale 7，A15）。
   - T9 `dump_script`、`InsertBatcher`、`build_copy_select`、`DumpJob`、`/data/dump/plan`；dump → run 的往返測試（A7）。
   - T10 `type_map`、`OneTimePlanStore`、`/data/transfer/plan`。
   - T11 `TransferSource`／`TransferSink`／`RowPipe`；4×4 矩陣（A9、A11、A16）。
   - T12 量測（A10）、凍結的 sidecar 檢查、CLAUDE.md 與本文的〔06a〕註記。
   - **結束時**：用 API 與 WS 能在每個 engine dump、run、copy，結果分類都有測試；前端還沒有入口。
2. **06b：frontend（約 9 個 task）**〔06b：規劃成 9 個 task（plan 見 `docs/superpowers/plans/2026-10-23-06b-data-tools-frontend.md`），只做 dump 與 Run SQL file；CopyDataDialog、MappingTable 與 Copy 的入口連同 Copy 的 backend 是 06c。T1 ⌘Q 與關視窗的守衛（A12）；T2 backend 的 `resume_at`（B1）、`/data/run-file/excerpt`（B2），審查後加 `from_line`／`file_changed` 與 `StoppedAt.exact`；T3 `jobState`、`jobReport`；T4 `entryPoints`、`fileNames`、`dumpForm`、`runForm`；T5 `jobRunner`、`jobsStore`（取代 `useJobSession`）；T6 DumpDialog、job 的 view 與 Dump 的入口；T7 RunFileDialog 與 Run 的入口；T8 Jobs、重新載入、通知、刪除連線的守衛；T9 量測、打包版、CLAUDE.md 與本文〕
   - A12 probe 與 `chooseOpenPath`；`jobState`、`jobsStore`、`useJobSession`（Vitest）。
   - DumpDialog；RunFileDialog；CopyDataDialog 與 MappingTable。
   - 入口（樹、表格資料 tab、query 結果、Scripts 面板、⌘⇧P）。
   - Jobs 按鈕、重新載入後的清單、通知、視窗關閉與 ⌘Q 的守衛。
   - 每個 engine 的手動檢查、給使用者的最終測試清單、CLAUDE.md。
3. **06c：Copy data（backend 與前端）**〔06c：拆出來的 Copy（D-06-14、CR-6、S1），規劃成 9 個 task（plan 見 `docs/superpowers/plans/2026-10-25-06c-data-tools-copy.md`）：T1 型別家族集中到 `adapters/families.py` 與 `sql/type_map.py`；T2 `OneTimePlanStore`、`POST /data/transfer/plan`；T3 job 框架給 copy 的部分（helper job、held Cancel、來源 session 的 lease）、`RowPipe`、`TransferSource`（A11）；T4 `TransferSink`、`CopyDataCommand` 與結果（A16）；T5 4×4 矩陣（A9）；T6 前端的純模組（表單、對照、第三種 job）；T7 CopyDataDialog 與 MappingTable；T8 入口與來源連線的刪除守衛（B13）；T9 兩套測試、打包版、凍結的 sidecar 的 100 萬列 copy、CLAUDE.md 與本文。執行時另加 Task 5b（SQL Server 的反斜線加換行，Task 5 的矩陣找到的）與 Task 1、2、4、5、7／8 的修正波（Task 2 有四輪：key 的型別、建新表的 key 從 catalog 讀、依目標的 FK 排序）〕

**為什麼這樣拆**：backend 的風險是各 engine 的 DDL 順序、值與交易語意，全部能用整合測試驗，先定下 API；前端的風險是三個對話框與背景 job 的互動，要手動檢查（同 ⑤ 的順序）。若 06a 規劃時超過 13 個 task，把 T10–T11 連同 CopyDataDialog 拆成 06c（D-06-14）。

〔06c〕規劃時的 spec 問題（C1–C18；理由、證據與測試名在 06c plan 的「Spec issues found while planning」，接受為裁決）已寫進上面各處的〔06c〕註記：C1 §3.5.1；C2 R-06-8；C3 R-06-8、§3.5.5；C4 R-06-9、§3.5.4；C5 §3.5.3；C6 R-06-12；C7 §4；C8 R-06-9；C9、C10、C17 §3.6；C11 R-06-9；C12、C15 §3.5.4；C13 R-06-10；C14 §10 的 A16；C16 §3.5.3；C18 §3.5.5。執行時另加的：附加或取代進 MySQL、SQLite 有 FK 的表時預覽警告 FK 不檢查（L2）；pre-flight 的 H1–H3、M1–M4 與 L-a–L-l 都照它的修法做了（L-h、L-i 是說明，見 §3.6 的〔06c〕與 CLAUDE.md 的已知限制）。

## 12. 細化與之後

對 coordinator 問題的細化：
- **「restore 是開 query tab 還是專用的執行器」**：專用的（R-06-5），小檔案照舊可以開進 query tab。
- **「寫入目標經 `SafetyPolicy`」**：每句都經 policy，但用 `gate_confirmed`（R-06-6）：可寫的連線確認過就不分類（分類的答案不影響結果，量測慢 3–10 倍），唯讀照 `check()`。
- **「不會悄悄覆蓋」**：dump 預設不寫 DROP；run 的「會先 DROP」只能從 ⑥ 的檔頭得知（提示，不是許可）；copy 的建表遇到既有表就停、先刪除是明確的模式與 danger 色的 SQL。

Coordinator 審核（2026-10-20；使用者授權自主決定，以下裁決不另問）：
- **CR-1**：Couchbase query 結果 → 關聯式表的 copy 不進 ⑥（Q4：v1 聚焦 80% 情境；匯出的 JSON／JSONL 已能把資料拿出來），列在下面的「之後」，06b 的使用者清單提一句「需要再說」。
- **CR-2**：dump 照 R-06-2 帶 routine 與 trigger（使用者拿它取代 Navicat 的 Dump SQL File）；copy 的 key 衝突照 R-06-9 失敗、整張表 rollback，v1 沒有 upsert 或略過。
- **CR-3**：Run SQL file 不逐句分類（R-06-6），所以對話框必須把「會執行檔案裡的每一句，包括 DROP、TRUNCATE」寫清楚：目標連線的名稱與顏色、database、模式；紅色（production 色）連線的執行鈕是 danger 鈕，另外要勾一個「I've checked the file and the connection」才能按（Q3：確認的強度跟著連線的顏色走，同 ⑤ 的紅色 Commit）。
- **CR-4**：Dump 寫檔遇到磁碟滿（ENOSPC）或其他寫入錯誤：`.part` 刪除、原本的同名檔不變，結果寫「Disk full: nothing was written」（或 OS 的原文）並指出寫到哪裡；開始前以 `shutil.disk_usage` 檢查目的地的可用空間，少於估計大小（列數 × 平均列長的上限）或 1 GiB 時在預覽警告（不擋）。使用者的 Mac 磁碟曾經被寫滿、讓 OrbStack 出 I/O error（Q2）。
- **CR-5**：job 進行中的 ⌘Q／關視窗守衛（R-06-15）只涵蓋 ⑥ 的 job；把 quit 守衛擴大到所有 close guard 仍是 02c 的 carry（下面的「之後」）。Tauri 端以 `RunEvent::ExitRequested` 的 `prevent_exit()` 實作，Rust 只負責轉給前端問，決定在前端（CLAUDE.md：Rust 只負責視窗、選單與 sidecar）。〔06b：B3——⌘Q 不經過 `ExitRequested`（§10 的 A12），`ExitRequested` 不動。實作（`src-tauri/src/quit.rs`、`menu.rs`、`lib.rs`；`src/app/quitQuestion.ts`、`QuitGuard.tsx`）：選單的 Quit 是自己的項目，發 `app://quit-requested`；視窗的關閉（紅燈、⌘⇧W）在 `on_window_event` 擋下、發 `app://close-requested`。頁面馬上 ack（`quit_ack`／`close_ack`，兩種各自的 ack 不互相蓋掉），再問 backend 的 `GET /data/jobs`（含 backend 的位址最多等 1 秒，拿不到就當沒有 job，V8）：沒有 job 就經 `quit_app`／`close_window` 結束；有就先把視窗帶到前面（`reveal_window`：取消最小化、顯示、focus），再問「Quit DbaGuy?」／「Close the window?」（列出 job；Keep running 是預設，Return、Esc 都是留下）。2 秒內沒有 ack（頁面在載入、重載或卡住）就照以前直接結束或關閉，從不困住使用者。沒有新的 Tauri 權限（不用 `allow-destroy`）。Dock 的 Quit、登出、關機送的是 `terminate:`，仍不問（已知限制）；頁面的 JS 忙 ≥ 2 秒或清單超過 1 秒時也不問就結束〕
- **CR-6**：06a 依 §11 的 12 個 task 規劃；planner 超過 13 個就照 D-06-14 拆出 06c（Copy），不要擠。

〔06a〕規劃時的裁決（S1–S21；理由與出處在 06a plan 的「Spec issues found while planning」，數字是規劃時量的；執行時改過的在各條的〔06a 執行〕）：
- S1：Copy 的 backend（§11 的 06a T10–T11、A9、A11、A16、`copy_data`、`OneTimePlanStore`）連同 CopyDataDialog 拆成 06c；06a 交給 06c 的是 `TableColumn`／`TypeFamily`（含 `read_as`）、job 框架與清單、`InsertBatcher`（`backslash_escapes`、`max_rows`）與 `build_copy_select`。
- S2：dump 的結果是 `completed` 或 `failed`（什麼都沒寫），加上 `not_started`；取消是 `failed` 帶 `cancelled=true`。〔06a 執行〕第一句設定送出之前的取消是 `not_started`；`failed` 的 `rows` 是 0（什麼都沒寫，進度事件說到了哪裡）。
- S3：PG 的非有限 float 照既有的 literal 寫成 `'inf'`／`'nan'`（P4：PG 讀得回）；MySQL、SQL Server 的 float 沒有非有限值；只有 SQLite 需要 `9e999`（L3）。〔06a 執行〕NaN 在 SQLite 寫 NULL；−0.0 保住正負號：PG `'-0.0'`、MySQL `-0.0e0`（Task 8 的 fuzz 抓到 PG 300 列裡 2 次、MySQL 4 次的遺失）；float 的規則只在 `float_literal`。
- S4：A15 的 CAST 文字寫不回 datetimeoffset：改用 `CONVERT(…, 126)`；`datetime_literal` 也把位移前的空格拿掉（⑤ 的 grid 編輯輸入 `… +08:00` 時一樣受惠）。〔06a 執行〕style 126 寫 `+00:00`（不是 `Z`）、不寫全為 0 的小數，`0001-01-01T00:00:00-14:00`、`9999-12-31T23:59:59.9999999+14:00` 在 us_english 與 British 都寫回相同。
- S5：`STREAM_CHUNK` 是 512 KiB（不是 1 MiB）：切 231 MB、1,000 句的 dump，1 MiB 一塊時切句本身的 RSS 尖峰 +91 MB，512 KiB 時 +28 MB（38.7 對 42.7 秒）；sqlite3 的連線 `cached_statements=0`（dump 的大 INSERT 每句不同，預設 128 句的快取在 200 句 1,000 列的 INSERT 後讓 RSS 多 111 MB，關掉後 4 MB）。
- S6：§1 成功標準的「全有或全無」對 MySQL 做不到（DDL 隱含 commit）：MySQL 的往返以逐句還原。〔06a 執行〕全有或全無不再「在第一個 CREATE TABLE 之後停（partial）」：見 S22，DbaGuy 的 MySQL dump 全有或全無在第 5 句（第一個 DDL）送出前就被拒（rolled_back／`implicit_commit`），往返測試兩種都測。
- S7：SQL Server 的 function 與 view、synonym 一起在資料之後依 `sql_expression_dependencies` 排序（inline／table-valued 與 schema-bound 的 function 建立時就要表在）；只有表的預設值、check、computed 欄用到的 function 在表之前。〔06a 執行〕那些 function 讀 view 時，綁定它們的 view 跟著移到表之前；預設值或 check 的 function 讀表、view 或 synonym 時，那個預設值／check 在 view 之後才以 `ALTER TABLE` 加上（Task 7 審查）。
- S8：SQL Server 的 IDENTITY_INSERT 讓 identity 接在寫入的最大 id 之後（附錄 A S2），來源的值更大（刪過列）時資料之後補 `DBCC CHECKIDENT (…, RESEED, n)`，不會重複給號。〔06a 執行〕清空過的表要 `RESEED` 到 `n + increment`（`last_value` 是 NULL 時，pre-flight M8），只往前不往後（逐表讀時讀到的列可能超過先讀的計數），已 reseed 而空的表以 `IDENT_CURRENT` 帶過去；數值以 80 位的精確 decimal 算（decimal(38,0) 的計數）。
- S9：SQL Server 的 system-versioned、graph、ledger 表不寫（照抄成一般表就不是同一種表），檔頭列出。〔06a 執行〕有 period 的表、ledger 表的 view 也是；需要它們的物件一起略過並點名。
- S10：MySQL 的 `AUTO_INCREMENT=n` 照 `SHOW CREATE TABLE` 寫：它不在 snapshot 裡，可能比 dump 的列大，不會重複給號。
- S11：沒有 `--hex-blob` 的 mysqldump 檔（二進位欄是原始 bytes）不是 UTF-8：照 §1 的範圍不支援，在那一行停並提示 `--hex-blob`，檢查時也警告。〔06a 執行〕那一行之前的語句照樣送（pre-flight M3：同一個 512 KiB 塊裡、壞行之前的語句以前會被丟掉），`stopped_at` 是讀到的最後一句的下一句、行號是壞的那一行。
- S12：加 `POST /data/jobs/clear`（Jobs 的 Clear finished，§3.6）。
- S13：SQL Server 的 SNAPSHOT 從第一次讀取開始（不是 BEGIN）：dump 的第一次讀取就是 catalog。〔06a 執行〕catalog view 不分版本：snapshot 開始後才建的表或加的欄 catalog 看得到，讀它的列時是 3961（dump 說那張表在 dump 中被改了，請重新 dump）。
- S14：逐句的 run 讀完檔案時若交易還開著，rollback 並在結果說明（不替檔案 commit）。〔06a 執行〕不只檔案結束時：檔案自己開的交易被追蹤（pre-flight H2），在交易裡停下也 rollback，並把那段的語句從「已套用」的計數拿掉；PG 跳過錯誤時，abort 的交易讓 run 停下。
- S15：檔頭與 `-- Data:` 等註解裡的名稱把換行寫成 `\n`（`comment_safe`）：否則名稱的其餘部分會被當成 SQL 執行。〔06a 執行〕`str.splitlines` 會斷行的每個字元與 NUL 都寫成跳脫（U+2028 會讓 `parse_header` 讀短檔頭）。
- S16：`transaction_ended` 的括號依 dialect：MySQL 是「MySQL commits around DDL」，其他是「a statement in the file ended it」。〔06a 執行〕其他 dialect 的結果是 unknown（不是 partial），文字是「(a statement in the file committed or rolled it back): what ran before it may or may not be applied」（pre-flight H3：T-SQL 的 TRY/CATCH ROLLBACK 讓「之前的已套用」不成立）；全有或全無時 T-SQL 批次裡的 COMMIT／ROLLBACK／BEGIN TRAN／SAVE TRAN 在送出前就拒絕。
- S17：`DumpObjectKinds` 沒有另外的勾選：MySQL 的 event 跟著 routines，SQL Server 的 synonym 跟著 views。
- S18：MySQL 的 database 就是 schema 節點：範圍 `database` 回 422（「dump its schema node」）。
- S19：PG 繼承（`INHERITS`）的子表 dump 成獨立的表（`table_ddl` 不寫 INHERITS），列各自讀（`FROM ONLY`）。〔06a 執行〕檔頭的 Not included 寫「inheritance (N child tables come back as tables of their own)」（L6）；typed table 的 `OF` 同樣不寫、同樣註明。
- S20：MySQL 的 stored program 照它建立時的 sql_mode 寫（`SET SESSION sql_mode = …` 夾在 DELIMITER 裡），之後恢復檔頭的模式。〔06a 執行〕表與 view 也在還原時的模式下讀（dump 的 session 先 `SET SESSION sql_mode` 成檔頭的，pre-flight M5：`ANSI_QUOTES` 的 server 原本寫出 `"m"`）；program 與 view 另帶自己的 `character_set_client`／`collation_connection`，event 帶自己的 `time_zone`（Task 6 審查）。
- S21：SQL Server 的檔頭不寫 `SET NOCOUNT ON`：在它之下 INSERT 不回報列數，還原的結果會說 0 列（其他兩句照 §3.3.3）。
- S22（pre-flight H1，06a 執行時加）：MySQL 全有或全無的 run 在送出前就拒絕 MySQL 會先隱含 commit 的語句（DDL、LOCK／UNLOCK TABLES、GRANT 等、START／STOP REPLICA、SET PASSWORD、`SET autocommit`；CREATE／DROP TEMPORARY 除外，`implicit_commit`）與 CALL／EXECUTE（`procedure_call`）：送出之後失敗的 DDL 也已 commit 了前面的語句，以前回報 rolled_back，mysqldump 的 `DROP TABLE` 也會在「全有或全無」下刪掉表。結果 rolled_back，什麼都沒套用。

〔06a〕執行時的其他決定（細節在各 task 的 report 與 CLAUDE.md「資料工具」）：
- Job 框架：plan 的 `replay_job`（F2）換成 `SessionWorker.attach_emitter(emit)`：在 job-send lock 下先把 job 最後的 `job_progress` 與 `job_outcome` 送給新的 client，再換 emitter，所以重送的事件不會重複、也不會被即時事件超前（pre-flight M2）；job 結束後才接上的 client 拿到最後一個 `job_progress`、`job_outcome`（修正波 F3 起帶 `error`）與 attach 時的 `session_state`，沒有 `finished`。WS 層就被拒絕的 job 指令也有 `job_outcome(not_started)`（M1）；session 在 job 的最後一個事件之前都算忙碌（L1）；壞掉的 job 留著的交易 rollback（L2）；session 先關、job 還沒結果時清單標 unknown（L3）。
- `RunOptions(describe=False)` 也表示不預讀列（SQLite 200 列、SQL Server 100 列的型別 peek，PG cursor 的 200 列預讀），dump 依列的大小讀才算數；grid 與匯出不變。`fetch()` 只有 `[]` 是結束：SQL Server 在列之後的錯誤以前只在 `next_result()` 丟出，dump 與 CSV 匯出會把被截斷的表當成完整（Task 9 審查 H1），現在下一次 fetch 就丟出。
- Run SQL file 的結果（R-06-12 的表，每一列都以 server 上的實際狀態驗過）：MySQL 失敗後交易已不在時是 unknown（1213 的 deadlock 除外，R1）；SQL Server trigger 自己 COMMIT 的 3609 是 unknown（R2）；MySQL rollback 之後的警告 1196（MyISAM 等不能 rollback 的表）讓結果變 unknown（R3、N5）；從第 N 句開始時，前面 allow-list 上的 session 設定照送（R4，`sql/session_settings.py`）；`COMMIT AND CHAIN`／`completion_type=CHAIN` 從那裡重算檔案的交易（R5）；PG 的 `DO` 和 CALL 一樣算「可能 commit、會跑好幾句」（R6）；SQL Server 以 `CURRENT_TRANSACTION_ID()`（BEGIN 之後、失敗時、COMMIT 前）分辨交易被 commit 又重開（R10、N4），MySQL 的 CALL 在檔案的交易裡跑過時之後的 rollback 是 unknown（N1）；MySQL 的 cancel 剛好在語句完成時到，那句算已套用（R8）；MySQL 停下時在 rollback 之後送 `UNLOCK TABLES`（R9）。殘留的已知限制：trigger 先 commit 又重開，之後 XACT_ABORT 的錯誤或 deadlock 把重開的交易 rollback 時，回報 rolled_back 而 trigger commit 的列留著。
- Dump 的讀取（pre-flight M12、Task 9 審查 M1）：`FIRST_FETCH` = 1，之後每次 `FETCH_CHARS`（32 MiB）÷ 目前最大的一列、最多 `FETCH_ROWS` = 1,000；磁碟每寫 `DISK_CHECK_CHARS`（16 Mi 字元）看一次、少於 `DISK_FLOOR_BYTES`（256 MiB）就停（M14）；`atomic_text_file` 在換名前 `fsync`（L9，所有使用者）；不能寫進 SQLite 資料庫檔（M13）；沒有可寫欄的表以預設值的列還原同樣的列數（Task 7 審查）；只有資料的 dump 父表先寫、PG 與 SQL Server 警告 FK 循環與參照自己的表（M9、M4），PG 另 `REFRESH` materialized view（L3）；dump 中途表被改（1412、3961）有自己的訊息。
- 篩選（dump 與表格資料 tab，Task 9 審查 M2）：只能是一個條件：`check_filter` 的單一運算式、dialect lexer 下 depth 0 的子句字（每個 dialect 只列它的 server 在 WHERE 條件之後讀成子句的字）與沒開過的 `)` 都拒絕，組出來的 SELECT 與 COUNT 都要是唯讀 allow-list 的 READ 並過 session 的閘門，否則什麼都不送（`invalid_filter`）。PG 的 plan 在 `SET LOCAL lock_timeout = 5000` 下讀 catalog（M3）。
- T-SQL 的數字照 SQL Server 2022 的讀法切（`LexRules.number`，splitter、classifier、`select_builder` 共用；Task 9 fix round 3）：`0x` 加任意長度的 hex 是一個 binary literal，指數記號不論有沒有正負號與數字都跟著數字，`$1.5` 是 money，`$action` 等是一個名稱。以前 `0x1TRUNCATE` 被切成 `0` + `x1TRUNCATE`（server 讀成 `0x1` + `TRUNCATE`），唯讀的 query tab、表格瀏覽與 dump 的篩選都能藉此送出寫入；這修的是 ③ 就有的 lexer。其他 dialect 不變（它們的 server 拒絕黏在一起的寫法）。
- 一句太長（pre-flight M11、Task 9 fix L5；R-06-11 原本寫「超過 64 MiB 的一句」）：`sql/bulk_insert.size_warning` 每張表第一個太長的語句警告一次：超過 Run SQL file 的 256 MiB（`MAX_STATEMENT_CHARS`，每個 dialect，以字元計），或 MySQL 超過被 dump 的 server 自己的 `@@max_allowed_packet`（UTF-8 bytes；讀不到時才用 `MYSQL_PACKET_BYTES` 的 64 MiB；5.7 的預設是 4 MiB，所以一批 1 MiB 的多列 INSERT 也可能太長）。
- 各 engine 的 catalog 超出 §3.3.3 的部分（PG 的 range、collation、aggregate 會寫，operator 與 text search 只計數並點名用到它們的物件；MySQL 的 NO_BACKSLASH_ESCAPES program、invalid view、用到讀不到的 routine 的物件略過並點名；SQLite 用到 virtual table 的 view 與 trigger 略過並點名；SQL Server 依 `dump_graph` 帶走需要被略過物件的物件）：各 task 的 report，摘要在 CLAUDE.md。

〔06a〕最終審查的修正波（F1–F5）：
- F1：PG 的 run 的記憶體是 psycopg 3 的 prepared-statement 快取（見 §7）：每條 PG 連線 `prepare_threshold=None`（adapter 的 `open()` 一處；DbaGuy 沒有用 named prepared statement），profile 的 options 不能帶 psycopg 自己的 `connect()` 參數。
- F2：篩選過的單表 dump 在檔頭寫 `-- Rows: only those of <table> that match a filter: <filter>`（`comment_safe`），`DumpHeader` 讀出 `filtered`／`filtered_table`；Run SQL file 的 inspect 警告「The file holds only the rows of <table> that match a filter」，會先 DROP 時加上「: restoring it drops the table and replaces it with those rows」；dump 的 plan（請求加 `drop_first`、`filter`）與結果在篩選加 `drop_first` 時同樣警告。
- F3：`JobOutcomeEvent.error`（`JobError`：前面 `error` 事件的 code 與文字，run 停下時加 `stopped_at` 的行號與第幾句），job 層級的警告（`JOB_ROLLBACK_FAILED`、開始前被取消或 session 先關）也進 `warnings`：重新載入的視窗、晚接上的 client 與 `GET /data/jobs` 都看得到為什麼停或失敗。
- F4：SQLite 全有或全無的 COMMIT 因 FK 被拒、檔案有 `PRAGMA foreign_keys = OFF` 時，訊息提示逐句執行（交易裡那句不生效；逐句時生效，來源本來就違反 FK 的列照原樣載入，已實測）。⑤ 的 `SQLITE_BUSY_NOTE` 是前端的文字，backend 不能 import：06b 依 `job_outcome.error.code` 加。
- F5：MySQL 登入讀不到定義的物件除了計數也點名（一則警告，`shown_names`）；MySQL 的 Not included 計數一個時用單數（`adapters/dump.counted`；其他 engine 之後改）。

之後（不在 ⑥）：
- Couchbase query 結果 → 關聯式表的 copy（R-06-14；值與欄位走匯出的路徑，`MISSING` → NULL、物件與陣列 → JSON 文字）。
- copy 的 upsert（PG `ON CONFLICT`、MySQL `ON DUPLICATE KEY UPDATE`、SQL Server `MERGE`）與「略過既有的列」。
- 批次協定：PG `COPY`（psycopg 的 `cursor.copy()`）、SQL Server 一個批次多句（〔實測〕20 列 × 50 句 26,931 列／秒）；MySQL `LOAD DATA LOCAL` 要 server 開 `local_infile`，多半沒有。
- Run 的速度：切句與 classifier 共用 token；⑥ 自己寫的 INSERT 的快速路徑。
- 可續傳的 copy（依 key 的 keyset 分頁）；dump 的壓縮；跨 engine 更完整的建表（預設值、index 的翻譯）。
- ⑤ M9：apply 改用 History 的 `begin`／`finish`。
- 02c 的 carry：⌘Q 與視窗關閉的守衛擴大到所有 close guard（交易、未存的 script）。

## 13. 收尾時要更新的文件

- `CLAUDE.md`：Repo 結構加 `sessions/jobs/`、`sql/{statement_stream, bulk_insert, dump_script, type_map}.py`、`adapters/*/dump.py`、`adapters/columns.py`、`services/{data_service, plans}.py`、`api/data.py`、`features/data-tools/`；架構重點加「資料工具」一段（job 框架與 session、dump 的順序與一致性、run 的兩種模式與 `gate_confirmed`、copy 的管線與型別對照、結果分類、History 的 running／interrupted）；已知限制（§8、§9 的踩到）；打包版量測；進度表 ⑥。
- 每份 plan 的 followups note，含給使用者的最終測試清單：四個 engine 的 dump → run 往返；mysqldump 與 pg_dump 的檔案；copy 的跨 engine 與附加；取消與 Show in Finder；Jobs 與重新載入；⌘Q 守衛；公司資料庫的檢查清單（SQL Server 的 snapshot 設定與 VIEW DEFINITION、MySQL 的 DEFINER 與 `sql_mode`、PG 的 extension 與 `money[]`、大表的速度）。

## 附錄 A：探測紀錄（2026-10-20，dev 資料庫）

腳本在本次 session 的 scratch 目錄（session 結束後可能清除），結論如下。

**PostgreSQL 17.11／psycopg 3.3.6**
- P1：寫入 `GENERATED ALWAYS AS IDENTITY` 欄 → 428C9；加 `OVERRIDING SYSTEM VALUE` 可以；generated 欄加了 `OVERRIDING` 仍是 428C9。
- P2：明確寫入 identity（id 5、10、11）之後，`zz_06_pg_a_id_seq` 是 `last_value 1, is_called false`，下一個 DEFAULT 的 INSERT 拿到 id 1：序列要 `setval`。`pg_get_serial_sequence` 對 identity 與 serial 都回傳序列名。
- P3：多列 VALUES 第一列全是 NULL、其餘是 unknown literal（jsonb、`int[]`、timestamptz、interval、38 位 numeric、enum、`'\x00ff'::bytea`、real、double）：型別取自目標欄，寫入成功。
- P4：float 欄寫 `'inf'`、`'nan'` 存成 inf、nan；text-first 讀回：jsonb 是 server 正規化的文字、陣列 `{1,2,NULL}`、timestamptz 依 session 時區（UTC 時 `…+00`，只有小時）、numeric 精確、enum、bytea 原生 bytes、real 1.1、double 0.1。`IntervalStyle = sql_standard` 的輸出（`1 2:03:04`）在 postgres style 下讀回相同的值；ISO 8601 的 `P1DT2H3M4S` 也可以。
- P5：`search_path = ''` 時 `format_type` 寫 `public.zz_06_mood`、預設值寫 `nextval('public.zz_06_pg_a_s_seq'::regclass)`；預設的 search_path 時都不帶 schema。
- P6：子表先寫（FK 指向不存在的父列）→ 23503。
- P7：`'NaN'::numeric` 可以；`'1e400'::numeric` 精確。
- P8：容器裡的 `pg_dump 17.11 --inserts` 輸出：第一行 `\restrict <key>`、最後一行 `\unrestrict <key>`；中間有 `SET statement_timeout = 0` 等十多行 `SET`、`SELECT pg_catalog.set_config('search_path', '', false)`、`SET check_function_bodies = false`、`CREATE TABLE public.…`、`ALTER TABLE public.… OWNER TO dbaguy`、`INSERT INTO public.… VALUES (1, 'a''b')`、PK 在資料之後以 `ALTER TABLE ONLY … ADD CONSTRAINT`。
- P9：`CREATE FUNCTION … LANGUAGE sql AS 'SELECT count(*) FROM zz_06_missing'`：`check_function_bodies` 開著是 42P01，`SET check_function_bodies = false` 之後可以建；`RETURNS SETOF zz_06_missing`（表的 row type 還不存在）關掉檢查也是 42704。
- P10：`lc_monetary = en_US.utf8` 的 server，`money` 欄的 `1234.56` 文字輸出是 `$1,234.56`、`m::numeric` 是 `1234.56`；數字 literal `-98765432.1` 寫進 money 欄讀回 `-98765432.10`。

**MySQL 8.4.11／mysql-connector-python 26.7.0（pure）**
- M1：預設 `sql_mode`（`ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION`）寫 `'0000-00-00'` → 1292；`SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO'` 之後 `'0000-00-00'` 與 `'2026-00-15 00:00:00'` 寫入、沒有警告；AUTO_INCREMENT 欄寫 0 保留為 0。
- M2：多列 VALUES（第一列全 NULL；DATE、DATETIME(6)、`CAST(… AS JSON)`、`b'1010'`、DECIMAL(30,10)、`X'00ff'`、`'C:\\tmp'`、18446744073709551615、ENUM、`'838:59:59'`）寫入成功；JSON 欄寫一般字串 `'{"x": 1.10}'` 被解析成 JSON，存成 `{"x": 1.1}`。
- M3：generated 欄寫值 → 3105；寫 `DEFAULT` 可以。
- M4：`SHOW CREATE TABLE` 的結尾 `ENGINE=InnoDB AUTO_INCREMENT=9 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci`；FK 在 CREATE TABLE 裡。子表先寫 → 1452；`SET FOREIGN_KEY_CHECKS = 0` 之後可以。
- M5：`information_schema.VIEW_TABLE_USAGE` 可用（dev 有 25 列），列出 view 用到的表。
- M6：`SHOW CREATE VIEW`：目前的 database 就是 view 的 database 時，表與欄只寫 `` `zz_06_my_t`.`id` ``；從別的 database 看，寫成 `` `dbaguy`.`zz_06_my_t`.`id` ``。兩者都有 `ALGORITHM=UNDEFINED DEFINER=`dbaguy`@`%` SQL SECURITY DEFINER`。
- M7：BIT(4) 欄的 `b'1010'` 以 text protocol 讀回是 10（adapter 的 `_bit` 轉成整數）。
- M8：`START TRANSACTION` → INSERT → `CREATE TABLE` → `ROLLBACK`：INSERT 的列還在（DDL 隱含 commit）。
- M9：`START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY` 裡 `SELECT COUNT(*)` 與 `SHOW CREATE TABLE` 照常。`@@max_allowed_packet` 是 67,108,864。
- M10：`CREATE PROCEDURE … SELECT COUNT(*) FROM zz_06_missing` 可以建（body 建立時不檢查表）；`CREATE VIEW … AS SELECT zz_06_missing_fn()` 是 1305（view 建立時檢查 function）。

**SQL Server 2022 CU27（16.0.4305.5）／pymssql 2.4.4**
- S1：`datetime` 收 `N'2026-10-05T12:34:56.123'`，不收六位小數的 `.123000`（241，INSERT、比較、變數都是）；`smalldatetime` 收 `…12:34:00`，不收 `….000000`（295）。adapter 本來就以最少的小數位寫（`mssql/types._timespec`），所以 ⑤ 的 literal 不受影響。
- S2：寫 IDENTITY 欄 → 544；`SET IDENTITY_INSERT … ON` 之後寫入 id 100，關掉後下一個是 101。
- S3：computed 欄 → 271；rowversion 欄 → 273。
- S4：多列 VALUES 第一列全 NULL、第二列有 uniqueidentifier、`0x00ff`、`N'é😀'`、xml、money、decimal(38,10)、七位小數的 datetimeoffset：寫入成功。
- S5：一句 1,001 列的 VALUES → 10738；1,000 列可以。
- S6：geography 以 `CAST(g AS varbinary(max))` 讀出的 bytes，以 `0x…` 寫回 geography 欄可以。`sql_variant` 存 `CAST(42 AS sql_variant)`，driver 讀回 `b'*\x00\x00\x00'`。
- S7：`CAST(d2 AS varchar(27))`、`CAST(dto AS varchar(34))`、`CAST(t AS varchar(16))` 寫出 `2026-10-05 12:34:56.1234567`、`2026-10-05 12:34:56.1234567 +08:00`、`12:34:56.1234567`，寫回後三欄都與原列相等；driver 直接讀只到微秒。
- S8：`BEGIN TRAN; CREATE TABLE …; ROLLBACK` 之後表不存在。
- S9：dev 的 database `snapshot_isolation_state_desc = OFF`、`is_read_committed_snapshot_on = 0`。
- S10：`datetimeoffset` 不收 `+00`（241），收 `+00:00`；`CAST(N'…+00:00' AS datetime2(7))` 直接丟掉位移、保留牆上時間，不換算。
- S11：`CREATE SEQUENCE … START WITH 10 INCREMENT BY 5`：沒用過時 `sys.sequences` 的 `current_value` 是 10、`last_used_value` 是 NULL；`NEXT VALUE FOR` 兩次（10、15）之後 `current_value` 與 `last_used_value` 都是 15。

**SQLite 3.53.4（Python `sqlite3`）**
- L1：`CREATE VIEW v AS SELECT * FROM missing` 可以（建立時不檢查）。
- L2：寫 generated 欄 → 「cannot INSERT into generated column」。
- L3：AUTOINCREMENT 表明確寫 id 100 → `sqlite_sequence` 是 100；`9e999` 存成 REAL 的 inf，`'inf'` 存成 TEXT；`0.0/0.0` 存成 NULL；無型別欄的 `'007'` 是 TEXT。
- L4：600 列的 VALUES 可以；`BEGIN; CREATE TABLE …; ROLLBACK` 之後表不存在。
- L5：DbaGuy 的 SQLite adapter 連線後 `PRAGMA foreign_keys = ON`（`adapters/sqlite/adapter.py`）。

**不經資料庫（純函式）**
- X1：classifier（sqlglot）分類一句 1,000 列的 INSERT（84–92 KB）：PG 174 ms、MySQL 160 ms、T-SQL 212 ms、SQLite 154 ms；100 列 15–22 ms；1 列 0.4–0.5 ms。`tracemalloc` 的尖峰：0.08 MB 的語句 9.5 MB、0.84 MB 的語句 94.8 MB（113 倍）。sqlglot 對 PG `ON CONFLICT … DO UPDATE`、MySQL `ON DUPLICATE KEY UPDATE`、SQLite `ON CONFLICT`、`INSERT … SELECT`、`REPLACE INTO` 都是 WRITE、沒有危險理由；T-SQL `INSERT INTO t EXEC p` 是 UNKNOWN。
- X2：切句（`split`）：多列 INSERT 2.7–3.0 MB/s、一列一句 2.1 MB/s。切句之後每句再 `SafetyPolicy.check(confirmed=True)`：整體 1 列一句 0.30 MB/s、100 列 0.50、1,000 列 0.48；sqlglot 換成關鍵字規則時 0.47、0.64、0.63（`check()` 會再切一次、再 tokenize 一次）。
- X3：`open_writer(INSERT)` 寫 10 萬列（6 欄：大整數、含 emoji 與單引號的文字、浮點、時間、NULL、bytes）：PG 167k 列／秒、MySQL 164k、T-SQL 147k、SQLite 166k（23–28 MB/s）。
- X4：driver 直接載入 10 萬列（每句 1,000 列；auto-commit／一個交易）：PG 125,859／191,204 列／秒、MySQL 61,245／120,231、SQL Server 7,354／7,694、SQLite 518,742／615,426。SQL Server 2 萬列、一個交易：每句 1,000 列 7,678、100 列 21,847、20 列 16,536、每批 10 句 × 100 列 23,202、每批 50 句 × 20 列 26,931。
- X5：MySQL `CAST('… +00' AS DATETIME(6))` 是 NULL、INSERT 是 1292；`+00:00` 可以（DATETIME 收下牆上時間，TIMESTAMP 依 session 時區換算）。PG 在 `TimeZone = 'UTC'` 的 session 把 `…+08` 寫成 `2026-10-20 04:34:56.123456+00`、`+05:30` 也換成 `+00`。
