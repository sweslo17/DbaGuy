# 子專案 ⑧：MCP server（唯讀）— 設計

> 日期：2026-10-25 ｜ 狀態：coordinator 審核（2026-10-25）後修訂：裁決 CR-08-1…CR-08-12 在 §12，每一條都已併入本文（標〔CR-08-n〕）；OP-1、OP-2、OP-4…OP-8 照原文接受，OP-3 改為 ⑧ 的一個 task（CR-08-11）。branch `feat/08-mcp-server`，自 `main` 2990fc0。
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §2（Q1–Q6）、§3 S9（「MCP server（唯讀，沿用 IDE 已設好的連線）」）與 M2、M11、M12、M13、§5 AI 策略的原則 1–6（尤其原則 5：「給 agent 用的唯讀**必須在 DB 層級強制**……不能只靠 MCP 設定或 prompt」）、§8（「AI：**只做 MCP server**」）。前例：[landscape](../../research/2026-10-15-sql-ide-landscape.md) 的 DBHub（分層的 `search_objects`、row limit、timeout）、DBCode（連線的唯讀設定對 agent 同樣生效）、DBeaver（MCP allowlist、sample rows 預設關閉）、已封存的官方 Postgres MCP（Datadog 以堆疊語句加 `COMMIT` 繞過它的唯讀交易）。
> 技術依據：dev 資料庫上的探測（2026-10-25；PostgreSQL 17.11／psycopg 3.3.6、MySQL 8.4.11／mysql-connector-python 26.7.0 pure、SQL Server 2022 16.0.4305.5／pymssql 2.4.4、SQLite 3.54.1（venv 的 Python 3.13.5）、Couchbase EE 7.6.12），標 **〔實測〕**，紀錄在附錄 A（探測編號自成一組：G 是 DbaGuy 的分類器、P／M／S／T／C 是各 engine、K 是 SDK；審核後的複驗是 Q1-P、H1、H2、H3、H6）；MCP Python SDK `mcp` 2.3.0 與它的相依以 `pip download` 與 scratch 目錄裡的獨立 venv 檢查（沒有裝進專案的 venv），標 **〔SDK 原始碼〕**；標 **〔未實測〕** 的是假設，§10 彙整並指定由哪個 plan task 先驗證。探測只建 `zz_08_*` 物件，以完整名稱 DROP；Couchbase 只建過 `zz_08_` 名稱的東西與一個 index advisor session，都已移除（`app.orders` 仍是 500 份）。來源在附錄 B。

## 1. 目標

讓 Claude Code（與其他 MCP client）透過使用者在 DbaGuy 已經設好的連線，**唯讀地**看 schema、跑查詢。五個 engine 都支援。

| 編號 | 目標 | 對應 |
|---|---|---|
| G1 | sidecar 內的 MCP endpoint（Streamable HTTP，只綁 `127.0.0.1`，固定 port），DbaGuy 開著時才有 | S9、§8 |
| G2 | **預設關閉**：全域開關，加上每條連線自己的存取等級（Off／Schema only／Read-only queries，預設 Off） | 原則 1、3 |
| G3 | 寫入由**資料庫**擋（PG、MySQL、SQLite、Couchbase），加上 DbaGuy 的閘門：只收一句以允許的字開頭的讀取，關掉唯讀交易擋不住的路（server 檔案、殺 session、鎖、從文字執行 SQL）；SQL Server 只做得到 app 層加交易包住；登入的危險權限讀出來給使用者與 agent 看 | 原則 5、M11、Q3 |
| G4 | 分層探索 schema（列出、搜尋、describe），不一次 dump；查詢與 EXPLAIN（不 ANALYZE）只在 Queries 等級 | 原則 3、M2、DBHub |
| G5 | 每次查詢都有上限：列數、cell 長度、回應大小、記憶體、timeout；可取消 | M12、Q1 |
| G6 | 使用者看得到 agent 做了什麼：每個查詢（含被拒絕的）進 History，標 MCP 與 client 名稱，有自己的上限，不擠掉使用者的紀錄 | M13、Q3 |
| G7 | 不問密碼：沒有可用密碼的連線回「先在 DbaGuy 解鎖」 | Q3 |
| G8 | Settings 有開關、port、token（複製、重新產生）、可直接複製的 Claude Code 註冊指令、狀態；連線對話框有存取等級與登入權限；History 有 MCP 標記與篩選 | Q6 |

### 成功標準

- 開關關著：sidecar 不 import SDK、不開 port，啟動時間與記憶體和 ⑥ 相同（§7）。
- 開了之後，Settings 的指令貼進終端機，Claude Code 的 `/mcp` 看到 `dbaguy` 連上；`list_connections` 只列出存取等級不是 Off 的連線，並列出每條的登入權限。
- 五個 engine：`run_query` 跑一句 SELECT 回最多 `max_rows` 列；Datadog 的寫法（`SELECT 1; COMMIT; DROP TABLE …`）與附錄 A 的每個變形（含 H1–H3）都在送出前被拒，而且**閘門被拿掉時**（測試直接對 agent session 送寫入）PG、MySQL、SQLite、Couchbase 的 server 也拒絕寫入；SQL Server 的寫入被 rollback（序列等非交易的副作用除外，誠實列出）。
- `SELECT repeat('x', 100000000) FROM generate_series(1, 1000)` 這類查詢不會讓 sidecar 的記憶體超過 §3.6 的上界。
- 被拒、出錯、逾時的查詢都進 History（`source=mcp`、client 名稱、狀態）；agent 的紀錄最多 5,000 筆、不擠掉使用者的 50,000 筆；server 的錯誤原文只出現在工具結果與 DEBUG log。
- 沒存密碼也沒解鎖的連線：工具結果是「先在 DbaGuy 連一次或解鎖」，DbaGuy 不跳任何對話框。
- token 錯、Host 不對、帶 Origin 的請求分別是 401、421、403，都不進 MCP 的處理。

### 不在範圍內

任何寫入（含 DML 的 EXPLAIN，R-08-7）；透過 MCP 的 Copy、dump、Run SQL file；AI chat、explain／fix 按鈕（§8）；MCP resources 與 prompts（R-08-11）；OAuth（本機單一使用者，bearer token 就夠，R-08-3）；stdio bridge（R-08-10）；agent 專用的登入欄位（CR-08-2：另開一條連線）；Couchbase 的文件 KV 讀取（`run_query` 的 `USE KEYS` 就夠）；遠端（非 localhost）存取；多使用者或每個 client 不同的權限；row-level 遮罩。

## 2. 關鍵決策

R-08-x 回答 coordinator 列出的設計問題，D-08-x 是其下的設計決定，CR-08-x 是 coordinator 審核的裁決（§12，產品面的決定屬於 coordinator）。「§x」指本文，「FP §x」指 feature-priorities，「附錄 A」是探測紀錄。

| 決策 | 選項 | 結論與理由（含證據） |
|---|---|---|
| **R-08-1** 在哪裡跑、用什麼 | (a) sidecar 內，官方 SDK；(b) 自己實作 JSON-RPC；(c) 另一個 process | **(a)**，`mcp` 2.3（`mcp>=2.3,<3`）的 `MCPServer`（v2 把 FastMCP 改名成 `mcp.server.mcpserver.MCPServer`，`mcp.server.fastmcp` 只剩一個指向遷移指南的 `ModuleNotFoundError`〔SDK 原始碼〕），以 `streamable_http_app()` 取得 Starlette app，跑在**第二個 uvicorn server**（自己的 thread 與 event loop、自己的 socket），與 API 的 server 分開：API 的 port 每次啟動隨機，不能註冊進 Claude Code；MCP 的 port 要固定（R-08-2）。不自己實作：SDK 同時處理兩個協定時代（2025-11-25 的 `initialize`＋session 與 2026-07-28 的每個請求自帶 `_meta`、`MCP-Protocol-Version`／`Mcp-Method`／`Mcp-Name` header 的比對〔SDK 原始碼、附錄 B 規格〕）。工具的服務（metadata、schema cache、session、History）是 container 裡同一份。代價（K1、K2）：SDK 帶進 `cryptography`（`_rust.abi3.so` 10.4 MB）、`httpx2`、`jsonschema`、`rpds-py`、`pyjwt`、`sse-starlette`、`python-multipart` 等，安裝約 22 MB；`import mcp` 一定會載入 client 與 `cryptography`，import 0.21 秒、RSS +23 MB〔實測，scratch venv〕。所以 **lazy import**：開關打開時才 import（D-08-8），關著的使用者完全不付這個成本（Q1）。 |
| **R-08-2** port | 固定 port、可改 | **17531**（設定範圍 1024–49151）。理由：低於 macOS 的 ephemeral 範圍 49152–65535（`net.inet.ip.portrange.first`〔實測〕），OS 不會把它配給別的程式的隨機 port，包括 DbaGuy 自己 API 的隨機 port；`/etc/services` 沒有登記；不是常見開發工具的預設（3000、5173、8000、8080、MCP Inspector 的 6274／6277），本機 `lsof` 也沒有人在用〔實測〕。正式 data dir 之外的 data dir（`dev/data`、測試、臨時 sidecar）預設 **17532**，比照 `keychain_service`：dev 與正式 app 同時開著也不互搶。被占用時狀態是 `port_in_use`。DbaGuy 沒開時別的程式可以聽這個 port 收 token：已知限制（CR-08-10）。 |
| **R-08-3** 認證 | (a) 長期的 bearer token；(b) SDK 的 OAuth | **(a)**：`secrets.token_urlsafe(32)`（43 字元），存 Keychain（service 依 data dir：`app.dbaguy`／`app.dbaguy.dev`，account `mcp:token`），啟動 MCP 時讀一次、留在記憶體；比較用 `api/deps.py` 既有的 `_same_token`。Settings 可複製、可重新產生（舊 token 的下一個請求就是 401）。(b) 要 issuer 與授權伺服器 metadata，本機單一使用者不需要；規格對本機 HTTP server 的建議就是「Require an authorization token」（附錄 B）。代價：Claude Code 把 header 存在 `~/.claude.json`（明文、mode 600），任何以使用者身分執行的程式讀得到，比 Keychain 裡的 DB 密碼弱（§9）。 |
| **R-08-4** Host／Origin（DNS rebinding） | SDK 的預設、或更嚴 | Host 只收 `127.0.0.1:<port>` 與 `localhost:<port>`（421）；**帶 `Origin` 的請求一律 403**：CLI client 不送 Origin，送的只有瀏覽器。SDK 對 localhost 的自動設定允許 `http://localhost:*` 與 `http://127.0.0.1:*` 的 Origin〔SDK 原始碼 `sse_app`〕，任何本機網頁都符合，所以改傳 `TransportSecuritySettings(allowed_hosts=[…兩個…], allowed_origins=[])`。代價：瀏覽器裡的 client（MCP Inspector）連不上。認證的 middleware 在最外層。請求 body 上限 1 MiB。 |
| **R-08-5** 誰看得到哪條連線 | 全部、或逐條開啟 | 全域開關（預設關）＋每個 profile 的 `mcp_access`：`off`（預設，所有既有與新的連線）／`schema`／`query`。`schema`：名稱、型別、key、index、DDL，**不讀列**；`query`：再加 `run_query`、`explain`、Couchbase 的 `sample_fields`。每次呼叫都重讀 profile；profile 一存檔就關掉它的 agent session 與 agent 的 catalog 連線（D-08-1）。連線本身是唯讀與否不影響：MCP 一律唯讀。agent 一律用 profile 的登入；要 agent 用另一個（唯讀）登入，就另開一條連線、只對它開 MCP（CR-08-2）。 |
| **R-08-6** 唯讀怎麼強制 | (a) 只靠分類器；(b) 分類器＋server 的唯讀 | **(b)，分層**（§3.4）：① DbaGuy 的閘門（D-08-2）；② agent session 一律以**唯讀的 profile 副本**開；③ 每次呼叫包在 server 強制的唯讀交易裡、**一律 ROLLBACK**，之後釋放 session 層的鎖（D-08-3）；④ server 的時間上限。為什麼要 ②＋③ 都有〔實測〕：PG 只有 `BEGIN READ ONLY` 時堆疊的 `COMMIT; DROP` 照樣成功（P1），session 預設唯讀才擋住（P2）；function 能在交易裡把 session 預設關掉，只有 ROLLBACK 能還原（P3）。MySQL：function 能把 session 改成 READ WRITE、ROLLBACK 不還原（M3），只有每次明確的 `START TRANSACTION READ ONLY` 擋得住（M3e）。SQLite 的 `mode=ro` 擋不住 ATTACH 與 `VACUUM INTO`（S3、S4），所以另加 `query_only` 與 authorizer（D-08-4）。**唯讀交易只擋寫入資料庫**：它不擋 server 檔案的讀寫與執行程式（PG superuser 或 `pg_read_server_files`／`pg_write_server_files`／`pg_execute_server_program`：`COPY … TO` 檔案與 `PROGRAM`、`pg_read_file`、`pg_ls_dir`、`pg_stat_file`，Q1-P；MySQL 有 FILE 時的 `INTO OUTFILE`）、殺別的 session（`pg_terminate_backend`，P8）、session 層的鎖（P6、M5）、從文字執行 SQL 的內建 function（H1）。這些由閘門逐一關掉（CR-08-1、CR-08-4），登入的權限讀出來揭露（CR-08-1）。 |
| **R-08-7** 工具與上限 | — | 7 個工具，全部 `readOnlyHint`（§3.5）：`list_connections`、`list_objects`、`search_objects`、`describe`（Schema）、`run_query`、`explain`、`sample_fields`（Queries；最後一個只有 Couchbase）。Couchbase 的 query context 不另開工具（OP-5）。上限（§3.6）：列數預設 100、最多 1,000；cell 1,000 字元、最多 20,000；回應最多 40,000 字元；記憶體上界是回應預算加一頁（CR-08-7）；timeout 預設 30 秒、最多 60 秒，red 連線都是 30 秒（CR-08-8）；`sql` 最多 20,000 字元（CR-08-3）。`explain` 只接受讀取（OP-6：MySQL 在唯讀交易裡連 `EXPLAIN INSERT` 都拒絕，M8）。 |
| **R-08-8** 使用者看得到什麼 | 只有 log、或 History | **History**：`run_query`、`explain`、`sample_fields` 每次一筆，`source = "mcp"`、`client` = client 自報的名稱；被閘門拒絕的也記（狀態 `refused`，OP-7）。agent 的紀錄有自己的上限與合併規則、重開到 tab 時有橫幅、被拒的不能重開（CR-08-3）。schema 工具不進 History，INFO log 記工具、連線、物件名稱與筆數。 |
| **R-08-9** 密碼 | 問、或不問 | **不問**：開 agent session 或 agent 的 catalog 連線走 `ProfileService.resolve_secrets`，`PasswordRequired` 變成 `password_required`：「'<name>' needs its password: connect to it in DbaGuy first (or unlock it). DbaGuy never asks for a password over MCP」。`list_connections` 以 `known_secrets` 算出 `needs_unlock`。 |
| **R-08-10** stdio bridge | 做、或不做 | **不做**。Claude Code（§8 的使用者唯一提到的 client）原生支援 HTTP 加 header（附錄 B）；bridge 要讓每個 client session 多啟動一個凍結的 sidecar（啟動 0.5 秒、RSS 約 70–80 MB，⑤⑥ 量過的單獨 sidecar），只為了轉送（Q1）；token 還得經環境變數或另一條讀 Keychain 的路。只支援 stdio 的 client 可以自己用通用的 stdio↔HTTP bridge（例如 supergateway、mcp-remote），不隨 DbaGuy 附帶。 |
| **R-08-11** resources／prompts | 做、或不做 | **不做**。schema 已經是工具；prompts 是 chat 的東西（§8 不做）。只給 server `instructions` 一段（§3.5）。 |
| **R-08-12** SQL Server | 允許 Queries、或禁止 | **允許，誠實揭露**：沒有 session 唯讀（03b），寫入只由閘門擋；`BEGIN TRANSACTION … ROLLBACK` 讓漏網的 DML／DDL 回復（T6），非交易的副作用回復不了（T3：`NEXT VALUE FOR`；linked server、extended procedure）。對話框、`list_connections` 的 `read_only_by: "dbaguy"`、Settings 都說明；建議另開一條只有 `db_datareader` 的連線（CR-08-2）。登入權限見 CR-08-1。 |

