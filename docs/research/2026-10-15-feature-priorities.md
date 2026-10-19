# 功能優先序：社群需求排名與 MoSCoW

> 日期：2026-10-15
> 前置文件：[2026-10-15-sql-ide-landscape.md](2026-10-15-sql-ide-landscape.md)（市場概覽與技術選型）
> 目的：搞清楚社群**真正**最在意什麼，避免包山包海、過度設計

---

## 0. 結論

社群最在意的不是功能多，而是**快、穩、不丟資料、鍵盤順手、一個工具搞定多種 DB**。

功能面的核心迴圈是：**連線 → 找到物件 → 寫 SQL、執行 → 看懂結果 → 複製或匯出**。DataGrip 官方調查的每日使用比例是：寫與執行 SQL 97%、瀏覽物件 90%、建立連線 83%、匯出資料 76%。

v1 的定位：**把這個核心迴圈做到極致，再加上 Couchbase 一級支援。**

HN 上約 25 人主動抱怨「macOS DB 工具功能膨脹」，並表示 99% 的時間只用到表與資料、結構與索引、搜尋、寫 SQL。所以下面的 **Won't 清單和 Must 清單一樣重要**。

---

## 1. 資料來源

| 代號 | 來源 | 樣本量 | 訊號類型 |
|---|---|---|---|
| GH | 17 個 SQL client 的 GitHub issue（DBeaver、TablePlus、Postico、Beekeeper、Sequel Ace、vscode-mssql 等） | 2,074 個 issue，465 個功能請求已對應到功能編號 | 👍 票數 |
| YT | JetBrains YouTrack（DataGrip，專案代號 DBE） | 每類取前 150 名 | 票數 |
| AS | Mac App Store 評論（Sequel Ace、SQLPro 系列、Valentina 等） | 1,352 則，46 個國家商店 | 稱讚與抱怨次數 |
| VS | VS Code 市集 | 前 17 個 DB 擴充套件 | 安裝數與評分 |
| HN | Hacker News | 30 個討論串、3,184 則留言 | 提及人數（約） |
| RD | Reddit（經 Arctic Shift 封存 API） | 82 個討論串、約 1,450 則留言，以 2025–2026 為主 | 留言分數 |
| SO | Stack Overflow / dba.SE / softwarerecs | 98 次 API 請求 | 實作卡點 |
| SV | 開發者調查（State of PostgreSQL、DataGrip UX、Stack Overflow 2026、Redgate） | 見各調查 | 使用比例 |
| CB | Couchbase 論壇、JIRA、VS Code/JetBrains 外掛 issue | 約 75 個主題、76 張 JIRA 票 | 瀏覽數 |

注意事項：
- GitHub 和 YouTrack 的票數偏向舊 issue（Postico 2015–2017 年的 issue 特別多）。
- HN 偏向 Mac 和 Linux 的重度使用者。
- Reddit 的留言關鍵字搜尋常常逾時，只讀得到熱門討論串。
- 「支援更多 DB 引擎」在每個來源都是第一名（YT 佔 69% 的票），但你的 5 個引擎已經固定，所以下面的排名**扣掉了這一類**。

---

## 2. 品質需求（比任何功能都重要）

