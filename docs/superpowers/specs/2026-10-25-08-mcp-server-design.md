# 子專案 ⑧：MCP server（唯讀）— 設計

> 日期：2026-10-25 ｜ 狀態：草稿，待 coordinator 審核（branch `feat/08-mcp-server`，自 `main` 2990fc0）
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §2（Q1–Q6）、§3 S9（「MCP server（唯讀，沿用 IDE 已設好的連線）」）與 M2、M11、M12、M13、§5 AI 策略的原則 1–6（尤其原則 5：「給 agent 用的唯讀**必須在 DB 層級強制**……不能只靠 MCP 設定或 prompt」）、§8（「AI：**只做 MCP server**」）。前例：[landscape](../../research/2026-10-15-sql-ide-landscape.md) 的 DBHub（分層的 `search_objects`、row limit、timeout）、DBCode（連線的唯讀設定對 agent 同樣生效）、DBeaver（MCP allowlist、sample rows 預設關閉）、已封存的官方 Postgres MCP（Datadog 以堆疊語句加 `COMMIT` 繞過它的唯讀交易）。
> 技術依據：dev 資料庫上的探測（2026-10-25；PostgreSQL 17.11／psycopg 3.3.6、MySQL 8.4.11／mysql-connector-python 26.7.0 pure、SQL Server 2022 16.0.4305.5／pymssql 2.4.4、SQLite 3.54.1（venv 的 Python 3.13.5）、Couchbase EE 7.6.12），標 **〔實測〕**，紀錄在附錄 A（本文的探測編號自成一組：G 是 DbaGuy 的分類器、P／M／S／T／C 是各 engine、K 是 SDK）；MCP Python SDK `mcp` 2.3.0 與它的相依以 `pip download` 與 scratch 目錄裡的獨立 venv 檢查（沒有裝進專案的 venv），標 **〔SDK 原始碼〕**；標 **〔未實測〕** 的是假設，§10 彙整並指定由哪個 plan task 先驗證。探測只建 `zz_08_*` 物件，以完整名稱 DROP；Couchbase 只寫了 `zz_08_` 名稱的東西並已移除（`app.orders` 仍是 500 份）。來源在附錄 B。

## 1. 目標

讓 Claude Code（與其他 MCP client）透過使用者在 DbaGuy 已經設好的連線，**唯讀地**看 schema、跑查詢。五個 engine 都支援。

| 編號 | 目標 | 對應 |
|---|---|---|
| G1 | sidecar 內的 MCP endpoint（Streamable HTTP，只綁 `127.0.0.1`，固定 port），DbaGuy 開著時才有 | S9、§8 |
| G2 | **預設關閉**：全域開關，加上每條連線自己的存取等級（Off／Schema only／Read-only queries，預設 Off） | 原則 1、3 |
| G3 | 唯讀由**資料庫**強制（PG、MySQL、SQLite、Couchbase），加上 DbaGuy 自己的單句讀取閘門；SQL Server 只做得到 app 層加交易包住，誠實揭露 | 原則 5、M11、Q3 |
| G4 | 分層探索 schema（列出、搜尋、describe），不一次 dump；查詢與 EXPLAIN（不 ANALYZE）只在 Queries 等級 | 原則 3、M2、DBHub |
| G5 | 每次查詢都有上限：列數、cell 長度、回應大小、timeout；可取消 | M12、Q1 |
| G6 | 使用者看得到 agent 做了什麼：每個查詢（含被拒絕的）進 History，標 MCP 與 client 名稱 | M13、Q3 |
| G7 | 不問密碼：沒有可用密碼的連線回「先在 DbaGuy 解鎖」 | Q3 |
| G8 | Settings 有開關、port、token（複製、重新產生）、可直接複製的 Claude Code 註冊指令、狀態；連線對話框有存取等級；History 有 MCP 標記與篩選 | Q6 |

### 成功標準

- 開關關著：sidecar 不 import SDK、不開 port，啟動時間與記憶體和 ⑥ 相同（§7）。
- 開了之後，Settings 的指令貼進終端機，Claude Code 的 `/mcp` 看到 `dbaguy` 連上；`list_connections` 只列出存取等級不是 Off 的連線。
- 五個 engine：`run_query` 跑一句 SELECT 回最多 `max_rows` 列；Datadog 的寫法（`SELECT 1; COMMIT; DROP TABLE …`）與附錄 A 的每個變形都在送出前被拒，而且**閘門被拿掉時**（測試直接對 agent session 送寫入）PG、MySQL、SQLite、Couchbase 的 server 也拒絕；SQL Server 的寫入被 rollback（序列等非交易的副作用除外，誠實列出）。
- 被拒、出錯、逾時的查詢都進 History（`source=mcp`、client 名稱、狀態），server 的錯誤原文只出現在工具結果與 DEBUG log。
- 沒存密碼也沒解鎖的連線：工具結果是「先在 DbaGuy 連一次或解鎖」，DbaGuy 不跳任何對話框。
- token 錯、Host 不對、帶 Origin 的請求分別是 401、421、403，都不進 MCP 的處理。

### 不在範圍內

任何寫入（含 DML 的 EXPLAIN，R-08-7）；透過 MCP 的 Copy、dump、Run SQL file；AI chat、explain／fix 按鈕（§8）；MCP resources 與 prompts（R-08-11）；OAuth（本機單一使用者，bearer token 就夠，R-08-3）；stdio bridge（R-08-10）；Couchbase 的文件 KV 讀取（`run_query` 的 `USE KEYS` 就夠）；遠端（非 localhost）存取；多使用者或每個 client 不同的權限；row-level 遮罩。

## 2. 關鍵決策

R-08-x 回答 coordinator 列出的設計問題，D-08-x 是其下的設計決定。「§x」指本文，「FP §x」指 feature-priorities，「附錄 A」是探測紀錄。與 coordinator 的裁決不同之處在 §12 的 OP-1…OP-8 列出理由。