| 決策 | 內容 |
|---|---|
| **D-08-1** agent session | `SessionManager.create_agent(profile_id, database)`：與 tab 的 session 同一個類別與 reaper，但 ① opener 用 `profile.model_copy(update={"read_only": True})` 並帶 `agent=True`（D-08-4）；② hooks 用 agent 版（`check` = D-08-2 的閘門，`on_record` 寫 `source="mcp"`，`capabilities` 的 `read_ahead_bytes` 設為 None：暫停時不預讀，記憶體上界才成立，CR-08-7）；③ 沒有 WebSocket：MCP 層自己裝 emitter，依 `request_id` 把事件交給等待中的呼叫。key 是 (profile, database)（PG／SQL Server 一個 database 一條連線；MySQL 的 database 就是 schema；SQLite 忽略；Couchbase 改用 `query_context`）。同一個 session 一次一個呼叫（MCP 層的 lock）；等 lock 的時間算進 timeout。最多 8 個 agent session，滿了先關最久沒用的閒置那個；閒置的照既有 reaper（600 秒）關。開啟時讀一次登入權限（CR-08-1）；MySQL 另把 session 的 `sql_mode` 去掉 `NO_BACKSLASH_ESCAPES` 與 `ANSI_QUOTES`（CR-08-5）。不碰使用者的 tab、workspace、暫存。profile 存檔或刪除時關掉（`_forget_profile_state` 加一行）。 |
| **D-08-2** 閘門（`SafetyPolicy.agent_read(sql, dialect, purpose)`） | 純函式，MCP 層在送出前呼叫（拒絕時寫 History `refused`、回工具錯誤），agent session 的 worker hooks 也用同一個（後盾）。依序：① U+0000 拒絕（`_check_text`）；② **剛好一句**：`split()` 一段、段內沒有 `;`，T-SQL 另外要 `tsql.statements()` 剛好一句（G4、G5）；③ **開頭的字在 dialect 的允許清單**〔CR-08-6〕：PG `SELECT`、`WITH`、`VALUES`、`TABLE`、`SHOW`；MySQL `SELECT`、`WITH`、`VALUES`、`TABLE`、`SHOW`；SQLite `SELECT`、`WITH`、`VALUES`（裁決的清單裡 SQLite 沒有的語句 `TABLE`、`SHOW` 不列）；T-SQL `SELECT`、`WITH`；SQL++ `SELECT`、`WITH`。`EXPLAIN`／`DESCRIBE`／`DESC` 只由 `explain` 工具自己組（`purpose="explain"` 時允許 D-08-10 組出的開頭）。清單擋掉分類器叫 READ 卻不是查詢的東西：T-SQL 的 `DECLARE`（含 `CURSOR GLOBAL`，ROLLBACK 後還在，H3-b）、`WAITFOR`、`PRINT`、`THROW`、`OPEN`、`FETCH`、`RAISERROR … WITH LOG`（寫 error log）；SQLite 的 `PRAGMA`（`wal_checkpoint`，H3-c）；SQL++ 的 `INFER`、`PREPARE`（C3）、`ADVISE`；④ 分類器說 `READ` 並通過 `_allowed_when_read_only`（既有的 `is_plain_read`）：`WITH … DELETE／UPDATE／INSERT`、`WITH … SELECT INTO` 都不是 READ，所以照樣拒絕（H3-a，五個 dialect 都驗過）；⑤ **function 與子句的拒絕清單**（表在 `services/safety.py`，和既有的 `_PG_HARMLESS_SETTINGS` 放在一起）：以 function 呼叫的名稱比對（schema 限定時取最後一段；沒引號的不分大小寫；引號裡的照原樣，`"PG_TERMINATE_BACKEND"` 是另一個不存在的 function；**PG 的 `U&"…"` 以 `UESCAPE` 解碼後再比**〔CR-08-4〕）。PG：`pg_terminate_backend`、`pg_cancel_backend`、`pg_advisory_*`／`pg_try_advisory_*`、`pg_reload_conf`、`pg_rotate_logfile`、`pg_switch_wal`、`pg_promote`、`pg_create_*`、`pg_drop_*`、`pg_replication_*`、`pg_log_*`、`set_config`、`dblink*`、`pg_file_*`，server 檔案的 `pg_read_file`、`pg_read_binary_file`、`pg_ls_dir`（與 `pg_ls_*`）、`pg_stat_file`、`lo_*`〔CR-08-1〕，從文字執行 SQL 的 `query_to_xml`、`query_to_xmlschema`、`query_to_xml_and_xmlschema`、`cursor_to_xml`、`ts_stat`、`ts_rewrite`〔CR-08-4〕；`COPY` 不論哪種形式都因開頭不在清單而拒絕。MySQL：`GET_LOCK`、`RELEASE_LOCK`、`RELEASE_ALL_LOCKS`、`LOAD_FILE`，以及任何 `INTO` 子句（`OUTFILE`、`DUMPFILE`、`@var`；H2 的變形靠 CR-08-5 讓 lexer 與 server 一致）〔CR-08-1〕。SQLite：`load_extension`。SQL++：`CURL`、`ADVISOR`（`start` 在 `readonly=true` 下照樣建立 advisor session，H3-d）。拒絕清單擋不住使用者自訂 function 裡的同類呼叫：鎖由 D-08-3 的收尾釋放；殺同一個登入的其他 session、讀 server 檔案只有另一個登入能完全防住（CR-08-2，§9）。 |
| **D-08-3** 唯讀框架（`sql/dialects.py` 的 `ReadFrame` 表，與 ⑤ 的 `TransactionFrame` 並列） | 每次 `run_query`／`explain`：以 `worker.call()` 送 DbaGuy 自己的開頭（不經 agent 閘門，和 catalog 查詢同一條路），語句以 `ExecuteCommand` 經閘門送出，分頁讀到預算（CR-08-7）後 discard，再以 worker 的 `RollbackCommand`（adapter 的 `rollback()`）結束，最後以 `call()` 送收尾。開頭：PG `BEGIN READ ONLY` 加 `SET LOCAL statement_timeout = <timeout + 5 秒>`（P10）；MySQL 先讀 `SELECT @@SESSION.sql_mode, @@SESSION.transaction_read_only`（有 `NO_BACKSLASH_ESCAPES` 或 `ANSI_QUOTES` 就拒絕這次呼叫並關掉 session，`session_changed`，CR-08-5；`transaction_read_only` 由 CR-08-11 的共用 helper 處理），再 `SET SESSION max_execution_time = <ms>` 與 `START TRANSACTION READ ONLY`（M3e、M7）；SQL Server `BEGIN TRANSACTION`；SQLite、Couchbase 沒有。收尾〔CR-08-4〕：PG `SELECT pg_advisory_unlock_all()`、MySQL `SELECT RELEASE_ALL_LOCKS()`，在 ROLLBACK 之後：使用者自訂 function 拿的 session 鎖也放掉（H1-e）。ROLLBACK 或收尾失敗就關掉這個 agent session：絕不帶著可能未結束的交易或鎖接下一個呼叫。每次多 3–4 個往返，本機 SQL Server 的交易包裝 0.45 → 1.30 ms（T8）。 |
| **D-08-4** agent 連線（`DatabaseAdapter.open(…, agent=True)`） | `ConnectionService.open` 多一個 `agent` 參數。PG、MySQL、SQL Server、Couchbase 不需要額外動作（唯讀副本、框架、MySQL 的 `sql_mode` 在 D-08-1／D-08-3）；**SQLite**：`mode=ro` 之外再 `PRAGMA query_only = ON`，並裝 `set_authorizer`：允許 `SQLITE_SELECT`、`SQLITE_READ`、`SQLITE_FUNCTION`（`load_extension` 除外）、`SQLITE_RECURSIVE`，以及名稱在唯讀清單的 `SQLITE_PRAGMA`（`table_list`、`table_info`、`table_xinfo`、`index_list`、`index_info`、`index_xinfo`、`foreign_key_list`、`database_list`：沒有寫入形式的 pragma，DbaGuy 的 SQLite catalog 用的就是其中幾個，H6），其餘（ATTACH、DETACH、其他 PRAGMA、寫入、DDL、`VACUUM`、BEGIN）都是 `SQLITE_DENY`（S9、H6：ATTACH、`VACUUM INTO`、`PRAGMA query_only = 0` 都被拒）。理由：`mode=ro` 下 ATTACH 進來的檔案可寫（S4），`VACUUM INTO` 會寫出新檔（S3）；`query_only` 擋住前者，但 `VACUUM INTO` 仍留下空檔（S5）。authorizer 是 SQLite 引擎在 prepare 時的檢查，屬於「DB 層級」。 |
| **D-08-5** MySQL 唯讀的共用 helper〔CR-08-11〕 | M3：一個 stored function 能在 SELECT 裡把 session 改成 READ WRITE，ROLLBACK 不還原，之後的 autocommit 語句就能寫（M3d）。這也影響**使用者自己的唯讀 tab**（唯讀 profile 只靠連線時的 `SET SESSION TRANSACTION READ ONLY`）。⑧ 做一個 MySQL adapter 裡的共用 helper（`adapters/mysql/read_only.py`），agent session 與唯讀的 tab 都用：每句之前確認 session 仍是 READ ONLY（`transaction_read_only` 是 0 就先送 `SET SESSION TRANSACTION READ ONLY`，WARNING log），像 PG 的 `_restore_read_only`；agent 的框架另外每次明確 `START TRANSACTION READ ONLY`。確認的成本是每句一個往返；plan 可改用協定的 session-state tracking（`session_track_system_variables` 加 `transaction_read_only`）省掉它，前提是 pure driver 讀得到（08a 的 task 先驗證）。 |
| **D-08-6** 工具結果的形狀 | 每個工具回 `CallToolResult`，內容是一段精簡的 JSON 文字，失敗時 `is_error=True`、`{"error": {"code", "message"}}`。不宣告 `outputSchema`、不回 `structuredContent`（同一份資料會出現兩次，吃掉一半的大小上限）。**錯誤一律以結果回傳、不 raise `ToolError`**：SDK 對 `ToolError` 會 `logger.info("Tool %r failed: %r", …, str(exc))`〔SDK 原始碼〕，server 的錯誤原文就進了 INFO log；回傳 `CallToolResult` 時 SDK 原樣轉送、不記錄。沒預料的例外：INFO 以上只記型別。 |
| **D-08-7** client 是誰 | 2025-11-25 的 client 在 `initialize` 自報 `clientInfo`，只存在 stateful session 裡（SDK 的 stateless 舊路徑給 tool 的 `client_params` 是 None〔SDK 原始碼〕），所以舊路徑用 stateful；2026-07-28 每個請求的 `_meta` 帶 `io.modelcontextprotocol/clientInfo`（可省略）。取 `name`（最多 80 字元，去掉控制字元），沒有就是 null。這是自報的、不是認證。 |
| **D-08-8** 生命週期（`agent/host.py` 的 `AgentHost`） | `mcp.enabled` 變成 true（或啟動時已是 true）：背景 thread 裡 lazy import SDK、從 Keychain 讀 token（沒有就產生）、bind `127.0.0.1:<port>`、建一個**新的** `MCPServer` 與 app（SDK 的 session manager 不能重用〔SDK 原始碼〕）、跑第二個 `uvicorn.Server`。關閉：`should_exit`、join（最多 5 秒）、關掉所有 agent session 與 agent 的 catalog 連線。port 改了＝關再開。狀態 `off`／`starting`／`listening`／`port_in_use`／`error`。啟動時若已開啟，在 API 就緒之後才在背景啟動（Q1）。 |
| **D-08-9** SDK 的 transport 設定 | `streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=False, max_sessions=32, session_idle_timeout=1800, max_request_body_size=1 MiB, transport_security=R-08-4)`。stdlib 的 `mcp.*` logger 設成 WARNING，經既有的 `InterceptHandler` 進 loguru；`MCPServer` 呼叫的 `logging.basicConfig` 在 root 已有 handler 時是 no-op〔SDK 原始碼〕。 |
| **D-08-10** explain 怎麼組 | agent 給一句通過閘門（`purpose="query"` 的規則）的讀取，DbaGuy 依 dialect 表組：PG `EXPLAIN <stmt>`（文字）；MySQL 8.0.16 以上 `EXPLAIN FORMAT=TREE <stmt>`，更舊的 `EXPLAIN <stmt>`；SQLite `EXPLAIN QUERY PLAN <stmt>`；SQL Server 以 `call()` 在語句前後送 `SET SHOWPLAN_TEXT ON`／`OFF`（SHOWPLAN 下語句不執行，T7；`OFF` 失敗就關掉 session）；Couchbase `EXPLAIN <stmt>` 加 ④ 既有 `explain.py` 的 findings。組出來的文字以 `purpose="explain"` 再過一次閘門與框架。 |
| **D-08-11** schema 工具走 agent 自己的唯讀連線〔CR-08-9〕 | `MetadataService` 多一種 key：(profile, database, agent)。它的 worker 和 agent session 一樣以唯讀副本加 `agent=True` 開（SQLite 有 authorizer、PG／MySQL 的 session 唯讀、Couchbase 的內部查詢本來就 `readonly=true`），其餘照既有的 metadata 機制（重連、閒置 300 秒關閉、寫入不重送）。五個 engine 的 catalog 都是 `DatabaseConnection` 的方法，**都能在這條連線上跑**：SQLite 的 catalog 在 authorizer 下照常（H6：`list_children`、`object_detail`、snapshot 都成功，只用到 `database_list`、`foreign_key_list`、`table_xinfo` 等唯讀 pragma）；SQL Server 的 catalog 會把連線設成 `READ UNCOMMITTED`、`LOCK_TIMEOUT 5000`、`XACT_ABORT ON`（`_metadata_call`），所以它必須是和 agent session 分開的一條，不能共用（否則 agent 的查詢變成 dirty read）——這也是為什麼五個 engine 一律用分開的 catalog 連線，而不是在 agent session 上 `call()`。代價：agent 用到的每個 (profile, database) 最多兩條連線（查詢一條、catalog 一條，後者閒置 300 秒就關）。`SchemaCache` 與 `FieldSampleService` 的快取共用：已經有 snapshot 或取樣就直接用（記憶體裡的資料，不連線）；沒有時才由 agent 的 catalog 連線建立。describe 的組法不變：`detail(path)`＋snapshot 的 PK／FK＋indexes 資料夾；DDL 預設不給；物件可用 `path` 或 `{database?, schema, name}`（snapshot 精確比對、再不分大小寫，用 `relation_path` 組路徑）。agent 給的名稱只進既有的 catalog 查詢（bind 或 `sql/dialects.py` 的 quote）；08a 以惡意名稱測一次。 |
| **D-08-12** 設定與 API | `AppSettings.mcp = McpSettings(enabled=False, port=17531 或 17532)`；profile 的 `mcp_access: Literal["off", "schema", "query"] = "off"`（舊檔讀起來就是 off）。API（API token 保護）：`GET /mcp/status`、`GET /mcp/token`、`POST /mcp/token/regenerate`；`GET /mcp/connections/{profile_id}/login`（連線對話框用：讀一次登入權限，CR-08-1）；`PUT /settings` 改了 `mcp` 就通知 `AgentHost`。 |
| **D-08-13** History 的資料〔CR-08-3〕 | 表加 `source TEXT NOT NULL DEFAULT 'app'`、`client TEXT`、`repeats INTEGER NOT NULL DEFAULT 1`，與索引 `(source, id)`（開檔時以 `PRAGMA table_info` 檢查、`ALTER TABLE … ADD COLUMN`，舊檔原地升級）；`HistoryStatus` 加 `refused`；`GET /history` 加 `source` 篩選。修剪分開：`app` 保留 50,000、`mcp` 保留 5,000，各自依 id 刪最舊的。client 隨 MCP 內部的執行指令帶進 worker（`ExecuteCommand` 的內部子類，不在 WebSocket 的 union 裡）。`explain` 記 `EXPLAIN` 加語句（SQL Server 註明是 SHOWPLAN）；`sample_fields` 記一行摘要（`-- MCP sample_fields <bucket.scope.collection> (<n> documents)`）。 |
| **D-08-14** Log | INFO：`MCP <tool> on '<profile>' by <client>: <outcome>, <rows> rows, <ms> ms`；拒絕：`MCP refused <tool> on '<profile>': <reason code>`（不帶 SQL）；錯誤只記 code；AgentHost 的啟停、port、狀態；登入權限（例如 `superuser`、`FILE`）在 INFO。DEBUG：SQL 文字與 server 的錯誤原文。token 永遠不進 log。 |