| # | 品質 | 證據 | 對設計的影響 |
|---|---|---|---|
| Q1 | **輕、快、低記憶體** | HN #1（約 150 人）；AS 稱讚第一名（131 則）；RD 上 DBeaver 和 DataGrip 被抱怨最多的就是吃記憶體 | Tauri + 精簡 sidecar。啟動時間和閒置記憶體當成要量測的指標。 |
| Q2 | **穩定、不丟工作** | HN #2（約 85 人）；AS 有 58 則抱怨當機；RD 抱怨「卡死後沒存的 query 全沒了」 | 未存檔的 tab 自動保存、可在當機後還原；一個 driver 出錯不能拖垮整個 app。 |
| Q3 | **可信任**：免登入、無遙測、離線可用、密碼進 Keychain | HN 有人看到登入畫面就直接解除安裝；RD 不喜歡遙測和推銷 | 自用天生就滿足，維持即可。 |
| Q4 | **聚焦 80% 的使用情境** | HN 約 25 人提到 | 本文的 Won't 清單。 |
| Q5 | **多引擎一開始就設計進去** | Sequel Ace 拒絕支援 Postgres（160 👍），維護者認為「沒人想花好幾個月做」 | 從第一天就有 adapter 介面，不要先做 PG 再補其他。 |
| Q6 | **遵守 macOS 慣例** | HN：Cmd+W 要關 tab 而不是關視窗；qStudio 被批「不符合平台習慣」；RD：DataGrip 的快捷鍵和 macOS 衝突 | 預設快捷鍵照 Mac 習慣，但都可以改。 |

---

## 3. 功能 MoSCoW

> 難度：低＝數天；中＝1–3 週；高＝1 個月以上
> 證據欄的數字：GH/YT 是票數，HN 是約略提及人數，其他見 §1

### Must：v1 核心迴圈

| # | 功能 | 具體內容 | 證據 | 難度 |
|---|---|---|---|---|
| M1 | **連線管理** | SSH（直接呼叫系統的 `ssh -N -L`，沿用 `~/.ssh/config`、agent、ProxyJump）、SSL/CA、Keychain、分組資料夾、色彩標記、從連線 URL 匯入 | GH 699（SSH 約 285、分組約 265）；YT 分組 100；HN 約 50；SV 有 83% 的人每天建連線 | 中 |
| M2 | **完整的物件瀏覽** | 所有 schema 和物件類型（table、view、index、function、procedure、trigger、sequence、enum）；可唯讀查看原始碼、複製 DDL；lazy 載入加 cache；對游標下的物件做快速 describe（類似 psql 的 `\d`） | GH 非表格物件 484、複製 DDL 82；HN 的最低門檻是「顯示所有 schema」；SV 有 90% 的人每天瀏覽物件，而 **metadata 載入慢是 DataGrip 使用者第一名的效能抱怨（50%）**；YT 類似 `\d` 的指令 85+111 | 中 |
| M3 | **快速開啟與指令面板** | Cmd+P 找表和欄位；所有指令都能用鍵盤操作；快捷鍵可以改 | GH 鍵盤相關 368；HN 約 28 人（TablePlus 的 ⌘P 被點名稱讚）；RD 稱讚 DbGate「打字就能找到表」 | 低中 |
| M4 | **編輯器基本功** | CodeMirror 6、多 tab、session 還原、**未存檔內容的當機保護**、多游標與折疊（CodeMirror 內建） | GH tab 199、編輯器基本功 191；YT 104；AS 有人說「沒有 tab 就直接淘汰」；RD 抱怨卡死後丟失 query | 低中 |
| M5 | **明確的執行範圍** | 三個獨立指令：游標下語句、選取範圍、全部。執行前先高亮範圍，預設行為可以設定。正確處理 batch 語意（`GO`、`DELIMITER`、`$$`、巢狀註解） | RD 意見分歧，最高分回覆主張「別耍聰明」（56 分）；Beekeeper 因為 Run All 造成意外 UPDATE，把預設改成 Run Current；RD 抱怨 DBeaver 切分 T-SQL 後變數失效 | 中 |
| M6 | **Schema-aware 補全** | 表、欄位、關鍵字、alias 解析。大型 schema 也要夠快。**不做即時語法錯誤標示**，誤報被罵得比沒有還慘 | GH 277（Postico 第一名需求 159）；RD 上這是大家死守 DataGrip 的第一原因；HN 約 30 人；AS 有人因為大型 DB 補全不能用而跳槽 | 中（alias 為中高） |
| M7 | **結果 grid** | 虛擬化；自動加 LIMIT 並提供「載入更多」；寬表不卡；可固定欄位；值的呈現正確：NULL 與空字串分開、大整數不失真、UUID、BLOB 只預覽開頭 | YT 固定欄位約 120、顯示原始值 63；HN 最低門檻（906 列 × 60 欄就讓 Beekeeper 卡死）；RD 自動 LIMIT 22 分；GH 值的智慧呈現 93 | 中 |
| M8 | **JSON 與直式 record 檢視** | JSON tree、單列直式檢視 | GH 300（Postico JSON viewer 205）；HN 約 20 人（jsonb 顯示成 `[object Object]` 被罵）；CB #10 | 低中 |
| M9 | **複製與匯出** | 複製成 CSV/TSV/JSON/Markdown/INSERT/IN list；選取儲存格時顯示 sum、avg、count；完整結果串流匯出到檔案 | SV 有 76% 的人每天匯出；HN 約 25 人；RD 稱讚 SQL Prompt 的 grid 工具；YT 選取聚合 56；CB 第一名需求（約 1.14 萬次瀏覽） | 低中 |
| M10 | **錯誤訊息** | 原樣顯示 DB 錯誤、標出精確行號，點擊就跳過去 | RD「查詢失敗時 client 該怎麼做」的高分回覆都說：給原始錯誤就好 | 低中 |
| M11 | **安全** | production 色彩；DB 層級的唯讀；危險語句確認；**交易狀態指示，閒置未 commit 的交易要警告，並提供自動 rollback 計時**；重連時明確告知 session 狀態已遺失 | GH 安全相關約 250；HN 稱連線色彩是 killer feature；RD 有 66 則留言的討論串標題就叫「Block the use of dbeaver」，起因是它悄悄開交易鎖住正式環境；HN 有人因此停機 30 分鐘；pgAdmin 拒絕做「無聲重連」 | 中 |
| M12 | **取消查詢與每個 tab 的 timeout** | 五種 DB 各有做法（見 §6） | CB #6；SO；HN 抱怨卡死 | 中高 |
| M13 | **History 與 saved queries** | 永久保存、可搜尋、依連線分開；saved queries 存成 `.sql` 檔放在資料夾裡，可以用 git 管理 | RD 抱怨 DBeaver 關掉就清空、pgAdmin 只留 20 筆；RD 稱讚 DataGrip 的專案資料夾；HN 約 20 人；CB #12 | 低 |
| M14 | **MSSQL 多結果集與即時 PRINT 訊息** | 用 pymssql 的 `set_msghandler` 即時串流訊息 | YT 125；GH 84；RD | 中 |