| 決策 | 選項 | 結論與理由（含證據） |
|---|---|---|
| **R-08-1** 在哪裡跑、用什麼 | (a) sidecar 內，官方 SDK；(b) 自己實作 JSON-RPC；(c) 另一個 process | **(a)**，`mcp` 2.3（`mcp>=2.3,<3`）的 `MCPServer`（v2 把 FastMCP 改名成 `mcp.server.mcpserver.MCPServer`，`mcp.server.fastmcp` 只剩一個指向遷移指南的 `ModuleNotFoundError`〔SDK 原始碼〕），以 `streamable_http_app()` 取得 Starlette app，跑在**第二個 uvicorn server**（自己的 thread 與 event loop、自己的 socket），與 API 的 server 分開：API 的 port 每次啟動隨機，不能註冊進 Claude Code；MCP 的 port 要固定（R-08-2）。不自己實作：SDK 同時處理兩個協定時代（2025-11-25 的 `initialize`＋session 與 2026-07-28 的每個請求自帶 `_meta`、`MCP-Protocol-Version`／`Mcp-Method`／`Mcp-Name` header 的比對〔SDK 原始碼、附錄 B 規格〕），自己做要追著規格改。工具的服務（metadata、schema cache、session、History）是 container 裡同一份，和 API 共用。代價（K1、K2）：SDK 帶進 `cryptography`（`_rust.abi3.so` 10.4 MB）、`httpx2`、`jsonschema`、`rpds-py`、`pyjwt`、`sse-starlette`、`python-multipart` 等，安裝約 22 MB；`import mcp` 一定會載入 client 與 `cryptography`（`mcp/__init__.py` 匯出 client；`MCPServer` 用 `cryptography` 封 request state），import 0.21 秒、RSS +23 MB〔實測，scratch venv〕。所以 **lazy import**：開關打開時才 import（D-08-8），關著的使用者完全不付這個成本（Q1）。 |
| **R-08-2** port | 固定 port、可改 | **17531**（設定範圍 1024–49151）。理由：低於 macOS 的 ephemeral 範圍 49152–65535（`net.inet.ip.portrange.first`〔實測〕），所以 OS 不會把它配給別的程式的隨機 port，包括 DbaGuy 自己 API 的隨機 port；`/etc/services` 沒有登記；不是常見開發工具的預設（3000、5173、8000、8080、MCP Inspector 的 6274／6277），本機 `lsof` 也沒有人在用〔實測〕。正式 data dir 之外的 data dir（`scripts/dev.sh` 的 `dev/data`、測試、臨時 sidecar）預設 **17532**，比照 `keychain_service`：dev 與正式 app 同時開著 MCP 也不會互搶。被占用時狀態是 `port_in_use`，Settings 說明並讓使用者改。 |
| **R-08-3** 認證 | (a) 長期的 bearer token；(b) SDK 的 OAuth（`token_verifier`＋`AuthSettings`） | **(a)**：`secrets.token_urlsafe(32)`（43 字元），存 Keychain（service 依 data dir：`app.dbaguy`／`app.dbaguy.dev`，account `mcp:token`），sidecar 啟動 MCP 時讀一次、留在記憶體；比較用 `api/deps.py` 既有的 `_same_token`（`compare_digest`）。Settings 可複製、可重新產生（新 token 立刻生效，舊 token 的下一個請求就是 401）。(b) 要 issuer、授權伺服器 metadata，本機單一使用者不需要；規格對本機 HTTP server 的建議就是「Require an authorization token」（附錄 B）。代價：Claude Code 把 header 存在 `~/.claude.json`（明文、mode 600），任何以使用者身分執行的程式讀得到它，這比 Keychain 裡的 DB 密碼弱（§9）；靠預設關閉、逐連線開啟、唯讀、只綁 localhost 與 Regenerate 限縮。 |
| **R-08-4** Host／Origin（DNS rebinding） | SDK 的預設、或更嚴 | Host 只收 `127.0.0.1:<port>` 與 `localhost:<port>`（421）；**帶 `Origin` 的請求一律 403**：CLI client 不送 Origin，送 Origin 的只有瀏覽器。SDK 對 localhost 的自動設定允許 `http://localhost:*` 與 `http://127.0.0.1:*` 的 Origin〔SDK 原始碼 `sse_app`〕，任何本機網頁都符合，所以不用它的預設，改傳 `TransportSecuritySettings(allowed_hosts=[…兩個…], allowed_origins=[])`（Origin 不在清單 → 403，規格要求的狀態碼）。代價：瀏覽器裡的 client（MCP Inspector）連不上（§12）。認證的 middleware 在最外層：沒有 token 的請求不進 SDK。請求 body 上限 1 MiB（SDK 預設 4 MiB）。 |
| **R-08-5** 誰看得到哪條連線 | 全部、或逐條開啟 | 全域開關（預設關，FP 原則 1）＋每個 profile 的 `mcp_access`：`off`（預設，所有既有與新的連線）／`schema`／`query`。`schema`：名稱、型別、key、index、DDL，**不讀列**；`query`：再加 `run_query`、`explain`、Couchbase 的 `sample_fields`（取樣讀文件，所以算資料）。FP 原則 3「預設只送 metadata，sample rows 預設關閉」與 DBeaver 的 allowlist。每次呼叫都重讀 profile：改成 Off 下一次呼叫就拒絕，profile 一存檔就關掉它的 agent session（D-08-1）。連線本身是唯讀與否不影響：MCP 一律唯讀。 |
| **R-08-6** 唯讀怎麼強制 | (a) 只靠分類器；(b) 分類器＋server 的唯讀 | **(b)，分層**（§3.4 的表）：① DbaGuy 的閘門（D-08-2）：剛好一句、分類器說 READ、唯讀允許清單、有副作用的 function 清單；② agent session 一律以**唯讀的 profile 副本**開（adapter 既有的唯讀：PG `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`＋`conn.read_only`、MySQL `SET SESSION TRANSACTION READ ONLY`、SQLite `mode=ro`、Couchbase 每個請求 `readonly=true`），不管 profile 自己的唯讀旗標；③ 每次呼叫包在 server 強制的唯讀交易裡、**一律 ROLLBACK**（D-08-3）；④ 時間上限在 server 也有一份（PG `SET LOCAL statement_timeout`、MySQL `max_execution_time`）。為什麼要 ②＋③ 都有〔實測〕：PG 只有 `BEGIN READ ONLY` 時堆疊的 `COMMIT; DROP` 照樣成功（P1），session 預設唯讀才擋住 COMMIT 之後的語句（P2）；但 function 能在交易裡把 session 預設關掉，只有 ROLLBACK 能還原它（P3）。MySQL 相反：function 能把 session 的存取模式改成 READ WRITE、ROLLBACK 不還原（M3），只有每次明確的 `START TRANSACTION READ ONLY` 擋得住（M3e），而堆疊在 DbaGuy 的連線上本來就送不出去（`MULTI_STATEMENTS` 關閉，1064，M1）。SQLite 的 `mode=ro` 擋不住 ATTACH 進來的檔案與 `VACUUM INTO`（S3、S4），所以 agent 連線另加 `query_only` 與 authorizer（D-08-4）。 |
| **R-08-7** 工具與上限 | — | 7 個工具，全部 `readOnlyHint`（§3.5）：`list_connections`、`list_objects`、`search_objects`、`describe`（Schema）、`run_query`、`explain`、`sample_fields`（Queries；最後一個只有 Couchbase）。Couchbase 的 query context 不另開工具：`list_connections` 給連線的預設，`list_objects` 列出 bucket › scope，`run_query`／`explain` 收 `query_context`（OP-5）。上限（§3.6）：列數預設 100、最多 1,000；cell 1,000 字元、最多 20,000；整個回應最多 40,000 字元；timeout 預設 30 秒、最多 300 秒。`explain` 只接受讀取的語句：MySQL 在唯讀交易裡連 `EXPLAIN INSERT` 都拒絕（M8），PG 雖然允許（P9）但沒有理由讓兩邊不同（OP-6）。 |
| **R-08-8** 使用者看得到什麼 | 只有 log、或 History | **History**：`run_query`、`explain`、`sample_fields` 每次一筆，`source = "mcp"`、`client` = client 自報的名稱；被閘門拒絕的也記（新狀態 `refused`，OP-7）：agent 試圖寫入正是使用者最該看到的。schema 工具是 DbaGuy 自己的 catalog 讀取（和物件樹一樣），不進 History，INFO log 記工具、連線、物件名稱與筆數。log 規則照舊：SQL 文字與 server 的錯誤原文只在 DEBUG（D-08-14）。 |
| **R-08-9** 密碼 | 問、或不問 | **不問**：開 agent session 走 `ProfileService.resolve_secrets`，`PasswordRequired` 變成工具錯誤 `password_required`：「'<name>' needs its password: connect to it in DbaGuy first (or unlock it). DbaGuy never asks for a password over MCP」。`list_connections` 以 `known_secrets`（不問）算出 `needs_unlock`。DbaGuy 不跳對話框（Keychain 自己的「允許存取」除外，見 §9）。 |
| **R-08-10** stdio bridge（例如 sidecar 的 `--mcp-stdio` 模式轉送到執行中的 app） | 做、或不做 | **不做**。Claude Code（§8 的使用者唯一提到的 client）原生支援 HTTP 加 header（`claude mcp add --transport http … --header …`，附錄 B）；bridge 要讓每個 client session 多啟動一個凍結的 sidecar（啟動 0.5 秒、RSS 約 70–80 MB，⑤⑥ 量過的單獨 sidecar），只為了轉送，違反 Q1；token 還得經環境變數或另一條讀 Keychain 的路（重新簽章後會跳允許存取）。只支援 stdio 的 client 可以自己用通用的 stdio↔HTTP bridge（例如 supergateway、mcp-remote）接同一個 URL 與 header，不隨 DbaGuy 附帶、不在測試範圍。 |
| **R-08-11** resources／prompts | 做、或不做 | **不做**。schema 已經是工具（分層、帶上限），再做成 resource 是同一份資料的第二個入口；prompts 是 chat 的東西（§8 不做）。只給 server `instructions` 一段（約 600 字元）：先 `list_connections`、路徑怎麼用、上限、唯讀、SQL Server 的差異。Claude Code 的 tool search 會用它判斷何時載入工具（附錄 B）。 |
| **R-08-12** SQL Server | 允許 Queries、或禁止 | **允許，誠實揭露**：SQL Server 沒有 session 唯讀（03b），寫入只由 ① 閘門擋；③ 的 `BEGIN TRANSACTION … ROLLBACK` 讓漏網的 DML／DDL 回復（T6：交易裡的 `SELECT … INTO` 在 ROLLBACK 後不存在），但非交易的副作用回復不了（T3：`NEXT VALUE FOR` 的序列值 ROLLBACK 後照樣前進；linked server、extended procedure）。對話框選 Queries 時、`list_connections` 的 `read_only_by: "dbaguy"`、Settings 都說明，並建議用只有 `db_datareader` 的 login；agent session 開啟時讀一次 login 能不能寫（`IS_MEMBER('db_owner')`、`HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'/'UPDATE'/'DELETE'/'ALTER'/'EXECUTE')`），`list_connections` 回 `login_can_write`，讓 agent 與使用者都看得到（dev 的 `dbaguy` 是 `db_owner`，T0）。 |