## 3. 架構

### 3.1 模組總覽

```
backend/dbaguy_backend/
  agent/                      # ⑧：MCP endpoint（不叫 mcp/，免得和 SDK 的頂層套件 `mcp` 混淆）
    host.py                   # AgentHost：lazy import、第二個 uvicorn、啟停、狀態（D-08-8）
    server.py                 # 建 MCPServer、註冊工具、instructions；ASGI：bearer 認證 middleware（最外層）
    tools.py                  # 7 個工具：參數模型、等級檢查、呼叫 services、CallToolResult（D-08-6）
    reader.py                 # AgentReader：agent session 的取得與 lock、框架、分頁、discard、rollback、收尾
    shape.py                  # 純函式：列、cell、回應的預算、分頁大小與截斷（§3.6）
    login.py                  # 登入權限的讀取與摘要（CR-08-1；engine 的查詢在各 adapter）
    token_store.py            # Keychain 的 MCP token
  domain/agent.py             # McpAccess、McpSettings、McpStatus、LoginPrivileges、工具的結果模型、錯誤碼
  services/safety.py          # + agent_read()、允許的開頭、拒絕清單、U& 解碼（D-08-2）
  services/metadata_service.py# + agent 的 key（D-08-11）
  sql/dialects.py             # + ReadFrame 表、explain 的組法（D-08-3、D-08-10）
  adapters/base.py            # open(…, agent=False)；login_privileges()
  adapters/sqlite/adapter.py  # agent=True：query_only＋authorizer（D-08-4）
  adapters/mysql/read_only.py # 共用的唯讀 helper（D-08-5），tab 與 agent 都用
  sessions/manager.py         # + create_agent()；sessions/hooks.py：agent 版 hooks
  services/history_service.py # + source、client、repeats、refused、分開修剪（D-08-13）
  api/mcp.py                  # /mcp/status、/mcp/token、regenerate、登入權限
src/
  features/settings/          # McpSection.tsx、mcpCommand.ts（純函式：指令字串）
  features/connections/       # ConnectionDialog：AI access、登入權限；Duplicate connection（CR-08-2）
  features/history/           # MCP 標記、Source 篩選、refused、合併次數、只能複製
  features/query/             # 從 History 重開的 MCP 語句的橫幅（CR-08-3）
  features/explorer/          # 連線列的 AI 標記；連線選單的 Duplicate…
```

依賴方向：`agent/` → services、sessions、domain；services 不知道 `agent/`。engine 的知識只在 adapter 與 `sql/`、`services/safety.py` 的表（ReadFrame、explain、允許的開頭、拒絕清單、登入權限的查詢）。

### 3.2 HTTP endpoint

- URL：`http://127.0.0.1:<port>/mcp`，只 bind `127.0.0.1`。
- 每個請求依序：bearer 認證（401，`WWW-Authenticate: Bearer`）→ Host（421）→ Origin（有就 403）→ Content-Type → body 上限 → 協定版本與 header 比對（SDK）。
- 兩個協定時代都接：2025-11-25 走 stateful session（`Mcp-Session-Id`，閒置 30 分鐘、最多 32 個）；2026-07-28 沒有 session。
- 同步的工具在 `anyio.to_thread` 執行〔SDK 原始碼〕。`run_query`／`explain` 是 async，等待 `to_thread`（`abandon_on_cancel=True`），被 client 取消時呼叫 `worker.cancel(request_id)`。〔未實測：A4〕