### Should：v1.x，需求高、成本可控

| # | 功能 | 證據 | 難度 | 備註 |
|---|---|---|---|---|
| S1 | **Grid 內直接編輯**：修改先暫存，預覽 DML 後一次 commit | GH 290；YT 206+55+63；HN 約 25 人，也是最低門檻之一 | **高** | 需求最高但也最難。**如果你每天常改資料，建議升為 Must。** |
| S2 | 不寫 SQL 的篩選與排序（filter bar 可轉成 WHERE） | GH 265；AS；HN 約 10 人 | 中 | |
| S3 | Formatter：手動觸發、可設定、絕不自動改寫 | GH 225；RD 意見兩極（很多人寧願自己排） | 低 | 用 sql-formatter，只在本地執行 |
| S4 | FK 導覽：點 FK 跳到關聯列、顯示關聯列摘要 | HN 約 10 人但態度強烈；YT 42 | 中 | |
| S5 | Visual EXPLAIN：PG（pev2）先，再做 Couchbase，最後 MySQL/MSSQL | HN 有人說「這是我換工具 99% 的理由」；YT 47；CB #4 | 中 | |
| S6 | Query 參數（`:param` 執行時跳出輸入框） | HN；CB #7（4.6k 次瀏覽）；GH 59 | 低中 | |
| S7 | Vim 模式 | GH TablePlus 184（拖了 7 年） | 低 | **直接用現成的 `@replit/codemirror-vim`，不要自己做** |
| S8 | 長查詢完成時發 macOS 通知 | RD | 低 | |
| S9 | MCP server（唯讀，沿用 IDE 已設好的連線） | RD 有具體的正面使用案例；TablePlus 2026-03 已推出 | 中 | 有 adapter 層的話成本很低 |