| 決策 | 內容 |
|---|---|
| **D-08-1** agent session | `SessionManager.create_agent(profile_id, database)`：與 tab 的 session 同一個類別與 reaper，但 ① opener 用 `profile.model_copy(update={"read_only": True})` 並帶 `agent=True`（D-08-4）；② hooks 用 agent 版（`check` = D-08-2 的閘門，`on_record` 寫 `source="mcp"`）；③ 沒有 WebSocket：MCP 層自己裝 emitter，依 `request_id` 把事件交給等待中的呼叫。key 是 (profile, database)（PG／SQL Server 一個 database 一條連線；MySQL 的 database 就是 schema；SQLite 忽略；Couchbase 改用 `query_context`）。同一個 session 一次一個呼叫（MCP 層的 lock，框架的 begin、語句、discard、rollback 之間不能插進別的呼叫）；等 lock 的時間算進該呼叫的 timeout。最多 8 個 agent session，滿了先關最久沒用的閒置那個；閒置的照既有 reaper（600 秒）關。不碰使用者的 tab、workspace、暫存。profile 存檔或刪除時關掉它的 agent session（`api/profiles.py` 的 `_forget_profile_state` 加一行）。 |
| **D-08-2** 閘門（`SafetyPolicy.agent_read(sql, dialect)`） | 純函式，MCP 層在送出前呼叫（拒絕時寫 History `refused`、回工具錯誤），agent session 的 worker hooks 也用同一個（後盾，同一個答案）。規則：① U+0000 拒絕（worker 既有的 `_check_text`）；② **剛好一句**：`split()` 一段、段內沒有 `;`（`_single_statement_tokens` 的規則），T-SQL 另外要 `tsql.statements()` 剛好一句（`SELECT 1 SELECT 2`、`SELECT 1 COMMIT` 沒有分號也是兩句，G4、G5）；③ 分類器說 `READ`，並通過 `_allowed_when_read_only`（`set_config` 等）——也就是既有的 `is_plain_read`；TRANSACTION、SESSION（含「無害」的 SET／USE）一律拒絕：agent 不能改 session；④ 有副作用的 function 與字（表在 `services/safety.py`，和既有的 `_PG_HARMLESS_SETTINGS` 放在一起；以 function 呼叫的 token 比對，大小寫與引號照 dialect）：PG `pg_terminate_backend`、`pg_cancel_backend`、`pg_advisory_*`／`pg_try_advisory_*`（session 鎖 ROLLBACK 後還在，P6）、`pg_reload_conf`、`pg_rotate_logfile`、`pg_switch_wal`、`pg_promote`、`pg_create_*`、`pg_drop_*`、`pg_replication_*`、`pg_log_*`、`lo_*`、`dblink*`、`pg_file_*`；MySQL `GET_LOCK`、`RELEASE_LOCK`、`RELEASE_ALL_LOCKS`（ROLLBACK 後鎖還在，M5）、`LOAD_FILE`；SQLite `load_extension`；SQL++ 開頭的 `PREPARE`（`readonly=true` 照樣在 server 建 prepared statement，C3）與 `CURL`。理由：PG 的唯讀交易擋不住 `pg_terminate_backend`（P8：從唯讀交易殺掉同一個使用者的另一條連線，可能就是使用者的 tab）。這張表擋不住藏在使用者自訂 function 裡的同類呼叫（§9），所以建議唯讀 login。 |
| **D-08-3** 唯讀框架（`sql/dialects.py` 的 `ReadFrame` 表，與 ⑤ 的 `TransactionFrame` 並列） | 每次 `run_query`／`explain`：在 worker 上以 `worker.call()` 送 DbaGuy 自己的開頭（不經 agent 閘門，和 metadata 的 catalog 查詢同一條路），語句以 `ExecuteCommand` 經閘門送出，讀到上限後 discard，最後以 worker 的 `RollbackCommand`（adapter 的 `rollback()`；SQL Server 是既有的 `IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION`）。開頭：PG `BEGIN READ ONLY` 加 `SET LOCAL statement_timeout = <timeout + 5 秒>`（P10：生效、ROLLBACK 後恢復）；MySQL `SET SESSION max_execution_time = <ms>` 加 `START TRANSACTION READ ONLY`（M7：SELECT 逾時是 3024；`SLEEP()` 只是提早回 1）；SQL Server `BEGIN TRANSACTION`；SQLite 沒有（`mode=ro`＋`query_only`＋authorizer 已是引擎層，authorizer 也拒絕 BEGIN，S9）；Couchbase 沒有（沒有交易，每個請求 `readonly=true`）。ROLLBACK 失敗（連線斷了、`transaction_failed` 之外的錯誤）就關掉這個 agent session，下一次重開：絕不帶著可能未結束的交易接下一個呼叫。每次多 2–3 個往返，本機 SQL Server 0.45 → 1.30 ms（T8）。 |
| **D-08-4** agent 連線（`DatabaseAdapter.open(…, agent=True)`） | `ConnectionService.open` 多一個 `agent` 參數，傳給 adapter。PG、MySQL、SQL Server、Couchbase 不需要額外動作（唯讀副本已經夠，框架在 D-08-3）；**SQLite**：`mode=ro` 之外再 `PRAGMA query_only = ON`，並裝 `set_authorizer`：只允許 `SQLITE_SELECT`、`SQLITE_READ`、`SQLITE_FUNCTION`（`load_extension` 除外）、`SQLITE_RECURSIVE`，其餘（ATTACH、DETACH、PRAGMA、寫入、DDL、`VACUUM`、BEGIN）都是 `SQLITE_DENY`（S9：ATTACH、`VACUUM INTO`、`PRAGMA query_only = 0` 都被拒，`EXPLAIN QUERY PLAN` 與 `sqlite_master` 照常）。理由：`mode=ro` 下 ATTACH 進來的檔案可寫（S4），`VACUUM INTO` 會寫出一個新檔（S3）；`query_only` 擋住前者，但 `VACUUM INTO` 仍留下一個空檔（S5）。authorizer 是 SQLite 引擎在 prepare 時的檢查，屬於「DB 層級」。`pragma_table_info()` 等 table-valued pragma 也被拒（S9），describe 走 metadata 連線，不受影響。 |
| **D-08-5** MySQL 的唯讀（附帶的發現） | M3 也影響**使用者自己的唯讀 tab**：唯讀 profile 只靠 `SET SESSION TRANSACTION READ ONLY`，一個（已存在或使用者有權建立的）stored function 能在 SELECT 裡把它改成 READ WRITE，之後的 autocommit 語句就能寫（M3d）。⑧ 的 agent session 以 D-08-3 的每次明確交易擋住；使用者的唯讀 tab 要不要跟著加固（例如每句之前讀 `@@SESSION.transaction_read_only`，像 PG 的 `_restore_read_only`）是 ⑧ 範圍外的修正，列 OP-3。 |
| **D-08-6** 工具結果的形狀 | 每個工具回 `CallToolResult`，內容是**一段精簡的 JSON 文字**（`TextContent`），失敗時 `is_error=True`、JSON 是 `{"error": {"code", "message"}}`。不宣告 `outputSchema`、不回 `structuredContent`：同一份資料會在回應裡出現兩次（規格要求 structured 時也附文字），吃掉一半的大小上限（§3.6）。**錯誤一律以結果回傳、不 raise `ToolError`**：SDK 對 `ToolError` 會 `logger.info("Tool %r failed: %r", …, str(exc))`〔SDK 原始碼 `_handle_call_tool`〕，server 的錯誤原文就進了 INFO log（違反 log 規則）；工具回傳 `CallToolResult` 時 SDK 原樣轉送、不記錄。我們自己的程式裡沒預料的例外：INFO 以上只記例外型別。 |
| **D-08-7** client 是誰 | 2025-11-25 的 client 在 `initialize` 自報 `clientInfo`，只存在 stateful session 裡（SDK 的 stateless 舊路徑給 tool 的 `client_params` 是 None〔SDK 原始碼〕），所以舊路徑用 **stateful**（SDK 預設）；2026-07-28 每個請求的 `_meta` 帶 `io.modelcontextprotocol/clientInfo`（可省略）。工具從 `ctx.request_context.connection.client_params.client_info` 取 `name`（最多 80 字元，去掉控制字元），沒有就是 null。這是 client 自報的、不是認證：知道 token 的人可以自稱任何名字（§9）。 |
| **D-08-8** 生命週期（`agent/host.py` 的 `AgentHost`） | settings 的 `mcp.enabled` 變成 true（或 sidecar 啟動時已是 true）：在背景 thread 裡 lazy import SDK、從 Keychain 讀 token（沒有就產生並寫入）、bind `127.0.0.1:<port>`、建一個**新的** `MCPServer` 與 app（SDK 的 session manager 「cannot be reused after its run() context has completed」〔SDK 原始碼〕，所以每次啟動都新建）、跑第二個 `uvicorn.Server`。關閉：`should_exit`、join（最多 5 秒）、關掉所有 agent session。port 改了＝關再開。狀態 `off`／`starting`／`listening`／`port_in_use`／`error`（訊息）。sidecar 啟動時若已開啟，在 API 就緒之後才在背景啟動，不在第一個 API 請求的路徑上（Q1）。sidecar 結束時 thread 是 daemon，與 API 一起停；後端自動重啟後照 settings 再開。 |
| **D-08-9** SDK 的 transport 設定 | `streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=False, max_sessions=32, session_idle_timeout=1800, max_request_body_size=1 MiB, transport_security=R-08-4)`。`json_response=True`：工具不串流進度，一個 JSON 物件最簡單。`max_sessions` 32：本機只有幾個 client，SDK 預設 10,000 沒必要。stdlib 的 `mcp.*` logger 設成 WARNING（它們的 INFO 是每個請求一行的雜訊），經既有的 `InterceptHandler` 進 loguru；`MCPServer` 建構時呼叫的 `logging.basicConfig` 在 root 已有 handler 時是 no-op〔SDK 原始碼〕。 |
| **D-08-10** explain 怎麼組 | agent 給一句 READ（不能自己帶 EXPLAIN／ANALYZE 開頭），DbaGuy 依 dialect 表（`sql/dialects.py`）組：PG `EXPLAIN <stmt>`（文字格式，比 JSON 省 token）；MySQL 8.0.16 以上 `EXPLAIN FORMAT=TREE <stmt>`，更舊的 `EXPLAIN <stmt>`（依 `ServerInfo.version`）；SQLite `EXPLAIN QUERY PLAN <stmt>`；SQL Server 以 `call()` 在語句前後送 `SET SHOWPLAN_TEXT ON`／`OFF`（各自一個批次；SHOWPLAN 下語句不執行，T7；`OFF` 失敗就關掉 session，免得下一個呼叫拿到計畫而不是結果）；Couchbase `EXPLAIN <stmt>`，再加 ④ 既有 `explain.py` 的 findings（PrimaryScan、`#sequentialscan` 等）。組出來的文字照樣經閘門與框架。 |
| **D-08-11** describe 怎麼組 | 都是既有的服務、metadata 連線：`MetadataService.detail(path)`（欄位、型別、NULL、預設值、註解、DDL）＋schema snapshot 的那個 relation（PK、FK，`SchemaCache`）＋`list_children` 的 indexes 資料夾（名稱與 detail）。DDL 預設不給（`include_ddl=false`），省 token。物件可以用 `list_objects`／`search_objects` 回的 `path`，或 `{database?, schema, name}`：先在 snapshot 精確比對、再不分大小寫，找到就用 adapter 的 `relation_path` 組路徑；同名多個時回 `ambiguous` 與候選。agent 給的名稱只進 adapter 既有的 catalog 查詢（名稱一律 bind 或經 `sql/dialects.py` 的 quote，物件樹就是這樣用的）；08a 以惡意名稱（`x'; DROP …`、`]` 與 `"` 的跳脫）測一次。 |
| **D-08-12** 設定與 API | `AppSettings.mcp = McpSettings(enabled=False, port=17531 或 17532)`（`settings.json`，token 不在裡面）；profile 的 `mcp_access: Literal["off", "schema", "query"] = "off"`（`profiles.json`；舊檔讀起來就是 off）。API（API token 保護，只給 app 的前端）：`GET /mcp/status`（enabled、state、port、url、message、可見的連線數依等級）、`GET /mcp/token`、`POST /mcp/token/regenerate`；`PUT /settings` 改了 `mcp` 就通知 `AgentHost`。 |
| **D-08-13** History | 表加兩欄 `source TEXT NOT NULL DEFAULT 'app'`、`client TEXT`（開檔時以 `PRAGMA table_info` 檢查、`ALTER TABLE … ADD COLUMN`，舊檔原地升級；舊版 DbaGuy 讀新檔照常，它的查詢列出欄名）；`HistoryStatus` 加 `refused`；`GET /history` 加 `source` 篩選（`app`／`mcp`）。agent session 的 `on_record` 寫 `source="mcp"` 與該次呼叫的 client（client 隨 MCP 內部的執行指令帶進 worker：`ExecuteCommand` 的內部子類，不在 WebSocket 的 `ClientCommand` union 裡，前端送不出它）。`explain` 記 `EXPLAIN` 加語句（SQL Server 註明是 SHOWPLAN）；`sample_fields` 記一行摘要（`-- MCP sample_fields <bucket.scope.collection> (<n> documents)`，同 Run SQL file 的寫法）。 |
| **D-08-14** Log | INFO：`MCP <tool> on '<profile>' by <client>: <outcome>, <rows> rows, <ms> ms`；拒絕：`MCP refused <tool> on '<profile>': <reason code>`（不帶 SQL）；錯誤只記 code；AgentHost 的啟停、port、狀態。DEBUG：SQL 文字（worker 既有的 `_log_statement`，前 2,000 字元）與 server 的錯誤原文。token 永遠不進 log（含 DEBUG）。schema 工具的物件名稱在 INFO（同 ⑤ 記 `schema.name`）。 |