### 3.3 存取等級、連線與登入

| 工具 | `schema` | `query` | 連線 |
|---|---|---|---|
| `list_connections` | 列出 | 列出 | 不連線（登入權限是開過 session 之後的快取） |
| `list_objects`、`search_objects`、`describe` | ✓ | ✓ | agent 的 catalog 連線（D-08-11） |
| `run_query`、`explain` | `not_allowed` | ✓ | agent session（D-08-1） |
| `sample_fields` | `not_allowed` | ✓ | agent 的 catalog 連線（Couchbase） |

`off` 的連線不列、也不存在（`unknown_connection`）。`connection` 收 profile 的 id 或名稱。`list_connections` 回：`id`、`name`、`engine`、`access`、`folder`、`color`（red 通常代表 production）、`database`、`default_query_context`（Couchbase）、`read_only_by`（`server`／`dbaguy`）、`needs_unlock`、`login`（`LoginPrivileges`，開過 agent session 或 catalog 連線之後才有，之前是 null）。

**登入權限**〔CR-08-1〕：agent 的連線（session 或 catalog）第一次開啟時，以 adapter 的 `login_privileges()` 讀一次（一個查詢），存在記憶體到 profile 改變：

| engine | 讀什麼 | 摘要 |
|---|---|---|
| PG | `rolsuper`；`pg_has_role(current_user, …, 'member')` 對 `pg_read_server_files`、`pg_write_server_files`、`pg_execute_server_program`、`pg_signal_backend` | `superuser`、`server_files_read`、`server_files_write`、`server_programs`、`signal_backends` |
| MySQL | `CURRENT_USER()` 的 FILE（`information_schema.USER_PRIVILEGES`；以 role 取得的權限 08a 確認讀不讀得到）、`@@secure_file_priv` | `file`、`secure_file_priv`（空字串＝任何目錄、NULL＝停用） |
| SQL Server | 既有的 `login_can_write`（`IS_MEMBER('db_owner')`、`HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'/'UPDATE'/'DELETE'/'ALTER'/'EXECUTE')`），加 `IS_SRVROLEMEMBER('sysadmin')`、`IS_SRVROLEMEMBER('bulkadmin')`（同一個查詢，便宜） | `can_write`、`sysadmin`、`bulkadmin` |
| SQLite、Couchbase | 無（檔案的權限是 sidecar 自己的；Couchbase 的 RBAC 由 `readonly=true` 與 server 的角色處理） | — |

有任何一項時，連線對話框的 MCP 一節列出它們與建議：「This login can <…>. DbaGuy blocks these routes, but a login without them is the only complete protection: duplicate this connection, give the copy a read-only login, and share only the copy」。**不因此拒絕 Queries 等級**（dev 的 PG 使用者就是 superuser，Q1-P）。閘門關掉的路見 D-08-2。

### 3.4 唯讀的分層（每一層都獨立有效）

| 層 | PostgreSQL | MySQL | SQLite | SQL Server | Couchbase |
|---|---|---|---|---|---|
| ① 閘門（一句、允許的開頭、READ、拒絕清單） | ✓ | ✓ | ✓ | ✓（唯一擋寫入的一層） | ✓ |
| 送出層的單句 | adapter 唯讀時 `split()` > 1 就拒（G6） | `MULTI_STATEMENTS` 關閉：堆疊是 1064（M1） | 一次只收一句（S2） | 無：一個批次多句照跑（T1） | 一個請求只收一句（C2） |
| ② session 唯讀 | `SESSION CHARACTERISTICS READ ONLY`＋`conn.read_only`（P2） | `SET SESSION TRANSACTION READ ONLY`＋每句確認（M2、M3，D-08-5）；`sql_mode` 不含兩個旗標（CR-08-5） | `mode=ro`＋`query_only`＋authorizer（S9） | 無 | 每個請求 `readonly=true`（C1） |
| ③ 每次的交易與收尾 | `BEGIN READ ONLY … ROLLBACK`（P3）＋`pg_advisory_unlock_all()` | `START TRANSACTION READ ONLY … ROLLBACK`（M3e）＋`RELEASE_ALL_LOCKS()` | 不需要 | `BEGIN TRANSACTION … ROLLBACK`（T6；序列不回復，T3） | 無交易 |
| ④ server 的時間上限 | `SET LOCAL statement_timeout`（P10） | `max_execution_time`（SELECT，M7） | worker 的 interrupt | worker 的 cancel | worker 的 cancel |
| 閘門漏掉的**寫入資料庫**，server 擋得住嗎 | 是（25006：寫入、`nextval`、`FOR UPDATE`、`lo_import`，P4、P5、P7、P11） | 是（1792，M2、M4） | 是（S1、S9） | **否**，只能 rollback | 是（1000，C1；`UPDATE STATISTICS` 例外，只有分類器擋） |
| 唯讀交易**擋不住**、只靠 ① 的（〔CR-08-1〕修正） | 有權限的登入：`COPY … TO` 檔案／`PROGRAM`、`pg_read_file`、`pg_ls_dir`、`pg_stat_file`（Q1-P）、`lo_export`；任何登入：`pg_terminate_backend`（同一登入的其他 session，P8）、session 的 advisory lock（P6）、從文字執行 SQL 的 function（H1） | FILE：`INTO OUTFILE`／`DUMPFILE`〔未實測：dev 的 `dbaguy` 沒有 FILE、root 只能寫 `secure_file_priv`，A12〕；`GET_LOCK`（M5）；`sql_mode` 讓 lexer 與 server 不一致（H2） | 無 | 全部（只有 ① 與 rollback） | `PREPARE`（C3）、`ADVISOR`（H3-d） |

worker 的 timeout 是主要的時間上限，server 的那份是 sidecar 卡住或死掉時的後盾。

### 3.5 工具

| 工具 | 參數 | 結果 | 重用 |
|---|---|---|---|
| `list_connections` | — | §3.3 的欄位 | `ProfileService`、registry 的能力 |
| `list_objects` | `connection`、`path`（省略＝根）、`limit`（100，最多 1,000） | `[{kind, name, path, has_children, detail}]`、`truncated` | `MetadataService.children`（agent 的 key） |
| `search_objects` | `connection`、`pattern`、`database?`、`include_columns`（false）、`limit`（50，最多 1,000） | 表、view、collection（⌘P 的排名）；`include_columns` 時另比對欄名 | `SchemaCache.search` 與 snapshot |
| `describe` | `connection`、`path` 或 `{database?, schema, name}`、`include_ddl`（false） | 欄位、PK、FK、index、註解、DDL? | D-08-11 |
| `run_query` | `connection`、`sql`（最多 20,000 字元）、`database?`、`query_context?`（Couchbase，`bucket.scope`）、`max_rows`、`max_cell_chars`、`timeout_s` | `{columns, rows, row_count, more_rows, truncated, elapsed_ms, notes}` | 閘門、agent session、worker（policy、timeout、cancel、分頁、`encode_row`、History） |
| `explain` | `connection`、`sql`、`database?`、`query_context?`、`timeout_s` | 計畫文字；Couchbase 另有 `findings` | D-08-10；`explain.py` |
| `sample_fields` | `connection`、`bucket`、`scope`、`collection`、`size`（預設＝app 設定，最多 1,000） | 路徑、型別、次數、上限旗標（`FieldSample`） | `FieldSampleService`（agent 的 catalog 連線、快取共用） |

**一次 `run_query` 的流程**（`agent/reader.py`；`explain` 相同，只是語句由 D-08-10 組）：

1. 解析 `connection`；重讀 profile：Off → `unknown_connection`，`schema` → `not_allowed`；`timeout_s` 依連線顏色夾住（CR-08-8），時鐘從這裡開始。
2. 閘門（D-08-2）：拒絕 → History `refused`（60 秒內同一 client 的同一段文字合併成一筆加次數，CR-08-3）、INFO 一行、回 `refused`；什麼都沒連線。
3. 取得 agent session（`password_required` 在這裡）；拿它的 lock（逾時 `busy`）。
4. `call()` 送 ReadFrame 的開頭（MySQL 先檢查 `sql_mode`）。
5. 送 MCP 內部的執行指令：`page_size` = 第一頁（**1 列**）、剩下的 `timeout_s`、`text_limit = max_cell_chars`、`client`、`query_context`，`RunOptions(describe=False)`（不預讀：PG cursor 的 200 列、SQLite／SQL Server 的型別 peek 都關掉）；worker 照常過 hooks 的閘門、arm／disarm、timeout、`encode_row`、`on_record`。
6. 收到一頁（`result_set_end(has_more=True)`）就由 `shape.py` 量：編碼後的大小（對回應預算）與每列的**原始大小**（cell 編碼裡的 `size`：被截斷的文字、bytes 都帶它；其餘以編碼後的長度計）。未超過回應預算且未到 `max_rows` → `FetchMoreCommand(count=下一頁)`（§3.6 的頁大小）；否則 → `DiscardCommand`。結束於 `finished` 或 discard 的結束。
7. `RollbackCommand`（SQLite、Couchbase 不需要）→ `call()` 送收尾（PG、MySQL 的鎖釋放）；失敗就關掉 session。放開 lock。
8. 組結果；INFO 一行。

結果的樣子（一個 `TextContent`）：

```json
{"columns": [{"name": "id", "type": "int8"}, {"name": "note", "type": "text"}],
 "rows": [["9007199254740993", "it's 中文"], [2, {"$type": "text", "preview": "…", "size": 52000, "truncated": true}]],
 "row_count": 2, "more_rows": true, "truncated": null, "elapsed_ms": 12,
 "notes": ["More rows exist: raise max_rows (at most 1000) or narrow the query"]}
```

server `instructions`（草稿）：「DbaGuy shares some of the user's database connections, read-only. Start with list_connections (it also shows what each login is allowed to do). Explore with list_objects (pass back the `path` it returns), search_objects and describe. run_query and explain need the connection's 'Read-only queries' access; each call runs exactly one SELECT/WITH/VALUES/TABLE/SHOW statement, inside a read-only transaction that is rolled back. Results are capped (max_rows, max_cell_chars, about 40,000 characters, timeout up to 60 s). SQL Server connections are read-only by DbaGuy's checks only. Couchbase takes a query_context (bucket.scope).」

工具描述以「Read-only.」開頭，寫明需要的等級。cell 的編碼就是 grid 的 `encode_value`（大整數是字串、bytes 是 `{"$type": "bytes", size, preview}`、Couchbase 的 MISSING 與 JSON 原文照 `$type`）；長文字以 `encode_row(row, text_limit=max_cell_chars)` 變成 `{"$type": "text", preview, size, truncated: true}`。SQL Server 在 `describe=False` 時欄位型別名稱可能比 grid 粗（沒有 `sys.dm_exec_describe_first_result_set`），A13。

### 3.6 上限與結果

| 上限 | 預設 | 最大 | 理由 |
|---|---|---|---|
| `max_rows` | 100 | 1,000 | DBHub 也是 100／1,000；100 列 × 10 欄 × 20 字元約 20,000 字元 |
| `max_cell_chars` | 1,000 | 20,000 | 一份 Couchbase 文件或一段 JSON 通常放得下 20,000 |
| 回應 | 40,000 字元（JSON 文字） | 同 | Claude Code 在工具輸出超過 10,000 token 時警告、預設上限 25,000 token，成功的文字結果超過 50,000 字元就存成檔案（附錄 B）；英文約 4 字元一 token。超過時停止讀取，`truncated: {"rows_dropped_for_size": n}`（最後一頁裡放不下的列），`notes` 說明怎麼縮小 |
| `timeout_s`〔CR-08-8〕 | 30 | 60；`color = red` 的連線預設與最大都是 30 | 含等 lock 與 session 開啟；Claude Code 的工具 timeout 預設約 28 小時，所以由我們決定。讀取的鎖最多持有這麼久（§9） |
| `sql`〔CR-08-3〕 | — | 20,000 字元 | agent 的查詢不需要更長；也是 History 一筆 MCP 紀錄的上限（5,000 × 20,000 字元的最壞情況約 100 M 字元，一般的查詢 < 1,000 字元，5,000 筆約 5 MB） |
| server messages | 20 則、各 500 字元 | — | PG NOTICE、SQL Server PRINT 放在 `notes` |
| agent session | — | 8 | D-08-1 |