### Could：有空再做

| # | 功能 | 證據 | 難度 |
|---|---|---|---|
| C1 | ER 圖 | YT 314（至今未做）；GH 175；HN 約 20 人 | 中 |
| C2 | AI chat：自帶 key 或本地模型、需手動開啟、可全域關閉 | GH 自帶模型 192；YT 自帶或本地模型約 700；但 RD 普遍懷疑 | 中高 |
| C3 | AI 的 Explain / Fix 按鈕 | GH 約 0；RD 有經驗的使用者排斥 | 低 |
| C4 | Server 監控：process list、kill session | YT 131；GH 132（MSSQL Profiler） | 中 |
| C5 | CSV/Excel 匯入 | GH 183 | 中 |
| C6 | Notebook | RD 有人懷念 Azure Data Studio 的 notebook（48 分）；HN 約 0 | 中 |
| C7 | 跨表搜尋資料 | GH 149 | 中 |
| C8 | 自訂虛擬 FK、自動推論 FK | RD（很多 DB 根本沒有 FK） | 中 |
| C9 | Table designer | GH 392（但複製 DDL 已併入 M2） | 高 |
| C10 | 編輯 function / procedure | GH 253（Postico 做了好幾年） | 高 |
| C11 | 序列化欄位解碼（PHP serialize 等） | RD | 低中 |

### Won't：v1 明確不做

| 不做的功能 | 理由 |
|---|---|
| 結果圖表 | GH 只有 8 票；Couchbase 研究判斷不值得做；HN 上只有分析師想要 |
| Schema compare / sync | GH 71；HN 約 0；YT 只佔 1.1% |
| Data compare、fake data / masking、設定同步 | 所有來源都約等於 0 |
| 即時語法錯誤標示 | RD 指出 DBeaver 的誤報比沒有更糟 |
| AI 行內補全（ghost text） | 難度高、會編造欄位名；確定性的補全更可靠 |
| 視覺化 query builder | RD 和 HN 的有經驗使用者都排斥 |
| Routine debugger | YT 約 550 票，但難度極高 |
| 備份、還原、dump | 直接用 pg_dump 等 CLI；DBeaver 依賴外部 binary 的問題反而是抱怨大戶（約 310 👍） |
| Cloud IAM / SSO 驗證 | 目前用不到（除非你有需要） |
| 同時查詢多台伺服器 | 太小眾 |
| Linux / Windows 版 | 自用 |
| 獨立的 index advisor | Couchbase 的 ADVISE 併進 EXPLAIN 即可 |
| 帳號系統、雲端同步、遙測 | 違反 Q3 |

---

## 4. Couchbase 一級支援

這部分依**你的工作需要**排優先，不看社群票數。社群的 Couchbase 聲量很小：Stack Overflow 調查的資料庫清單沒列、r/Couchbase 自 2023 年起只有 7 篇文章、官方 VS Code extension 只有 4.4K 安裝。

### Must（Couchbase 使用者會跳槽的最小功能組合）