## 3. 架構

### 3.1 模組總覽

```
backend/dbaguy_backend/
  agent/                      # ⑧：MCP endpoint（不叫 mcp/，免得和 SDK 的頂層套件 `mcp` 混淆）
    host.py                   # AgentHost：lazy import、第二個 uvicorn、啟停、狀態（D-08-8）
    server.py                 # 建 MCPServer、註冊工具、instructions；ASGI：bearer 認證 middleware（最外層）
    tools.py                  # 7 個工具：參數模型、等級檢查、呼叫 services、CallToolResult（D-08-6）
    reader.py                 # AgentReader：agent session 的取得與 lock、框架、送出、收事件、discard、rollback
    shape.py                  # 純函式：列、cell、回應大小的預算與截斷（§3.6）
    token_store.py            # Keychain 的 MCP token（service 依 data dir，account mcp:token）
  domain/agent.py             # McpAccess、McpSettings、McpStatus、工具的結果模型、錯誤碼
  services/safety.py          # + agent_read() 與副作用 function 表（D-08-2）
  sql/dialects.py             # + ReadFrame 表、explain 的組法（D-08-3、D-08-10）
  adapters/base.py            # open(…, agent=False)
  adapters/sqlite/adapter.py  # agent=True：query_only＋authorizer（D-08-4）
  sessions/manager.py         # + create_agent()；sessions/hooks.py：agent 版 hooks（閘門、source=mcp）
  services/history_service.py # + source、client 欄、refused、篩選（D-08-13）
  api/mcp.py                  # /mcp/status、/mcp/token、/mcp/token/regenerate
src/
  features/settings/          # McpSection.tsx、mcpCommand.ts（純函式：指令字串）
  features/connections/       # ConnectionDialog：AI access（MCP）
  features/history/           # MCP 標記、Source 篩選、refused 狀態
  features/explorer/          # 連線列的 AI 標記（存取不是 Off 時）
```

依賴方向：`agent/` → services、sessions、domain；services 不知道 `agent/`。engine 的知識只在 adapter 與 `sql/` 的表（ReadFrame、explain、副作用 function），`agent/` 沒有任何 `if engine == …` 之外的分派（分派也只到「這個 engine 有沒有 query context／sample_fields」，讀能力欄位）。

### 3.2 HTTP endpoint

- URL：`http://127.0.0.1:<port>/mcp`，只 bind `127.0.0.1`（規格：「SHOULD bind only to localhost」）。
- 每個請求依序：bearer 認證（我們的 middleware；401，`WWW-Authenticate: Bearer`）→ SDK 的 Host 檢查（421）→ Origin（有就 403）→ Content-Type → body 上限 → 協定版本與 header 比對（SDK）。
- 兩個協定時代都接（SDK 依 `MCP-Protocol-Version` 分流）：2025-11-25 走 stateful session（`Mcp-Session-Id`，閒置 30 分鐘、最多 32 個）；2026-07-28 沒有 session。
- 工具是同步函式：SDK 以 `anyio.to_thread.run_sync` 執行〔SDK 原始碼〕。`run_query`／`explain` 例外：寫成 async，等待 `to_thread`（`abandon_on_cancel=True`），被 client 取消時（2025：`notifications/cancelled`；2026：關閉回應）呼叫 `worker.cancel(request_id)`，DB 的語句跟著停。〔未實測：A4〕

### 3.3 存取等級與連線

| 工具 | `schema` | `query` | 備註 |
|---|---|---|---|
| `list_connections` | 列出 | 列出 | `off` 的不列、也不存在（其他工具以 `unknown_connection` 拒絕，不洩漏它存在） |
| `list_objects`、`search_objects`、`describe` | ✓ | ✓ | metadata 連線（使用者的 tree 也用它），DbaGuy 固定的 catalog 查詢 |
| `run_query`、`explain` | `not_allowed` | ✓ | agent session（D-08-1） |
| `sample_fields` | `not_allowed` | ✓ | 只有 Couchbase（`FieldSampleService`，metadata 連線、`readonly=true`） |

`connection` 參數收 profile 的 id 或名稱（名稱在可見的連線裡重複時回 `ambiguous`）。`list_connections` 回：`id`、`name`、`engine`、`access`、`folder`、`color`（使用者的標記；描述裡說明 red 通常代表 production）、`database`（預設）、`default_query_context`（Couchbase）、`read_only_by`（`server`／`dbaguy`）、`login_can_write`（SQL Server，開過 agent session 之後才知道，之前是 null）、`needs_unlock`。

### 3.4 唯讀的分層（每一層都獨立有效）

| 層 | PostgreSQL | MySQL | SQLite | SQL Server | Couchbase |
|---|---|---|---|---|---|
| ① 閘門（剛好一句 READ＋副作用表） | ✓ | ✓ | ✓ | ✓（唯一擋寫入的一層） | ✓（含 PREPARE、CURL） |
| 送出層的單句 | adapter 唯讀時 `split()` > 1 就拒（`read_only_single_statement`，G6） | `MULTI_STATEMENTS` 關閉：堆疊是 1064（M1） | `sqlite3` 一次只收一句（S2） | 無：一個批次多句照跑（T1） | server 一個請求只收一句（3000，C2） |
| ② session 唯讀 | `SESSION CHARACTERISTICS READ ONLY`＋`conn.read_only`（P2） | `SET SESSION TRANSACTION READ ONLY`（M2；可被 function 改掉，M3） | `mode=ro`＋`query_only`＋authorizer（S9） | 無 | 每個請求 `readonly=true`（C1） |
| ③ 每次的交易 | `BEGIN READ ONLY … ROLLBACK`（ROLLBACK 還原 function 改的設定，P3） | `START TRANSACTION READ ONLY … ROLLBACK`（M3e） | 不需要 | `BEGIN TRANSACTION … ROLLBACK`（回復 DML／DDL，T6；序列不回復，T3） | 無交易 |
| ④ server 的時間上限 | `SET LOCAL statement_timeout`（P10） | `max_execution_time`（SELECT，M7） | worker 的 interrupt | worker 的 cancel | worker 的 cancel（server 的 1080 原文照顯示） |
| 閘門漏掉時，server 擋得住嗎 | 是（25006：寫入、`nextval`、`FOR UPDATE`、`lo_import`，P4、P5、P7、P11） | 是（1792，M2、M4） | 是（S1、S9） | **否**，只能 rollback | 是（1000，C1；`UPDATE STATISTICS` 例外，只有分類器擋，④ 已知） |

worker 的 timeout（`ExecuteCommand.timeout_s`）是主要的時間上限，server 的那份是 sidecar 卡住或死掉時的後盾。

### 3.5 工具

| 工具 | 參數 | 結果 | 重用 |
|---|---|---|---|
| `list_connections` | — | §3.3 的欄位 | `ProfileService`、registry 的能力 |
| `list_objects` | `connection`、`path`（省略＝根）、`limit`（100，最多 1,000） | `[{kind, name, path, has_children, detail}]`、`truncated` | `MetadataService.children` |
| `search_objects` | `connection`、`pattern`、`database?`、`include_columns`（false）、`limit`（50，最多 1,000） | 表、view、collection（⌘P 的排名）；`include_columns` 時另比對欄名，回 `{relation, column, type}` | `SchemaCache.search` 與 snapshot |
| `describe` | `connection`、`path` 或 `{database?, schema, name}`、`include_ddl`（false） | 欄位、PK、FK、index、註解、DDL? | D-08-11 |
| `run_query` | `connection`、`sql`（最多 100,000 字元）、`database?`、`query_context?`（Couchbase，`bucket.scope`，同 History 的寫法）、`max_rows`、`max_cell_chars`、`timeout_s` | `{columns: [{name, type}], rows: [[…]], row_count, more_rows, truncated, elapsed_ms, notes}` | 閘門、agent session、worker（policy、timeout、cancel、`encode_row`、History） |
| `explain` | `connection`、`sql`、`database?`、`query_context?`、`timeout_s` | 計畫文字；Couchbase 另有 `findings` | D-08-10；`explain.py` |
| `sample_fields` | `connection`、`bucket`、`scope`、`collection`、`size`（預設＝app 設定，最多 1,000） | 路徑、型別、出現次數、各上限的旗標（`FieldSample`） | `FieldSampleService`（快取共用） |

**一次 `run_query` 的流程**（`agent/reader.py`；`explain` 相同，只是語句由 D-08-10 組）：

1. 解析 `connection`；重讀 profile：Off → `unknown_connection`，`schema` → `not_allowed`；`timeout_s` 的時鐘從這裡開始。
2. 閘門（D-08-2）：拒絕 → History 一筆 `refused`（sql、原因、client）、INFO 一行、回 `refused`；什麼都沒連線。
3. 取得 (profile, database) 的 agent session（沒有就 `create_agent`：`resolve_secrets` 不問密碼，`password_required` 在這裡）；拿這個 session 的 lock（等待算進 timeout，逾時 `busy`）。
4. `worker.call()` 送 ReadFrame 的開頭（PG `BEGIN READ ONLY`＋`SET LOCAL statement_timeout`；MySQL `SET SESSION max_execution_time`＋`START TRANSACTION READ ONLY`；SQL Server `BEGIN TRANSACTION`；SQLite、Couchbase 沒有）。
5. 送 MCP 內部的執行指令（`ExecuteCommand` 的子類：`page_size = max_rows + 1`、剩下的 `timeout_s`、`text_limit = max_cell_chars`、`client`、`query_context`）；worker 照常過 hooks 的閘門（後盾）、arm／disarm、timeout、`encode_row`、`on_record`（History `source=mcp`）。
6. 收事件直到 `finished`，或第一個結果集 `result_set_end(has_more=True)`（暫停）→ 送 `DiscardCommand` 並等它的結束。
7. `RollbackCommand`（SQLite、Couchbase 不需要）；失敗就關掉這個 agent session。放開 lock。
8. `shape.py` 依回應預算組結果；INFO 一行（工具、連線、client、結果、列數、ms）。