**記憶體上界**〔CR-08-7〕：MCP 層用 worker 既有的分頁（`page_size`、`FetchMoreCommand`）與 `text_limit`，不另外讀取。**第一頁 1 列**（同 ⑥ dump 的 `FIRST_FETCH`）；之後每頁 = `PAGE_RAW_CHARS`（8 Mi 字元）÷ 目前最大的一列的原始大小，夾在 1–100 列之間，也不超過剩下的 `max_rows` 與「剩下的回應預算 ÷ 目前最大的一列編碼後的大小」（至少 1）。worker 每次向 driver 只拿這一頁（`_CHUNK` 200 是它的上限），`describe=False` 不預讀、agent session 不做暫停預讀（D-08-1），worker 為了 `has_more` 另 peek 1 列。所以任何時刻記憶體裡只有：已收的編碼後的列（≤ 回應預算 40,000 字元，每個 cell ≤ `max_cell_chars`）＋**一頁**的 driver 原始列（≤ max(8 Mi 字元, 最大的一列)）＋一列 peek。**例外**：一個值本身很大時（`repeat('x', 100000000)`），driver 會把那一個值整個讀進來（沒有 driver 能只讀半個值），所以最壞是同時持有**兩列**那樣的列（那一頁的一列加 peek 的一列，約 200 MB），之後每頁一列、讀到回應預算就停（1,000 列的那條查詢在 40,000 字元處停下，不會讀完 1,000 列）。單一巨大的值與 grid 的既有行為相同，列為已知限制（§9）。

一句只取第一個結果集。`row_count` 是回的列數，`more_rows` 表示 server 還有。

### 3.7 History 與可見性〔CR-08-3〕

- History 面板的每一筆 MCP 紀錄有「MCP」標記（tooltip：client 名稱）；篩選 All／App／MCP。
- **上限 5,000 筆**，與使用者的 50,000 筆分開修剪：一個在迴圈裡的 agent 擠不掉使用者的紀錄。5,000 的理由：忙碌的 agent 約每 5 秒一個查詢，一個 8 小時的工作日約 5,760 個；5,000 筆大約保留最近一整天的 agent 活動，足夠回頭查「它剛剛做了什麼」，又只有使用者上限的十分之一。加上 `sql` 的 20,000 字元上限，最壞情況的大小有界（§3.6）。
- **被拒的合併**：同一個 client、同一段文字、60 秒內再次被拒 → 不另寫一筆，那一筆的 `repeats` 加一（面板顯示「×N」）。
- **重開到 tab**：MCP 紀錄的「Open in new tab」開出的 query tab 頂端有橫幅「Written by an AI agent (MCP · <client>): read it before you run it」，可以關掉；橫幅狀態隨 tab 存進 workspace（`QueryTabState.from_agent`）。
- **被拒的不能重開**：`refused` 紀錄沒有「Open in new tab」與任何會把它放進編輯器的動作，只有 Copy（文字上標註被拒的原因）。
- `refused` 有自己的圖示與「Refused」，原因在錯誤欄。

### 3.8 Frontend（08b）

- **Settings › MCP server**：開關「Let AI tools read through DbaGuy」；狀態行；Port；token（遮罩、Copy、Regenerate…）；Claude Code 指令（Copy）：`claude mcp add --transport http --scope user dbaguy http://127.0.0.1:17531/mcp --header "Authorization: Bearer <token>"`（`--header` 是 variadic，必須放在 URL 之後〔實測：Claude Code 2.1.295 的 `claude mcp add --help`〕；user scope 存在 `~/.claude.json`；不建議 project scope）；Regenerate 之後指令前面加 `claude mcp remove --scope user dbaguy &&`；「Connections shared: 2 schema only, 1 read-only queries」。指令字串由純函式 `mcpCommand.ts` 組（Vitest）。
- **連線對話框**：「AI access (MCP)」Off／Schema only／Read-only queries；說明：Queries 時「Query results from this connection are sent to the AI client and its model provider」；SQL Server 加「SQL Server cannot enforce read-only: DbaGuy checks each statement and rolls back」；red 加「This connection is marked as production: AI queries stop after 30 s」；選了 Schema 或 Queries 時讀一次登入權限（`/mcp/connections/{id}/login`）並列出（§3.3），加上**文件化的做法**〔CR-08-2〕：「To give AI tools a read-only login, duplicate this connection, change the copy's login, and share only the copy」，旁邊有 Duplicate 按鈕。
- **Duplicate connection**〔CR-08-2〕：目前沒有這個動作（〔實測〕前端沒有任何 duplicate 的入口）。物件樹的連線選單與連線對話框各一個「Duplicate…」：以同一份設定開啟新連線的對話框（名稱加「 (copy)」、存取等級重設為 Off、密碼不複製），存檔後是一條新的 profile。
- **物件樹**：存取不是 Off 的連線列有一個小的 AI 標記（tooltip 寫等級）。
- **History**：§3.7。

## 4. 錯誤處理

工具錯誤一律是 `is_error=True` 的結果（D-08-6）。

| code | 何時 | message（摘要） |
|---|---|---|
| `unknown_connection` | 沒有這條、或它是 Off | No connection '<x>' is shared with AI tools; list_connections lists them |
| `ambiguous` | 名稱重複、describe 的名稱多個符合 | 列出候選 |
| `not_allowed` | Schema 等級呼叫 Queries 的工具；非 Couchbase 呼叫 `sample_fields` | '<x>' allows schema only: run_query needs 'Read-only queries' in DbaGuy's connection settings |
| `refused` | 閘門拒絕（D-08-2） | DbaGuy runs exactly one read statement per call: <原因>；原因是 `several statements`／`starts with <word>: only SELECT, WITH, … are run`／`not a read (write|ddl|…)`／`<function> is not allowed (<server files|kills sessions|locks|runs SQL from text>)`／`INTO is not allowed`／`EXPLAIN is added by the explain tool` |
| `session_changed` | MySQL 的 `sql_mode` 出現 `NO_BACKSLASH_ESCAPES` 或 `ANSI_QUOTES`（CR-08-5） | The connection's sql_mode changed: the call was not run and the connection was reset |
| `password_required` | R-08-9 | '<x>' needs its password: connect to it in DbaGuy first (or unlock it) |
| `connection_failed` | 開 session 失敗 | server 或 driver 的原文（不進 INFO） |
| `query_failed` | server 的錯誤 | server 的原文與 code（唯讀被 server 擋下時另加「DbaGuy's MCP access is read-only」） |
| `timeout` | 超過 `timeout_s` | Stopped after 30 s (timeout_s; at most 60, 30 on production connections) |
| `cancelled` | client 取消 | — |
| `busy` | 等 lock 逾時 | Another call on '<x>' was still running |
| `not_found` | describe／list 的路徑不存在 | 既有的 NotFound 文字 |

AgentHost 的啟動失敗不影響 API，只反映在 `/mcp/status`。框架的 ROLLBACK 或收尾失敗：關掉那個 agent session，結果照回，`notes` 說明連線已重開。

## 5. Log（loguru）

D-08-14。SDK 的 stdlib logger（`mcp.*`）設 WARNING；SDK 記 Host／Origin 被拒是 WARNING，內容是對方送的 header，不含 token。測試以 `caplog` 確認：一次失敗的查詢（PG 42P01）與一次被拒的查詢之後，INFO 以上沒有 server 的原文、沒有 SQL、沒有 token。

## 6. 測試（依「只測容易出錯的邏輯」）

- **單元（pytest，純）**：閘門：附錄 A 的 G1–G10、H1–H3 與 Datadog 變形逐 dialect（巢狀註解、`$tag$`、E 字串、T-SQL 沒有分號的兩句、MySQL 版本註解、`U&"…"` 加 `UESCAPE`、schema 限定、引號的大小寫、允許的開頭、`WITH … DML`、`INTO`）；`shape.py` 的預算與分頁大小；History 的升級、分開修剪、被拒的合併。
- **整合（dev DB；scratch 一律 `zz_08_*`，`finally` 以完整名稱 DROP；SQLite 用 `tmp_path`；Couchbase 只讀）**：每個 engine：列、上限、`more_rows`、timeout、cancel；**拿掉閘門**直接對 agent session 送寫入（PG 25006、MySQL 1792、SQLite `not authorized`、Couchbase 1000，SQL Server 的 DML 在 ROLLBACK 後不存在）；PG 的 `zz_08_flip()` 之後仍唯讀、自訂 function 拿的 advisory lock 在呼叫結束後已釋放；MySQL 的 `zz_08_flip()` 之後仍擋寫入（tab 與 agent 都測：D-08-5）、`sql_mode` 被改回時 `session_changed`；SQLite ATTACH、`VACUUM INTO` 被拒、沒有留下檔案、catalog 在 authorizer 下照常；SQL Server 的 SHOWPLAN 在 explain 之後關掉；記憶體：PG `SELECT repeat('x', 1000000) FROM generate_series(1, 1000)` 的 RSS 增加在 §3.6 的上界內；登入權限的讀取（dev 的 PG superuser、MySQL 的 `dbaguy` 與 root、SQL Server 的 `db_owner`）；`describe` 的惡意名稱；`password_required` 不連線。
- **HTTP（TestClient 與 `mcp.Client` 對真的 port）**：401、421、帶 Origin 403；兩個協定版本各一次 `tools/list`、`tools/call`；client 名稱進 History；regenerate 後舊 token 401。
- **前端（Vitest）**：`mcpCommand.ts`、History 的 source 篩選與「不能重開」的規則（純函式的部分）。
- **手動**：真的 Claude Code 註冊與每個工具、開關、port、Regenerate、DbaGuy 關掉時、Duplicate connection、History 的橫幅與被拒的紀錄（08b plan 的使用者清單）。
- 規則同 ⑤⑥：scratch 物件存在時不跑完整的 backend 測試，一次只讓一個人跑 live 測試。

## 7. 效能、打包與量測

| 項目 | 預算 | 備註 |
|---|---|---|
| 開關關著 | 冷啟動與 RSS 與 06 相同 | 不 import SDK（測試：`sys.modules` 沒有 `mcp`） |
| 開啟（第一次） | sidecar RSS 增加 ≤ 30 MB；啟動 MCP ≤ 1 秒；第一個 API 請求不受影響 | 〔實測，scratch venv〕import 0.21 秒（冷 0.52 秒）、RSS +23 MB |
| `run_query`（本機，100 列、10 欄，session 已開） | ≤ 100 ms（含框架與分頁） | 框架 +3–4 個往返；100 列是 2 頁（1 列，然後其餘的 99 列） |
| `list_objects`／`describe`（snapshot 已在） | ≤ 200 ms | agent 的 catalog 連線 |
| 打包 | sidecar 增加約 14 MB（06b 量的 49,552 KB → 約 64 MB），**〔估計〕** | `cryptography` 的 `_rust.abi3.so` 10.4 MB（靜態連結 OpenSSL，不影響 `build-backend.sh` 的 libcrypto 檢查）、`rpds` 0.9 MB、`_cffi_backend` 0.2 MB；其餘進 PYZ |