1. **連線**：`couchbase://` 和 `couchbases://`（含 Capella DNS SRV）、內建 Capella root CA、可匯入自訂 CA、多 cluster、Keychain。
2. **Keyspace 樹**：bucket › scope › collection，加上每個 collection 的 index（狀態和定義）。點擊節點就設定 query context。要能承受大量 collection：官方 UI 有使用者回報 45 個只顯示 30 個。
3. **編輯器**：游標下語句或選取範圍執行、正確切分腳本、Cmd+Enter、可取消、每個 tab 有自己的 timeout、有型別的 named/positional 參數、query context 選擇器。
4. **結果**：串流加虛擬化 grid，搭配 JSON tree。欄位順序用回應裡的 `signature`（`SELECT *` 會變字母序）。形狀不一的文件取所有 key 的聯集。**int64 和 CAS 值不能經過 double。** 顯示 elapsed、execution、result count、size、mutations、warnings 等 metrics。設列數上限並提供「載入更多」。
5. **完整結果匯出**：CSV、JSON、JSON Lines，串流寫入檔案。
6. **用 ID 開文件**：JSON 編輯器，顯示 META（cas、expiry）；存檔時用 CAS 做樂觀檢查，並保留 expiry。
7. **一鍵 EXPLAIN**：operator tree，並標出反模式（PrimaryScan、`#sequentialscan`、index 沒涵蓋導致的 Fetch、IntersectScan），附上 ADVISE 產生的 CREATE INDEX。
8. **本地 history**：依連線分開、可搜尋、可加入最愛。

### Won't（Couchbase 部分）

圖表、叢集管理（bucket、rebalance、XDCR、eventing）、backup / cbimport / cbexport 的包裝、Analytics / Columnar / FTS / vector workbench、Sync Gateway、ER 圖、notebook、視覺化 query builder、連線時自動跑 INFER（改成手動觸發，並可設定 sample size）。

### SQL++ 寫作輔助（Should）

論壇問題的分布：index 與效能約 31%、陣列與巢狀資料約 25%、mutation 約 14%、日期約 14%。

- 補全巢狀欄位路徑，自動替特殊名稱加上 backtick（例如 `travel-sample` 或 `value` 這類保留字）。
- 在結果的 JSON tree 上點「用這個陣列欄位篩選」，自動產生 ANY/UNNEST 子句。
- 結果一律顯示 `META().id`、cas、expiration。

---

## 5. AI 策略

社群的立場：Reddit 普遍懷疑甚至敵視；約一半 SQL 使用者有在用 AI 輔助（Stack Overflow 2026）；投票集中在「讓我用自己的模型」。

原則：
1. **預設關閉、可全域關閉**。RD 有人明確要求一個開關就能關掉所有 AI；HN 上「沒辦法關掉」的抱怨有 16 則回覆。
2. **自帶 key（Claude API）或用本地模型（Ollama）**，不做內建付費 AI。
3. **預設只送 metadata**（名稱、型別、PK/FK），sample rows 預設關閉。
4. **原樣錯誤優先**，AI 解釋只是可選的按鈕。
5. AI 產生的 DML / DDL **絕不自動執行**；給 agent 用的唯讀**必須在 DB 層級強制**（唯讀角色、`default_transaction_read_only`、`statement_timeout`），不能只靠 MCP 設定或 prompt。
6. 優先做 **MCP server（S9）**：讓 Claude Code 透過 IDE 已設好的連線查資料，是社群回報最具體的正面用法。

---

## 6. 和第一輪結論的差異