結果的樣子（精簡的 JSON 文字，一個 `TextContent`）：

```json
{"columns": [{"name": "id", "type": "int8"}, {"name": "note", "type": "text"}],
 "rows": [["9007199254740993", "it's 中文"], [2, {"$type": "text", "preview": "…", "size": 52000, "truncated": true}]],
 "row_count": 2, "more_rows": true, "truncated": null, "elapsed_ms": 12,
 "notes": ["More rows exist: raise max_rows (at most 1000) or narrow the query"]}
```

server `instructions`（草稿，英文，進 SDK 的 `instructions`）：「DbaGuy shares some of the user's database connections, read-only. Start with list_connections. Explore with list_objects (pass back the `path` it returns), search_objects and describe. run_query and explain need the connection's 'Read-only queries' access; each call runs exactly one read statement, inside a read-only transaction that is rolled back. Results are capped (max_rows, max_cell_chars, about 40,000 characters). SQL Server connections are read-only by DbaGuy's checks only. Couchbase takes a query_context (bucket.scope).」

工具描述（tool search 讀的是開頭）以「Read-only.」開頭，寫明需要的等級。cell 的編碼就是 grid 的 `encode_value`（大整數是字串、bytes 是 `{"$type": "bytes", size, preview}`、Couchbase 的 MISSING 與 JSON 原文照 `$type`），所以 agent 看到的值和 grid 一樣不失真；長的文字以 `encode_row(row, text_limit=max_cell_chars)` 變成 `{"$type": "text", preview, size, truncated: true}`（同一個函式，只是上限不同：MCP 內部的執行指令帶 `text_limit`）。

### 3.6 上限與結果

| 上限 | 預設 | 最大 | 理由 |
|---|---|---|---|
| `max_rows` | 100 | 1,000 | DBHub 的 `search_objects` 也是 100／1,000；100 列 × 10 欄 × 20 字元約 20,000 字元。worker 的 `page_size = max_rows + 1`，多讀的一列只為了 `more_rows`，之後 discard（PG 是 CLOSE、MySQL 是 KILL QUERY 加 drain，既有行為） |
| `max_cell_chars` | 1,000 | 20,000 | 一份 Couchbase 文件或一段 JSON 通常放得下 20,000；再長的請 agent 投影欄位 |
| 回應 | 40,000 字元（JSON 文字） | 同 | Claude Code 在工具輸出超過 10,000 token 時警告、預設上限 25,000 token，成功的文字結果超過 50,000 字元就存成檔案（附錄 B）；英文約 4 字元一個 token，40,000 字元約在警告線上、遠低於檔案門檻。超過時從最後一列往前丟，`truncated: {"rows_dropped_for_size": n}`，並在 `notes` 說明怎麼縮小（投影、`max_cell_chars`）。CJK 的文字每字元 token 較多，可能超過警告線（只是警告） |
| `timeout_s` | 30 | 300 | 含等 lock 與 session 開啟；Claude Code 的工具 timeout 預設約 28 小時（附錄 B），所以由我們決定。逾時＝worker cancel（SQL Server 先 cancel，必要時依 03b 的規則斷線、session 重開） |
| `sql` | — | 100,000 字元 | agent 的查詢不需要更長；body 上限 1 MiB |
| server messages | 20 則、各 500 字元 | — | PG NOTICE、SQL Server PRINT 放在 `notes` |
| agent session | — | 8 | D-08-1 |

一句只取第一個結果集（單句 READ 本來只有一個；有第二個時在 `notes` 說明）。`row_count` 是回的列數，`more_rows` 表示 server 還有。

### 3.7 History 與可見性

History 面板的每一筆 MCP 紀錄有「MCP」標記（tooltip：client 名稱），`refused` 有自己的圖示與「Refused」文字（原因在錯誤欄，例如 `not a single read statement (write)`）；篩選 All／App／MCP。History 的保留與搜尋照舊（50,000 筆、全文搜尋）。

### 3.8 Frontend（08b）

- **Settings › MCP server**：開關「Let AI tools read through DbaGuy」；狀態行（Listening on 127.0.0.1:17531／Port 17531 is in use: choose another／Off／Error: …）；Port 欄位；token（遮罩、Copy、Regenerate…，確認文字說明已註冊的 client 要重新註冊）；Claude Code 指令（等寬、Copy）：`claude mcp add --transport http --scope user dbaguy http://127.0.0.1:17531/mcp --header "Authorization: Bearer <token>"`（`--header` 是 variadic，必須放在 URL 之後〔實測：Claude Code 2.1.295 的 `claude mcp add --help`〕；user scope：所有專案都看得到、存在 `~/.claude.json`；不建議 project scope，`.mcp.json` 會進 git）；Regenerate 之後指令前面加 `claude mcp remove --scope user dbaguy &&`；說明「token 存在 `~/.claude.json`」與「Connections shared: 2 schema only, 1 read-only queries」（點了打開連線清單）。指令字串由純函式 `mcpCommand.ts` 組（Vitest），token 含的字元只有 URL-safe base64，不需要 shell 跳脫。
- **連線對話框**：「AI access (MCP)」Off／Schema only／Read-only queries，下方一行說明：Queries 等級時「Query results from this connection are sent to the AI client and its model provider」；SQL Server 加「SQL Server cannot enforce read-only: DbaGuy checks each statement and rolls back. Use a login with read-only rights」；顏色是 red 時加「This connection is marked as production」。全域開關關著時這一列仍可設，旁邊說明「MCP server is off (Settings)」。
- **物件樹**：存取不是 Off 的連線列有一個小的 AI 標記（tooltip 寫等級）：使用者一眼看得出哪些連線對 agent 開放（Q3）。
- **History**：§3.7。

## 4. 錯誤處理

工具錯誤一律是 `is_error=True` 的結果（D-08-6），`code` 固定、`message` 是給 agent 看的一句話。

| code | 何時 | message（摘要） |
|---|---|---|
| `unknown_connection` | 沒有這條、或它是 Off | No connection '<x>' is shared with AI tools; list_connections lists them |
| `ambiguous` | 名稱重複、describe 的名稱多個符合 | 列出候選的 id 或路徑 |
| `not_allowed` | Schema 等級呼叫 Queries 的工具；非 Couchbase 呼叫 `sample_fields` | '<x>' allows schema only: run_query needs 'Read-only queries' in DbaGuy's connection settings |
| `refused` | 閘門拒絕（D-08-2） | DbaGuy runs exactly one read statement per call: <原因>；原因是 `several statements`／`not a read (write|ddl|transaction|session|unknown)`／`<function> has side effects`／`EXPLAIN is added by the explain tool` |
| `password_required` | R-08-9 | '<x>' needs its password: connect to it in DbaGuy first (or unlock it) |
| `connection_failed` | 開 session 失敗 | server 或 driver 的原文（工具結果裡，不進 INFO） |
| `query_failed` | server 的錯誤 | server 的原文與 code（含唯讀被 server 擋下：25006、1792、1000、SQLite 的 `not authorized`，另加一句「DbaGuy's MCP access is read-only」） |
| `timeout` | 超過 `timeout_s` | Stopped after 30 s (timeout_s; at most 300) |
| `cancelled` | client 取消 | — |
| `busy` | 等這條連線的 lock 等到逾時 | Another call on '<x>' was still running |
| `not_found` | describe／list 的路徑不存在 | metadata 服務既有的 NotFound 文字 |
| `too_many_sessions` | 不會發生（滿了會關閒置的）；全部都在跑時 | Too many connections are busy; try again |

AgentHost 的啟動失敗（port 被占用、Keychain 讀寫失敗、import 失敗）不影響 API，只反映在 `/mcp/status` 與 Settings。框架的 ROLLBACK 失敗：關掉那個 agent session，該次呼叫的結果照舊回（已經讀到的列），`notes` 說明連線已重開。

## 5. Log（loguru）

D-08-14。另外：SDK 的 stdlib logger（`mcp.*`）設 WARNING；SDK 記 Host／Origin 被拒（`Invalid Host header: …`）是 WARNING，內容是對方送的 header，不含 token。測試以 `caplog` 確認：一次失敗的查詢（PG 42P01）之後，INFO 以上的 log 沒有 server 的原文、沒有 SQL、沒有 token。

## 6. 測試（依「只測容易出錯的邏輯」）

- **單元（pytest，純）**：閘門：附錄 A 的 G1–G10 與 Datadog 變形逐 dialect（含巢狀註解、`$tag$`、E 字串、T-SQL 沒有分號的兩句、MySQL 版本註解、SQL++ 的 PREPARE、副作用 function 的大小寫與引號）；`shape.py` 的三種預算（列、cell、回應）與 `truncated`；History 的升級（舊 schema 的檔加欄、資料不變）。
- **整合（dev DB；scratch 一律 `zz_08_*`，`finally` 以完整名稱 DROP；SQLite 用 `tmp_path`；Couchbase 只讀）**：每個 engine：`run_query` 的列、上限、`more_rows`、timeout、cancel；**拿掉閘門**直接對 agent session 送寫入（PG 25006、MySQL 1792、SQLite `not authorized`、Couchbase 1000，SQL Server 的 DML 在 ROLLBACK 後不存在）；PG 的 `zz_08_flip()`（把 session 預設關掉的 function）之後下一次呼叫仍唯讀；MySQL 的 `zz_08_flip()` 之後下一次呼叫仍擋寫入（M3 的回歸）；SQLite ATTACH、`VACUUM INTO` 被拒、沒有留下檔案；SQL Server 的 SHOWPLAN 在 explain 之後關掉；`describe` 的惡意名稱；`password_required` 不連線。
- **HTTP（TestClient 對 MCP 的 ASGI app，與 `mcp.Client` 對真的 port）**：401、421、帶 Origin 403；兩個協定版本各一次完整的 `tools/list`、`tools/call`；client 名稱進 History；regenerate 後舊 token 401。
- **Log**：§5。
- **前端（Vitest）**：`mcpCommand.ts`、History 的 source 篩選（純函式的部分）。
- **手動**：使用者以真的 Claude Code 註冊、`/mcp`、每個工具一次、開關、改 port、port 被占用、Regenerate、DbaGuy 關掉時 Claude Code 的樣子（給使用者的測試清單，08b plan 的附錄）。
- 規則同 ⑤⑥：scratch 物件存在時不跑完整的 backend 測試，一次只讓一個人跑 live 測試。