- 打包：hooks-contrib 有 `cryptography`、`jsonschema_specifications` 的 hook；spec 加 `collect_submodules("mcp")`、`collect_submodules("mcp_types")`；`codesign --verify --strict` 涵蓋新的 `.so`。打包後的檢查：凍結的 sidecar（暫存 data dir、settings 先開 MCP）以 `mcp.Client` 對五個 engine 各呼叫一次每個工具、確認 401／421／403、stderr 沒有 `ModuleNotFoundError`；量大小、開啟前後的 RSS 與 `footprint`、`measure-startup.sh`（關著）。
- 相依：`mcp` 2.3.0 與 backend 現有的 starlette 1.8.0、pydantic 2.14.1、anyio 4.15.1、uvicorn 0.54.0、opentelemetry-api 1.45.1 相容（scratch venv 一起安裝沒有衝突〔實測〕）；新的 runtime 相依：`mcp`、`mcp-types`、`httpx2`、`httpcore2`、`truststore`、`jsonschema`（＋`referencing`、`rpds-py`、`attrs`、`jsonschema-specifications`）、`pyjwt`、`cryptography`（＋`cffi`、`pycparser`）、`sse-starlette`、`python-multipart`。

## 8. 痛點對照

| 需求 | 這個子專案怎麼解 | 踩到／退步 |
|---|---|---|
| Q1 輕快低記憶體 | 關著＝零成本；開著的回應與記憶體有上界（回應預算加一頁）、session 有上限且閒置就關；啟動 MCP 在背景 | 開著時 RSS +約 23 MB；`.app` 約 +14 MB；一個巨大的值仍整個讀進記憶體；agent 用到的連線最多兩條 |
| Q2 穩定不丟工作 | agent 自己的 session，不碰使用者的 tab、交易、暫存；每次 ROLLBACK 與釋放鎖；timeout 與取消；agent 的 History 不擠掉使用者的 | 讀取的鎖最多 60 秒（red 30 秒），可能讓使用者的 DDL 或 SQLite commit 等待（§9） |
| Q3 可信任 | 預設關、逐連線開啟；DB 層唯讀（四個 engine）＋閘門關掉唯讀交易擋不住的路；登入權限揭露；token、只綁 localhost、拒絕 Origin；不問密碼；History 記每一個查詢與被拒的嘗試，被拒的不能重開，重開的有橫幅；MySQL 唯讀 tab 的漏洞順手補上（D-08-5） | SQL Server 只到 app 層加 rollback；token 在 `~/.claude.json` 是明文；同一個登入的其他 session 只有另一個登入能完全防住 |
| Q4 聚焦 | 7 個工具、沒有 resources／prompts／chat／寫入；不做 agent 專用的登入欄位（用 Duplicate） | — |
| Q5 多引擎 | 五個 engine 同一套工具，差異只在 adapter 與表 | Couchbase 多 `sample_fields` 與 `query_context` |
| Q6 macOS 慣例 | Settings 的一節；複製按鈕；Duplicate connection | — |
| FP §3 S9、M2、M11、M12、M13 | MCP；分層探索；DB 層唯讀、production 的較短 timeout；timeout 與取消；History | — |
| FP §5 原則 1／3／4／5／6 | 預設關；連線預設 Off、`schema` 不讀列；server 的原文；§3.4；就是 MCP | 原則 5：SQL Server 例外，揭露 |
| 原則 2 | 不適用：DbaGuy 不呼叫模型 | — |
| FP §8 | 沒有 chat、explain／fix 按鈕 | — |

## 9. 風險

| 風險 | 處理 |
|---|---|
| 登入有 server 檔案、執行程式或 FILE 的權限（CR-08-1） | 閘門關掉已知的路（`COPY`、`pg_read_file` 等、`lo_*`、從文字執行 SQL、`INTO`）；權限讀出來在 `list_connections` 與對話框揭露；**文件化的做法是另開一條唯讀登入的連線、只對它開 MCP**（CR-08-2）。不拒絕：使用者可能本機就用 superuser |
| 自訂 function 裡的副作用（PG `SECURITY DEFINER` function 呼叫 `pg_terminate_backend`、dblink、server 檔案；MySQL function 呼叫 `GET_LOCK`） | 寫入照樣被 server 擋；鎖由收尾釋放（D-08-3）；**殺同一個登入的其他 session（包括使用者的 tab）只有另一個登入能完全防住**（CR-08-4、CR-08-2） |
| Prompt injection（資料指使 agent） | 只有唯讀工具；對話框說明 Queries 等級的列會送到 AI client 與模型供應商 |
| token 外洩（`~/.claude.json` 明文） | 唯讀、只綁 localhost、只有開放的連線、DbaGuy 開著時才有；Regenerate；之後可改 `headersHelper`（§12） |
| 固定 port 被搶（CR-08-10） | DbaGuy 沒開時，別的程式可以聽 17531，收到 Claude Code 送來的 bearer token。**已知限制**：同一個使用者的程式本來就讀得到 `~/.claude.json` 的 token，所以新增的暴露只有同一台 Mac 的**另一個 OS 使用者**；固定 port 是 Claude Code 註冊的前提，保留 |
| 讀取的鎖（CR-08-8） | 一次呼叫最多 60 秒（red 30 秒）：PG 的 AccessShareLock 與 MySQL 的 metadata lock 讓使用者的 `ALTER`／`DROP` 等待，而排在 DDL 後面的其他查詢也跟著等；SQLite 的 SHARED lock 讓使用者自己的 commit 在 busy timeout（5 秒）後 `SQLITE_BUSY`；SQL Server 在 READ COMMITTED 下讀取時持有 shared lock。**已知限制**，ROLLBACK 之後立刻釋放 |
| 一個巨大的值（CR-08-7） | driver 整個讀進來；第一頁 1 列、之後依原始大小縮到 1 列，最壞同時兩列（一頁加 peek） |
| SQL Server 沒有 server 唯讀 | R-08-12 |
| SDK 的變動 | pin `mcp>=2.3,<3`；只用四個入口；升級時跑 §6 的 HTTP 測試 |
| Keychain：重新 build 後啟動 MCP 讀 token 時跳「允許存取」 | 與密碼相同的既有限制 |
| client 自報的名字可以是假的 | History 標示為自報；不作為權限依據 |
| 同一條連線的呼叫排隊 | 一次一個，等候計入 timeout |

## 10. 未實測的假設

| # | 假設 | 驗證 |
|---|---|---|
| A1 | Claude Code 2.1.295 以 `--header` 帶 bearer 連上 SDK 的 Streamable HTTP（JSON 回應、兩個協定版本之一），`/mcp` 顯示連上；它自報的 `clientInfo.name` | 08a Task 1：`claude --mcp-config <scratch JSON> --strict-mcp-config` 一次性載入（兩個旗標存在〔實測 `claude --help`〕），不改 `~/.claude.json` |
| A2 | 錯的 token 的 401：Claude Code 是否嘗試 OAuth discovery | 08a Task 1 |
| A3 | 第二個 uvicorn 在 thread 裡反覆開關沒有洩漏 | 08a Task 1 |
| A4 | client 取消會取消 tool 的 task，`worker.cancel` 停掉 DB 的語句 | 08a Task 5 |
| A5 | PyInstaller 收得到 SDK；`codesign --verify --strict` 通過；大小約 +14 MB | 08a Task 9 |
| A6 | 40,000 字元的回應不觸發 Claude Code 的 10,000 token 警告（英文為主） | 08a Task 1 |
| A7 | PG 的 `SET LOCAL statement_timeout` 在 DECLARE／FETCH 的路徑照樣生效 | 08a Task 4 |
| A8 | SQL Server 的 `SET SHOWPLAN_TEXT ON` 與 adapter 的狀態讀取共存；缺 SHOWPLAN 權限時的錯誤 | 08a Task 5 |
| A9 | SQLite 的 authorizer 與 adapter 既有的行為（`cached_statements=0`、`interrupt`）相容 | 08a Task 4 |
| A10 | MySQL 5.7（公司）：`START TRANSACTION READ ONLY`、`max_execution_time`、`RELEASE_ALL_LOCKS`（5.7.5 起）、沒有 `FORMAT=TREE` 時退回 | 公司資料庫的檢查清單 |
| A11 | Couchbase 的 `EXPLAIN` 在 `readonly=true` 下對 SELECT 照常 | 08a Task 5 |
| A12 | MySQL 有 FILE 的登入在 `START TRANSACTION READ ONLY` 裡 `INTO OUTFILE` 照樣寫檔（dev 無法不留檔地驗證；H2 證明 server 會解析到 `INTO OUTFILE` 與權限檢查）；閘門無論如何拒絕 `INTO` | 文件；不需要 dev 驗證 |
| A13 | SQL Server 在 `describe=False` 時的欄位型別名稱夠 agent 用 | 08a Task 5 |
| A14 | MySQL 的唯讀確認能改用 session-state tracking（D-08-5） | 08a Task 6 |
| A15 | MySQL 以 role 取得的 FILE 讀得到（`USER_PRIVILEGES` 只列直接授予的） | 08a Task 3 |

## 11. 執行計畫拆分

**08a backend**（branch `feat/08-mcp-server`）：
1. 相依與 spike：`poetry add "mcp>=2.3,<3"`；`AgentHost`、`token_store`、認證、`TransportSecuritySettings`；`McpSettings`、`/mcp/status`、`/mcp/token`、regenerate；HTTP 測試；A1–A3、A6。
2. Profile 的 `mcp_access`；agent 的 catalog 連線（`MetadataService` 的 agent key）；`list_connections`、`list_objects`、`search_objects`、`describe`；惡意名稱測試。
3. 登入權限：各 adapter 的 `login_privileges()`、`/mcp/connections/{id}/login`、`list_connections` 的 `login`（A15）。
4. 閘門 `SafetyPolicy.agent_read`：允許的開頭、拒絕清單、`U&` 解碼、MySQL `INTO`；附錄 A 的 G、H1–H3 成為回歸測試。
5. agent session：`create_agent`、`open(…, agent=True)`、SQLite authorizer（含唯讀 pragma）＋`query_only`、MySQL `sql_mode` 的清理與每次檢查、`ReadFrame` 與收尾、`AgentReader`（lock、emitter、分頁、discard、rollback、失敗就關）；拿掉閘門的 live 測試、記憶體測試（A7、A9）。
6. **MySQL 唯讀的共用 helper**（D-08-5，CR-08-11）：tab 與 agent 都用；M3 的回歸（唯讀 tab 呼叫 `zz_08_flip()` 之後的寫入仍是 1792）；A14。
7. `run_query`、`explain`、`sample_fields`；`shape.py`；timeout（含 red）、cancel（A4、A8、A11、A13）。
8. History：欄位升級、`refused`、`repeats` 與 60 秒合併、分開修剪（5,000）、`source` 篩選、agent 的 `on_record`；錯誤與 log（`CallToolResult`、`mcp.*` logger、caplog 稽核）；`instructions` 與工具描述。
9. 打包與量測（§7、A5）；CLAUDE.md。

**08b frontend**：
1. Settings 的 MCP 一節、`mcpCommand.ts`（Vitest）、Regenerate 的確認。
2. 連線對話框的 AI access、說明、登入權限；**Duplicate connection**（物件樹的連線選單與對話框）；物件樹的 AI 標記。
3. History：MCP 標記、Source 篩選、Refused 與 ×N、被拒的只能 Copy；從 History 重開的 MCP 語句的橫幅（workspace 存它）；`npm run gen:api`。
4. 給使用者的測試清單（真的 Claude Code、Duplicate、橫幅、被拒的紀錄）。

## 12. 細化與之後

### Coordinator 審核的裁決（CR，2026-10-25；產品面的決定屬於 coordinator）