| 項目 | 第一輪 | 第二輪修正 |
|---|---|---|
| MSSQL driver | 最大風險，要先 spike | **改用 pymssql 2.4.4，無卡點**：OpenSSL 內建；伺服器強制加密也能連；8 執行緒並發讀 120 萬列 0 錯誤；PyInstaller 打包實測可用。mssql-python 在本機重現了 segfault，排除。 |
| 依錯誤修 SQL（AI） | 實用度高 | **降級**：Reddit 上有經驗的使用者要的是原始錯誤加精確位置，GitHub 幾乎沒人投票 |
| 執行游標下語句 | 照 DataGrip 做 | **意見分歧**：改成三個明確指令、執行前高亮範圍、預設可設定 |
| Formatter | 低難度、值得做 | 意見兩極：只手動觸發、可設定、不是賣點 |
| Couchbase 沒有 Mac 桌面工具 | 沒有 | **有 Capella DataStudio**（Couchbase Labs，非原生，2025-01 後未更新） |
| Schema 用固定數量的查詢一次載入 | 建議 | MySQL 5.7 表很多時大量查詢 information_schema 會卡好幾分鐘，**改成依 schema lazy 載入** |
| Tauri sidecar 放在 `externalBin` | 建議 | `externalBin` 只能帶單一檔案，**PyInstaller onedir 要放在 `bundle.resources`**，用絕對路徑啟動 |
| 自用可以跳過簽章 | 可跳過 | 公證可以跳過，但 **arm64 上每個 binary 仍需有效的 ad-hoc 簽章** |
| Notebook | Phase 3 | Reddit 有懷念的聲音，但 HN、GitHub 約 0，維持 Could |
| 交易模式 | 低中、普通 | **升級**：閒置未 commit 的交易會鎖住正式環境，要做狀態指示加自動 rollback 計時 |

---

## 7. 實作注意事項

這些都是社群踩過的坑，設計時直接遵守。

### 架構與連線
- 每個 tab 一條連線、一個 worker thread。每個 profile 另有一條 metadata 連線，需要時才開、閒置就關。**不要多開連線**，DBA 會抱怨（RD）。
- 重連時要明確告知 session 狀態已遺失（temp table、交易、search_path），參考 pgAdmin 拒絕做無聲重連的理由。
- 記下 backend pid、`CONNECTION_ID()`、`@@SPID`，sidecar 當掉後可以提示清理殘留的 session。
- 開啟 TCP keepalive，經 SSH 時特別重要。
- SSH 直接呼叫系統的 `ssh -N -L`，或用 asyncssh。**避開 sshtunnel**：在 macOS 上它會讀 `~/.ssh/config` 並用 `id_rsa` 取代你指定的 key，也不支援 OpenSSH 格式的 key 和 2FA。

### 打包
- PyInstaller onedir 放在 `Contents/Resources`，用絕對路徑啟動，不用 onefile（每次啟動都要解壓）。
- `keyring.set_keyring(macOS.Keyring())` 要手動指定，打包後自動偵測 backend 會失敗。
- 加 hidden imports，例如 Couchbase 的 `pycbc_core`。psycopg3 打包後讀 uuid 欄位會失敗，要另外處理。
- arm64 上每個 Mach-O 都要有效的 ad-hoc 簽章。

### 執行與切分
- 使用者寫的 SQL 一律 `params=None` 執行，否則 `%` 必須跳脫成 `%%`。
- MySQL 執行 CALL 後也要迴圈呼叫 `nextset()`，否則會出現 "Commands out of sync"。
- 切分器要吃掉 `DELIMITER` 那一行、不送給伺服器；`GO` 只在單獨一行時才算 batch 分隔（允許 `GO n` 和行尾註解）；PG 和 T-SQL 支援巢狀 `/* */`，MySQL 不支援。

### Streaming
- psycopg：SELECT 用 named cursor 或 `stream()`；其他語句用一般 cursor 加列數上限。一般 cursor 執行時會**一次拉回全部結果**。
- MySQL：用 unbuffered cursor 搭配 tuple（dict cursor 吃約 4 倍記憶體）。**串流期間整條連線都被佔住**。
- Couchbase：增量解析回應，錯誤可能在一部分 rows 之後才出現。

### 型別
- 大整數和 Decimal 以字串傳到前端。**FastAPI 的 `jsonable_encoder` 會把 Decimal 轉成 float**，要自己序列化。
- MySQL 的 DATE 和 DATETIME 以字串讀取，否則 `0000-00-00` 會變成 None，存回去就覆蓋成 NULL。
- MSSQL 的 GUID 要用 `uuid.UUID(bytes_le=…)` 解碼；bytea 和 BLOB 只顯示截斷的 hex 預覽，需要時再讀完整值。