## 7. 效能、打包與量測

| 項目 | 預算 | 備註 |
|---|---|---|
| 開關關著 | 冷啟動與 RSS 與 06 相同 | 不 import SDK（測試：`sys.modules` 沒有 `mcp`） |
| 開啟（第一次） | sidecar RSS 增加 ≤ 30 MB；啟動 MCP ≤ 1 秒；第一個 API 請求不受影響 | 〔實測，scratch venv〕import 0.21 秒（冷 0.52 秒）、RSS +23 MB |
| `run_query`（本機，100 列、10 欄，session 已開） | ≤ 100 ms（含框架） | 框架每次 +1–3 個往返（T8：SQL Server 0.45 → 1.30 ms） |
| `list_objects`／`describe`（snapshot 已在） | ≤ 200 ms | metadata 連線 |
| 打包 | sidecar 增加約 14 MB（06b 量的 49,552 KB → 約 64 MB），**〔估計〕** | 原生：`cryptography` 的 `_rust.abi3.so` 10.4 MB（靜態連結自己的 OpenSSL，不是 dylib，`build-backend.sh` 的 libcrypto 版本檢查不受影響）、`rpds` 0.9 MB、`_cffi_backend` 0.2 MB；其餘純 Python 進 PYZ |

- 打包：`pyinstaller-hooks-contrib` 有 `cryptography`、`jsonschema_specifications`（資料檔）的 hook；spec 加 `collect_submodules("mcp")`、`collect_submodules("mcp_types")`（版本模組依名稱載入的保險）。`codesign --verify --strict` 要涵蓋新的 `.so`。打包後的檢查：凍結的 sidecar（暫存 data dir、probe 自己啟動、settings 先開 MCP）以 `mcp.Client` 對五個 engine 各呼叫一次每個工具、確認 401／421／403、stderr 沒有 `ModuleNotFoundError`；量 sidecar 大小、開啟前後的 RSS 與 `footprint`、`measure-startup.sh`（關著）。
- 相依：`mcp` 2.3.0 要求 `starlette>=0.27`、`pydantic>=2.12`、`anyio>=4.9`、`uvicorn>=0.31.1`、`opentelemetry-api>=1.28`，與 backend 現有的版本（starlette 1.8.0、pydantic 2.14.1、anyio 4.15.1、uvicorn 0.54.0、opentelemetry-api 1.45.1）相容（scratch venv 以同版本一起安裝沒有衝突〔實測〕）；新的 runtime 相依：`mcp`、`mcp-types`、`httpx2`、`httpcore2`、`truststore`、`jsonschema`（＋`referencing`、`rpds-py`、`attrs`、`jsonschema-specifications`）、`pyjwt`、`cryptography`（＋`cffi`、`pycparser`）、`sse-starlette`、`python-multipart`。dev 的 `httpx`（TestClient）與 `httpx2` 並存。

## 8. 痛點對照

| 需求 | 這個子專案怎麼解 | 踩到／退步 |
|---|---|---|
| Q1 輕快低記憶體 | 關著＝零成本（lazy import、不開 port）；開著的回應有上限、session 有上限且閒置就關；啟動 MCP 在背景 | 開著時 RSS +約 23 MB；`.app` 大約 +14 MB（`cryptography`），每個使用者都付這個下載與磁碟成本 |
| Q2 穩定不丟工作 | agent 自己的 session，從不碰使用者的 tab、交易、暫存；每次 ROLLBACK；timeout 與取消；AgentHost 失敗不影響 API | — |
| Q3 可信任 | 預設關、逐連線開啟、Off 的連線對 agent 不存在；唯讀在 DB 層（四個 engine）；token、只綁 localhost、拒絕 Origin；不問密碼；History 記每一個查詢與被拒的嘗試；樹上的 AI 標記；沒有遙測 | SQL Server 只到 app 層加 rollback；token 在 `~/.claude.json` 是明文（§9） |
| Q4 聚焦 | 7 個工具、沒有 resources／prompts／chat／寫入 | — |
| Q5 多引擎 | 五個 engine 同一套工具，差異只在 adapter 與 `sql/` 的表（框架、explain、副作用 function） | Couchbase 多一個 `sample_fields` 與 `query_context` |
| Q6 macOS 慣例 | Settings（⌘,）裡的一節；複製按鈕；連線對話框 | — |
| FP §3 S9 | 就是它 | — |
| FP §3 M2、M11、M12、M13 | 分層探索；DB 層唯讀與 production 標記；timeout 與取消；History | — |
| FP §5 原則 1 | 全域開關預設關 | — |
| 原則 2（自帶 key／本地模型） | 不適用：DbaGuy 不呼叫任何模型，client 是使用者自己的 | — |
| 原則 3（只送 metadata、sample rows 預設關） | 連線預設 Off；`schema` 等級完全不讀列 | — |
| 原則 4（原樣錯誤優先） | 工具結果帶 server 的原文與 code | — |
| 原則 5（DML／DDL 絕不自動執行、唯讀在 DB 層） | §3.4 | SQL Server 例外，揭露 |
| 原則 6（優先 MCP） | — | — |
| FP §8（AI 只做 MCP server） | 沒有 chat、explain／fix 按鈕 | — |

## 9. 風險

| 風險 | 處理 |
|---|---|
| Prompt injection：表裡的資料（例如一個 comment 欄）指使 agent 做別的事 | DbaGuy 只給唯讀工具，最壞是 agent 讀了更多它被允許讀的東西；把資料送去別處是 client 的其他工具的事。連線對話框說明 Queries 等級的列會送到 AI client 與它的模型供應商 |
| token 外洩（`~/.claude.json` 明文、shell history） | 唯讀、只綁 localhost、只有開放的連線、DbaGuy 開著時才有；Regenerate；Settings 說明存放位置。之後可改 Claude Code 的 `headersHelper` 讀 Keychain（§12） |
| 自訂 function 裡的副作用（PG 的 `SECURITY DEFINER` function 呼叫 `pg_terminate_backend`、dblink；MySQL function 呼叫 `GET_LOCK`） | 寫入照樣被 server 的唯讀交易擋；副作用表只看得到語句本身的呼叫。Settings 與對話框建議 agent 用唯讀的 login；dev 的 PG 使用者是 superuser（P0），superuser 能讀伺服器檔案（`pg_read_file`）——唯讀 login 也解決這個 |
| SQL Server 沒有 server 唯讀 | R-08-12 |
| SDK 的變動（v2 剛改名、協定一年兩版） | pin `mcp>=2.3,<3`；工具層只用 `MCPServer`、`CallToolResult`、`TransportSecuritySettings`、`streamable_http_app` 四個入口；升級時跑 §6 的 HTTP 測試 |
| Keychain：重新 build 後 ad-hoc 簽章變了，sidecar 啟動 MCP 讀 token 時跳「允許存取」 | 與密碼相同的既有限制；只在 MCP 開著的啟動時發生 |
| client 自報的名字可以是假的 | History 標示為「client 自報」；不作為權限依據 |
| 兩個 DbaGuy（dev 與正式）搶同一個 port | R-08-2 的 17531／17532 |
| 大結果的 discard 慢（MySQL KILL＋drain，03a） | 在 timeout 之內；`more_rows` 之後才 discard，agent 已拿到結果 |
| 同一條連線的呼叫排隊 | 一次一個，等候計入 timeout，`busy` 說明 |

## 10. 未實測的假設

| # | 假設 | 驗證 |
|---|---|---|
| A1 | Claude Code 2.1.295 以 `--header` 帶 bearer 連上 SDK 的 Streamable HTTP（JSON 回應、stateful 的 2025-11-25 或 2026-07-28），`/mcp` 顯示連上、工具可呼叫；它自報的 `clientInfo.name` 是什麼 | 08a Task 1 的 spike：以 `claude --mcp-config <scratch 裡的 JSON> --strict-mcp-config` 一次性載入（Claude Code 2.1.295 有這兩個旗標〔實測 `claude --help`〕），不改使用者的 `~/.claude.json`；結果寫回本表 |
| A2 | 錯的 token 的 401：Claude Code 是否嘗試 OAuth discovery、顯示什麼 | 08a Task 1 |
| A3 | 第二個 uvicorn 在 thread 裡反覆開關（toggle、改 port）沒有洩漏；SDK 每次新建 session manager | 08a Task 1 |
| A4 | client 取消（兩個時代）會取消 tool 的 task，`worker.cancel` 停掉 DB 的語句 | 08a Task 5 |
| A5 | PyInstaller 收得到 SDK（`cryptography`、`jsonschema_specifications` 的資料檔、`truststore`、`mcp_types` 的版本模組），`codesign --verify --strict` 通過；大小增加約 14 MB | 08a Task 8 |
| A6 | 40,000 字元的回應不觸發 Claude Code 的 10,000 token 警告（英文為主的結果） | 08a Task 1 的 spike 量一次 |
| A7 | PG 的 `SET LOCAL statement_timeout` 在 DECLARE／FETCH 的路徑（交易中的 `_CursorResult`）照樣生效 | 08a Task 4 |
| A8 | SQL Server 的 `SET SHOWPLAN_TEXT ON` 與 adapter 的 describe／狀態讀取（`sys.dm_exec_describe_first_result_set`、`@@TRANCOUNT`）共存；SHOWPLAN 需要 SHOWPLAN 權限，缺了的錯誤原文 | 08a Task 5 |
| A9 | SQLite 的 authorizer 與 adapter 既有的行為（`cached_statements=0`、開檔時的 `PRAGMA foreign_keys`、取消的 `interrupt`）相容；authorizer 在 `PRAGMA query_only = ON` 之後才裝 | 08a Task 4 |
| A10 | MySQL 5.7（公司）：`START TRANSACTION READ ONLY`（5.6.5 起）、`max_execution_time`（5.7.8 起）、沒有 `FORMAT=TREE` 時退回 `EXPLAIN` | 公司資料庫的檢查清單 |
| A11 | Couchbase 的 `EXPLAIN` 在 `readonly=true` 下對 SELECT 照常（C7 對 UPSERT 都可以） | 08a Task 5 |