| # | 問題 | 裁決（coordinator） | 落在 |
|---|---|---|---|
| CR-08-1 | Q1：登入有 server 檔案、執行程式、`pg_signal_backend`、FILE 的權限；唯讀交易擋不住 `COPY … TO`、`pg_read_file` 等、`lo_export`、`INTO OUTFILE`；§3.4 的說法錯、G3 與 D-08-2 不一致 | 允許並揭露：agent 連線開啟時讀一次權限（PG superuser 與四個 `pg_*` role、MySQL FILE 與 `secure_file_priv`、SQL Server `login_can_write` 加 sysadmin／bulkadmin），在 `list_connections` 與對話框顯示並建議另開唯讀登入；不拒絕 Queries；閘門關掉 PG 的 `COPY`、`pg_read_file`、`pg_read_binary_file`、`pg_ls_dir`、`pg_stat_file`、`lo_*`、從文字執行 SQL 的 function，MySQL 的 `INTO OUTFILE`／`DUMPFILE`／`@var`；修正 §3.4 與 G3／D-08-2 | §3.3、§3.4 最後一列、D-08-2、R-08-6、§9；附錄 A 的 Q1-P、G3 |
| CR-08-2 | Q2：agent 一律用 profile 的登入 | 不做 agent 專用的登入欄位；做法是另開一條唯讀登入的連線、只對它開 MCP；沒有 Duplicate connection 就在 08b 加；寫進對話框與 §9 | R-08-5、§3.3、§3.8、§9、08b Task 2 |
| CR-08-3 | Q3：迴圈的 agent 擠掉使用者的 History；被拒的 DROP 重開到 tab 只差一個 ⌘↵ | `source=mcp` 自己的上限約 5,000、分開修剪；60 秒內同一 client 的同一段被拒文字合併計數；MCP 紀錄重開到 tab 有「written by an AI agent (MCP)」橫幅；被拒的不能重開、只能複製 | §3.7（5,000 的理由）、D-08-13、§3.6（`sql` 20,000）、08a Task 8、08b Task 3 |
| CR-08-4 | H1：`U&"…"`／`UESCAPE` 繞過名稱比對；`query_to_xml*`、`ts_stat`、`ts_rewrite` 從文字執行 SQL；`set_config` 同樣的洞 | 閘門解碼 `U&"…"`（含 `UESCAPE`）；拒絕從文字執行 SQL 的內建 function；每次 PG 呼叫在 ROLLBACK 後 `SELECT pg_advisory_unlock_all()`、MySQL `SELECT RELEASE_ALL_LOCKS()`；揭露同一登入的其他 session 只有另一個登入能完全防住。`U&'…'` 字串不需要解碼：規則比對的是 function 名稱，從文字執行 SQL 的 function 整個拒絕，字串內容不影響判斷 | D-08-2、D-08-3、§9；附錄 A 的 H1 |
| CR-08-5 | H2：MySQL 的 `sql_mode` 讓 lexer 與 server 不一致 | agent 的 MySQL session 從 `sql_mode` 去掉 `NO_BACKSLASH_ESCAPES` 與 `ANSI_QUOTES`；框架每次呼叫前檢查 `@@SESSION.sql_mode`，旗標回來就拒絕 | D-08-1、D-08-3、§4 `session_changed`；附錄 A 的 H2（stored function 改的 `sql_mode` 在返回時就還原，所以這個檢查是後盾） |
| CR-08-6 | H3：分類器叫 READ 的非查詢語句 | 每個 dialect 的開頭允許清單（PG／MySQL／SQLite：SELECT、WITH、VALUES、TABLE、SHOW；EXPLAIN／DESCRIBE 只經 `explain` 工具；T-SQL：SELECT、WITH；SQL++：SELECT、WITH，拒絕清單含 ADVISOR）；確認 `WITH … DELETE` 照樣被拒 | D-08-2 ③④；附錄 A 的 H3 |
| CR-08-7 | H4：worker 會先讀 1,001 列整列 | 小頁加位元組預算，停在回應預算；記憶體不超過回應預算加一頁；用 worker 的分頁與 `text_limit`；寫明上界 | §3.5 流程 5–6、§3.6 的記憶體上界（第一頁 1 列、頁依原始大小、8 Mi 字元、peek 1 列）、D-08-1（不預讀）、§9 |
| CR-08-8 | H5：讀取的鎖最多 300 秒 | timeout 預設 30、最多 60；red 連線都是 30；鎖的影響列為已知限制 | §3.6、§4、§9 |
| CR-08-9 | H6：schema 工具在 profile 的可寫 metadata 連線上跑 | 能的話走 agent 自己的唯讀連線；不能的 engine 說明為什麼仍安全 | D-08-11（五個 engine 都能；SQL Server 因 catalog 會改 session 設定，所以 catalog 連線一律與查詢 session 分開） |
| CR-08-10 | H7：固定 port 在 DbaGuy 沒開時可被別的程式聽 | 已知限制，寫明理由；保留固定 port | R-08-2、§9 |
| CR-08-11 | OP-3：MySQL 唯讀 tab 也有 READ WRITE 的洞 | 併入 ⑧，一個 MySQL 共用 helper，agent 與唯讀 tab 都用 | D-08-5、08a Task 6 |
| CR-08-12 | OP-1、OP-2、OP-4…OP-8 | 照原文接受 | 下表 |

### 研究後對原裁決的修正（OP，已接受）

| # | 原裁決 | 修正 |
|---|---|---|
| OP-1 | SQLite：`mode=ro`（與 `query_only`） | `mode=ro` 擋不住 ATTACH 的檔案（S4）與 `VACUUM INTO`（S3）；`query_only` 下 `VACUUM INTO` 仍留下空檔（S5）。加 authorizer（D-08-4） |
| OP-2 | 閘門＝一句＋READ＋唯讀政策 | 加拒絕清單（P6、P8、M5、C3；審核後擴充為 CR-08-1、CR-08-4、CR-08-6） |
| OP-4 | Host／Origin 驗證 | 有 Origin 就拒絕（R-08-4） |
| OP-5 | Couchbase 的 query context 工具 | 不另開工具（R-08-7） |
| OP-6 | `explain`（不 ANALYZE） | 只接受讀取（M8） |
| OP-7 | History 記每個 MCP 查詢 | 被拒的也記；schema 工具不記 |
| OP-8 | 官方 SDK | 照做，寫明成本；lazy import；工具回 `CallToolResult`（D-08-6） |

### Theirs

無。

### 之後

- Claude Code 的 `headersHelper` 讀 Keychain 的 token（`~/.claude.json` 不存明文）；需要讓 `security` 讀 sidecar 建的項目而不跳允許存取，`keyring` 做不到。
- DML 的 `explain`；Couchbase 的 `ADVISE`（`CouchbaseService.advise` 已有）。
- 每個 client 不同的 token 與權限；只對某些 schema 開放。
- 瀏覽器裡的 client：允許一個指定的 Origin。
- `outputSchema`／`structuredContent`。

## 13. 收尾時要更新的文件

CLAUDE.md：Repo 結構（`agent/`、`domain/agent.py`、`api/mcp.py`、`adapters/mysql/read_only.py`、前端各處）、技術選型（`mcp` SDK）、架構重點（MCP：port、token、Host／Origin、存取等級、登入權限、唯讀分層、框架與收尾、閘門的允許清單與拒絕清單、記憶體上界、History 的 source 與上限、MySQL 的共用唯讀 helper）、開發指令（dev 的 MCP port 17532、`claude --mcp-config` 一次性測試）、已知限制（SQL Server、token 明文、固定 port、讀取的鎖、巨大的值、同一登入的其他 session）、打包版量測、進度表 ⑧。

## 附錄 A：探測紀錄（2026-10-25，dev 資料庫）

腳本在 session 的 scratchpad（`probes/*.py`）；scratch 物件全部以完整名稱移除，結束時確認剩 0 個。

**G：DbaGuy 的分類器、`is_plain_read`、單句檢查、唯讀政策（純，`classify.py`）**
- G1 PG `SELECT 1; COMMIT; DROP TABLE zz_08_t`：3 段、DDL、唯讀政策 BLOCK。
- G2 PG `SELECT $$;$$; COMMIT`：2 段、TRANSACTION，唯讀政策是 RUN（交易語句沒有 WRITE 就允許）；agent 閘門以「2 段」拒絕，PG adapter 唯讀時也拒（G6）。
- G3 PG `SELECT pg_terminate_backend(1)`、`pg_advisory_lock(1)`、`lo_export(…)`、`pg_read_file(…)`、`dblink_exec(…)`：都是 READ、plain、RUN → 都在 D-08-2 的拒絕清單（〔CR-08-1〕修正：f0ecc82 的清單漏了 `pg_read_file`）。`SELECT set_config(…)` 已被 BLOCK（但 `U&` 寫法沒有，H1-d）。`SELECT … FOR UPDATE`、`WITH d AS (DELETE …) SELECT`、`SELECT … INTO`、`COPY` 都不是 READ。
- G4 T-SQL `SELECT 1 SELECT 2`：READ、plain、`tsql.statements` 是 2 → 閘門拒絕。
- G5 T-SQL `SELECT 1 COMMIT`：TRANSACTION、2 句 → 閘門拒絕。`SELECT 1; COMMIT`：段內有 `;`。
- G6 PG adapter（唯讀 profile）對 7 種堆疊變形全部 `read_only_single_statement`，`zz_08_victim` 都還在（`pg_adapter_probe.py`）。
- G7 T-SQL `SELECT 0x1DROP TABLE t`：READ、一句，server 是語法錯誤（03b 既有）。`NEXT VALUE FOR` 是 WRITE；`OPENQUERY`／`OPENROWSET`／`OPENDATASOURCE`、`EXEC`、`KILL` 是 UNKNOWN。
- G8 MySQL `INTO OUTFILE`／`DUMPFILE`：WRITE；`SELECT GET_LOCK(…)`、`LOAD_FILE(…)`、`SELECT 1 INTO @x`：READ；`/*!50000 DROP … */`：UNKNOWN。
- G9 SQLite `VACUUM INTO`：DDL；`ATTACH`、`PRAGMA query_only = 0`：SESSION；`SELECT load_extension('x')`、`PRAGMA table_info(t)`：READ。
- G10 SQL++ `UPDATE STATISTICS`、`ANALYZE KEYSPACE`、`NEXT VALUE FOR`、`UPSERT`、`DELETE FROM system:…`：WRITE；`EXECUTE FUNCTION`、`EXECUTE p`：UNKNOWN；`PREPARE`、`INFER`、`ADVISE`、`SELECT CURL(…)`：READ。

**P：PostgreSQL 17.11（`pg_probe.py`；dev 使用者 `dbaguy` 是 superuser，P0）**
- P1 `BEGIN READ ONLY` 之後 simple query 送 `SELECT 1; COMMIT; DROP TABLE zz_08_victim`：**DROP 成功**。
- P2 session 預設唯讀時同一段：25006；`SET default_transaction_read_only = off; DROP …` 堆在同一段也是 25006。
- P3 function 以 `set_config` 關掉預設：交易內 off；ROLLBACK 後 on；COMMIT 後 off。
- P4 寫入的 function、P5 `nextval`、P11 `SELECT … FOR UPDATE`：25006。
- P6 `pg_advisory_lock(808)` 在唯讀交易、ROLLBACK 之後：鎖仍在。
- P7 `lo_export(0, …)`：42704（物件不存在；唯讀沒擋）；`lo_import(…)`：25006。
- P8 唯讀交易裡 `pg_terminate_backend(<同一使用者的另一條連線>)`：那條連線死了。
- P9 `EXPLAIN (FORMAT JSON) INSERT …`：允許；`EXPLAIN ANALYZE INSERT`：25006。
- P10 `SET LOCAL statement_timeout = 500` 加 `pg_sleep(3)`：57014；ROLLBACK 後恢復 0。

**Q1-P：有權限的登入在唯讀交易裡（`h1_pg.py`；dev 的 `dbaguy` 是 superuser，因此也是四個 `pg_*` role 的成員）**
- `COPY (SELECT 1) TO '/dev/null'`：在 `BEGIN READ ONLY` 裡**成功**（server 寫檔，沒有留下東西）；`COPY (SELECT 1) TO PROGRAM 'true'`：**成功**（server 執行程式）；兩者的分類都不是 READ。
- `pg_read_file('/etc/hostname')`、`pg_ls_dir('.')`、`pg_stat_file('/etc/hostname')`：在 `BEGIN READ ONLY` 裡都成功，分類都是 READ、`is_plain_read` 是 True。