### MSSQL（pymssql）
- `pymssql.connect(..., autocommit=True)`：預設值會偷偷送出 `BEGIN TRAN`。
- import 時設定 `_mssql.min_error_severity = 11`，否則嚴重度 6–10 的訊息會中斷整個 batch。
- app 自己產生 `freetds.conf`（每個連線一個 section，設定 `encryption = require` 或 `strict` 和 `ca file`），並設好 `FREETDSCONF`。
- 取消用三層策略：
  1. fetch 迴圈之間檢查取消旗標。
  2. 用 `set_wait_callback` 每 100ms 檢查一次，在**同一個執行緒**上呼叫 `cancel()`。
  3. 都不行就關閉 socket，再重連並警告使用者。

  **絕對不要跨執行緒呼叫 `cancel()`**，實測在串流中會卡死。
- 不要用 `timeout=` 參數：它是整個 process 共用的，觸發後連線就斷了。
- 欄位型別名稱從 `sys.dm_exec_describe_first_result_set` 查。sql_variant 會回傳 raw bytes，以 hex 顯示並提供 CAST。

### Couchbase
> 2026-10-18：已由 ④ 的設計取代（不用 Python SDK，改用 Query Service REST；見 docs/superpowers/specs/2026-10-18-04-couchbase-design.md）。

- 建議用 Python SDK（4.x）：KV 的 get/replace 含 CAS、`preserve_expiry`、Capella CA、DNS SRV 都內建。
- 查詢一律帶 `QueryOptions(timeout=…, client_context_id=…)`。SDK 預設 75 秒 timeout，伺服器端另有自己的 timeout，兩者都要在 UI 上顯示。取消用 `client_context_id` 從 `system:active_requests` 刪除。
- 沒有 primary index 就無法列出文件，開文件改用 `USE KEYS`。
- 互動式 SQL++ 交易（BEGIN WORK）需要固定同一個 query node，v1 先不支援。

---

## 8. 你的使用習慣（2026-10-15 回覆）

社群排名只能當參考，以下才是決定 v1 範圍的依據：

| 問題 | 回覆 | 對範圍的影響 |
|---|---|---|
| 現在用的工具 | Navicat、Beekeeper、DBeaver、官方工具與 CLI | v1 要能取代這些工具的日常使用 |
| 最不爽的地方 | 又重又慢、當機或丟失工作、Couchbase 很難用、要切換好幾個工具 | 和社群的 Q1、Q2、Q5 一致；Couchbase 和「一個工具搞定 5 種 DB」是核心價值 |
| Grid 內直接編輯 | **升為 Must** | S1 → Must |
| 實際常用的進階功能 | **SQL dump**、**跨 DB 傳輸或複製資料**、**ER 圖 / Data Modeler** | 從 Won't / Could 升為 v1 範圍（細節在 v1 拆分時決定） |
| AI | **只做 MCP server** | S9 → v1；內建 AI chat、explain、fix 都不放進 v1 |
| Couchbase | **自架 7.x 以上** | 有 scope 和 collection；Capella 專屬的部分（內建 Capella CA、Data API）先不做 |

---

## 附錄 A：MSSQL 實機檢查清單（約 10 分鐘）

這些項目無法在本機模擬，需要連到公司實際的 SQL Server 確認：