## 11. 執行計畫拆分

**08a backend**（branch `feat/08-mcp-server`）：
1. 相依與 spike：`poetry add "mcp>=2.3,<3"`；`AgentHost`（lazy import、第二個 uvicorn、狀態）、`token_store`、認證 middleware、`TransportSecuritySettings`；`McpSettings`、`/mcp/status`、`/mcp/token`、regenerate；HTTP 測試（401／421／403）；A1–A3、A6 的 spike。
2. Profile 的 `mcp_access`；`list_connections`、`list_objects`、`search_objects`、`describe`（D-08-11），惡意名稱測試。
3. 閘門 `SafetyPolicy.agent_read` 與副作用表；附錄 A 的 G 系列成為回歸測試。
4. agent session：`create_agent`、`open(…, agent=True)`、SQLite authorizer＋`query_only`、`ReadFrame`、`AgentReader`（lock、emitter、discard、rollback、失敗就關 session）；拿掉閘門的 live 測試（每個 engine）、M3／P3 的回歸。
5. `run_query`、`explain`（D-08-10）、`sample_fields`；`shape.py` 的預算；timeout、cancel（A4、A7、A8、A11）。
6. History：欄位升級、`refused`、`source` 篩選、agent 的 `on_record` 與 client；被拒的嘗試寫入。
7. 錯誤與 log：`CallToolResult` 的錯誤、`mcp.*` logger、caplog 的稽核；`instructions` 與工具描述。
8. 打包與量測（§7）；CLAUDE.md（Repo 結構、架構重點、已知限制、開發指令）。

**08b frontend**：
1. Settings 的 MCP 一節（狀態輪詢只在 Settings 開著時）、`mcpCommand.ts`（Vitest）、Regenerate 的確認。
2. 連線對話框的 AI access 與說明；物件樹的 AI 標記。
3. History 的 MCP 標記、Source 篩選、Refused 狀態；`npm run gen:api`。
4. 給使用者的測試清單（真的 Claude Code：註冊、每個工具、開關、port、Regenerate、DbaGuy 關掉時）。

## 12. 細化與之後

### 對 coordinator 裁決的修正提議（OP）

| # | 裁決 | 研究結果與提議 |
|---|---|---|
| OP-1 | SQLite：`mode=ro`（與 `query_only`） | `mode=ro` 擋不住 ATTACH 進來的檔案（S4：寫入成功）與 `VACUUM INTO`（S3：寫出新檔）；`query_only` 擋住寫入，但 `VACUUM INTO` 仍留下空檔（S5）。**加上 authorizer**（D-08-4），這才是引擎層的唯讀 |
| OP-2 | 閘門＝剛好一句＋READ＋唯讀政策 | 唯讀交易擋不住的「讀取」有副作用：PG `pg_terminate_backend` 殺掉別的連線（P8）、advisory lock 與 MySQL `GET_LOCK` 在 ROLLBACK 後還在（P6、M5）、Couchbase `PREPARE` 在 `readonly=true` 下照建（C3）。**加副作用 function 表**（D-08-2） |
| OP-3 | MySQL：session READ ONLY＋`START TRANSACTION READ ONLY` | 照做；另外發現 M3：function 能把 session 改回 READ WRITE，**使用者自己的唯讀 tab** 也受影響。提議另開一個小修正（不在 ⑧）：唯讀的 MySQL 連線每句之前確認 `transaction_read_only`（或每句包 `START TRANSACTION READ ONLY`） |
| OP-4 | Host／Origin 驗證 | SDK 對 localhost 的預設接受任何 `http://localhost:*` 的 Origin；**改成有 Origin 就拒絕**（R-08-4），代價是瀏覽器裡的 client 不能用 |
| OP-5 | Couchbase 的 query context 工具 | 不另開工具：`list_connections` 的預設、`list_objects` 的 bucket › scope、`run_query`／`explain` 的 `query_context` 參數已經涵蓋，少一個工具（Q4） |
| OP-6 | `explain`（不 ANALYZE） | 只接受 READ：MySQL 在唯讀交易裡拒絕 `EXPLAIN INSERT`（M8），PG 允許（P9）；讓 engine 一致。DML 的計畫留給之後 |
| OP-7 | History 記每個 MCP 查詢 | **被拒的嘗試也記**（新狀態 `refused`）；schema 工具不進 History（只進 log） |
| OP-8 | 用官方 SDK | 照做，但它的成本要寫明：+約 14 MB 的 `.app`、開著時 +23 MB RSS（`cryptography` 是 `import mcp` 就會載入的）；以 lazy import 讓關著的人零成本。工具一律回 `CallToolResult`：SDK 會把 `ToolError` 的訊息記在 INFO（D-08-6） |

### Theirs（只有使用者能決定、會改變做法的問題）

無。（SQL Server 在 login 能寫入時要不要禁止 Queries 等級，原則 5 字面上傾向禁止；但使用者說公司少用 SQL Server（CLAUDE.md），且 dev 的 login 是 `db_owner`，禁止會讓它無法測試，所以照 coordinator 的裁決允許並揭露，`login_can_write` 讓兩邊都看得到。）

### 之後

- Claude Code 的 `headersHelper`（每次連線執行一個指令產生 header）讀 Keychain 的 token，`~/.claude.json` 就不存明文；需要讓 `security` 讀得到 sidecar 建的 Keychain 項目而不跳允許存取（ACL），`keyring` 做不到，要另外研究。
- DML 的 `explain`（PG、SQL Server 可以，MySQL 不行）；Couchbase 的 `ADVISE`（`CouchbaseService.advise` 已有）。
- 每個 client 不同的 token 與權限；只對某些 schema 開放。
- 瀏覽器裡的 client（MCP Inspector）：允許一個指定的 Origin。
- `outputSchema`／`structuredContent`（等 Claude Code 對兩者的計量清楚之後）。

## 13. 收尾時要更新的文件

CLAUDE.md：Repo 結構（`agent/`、`domain/agent.py`、`api/mcp.py`、前端三處）、技術選型（`mcp` SDK）、架構重點（MCP：port、token、Host／Origin、存取等級、唯讀分層、框架、閘門與副作用表、History 的 source）、開發指令（dev 的 MCP port 17532、以 `claude mcp add --scope local` 在 scratch 目錄測）、已知限制（SQL Server、token 明文、副作用表的範圍、瀏覽器 client）、打包版量測、進度表 ⑧。

## 附錄 A：探測紀錄（2026-10-25，dev 資料庫）

腳本在 session 的 scratchpad（`probes/*.py`）；scratch 物件全部以完整名稱移除，結束時確認剩 0 個。

**G：DbaGuy 的分類器、`is_plain_read`、單句檢查、唯讀政策（純，`classify.py`）**
- G1 PG `SELECT 1; COMMIT; DROP TABLE zz_08_t`：3 段、DDL、唯讀政策 BLOCK。
- G2 PG `SELECT $$;$$; COMMIT`：2 段、TRANSACTION，**唯讀政策是 RUN**（交易語句沒有 WRITE 就允許）；agent 閘門以「2 段」拒絕，PG adapter 唯讀時也拒（G6）。
- G3 PG `SELECT pg_terminate_backend(1)`、`pg_advisory_lock(1)`、`lo_export(…)`、`pg_read_file(…)`、`dblink_exec(…)`：都是 READ、plain、RUN → 副作用表（D-08-2）。`SELECT set_config(…)` 已被 BLOCK。`SELECT … FOR UPDATE`、`WITH d AS (DELETE …) SELECT`、`SELECT … INTO`、`COPY` 都不是 READ。
- G4 T-SQL `SELECT 1 SELECT 2`：READ、plain、單一 batch 但 `tsql.statements` 是 2 → 閘門拒絕。
- G5 T-SQL `SELECT 1 COMMIT`：TRANSACTION、2 句、唯讀政策 RUN → 閘門拒絕（不是 READ、也不是一句）。`SELECT 1; COMMIT`：段內有 `;`。
- G6 PG adapter（唯讀 profile）對 7 種堆疊變形（巢狀註解、`$x$`、E 字串、`'a\'`、換行分隔、開頭 COMMIT）全部 `read_only_single_statement`，`zz_08_victim` 都還在（`pg_adapter_probe.py`）。
- G7 T-SQL `SELECT 0x1DROP TABLE t`：READ、一句（lexer 照 server 讀 `0x1D`），server 是語法錯誤（03b 既有）。`SELECT NEXT VALUE FOR s` 是 WRITE；`OPENQUERY`／`OPENROWSET`／`OPENDATASOURCE`、`EXEC`、`KILL` 是 UNKNOWN。
- G8 MySQL `SELECT 1 INTO OUTFILE`／`DUMPFILE`：WRITE；`SELECT GET_LOCK(…)`、`LOAD_FILE(…)`：READ → 副作用表；`/*!50000 DROP … */`：UNKNOWN。
- G9 SQLite `VACUUM INTO`：DDL；`ATTACH`、`PRAGMA query_only = 0`：SESSION，唯讀 BLOCK；`SELECT load_extension('x')`：READ。
- G10 SQL++ `UPDATE STATISTICS`、`ANALYZE KEYSPACE`、`SELECT NEXT VALUE FOR`、`UPSERT`、`DELETE FROM system:…`：WRITE；`EXECUTE FUNCTION`、`EXECUTE p`：UNKNOWN；`PREPARE p AS SELECT 1`、`INFER`、`ADVISE`、`SELECT CURL(…)`：READ → PREPARE、CURL 進副作用表。