**H1：PG 的名稱比對與從文字執行 SQL（`h1_pg.py`；每一列都在 session 唯讀＋`BEGIN READ ONLY` 裡跑，之後 ROLLBACK）**
- H1-a `SELECT U&"pg\005fterminate\005fbackend"(<pid>)`：HEAD 的 `is_plain_read` True、唯讀政策 RUN；f0ecc82 的比對法（WORD／QUOTED 去引號）看不到它；**另一條 session 被殺**。解碼 `U&` 後的比對抓得到。
- H1-b 同上加 `UESCAPE '!'`：同樣被殺；解碼後抓得到。
- H1-c `query_to_xml('SELECT pg_terminate_backend(<pid>)', …)`、`ts_stat('SELECT to_tsvector(''simple'', pg_terminate_backend(<pid>)::text)')`、`ts_rewrite('a'::tsquery, 'SELECT … FROM (SELECT pg_terminate_backend(<pid>)) s')`：都 RUN、另一條 session 都被殺；`query_to_xmlschema(…)` 也 RUN，但只描述查詢、不執行（那條 session 還在）。f0ecc82 的清單沒有這些名稱。
- H1-d `SELECT U&"set\005fconfig"('default_transaction_read_only', 'off', false)`：HEAD 的 `set_config` 檢查沒擋（RUN）；交易內預設變 off，ROLLBACK 後回到 on。
- H1-e `query_to_xml('DELETE … RETURNING 1', …)`：0A000（寫入仍不行）；`query_to_xml('SELECT pg_advisory_lock(809)', …)` 拿的鎖在 ROLLBACK 後還在，`SELECT pg_advisory_unlock_all()` 之後沒了（CR-08-4 的收尾）。

**M：MySQL 8.4.11（`mysql_probe.py`、`mysql_m1b.py`；`dbaguy` 只有兩個 database 的 ALL，沒有 FILE）**
- M1 DbaGuy 的 adapter（唯讀與否）送 `SELECT 1; COMMIT; DROP TABLE zz_08_victim`：1064，表還在。driver 預設（`MULTI_STATEMENTS` 開）＋只有 `START TRANSACTION READ ONLY`：**DROP 成功**；再加 session READ ONLY：表還在。
- M2 唯讀 session 的 INSERT、M4 寫入的 function 在唯讀交易：1792。
- M3 function 內 `SET SESSION TRANSACTION READ WRITE`（或 `SET @@SESSION.transaction_read_only = 0`）：在 `START TRANSACTION READ ONLY` 裡呼叫、ROLLBACK 之後 session 的 `transaction_read_only` 是 0，**之後的 autocommit INSERT 成功**（M3d）；明確的 `START TRANSACTION READ ONLY` 裡仍是 1792（M3e）。
- M5 `GET_LOCK` 在唯讀交易、ROLLBACK 之後：鎖仍被持有。M6 `LOAD_FILE`：NULL（沒有 FILE）。M7 `max_execution_time = 500`：重的 SELECT 3024，`SLEEP(2)` 提早回 1。M8 `EXPLAIN INSERT` 在唯讀交易：1792。

**H2：MySQL 的 `sql_mode`（`h2_mysql.py`；沒有寫任何檔案）**
- `SELECT 'a\' INTO @zz_08 -- '`、`SELECT 'a\' INTO OUTFILE '/zz_08_nodir/x' -- '`、`SELECT 1 AS "a\" INTO @zz_08b -- "`：HEAD 的 `is_plain_read` 都是 True、唯讀政策 RUN（lexer 以為 `INTO` 在字串裡）。
- 預設的 `sql_mode`：三句都只是 SELECT，`@zz_08`、`@zz_08b` 沒被設（lexer 與 server 一致）。
- session 加 `NO_BACKSLASH_ESCAPES`：`@zz_08` 變成 `a\`（server 讀到 `INTO`）；OUTFILE 那句以 `dbaguy` 是 1227（需要 FILE），以 root 是 1290（路徑不在 `secure_file_priv`）：server 解析到 `INTO OUTFILE` 並做了權限檢查，有 FILE 又寫在 `secure_file_priv` 裡就會寫檔。
- session 加 `ANSI_QUOTES`：`@zz_08b` 變成 1。
- stored function 裡 `SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'`：呼叫前後 session 的 `sql_mode` 相同（MySQL 在 routine 返回時還原），所以 CR-08-5 的每次檢查是後盾。

**H3：分類器叫 READ 的非查詢（`h3.py`）**
- H3-a HEAD 的分類：T-SQL `RAISERROR(…) WITH LOG`、`DECLARE … CURSOR GLOBAL FOR SELECT 1`、`WAITFOR`、`PRINT`、`THROW`、`OPEN`、`FETCH NEXT`；SQLite `PRAGMA wal_checkpoint(TRUNCATE)`、`PRAGMA journal_mode`；SQL++ `SELECT ADVISOR({"action": "start" | "purge"})`：全部 READ、`is_plain_read` True、唯讀政策 RUN。資料修改的 CTE：PG `WITH d AS (DELETE|UPDATE … RETURNING *) SELECT …`、MySQL `WITH c AS (…) DELETE|UPDATE …`、SQLite `WITH c AS (…) DELETE|INSERT …`、T-SQL `WITH c AS (…) DELETE …`、`WITH … SELECT * INTO …`、SQL++ `WITH x AS (…) DELETE …`：全部是 WRITE 或 DDL，BLOCK。
- H3-b SQL Server：`RAISERROR … WITH LOG` 以 `dbaguy`（不是 sysadmin）是 2778（需要 sysadmin 或 ALTER TRACE；有的登入會寫進 error log）；`BEGIN TRANSACTION; DECLARE zz_08_c CURSOR GLOBAL …; ROLLBACK` 之後 `CURSOR_STATUS('global', 'zz_08_c')` 是 −1（**cursor 還在**），之後 DEALLOCATE（−3）。
- H3-c SQLite：`mode=ro` 開一個 WAL 的 scratch 檔，`PRAGMA wal_checkpoint(TRUNCATE)`：`disk I/O error`（唯讀連線做不到，但分類器放行）。
- H3-d Couchbase：`readonly=true` 下 `SELECT ADVISOR({"action": "start", "duration": "2s", "query_count": 1})` **成功**、建立一個 advisor session（`list` 看得到）；`readonly=true` 的 `purge` 回 success 但 session 仍在；以 `readonly=false` purge 之後才消失，`system:tasks_cache` 沒有它（已清除）。

**H6：DbaGuy 的 SQLite catalog 在 agent 式連線上（`h6_sqlite.py`：`mode=ro`＋`query_only`＋authorizer，允許唯讀 pragma）**
- `list_children`（根、Tables）、`object_detail`、`build_snapshot`（PK、FK、view）都成功；catalog 用到的 pragma 只有 `database_list`、`foreign_key_list`、`table_xinfo`；同一條連線上 ATTACH、`PRAGMA query_only = 0`、`VACUUM INTO` 仍被拒。

**S：SQLite 3.54.1（`sqlite_probe.py`、`sqlite_probe2.py`）**
- S1 `mode=ro` 的 INSERT：`attempt to write a readonly database`。S2 `SELECT 1; DELETE …`：`You can only execute one statement at a time`。
- S3 `mode=ro` 的 `VACUUM INTO`：**成功、寫出新檔**。S4 `mode=ro` 的 ATTACH 後 INSERT 進被 attach 的檔：**成功**。
- S5 加 `query_only = 1`：`VACUUM INTO` 報錯但**仍留下檔案**；ATTACH 可以、INSERT 被拒；`query_only` 可以再關掉（閘門擋）。
- S6 `load_extension`：`not authorized`。S7 `mode=ro` 的 BEGIN／SELECT／ROLLBACK 照常。S8 沒有 `query_only` 時 TEMP 表可以建。
- S9 authorizer（只允許 SELECT、READ、FUNCTION、RECURSIVE）：SELECT、遞迴 CTE、`EXPLAIN QUERY PLAN`、`sqlite_master` 照常；`VACUUM INTO`、ATTACH、`PRAGMA query_only = 0`、BEGIN、`PRAGMA table_info`、`pragma_table_info()` 都被拒（後兩者在加了唯讀 pragma 清單後允許，H6）。

**T：SQL Server 2022 16.0.4305.5（`mssql_probe.py`；`dbaguy` 是 `db_owner`、不是 sysadmin，T0）**
- T1 `BEGIN TRANSACTION` 之後一個批次 `SELECT 1; COMMIT; DROP TABLE dbo.zz_08_victim`，再 ROLLBACK：**表不見了**；沒有分號的寫法也一樣。
- T3 交易裡兩次 `NEXT VALUE FOR` 再 ROLLBACK：序列值前進了。T4 `IMPLICIT_TRANSACTIONS ON`：讀表的 SELECT 開了交易，`SELECT 1` 沒有。T5 會寫入的 UDF：建立時 443。
- T6 交易裡 `SELECT * INTO dbo.zz_08_new …` 再 ROLLBACK：新表不存在。T7 `SET SHOWPLAN_XML ON` 後送 INSERT：回計畫、沒有插入。T8 `SELECT 1` 0.45 ms；包在交易裡 1.30 ms。

**C：Couchbase EE 7.6.12（`cb_probe.py`，`readonly=true`）**
- C1 `UPSERT`：1000。C2 一個請求兩句：3000。C3 `PREPARE zz_08_p`：**成功**（之後刪除）。C4 `CURL()`：5010。C5 `INFER`：成功。C7 `EXPLAIN UPSERT`：成功。C8 請求 `timeout` 50 ms：1080。結束時 `app.orders` 500 份、沒有 `zz_08_` 文件、prepared 或 advisor session。

**K：MCP SDK 2.3.0（scratch 目錄的 wheel 與獨立 venv，已刪除）**
- K1 相依：`mcp` 369 KB、`mcp-types`、`cryptography` 50.0.2（`_rust.abi3.so` 10.4 MB）、`httpx2` 2.16.0、`jsonschema` 4.26.0、`rpds-py`、`pyjwt` 2.15.1、`sse-starlette` 3.5.1、`python-multipart` 0.0.32；與 fastapi 0.143.2、starlette 1.8.0、uvicorn 0.54.0 一起安裝沒有衝突，新增約 22 MB。
- K2 已 import fastapi 與 uvicorn 之後 import `MCPServer`：0.52 秒（第一次）、0.21 秒（之後）、RSS +23 MB；只 import lowlevel 的 `Server` 也 +21.9 MB（`mcp/__init__.py` 匯出 client）。
- K3 原始碼：`mcp.server.fastmcp` 是指向 `MCPServer` 的 `ModuleNotFoundError`；`streamable_http_app(…)` 的參數與預設；session manager 不能重用；stateless 舊路徑沒有 client 資訊；Host 不符 421、Origin 不符 403、沒有 Origin 放行；`ToolError` 的訊息在 INFO 記錄、回傳 `CallToolResult` 原樣轉送；同步工具在 `anyio.to_thread` 執行。

## 附錄 B：來源

- MCP 規格 2026-07-28，Streamable HTTP（Origin 驗證與 403、bind localhost、authentication；移除 session 與 GET stream；header 比對；`_meta` 的 clientInfo）：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- MCP 規格 2026-07-28，Transports 總覽：https://modelcontextprotocol.io/specification/latest/basic/transports
- MCP 規格，Security Best Practices（本機 HTTP server：「Require an authorization token」）：https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices
- MCP Python SDK（`mcp` 2.3.0）：https://pypi.org/project/mcp/ 、https://github.com/modelcontextprotocol/python-sdk 、https://py.sdk.modelcontextprotocol.io/
- Claude Code 的 MCP 文件（`claude mcp add --transport http … --header`、scope、`${VAR}`、10,000 token 警告與 25,000 token 上限、50,000 字元存檔、工具 timeout、`headersHelper`、tool search 與 instructions）：https://code.claude.com/docs/en/mcp
- Datadog Security Labs，Postgres MCP 的 SQL injection（2025-08-21）：https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server/
- DBHub：https://dbhub.ai/tools/search-objects 、https://dbhub.ai/config/toml
- stdio↔HTTP 的通用 bridge：https://npmjs.com/package/supergateway
- 前例（DBCode、DBeaver 26.x、TablePlus、已封存的 Postgres MCP）：[landscape](../../research/2026-10-15-sql-ide-landscape.md)。