1. 用預設值連線，執行 `SELECT encrypt_option, protocol_version FROM sys.dm_exec_connections WHERE session_id=@@SPID` 和 `@@VERSION`。
2. 設定 `ca file`（公司 CA），用和憑證相符的 hostname 連線。如果只有自簽憑證，決定是否接受不驗證。
3. 如果是 SQL Server 2022 且要求 strict，試試 `encryption = strict` 的 section。
4. 執行 `WAITFOR DELAY '00:01:00'` 後按 Stop（測 callback 層），應在 1 秒內停止，之後同一條連線執行 `SELECT 1` 要正常。
5. 對大結果的 SELECT 在串流途中按 Stop（測 fetch 迴圈層）。
6. 執行 `SELECT 1; WAITFOR DELAY '00:00:30'; SELECT 2`，在第二句時按 Stop（測 socket 層），再從另一個 tab 確認 session 已被清除。
7. 執行 `BEGIN TRAN; UPDATE #t…; WAITFOR…` 後強制取消，確認 UPDATE 有被 rollback。
8. 依序執行 PRINT、`RAISERROR(...,0,1) WITH NOWAIT`、延遲、`RAISERROR(...,10,1)`、`RAISERROR(...,16,1)`，確認訊息即時、依序出現，錯誤帶有編號和行號。
9. 型別：datetimeoffset(7)、decimal(38,10)、NEWID()、varbinary(max)、sql_variant（裝 int 和字串）、約 10 萬字元的 nvarchar(max)、xml、Chinese_Taiwan collation 的 varchar 欄位。
10. 兩個 tab 同時經由實際的 TLS 連線跑大 SELECT，用 HASHBYTES 比對結果。
11. 執行 `HAS_PERMS_BY_NAME(NULL,NULL,'VIEW SERVER STATE')`，確認監控相關功能可用與否。

---

## 附錄 B：主要來源

**GitHub 與 YouTrack**
- Postico 編輯 function（253）：https://github.com/jakob/Postico/issues/72
- Postico JSON viewer（205）：https://github.com/jakob/Postico/issues/395
- Postico 補全（159）：https://github.com/jakob/Postico/issues/52
- TablePlus Vim（184）：https://github.com/TablePlus/TablePlus/issues/1237
- TablePlus MCP server：https://github.com/TablePlus/TablePlus/issues/3794
- Sequel Ace 支援 PostgreSQL（160，已拒絕）：https://github.com/Sequel-Ace/Sequel-Ace/issues/143
- DBeaver 本地 LLM（153）：https://github.com/dbeaver/dbeaver/issues/36951
- Beekeeper 預設改成 Run Current：https://github.com/beekeeper-studio/beekeeper-studio/issues/1679
- DataGrip YouTrack：https://youtrack.jetbrains.com/issues/DBE

**調查**
- State of PostgreSQL 2024：https://www.tigerdata.com/state-of-postgres/2024
- DataGrip UX Survey #2：https://blog.jetbrains.com/datagrip/2025/08/04/datagrip-and-database-tools-ux-survey-2-results/
- Stack Overflow 2026：https://survey.stackoverflow.co/2026/
- Redgate 2026：https://www.red-gate.com/solutions/state-of-database-landscape/2026/

**Reddit 與 HN**
- 「執行範圍：別耍聰明」：https://www.reddit.com/r/SQL/comments/1wd2jcb/
- 「Block the use of dbeaver」（開交易問題）：https://www.reddit.com/r/PostgreSQL/comments/1pb8apb/
- 查詢失敗時 client 該怎麼做：https://www.reddit.com/r/SQL/comments/1vqmchq/
- Agent 不要碰正式環境：https://www.reddit.com/r/PostgreSQL/comments/1sq41hi/
- HN 功能膨脹：https://news.ycombinator.com/item?id=45014852
- HN Ask：你用什麼 PostgreSQL client：https://news.ycombinator.com/item?id=23208181

**Couchbase**
- Capella DataStudio：https://capelladatastudio.com
- 論壇：https://www.couchbase.com/forums/
- JIRA：https://jira.issues.couchbase.com/

**MSSQL**
- pymssql：https://pypi.org/project/pymssql/
- pymssql 取消相關 issue：https://github.com/pymssql/pymssql/issues/629
- mssql-python 問題：https://github.com/microsoft/mssql-python/issues/668 、/679、/685、/826

> 第二輪的原始資料和腳本（issue JSON、評論、HN 留言、MSSQL 模擬伺服器）放在本次 session 的 scratchpad 暫存目錄，session 結束後可能會被清除。