**P：PostgreSQL 17.11（`pg_probe.py`；dev 使用者 `dbaguy` 是 superuser，P0）**
- P1 `BEGIN READ ONLY` 之後在 simple query protocol 送 `SELECT 1; COMMIT; DROP TABLE zz_08_victim`：**DROP 成功**（Datadog 的形狀，只有交易唯讀不夠）。
- P2 session 預設唯讀（`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`）時同一段：25006，表還在；`SET default_transaction_read_only = off; DROP …` 堆在同一段也是 25006（同一個隱含交易）。
- P3 plpgsql function 以 `set_config('default_transaction_read_only','off',false)` 關掉預設：交易內 off；**ROLLBACK 後 on**；COMMIT 後 off。
- P4 寫入的 function 在 `BEGIN READ ONLY` 裡：25006。P5 `nextval`：25006。P11 `SELECT … FOR UPDATE`：25006。
- P6 `pg_advisory_lock(808)` 在唯讀交易、ROLLBACK 之後：鎖仍在（1）。
- P7 `lo_export(0, …)`：42704（先檢查物件，唯讀沒擋）；`lo_import(…)`：25006。
- P8 唯讀交易裡 `pg_terminate_backend(<同一使用者的另一條連線>)`：回 true，那條連線死了。
- P9 `EXPLAIN (FORMAT JSON) INSERT …` 在唯讀交易：允許；`EXPLAIN ANALYZE INSERT`：25006。
- P10 `SET LOCAL statement_timeout = 500` 加 `pg_sleep(3)`：57014；ROLLBACK 後 `statement_timeout` 回到 0。

**M：MySQL 8.4.11（`mysql_probe.py`、`mysql_m1b.py`；dev 使用者只有兩個 database 的 ALL）**
- M1 DbaGuy 的 adapter（唯讀與否）送 `SELECT 1; COMMIT; DROP TABLE zz_08_victim`：1064，表還在。driver 預設（`MULTI_STATEMENTS` 開）＋只有 `START TRANSACTION READ ONLY`：**DROP 成功**；再加 session READ ONLY：表還在。
- M2 唯讀 session 的 autocommit INSERT：1792。M4 寫入的 function 在唯讀交易：1792。
- M3 function 內 `SET SESSION TRANSACTION READ WRITE`（或 `SET @@SESSION.transaction_read_only = 0`）建得起來；在 `START TRANSACTION READ ONLY` 裡呼叫、ROLLBACK 之後 session 的 `transaction_read_only` 是 0；**之後的 autocommit INSERT 成功**（M3d）；明確的 `START TRANSACTION READ ONLY` 裡的 INSERT 仍是 1792（M3e）。
- M5 `GET_LOCK` 在唯讀交易、ROLLBACK 之後：鎖仍被那條連線持有。
- M6 `LOAD_FILE('/etc/hostname')`：NULL（沒有 FILE 權限；`secure_file_priv` 是 `/var/lib/mysql-files/`）。
- M7 `max_execution_time = 500`：重的 SELECT 3024；`SLEEP(2)` 提早回 1。
- M8 `EXPLAIN INSERT …` 在唯讀交易：1792。

**S：SQLite 3.54.1（`sqlite_probe.py`、`sqlite_probe2.py`，scratch 檔）**
- S1 `mode=ro` 的 INSERT：`attempt to write a readonly database`。S2 `SELECT 1; DELETE …`：`You can only execute one statement at a time`。
- S3 `mode=ro` 的 `VACUUM INTO '<scratch>/zz_08_copy.db'`：**成功、寫出新檔**。
- S4 `mode=ro` 的 ATTACH（路徑，或 `mode=rw` 的 URI）後 INSERT 進被 attach 的檔：**成功**（那個檔多了列）。
- S5 加 `PRAGMA query_only = 1`：`VACUUM INTO` 報錯但**仍留下檔案**；ATTACH 可以、INSERT 被拒；TEMP 表被拒。`PRAGMA query_only = 0` 可以再關掉（閘門擋）。
- S6 `load_extension`：`not authorized`（Python 預設關閉）。S7 `mode=ro` 的 BEGIN／SELECT／ROLLBACK 照常。S8 `mode=ro` 的 TEMP 表：可以（沒有 `query_only` 時）。
- S9 authorizer（只允許 SELECT、READ、FUNCTION、RECURSIVE）：SELECT、遞迴 CTE、`EXPLAIN QUERY PLAN`、`sqlite_master` 照常；`VACUUM INTO`、ATTACH、`PRAGMA query_only = 0`、BEGIN、`PRAGMA table_info`、`pragma_table_info()` 都被拒；被 attach 的檔沒有被寫。

**T：SQL Server 2022 16.0.4305.5（`mssql_probe.py`；`dbaguy` 是 `db_owner`、不是 sysadmin，T0；只讀 pymssql 例外的 `args`）**
- T1 `BEGIN TRANSACTION` 之後一個批次 `SELECT 1; COMMIT; DROP TABLE dbo.zz_08_victim`，再 ROLLBACK：**表不見了**；沒有分號的 `SELECT 1 COMMIT DROP TABLE …` 也一樣（server 沒有唯讀可擋）。
- T3 交易裡兩次 `NEXT VALUE FOR dbo.zz_08_seq` 再 ROLLBACK：序列值前進了（1 → 2），不回復。
- T4 `IMPLICIT_TRANSACTIONS ON`：讀表的 SELECT 開了交易（`@@TRANCOUNT` 1），`SELECT 1` 沒有（0）。
- T5 會寫入的 UDF：建立時 443（function 不能寫表）。
- T6 交易裡 `SELECT * INTO dbo.zz_08_new …` 再 ROLLBACK：新表不存在。
- T7 `SET SHOWPLAN_XML ON` 後送 INSERT：回 1 列計畫（2,318 字元），沒有插入。
- T8 本機 100 次：`SELECT 1` 每次 0.45 ms；包在 `BEGIN TRANSACTION … ROLLBACK` 每次 1.30 ms。

**C：Couchbase EE 7.6.12（`cb_probe.py`，Query Service、`readonly=true`）**
- C1 `UPSERT INTO dbaguy.app.orders … "zz_08_probe"`：1000，文件不存在。
- C2 `SELECT 1; SELECT 2` 與 `SELECT 1; DELETE …` 一個請求：3000 語法錯誤。
- C3 `PREPARE zz_08_p AS SELECT 1`：**成功**，`system:prepareds` 有它（之後以 `readonly=false` 刪除、確認不在）。
- C4 `SELECT CURL("http://127.0.0.1:8091/pools")`：5010（dev 沒開 CURL）。
- C5 `INFER dbaguy.app.users`：成功（讀文件）。C7 `EXPLAIN UPSERT …`：成功（只產生計畫）。
- C8 請求的 `timeout` 50 ms：1080。結束時 `app.orders` 500 份、沒有 `zz_08_` 文件、沒有 `zz_08_p`。

**K：MCP SDK 2.3.0（scratch 目錄的 wheel 與獨立 venv；Python 3.13.5）**
- K1 `pip download --no-deps`：`mcp` 369 KB、`mcp-types`、`cryptography` 50.0.2（wheel 3.9 MB，`_rust.abi3.so` 10.4 MB）、`httpx2` 2.16.0、`jsonschema` 4.26.0、`rpds-py`、`pyjwt` 2.15.1、`sse-starlette` 3.5.1、`python-multipart` 0.0.32；與 fastapi 0.143.2、starlette 1.8.0、uvicorn 0.54.0 一起安裝沒有衝突，新增套件安裝後約 22 MB。
- K2 已 import fastapi 與 uvicorn 之後 `from mcp.server.mcpserver import MCPServer`：0.52 秒（第一次）、0.21 秒（之後兩次）、RSS +22.9–23.1 MB；只 import lowlevel 的 `Server` 也一樣（+21.9 MB），因為 `mcp/__init__.py` 匯出 client，`httpx2`、`cryptography`、`sse_starlette`、`opentelemetry` 都會載入。
- K3 原始碼：`mcp.server.fastmcp` 是指向 `MCPServer` 的 `ModuleNotFoundError`；`streamable_http_app(json_response, stateless_http, max_sessions=10_000, session_idle_timeout=1800, max_request_body_size=4 MiB, transport_security, host)`；session manager 不能重用；stateless 的舊路徑沒有 client 資訊；`TransportSecurityMiddleware` 對 Host 不符回 421、Origin 不符回 403、沒有 Origin 放行；`ToolError` 的訊息在 INFO 記錄，回傳 `CallToolResult` 則原樣轉送；同步的工具在 `anyio.to_thread` 執行。

## 附錄 B：來源

- MCP 規格 2026-07-28，Streamable HTTP（「Servers MUST validate the Origin header」、invalid Origin 回 403、SHOULD bind only to localhost、SHOULD implement proper authentication；2026-07-28 移除 protocol-level session 與 GET stream；`MCP-Protocol-Version`、`Mcp-Method`、`Mcp-Name` 與 body 的比對；`_meta` 的 `io.modelcontextprotocol/clientInfo`）：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- MCP 規格 2026-07-28，Transports 總覽：https://modelcontextprotocol.io/specification/latest/basic/transports
- MCP 規格，Security Best Practices（本機 HTTP server：「Require an authorization token」；DNS rebinding）：https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices
- MCP Python SDK（`mcp` 2.3.0；`MCPServer`、v2 遷移）：https://pypi.org/project/mcp/ 、https://github.com/modelcontextprotocol/python-sdk 、https://py.sdk.modelcontextprotocol.io/
- Claude Code 的 MCP 文件（`claude mcp add --transport http … --header`、scope local／project／user 與存放位置、`.mcp.json` 的 `${VAR}`、10,000 token 警告與 25,000 token 上限、50,000 字元存成檔案、`MCP_TOOL_TIMEOUT` 與每個 server 的 `timeout`、`headersHelper`、tool search 與 server instructions）：https://code.claude.com/docs/en/mcp
- Datadog Security Labs，Postgres MCP 的 SQL injection（2025-08-21；`COMMIT; DROP SCHEMA public CASCADE;`、連線池的 session 狀態、改用 prepared statement 與用完即丟的連線）：https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server/
- DBHub：`search_objects`（`detail_level` names／summary／full、`limit` 預設 100、最多 1,000）https://dbhub.ai/tools/search-objects ；TOML 的 `readonly`、`max_rows`、`query_timeout`（PG `statement_timeout`、MySQL `max_execution_time`）https://dbhub.ai/config/toml
- stdio↔HTTP 的通用 bridge（R-08-10）：https://npmjs.com/package/supergateway
- 前例（DBCode、DBeaver 26.x、TablePlus、已封存的 Postgres MCP）：[landscape](../../research/2026-10-15-sql-ide-landscape.md) §AI 與附錄。
