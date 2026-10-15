# 子專案 ①：地基 — 設計

> 日期：2026-10-15 ｜ 狀態：已核准（使用者授權 Claude 自主設計與審核）
> 範圍依據：[feature-priorities.md](../../research/2026-10-15-feature-priorities.md) §3（M1、M2）、§7；[landscape.md](../../research/2026-10-15-sql-ide-landscape.md) §5

## 1. 目標

建立整個 app 的骨架，讓之後 7 個子專案都能直接往上疊：

1. Tauri 2 外殼啟動並監控 Python sidecar，打包成可執行的 `.app`。
2. 5 種 DB 共用的 **adapter 介面**（第一天就照 5 種 DB 設計），先實作 **PostgreSQL** 與 **SQLite**。
3. **連線管理**：profile CRUD、密碼存 Keychain、SSH tunnel、SSL、從 URL 匯入、測試連線。
4. **物件瀏覽**：lazy 載入的物件樹，含所有物件類型與 DDL / 原始碼檢視。
5. **執行管線**：每個 session 一條連線與一個 worker thread，經 WebSocket 串流結果，支援分頁、載入更多、取消。前端先用極簡 console 驗證，② 再換成正式編輯器與 grid。

### 成功標準

- `scripts/dev.sh` 一次啟動 backend 與 Tauri dev；純瀏覽器模式（Vite + backend）也能完整操作。
- PG、SQLite 的 profile 可新增、編輯、刪除、測試；**密碼只在 Keychain，不在 JSON**；可從 URL 匯入；經 SSH bastion 可連到 PG。
- 物件樹可 lazy 展開 PG 的 database → schema → 類別資料夾 → 物件 → 欄位 / index / trigger，所有物件類型都能看 DDL；SQLite 同理。
- 極簡 console 能執行語句：結果分頁串流（每頁 500 列、可載入更多）、顯示 server 訊息與帶位置的錯誤、可取消長查詢（PG `pg_sleep`、SQLite 大型遞迴 CTE）。
- Backend 測試通過，包含對 Docker PG 的整合測試。
- `npm run build:app` 產生的 `DbaGuy.app` 可以啟動、連線，結束時 sidecar 也跟著結束。
- 量測並記錄冷啟動時間與閒置記憶體（目標：2 秒內、300MB 以下）。

### 不在範圍內

CodeMirror 編輯器、正式的結果 grid、history、安全機制、補全、Cmd+P（以上都是 ②）；MySQL、MSSQL（③）；Couchbase（④）；grid 內編輯（⑤）。

## 2. 方案比較

| 方案 | 評估 | 結論 |
|---|---|---|
| **A. Tauri 2 + Python sidecar** | 體積小、原生選單與快捷鍵；符合使用者的 Python 技術棧；可用 sqlglot、DuckDB、MCP SDK | **採用** |
| B. Electron + Node drivers | MSSQL 有純 JS 的 tedious；但較重（違反品質需求 Q1）、偏離 Python 技術棧 | 不採用 |
| C. pywebview + Python 同一個 process | 最簡單、沒有 sidecar 生命週期問題；但原生整合弱、Mac 質感差 | 不採用 |

另外兩個關鍵決策：

| 決策 | 選項 | 結論與理由 |
|---|---|---|
| 並發模型 | (a) async driver 跑在 event loop；(b) **每個 session 一個 worker thread，driver 一律同步** | **(b)**。pymssql 與 sqlite3 都要求連線綁定單一執行緒，MSSQL 研究也證實「一連線一執行緒」最安全；統一同步寫法讓 adapter 更單純。 |
| 前後端傳輸 | (a) **前端直接走 HTTP + WebSocket 到 127.0.0.1**；(b) 全部經 Tauri IPC 由 Rust 轉送 | **(a)**。延遲低、Rust 端極薄；純瀏覽器也能開發與驗證。用每次啟動隨機產生的 token 防止其他程式存取。 |

## 3. 架構

### 3.1 Process 模型

```
DbaGuy.app
 ├─ Tauri（Rust）主程序 ── WKWebView（React UI）
 │     │ spawn：stdin pipe（watchdog）、env DBAGUY_TOKEN、DBAGUY_PORT=0
 │     ▼
 └─ dbaguy-backend（PyInstaller onedir，位於 Contents/Resources/backend/）
       FastAPI 綁定 127.0.0.1:<隨機 port>
       就緒時在 stdout 印出一行 {"event":"ready","port":N}

UI ──HTTP（Authorization: Bearer <token>）／WS（?token=<token>）──► backend
```

- **Token**：Rust 產生 32 bytes 隨機 hex，以環境變數傳給 sidecar；前端透過 Tauri command `backend_info` 取得 `{base_url, token}`。
- **Watchdog**：sidecar 用一條執行緒讀 stdin，讀到 EOF（父程序消失）就自行結束。
- **意外結束**：Rust 偵測到 sidecar 結束時，發出 `backend://exited` 事件；UI 顯示橫幅與「重新啟動 backend」按鈕（呼叫 `restart_backend` command）。尚未存檔的編輯內容在前端，不會遺失。
- **開發模式**：若設定了 `DBAGUY_DEV_BACKEND_URL` 與 `DBAGUY_DEV_BACKEND_TOKEN`，Rust 不 spawn sidecar，直接使用外部的 `uvicorn --reload`。
- **純瀏覽器模式**：前端偵測不到 Tauri 時，改讀 `VITE_BACKEND_URL` 和 `VITE_BACKEND_TOKEN`。這讓 Claude 能用內建瀏覽器驗證 UI。

### 3.2 Repo 結構

```
dbaguy/
├─ package.json            # 前端與 Tauri CLI 指令
├─ index.html, vite.config.ts, tsconfig.json
├─ src/                    # React 前端
├─ src-tauri/              # Rust 外殼
├─ backend/                # Python sidecar（poetry，venv 放在專案內）
├─ dev/                    # docker-compose.yml、seed SQL
├─ scripts/                # dev.sh、build-backend.sh、gen-api.sh
└─ docs/
```

### 3.3 Backend 模組

```
backend/dbaguy_backend/
├─ __main__.py         # 進入點：設定、logging、watchdog、啟動 uvicorn、印出 ready
├─ app.py              # create_app()：FastAPI factory、router、middleware、例外處理
├─ config.py           # Settings（pydantic-settings）：token、port、data_dir、log_level
├─ logging_setup.py    # loguru：stderr + 輪替檔案；攔截 stdlib / uvicorn logging
├─ domain/             # 純資料模型，不依賴任何 driver
│  ├─ profiles.py      # Engine、ConnectionProfile（依 engine 的 discriminated union）、SslConfig、SshConfig
│  ├─ metadata.py      # ObjectPath、ObjectNode、ObjectDetail
│  ├─ execution.py     # ColumnMeta、ServerMessage、執行事件與 WS 指令
│  └─ errors.py        # 領域例外
├─ storage/
│  ├─ paths.py         # app 資料目錄
│  ├─ profile_store.py # JSON 檔案 repository（atomic write）
│  └─ secret_store.py  # SecretStore 介面、KeyringSecretStore、InMemorySecretStore（測試用）
├─ connectivity/
│  ├─ ssh_tunnel.py    # SshTunnelManager：呼叫系統 ssh、ref count、askpass
│  └─ askpass.py       # SSH_ASKPASS helper 模式
├─ adapters/
│  ├─ base.py          # DatabaseAdapter、DatabaseConnection、StatementResult 介面
│  ├─ registry.py      # engine → adapter；URL scheme → adapter
│  ├─ values.py        # 共用的 JSON-safe 值編碼
│  ├─ postgres/        # adapter.py、introspection.py、ddl.py、loaders.py
│  └─ sqlite/          # adapter.py、introspection.py
├─ sessions/
│  ├─ worker.py        # SessionWorker：執行緒 + 指令佇列，驅動分頁與事件
│  └─ manager.py       # SessionManager：建立、查詢、關閉、閒置清理
├─ services/
│  ├─ connection_service.py  # 解析 endpoint（直連或 tunnel）、開連線、測試連線
│  └─ metadata_service.py    # 每個 (profile, database) 一個 metadata session，閒置自動關閉
└─ api/
   ├─ deps.py, errors.py
   ├─ profiles.py      # CRUD、URL 匯入、測試連線
   ├─ metadata.py      # children、object detail
   ├─ sessions.py      # 建立/關閉 session、WS 執行通道
   └─ system.py        # health、版本
```

相依方向：`api → services/sessions → adapters → domain`；`storage`、`connectivity` 只被 services 使用。新增 engine 只需新增一個 adapter 套件並在 registry 註冊（開放封閉原則）。

### 3.4 Frontend 模組

```
src/
├─ main.tsx, App.tsx, styles.css
├─ lib/
│  ├─ backend.ts        # 取得 base URL 與 token（Tauri invoke 或 env）
│  ├─ api/
│  │  ├─ schema.d.ts    # 由 OpenAPI 產生（openapi-typescript），不手寫
│  │  ├─ client.ts      # openapi-fetch client + 驗證 header
│  │  └─ session-socket.ts  # 執行通道的 WebSocket 包裝（重連、事件分派）
├─ stores/workspace.ts  # zustand：選中的連線、物件、開啟的 tab
├─ features/
│  ├─ connections/      # 側欄連線清單（依資料夾分組）、連線對話框、hooks
│  ├─ explorer/         # 虛擬化 lazy 物件樹、物件 DDL 檢視
│  └─ console/          # 極簡 console（textarea + 執行 + 簡易表格），② 會整個替換
└─ components/ui/       # Radix + Tailwind 的小型元件（Button、Input、Dialog、Select、Tabs…）
```

- **型別單一來源**：TS 型別一律由 backend 的 OpenAPI 產生（`npm run gen:api`）。WS 事件模型透過一個只供 schema 用的 route（`GET /_schema/execution-events`）納入 OpenAPI。
- **UI 風格**：桌面式高密度（13px、緊湊列高）、系統字型、跟隨系統深淺色；視窗用 overlay 標題列，頂部留出 drag region。
- **主要套件**：React 19、Vite 8、TypeScript、Tailwind 4、zustand 5、TanStack Query 5、TanStack Virtual 3、react-resizable-panels、radix-ui、lucide-react、openapi-fetch、openapi-typescript。

## 4. 核心介面

### 4.1 Connection profile

```python
class Engine(StrEnum): postgres, mysql, mssql, couchbase, sqlite

class SslConfig(BaseModel):
    mode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    ca_file: str | None; cert_file: str | None; key_file: str | None

class SshConfig(BaseModel):
    enabled: bool = False
    host: str            # 可以是 ~/.ssh/config 的 alias
    port: int | None; user: str | None; identity_file: str | None
    auth: Literal["agent_or_key", "password"] = "agent_or_key"   # password 存 Keychain

class ProfileBase(BaseModel):
    id: UUID; name: str; engine: Engine
    folder: str = ""     # 以 "/" 分隔的資料夾路徑
    color: Literal["none", "green", "yellow", "orange", "red", "purple"] = "none"
    read_only: bool = False      # ② 會依此強制唯讀
    save_password: bool = True

class ServerProfile(ProfileBase):    # postgres / mysql / mssql / couchbase 共用
    host: str; port: int | None; database: str | None; username: str | None
    ssl: SslConfig; ssh: SshConfig
    options: dict[str, str] = {}     # engine 專屬的額外參數

class SqliteProfile(ProfileBase):
    engine: Literal[Engine.sqlite]; path: str
```

- 持久化：`profiles.json`（`{"version": 1, "profiles": [...]}`），atomic write（寫入暫存檔後 rename）。
- 密碼：keyring，service 為 `app.dbaguy`，帳號為 `profile:<id>:password` 或 `profile:<id>:ssh_password`。
- URL 匯入：每個 adapter 實作 `parse_url(url) -> ProfileDraft`，registry 依 scheme 分派（`postgres://`、`postgresql://`、`sqlite:///path`；之後加入 `mysql://`、`mssql://`、`couchbase://`）。

### 4.2 Adapter 介面

```python
class DatabaseAdapter(ABC):
    engine: ClassVar[Engine]
    url_schemes: ClassVar[tuple[str, ...]]
    def parse_url(self, url: str) -> ProfileDraft: ...
    def open(self, profile: Profile, endpoint: Endpoint, password: str | None,
             database: str | None) -> DatabaseConnection: ...

class DatabaseConnection(ABC):
    """除了 cancel() 以外，所有方法只能在擁有它的 worker thread 上呼叫。"""
    def server_info(self) -> ServerInfo: ...
    def current_database(self) -> str | None: ...
    def list_children(self, path: ObjectPath) -> list[ObjectNode]: ...
    def object_detail(self, path: ObjectPath) -> ObjectDetail: ...
    def run(self, sql: str) -> StatementResult: ...      # 一次只執行一個語句或 batch
    def in_transaction(self) -> bool: ...
    def cancel(self) -> None: ...      # 任何執行緒都可呼叫；實際機制由 adapter 決定
    def close(self) -> None: ...

class StatementResult(ABC):
    def columns(self) -> list[ColumnMeta] | None: ...   # None 表示目前結果集不回傳列（DML/DDL）
    def fetch(self, n: int) -> list[list[Cell]]: ...    # 回傳已編碼的 cell；空 list 表示讀完
    def affected_rows(self) -> int | None: ...
    def next_result(self) -> bool: ...                   # 移到下一個結果集（MSSQL、MySQL CALL）
    def take_messages(self) -> list[ServerMessage]: ...
    def close(self) -> None: ...                          # 提前結束：取消並清空剩下的結果
```

- **分頁與事件由 `SessionWorker` 通用處理（DRY）**；adapter 只提供上述基本操作。
- `ObjectPath` 是 `list[{kind, name}]`，例如 PG 的 `[database:app, schema:public, folder:tables, table:users]`。`folder` 是 adapter 產生的分類節點。
- `ObjectNode`：`kind`、`name`、`path`、`has_children`、`system`（系統物件，UI 淡化顯示）、`detail`（例如欄位型別、列數估計）。
- `ObjectDetail`：`ddl`（文字）、`ddl_is_generated`（表格 DDL 是重組出來的，標示為近似值），以及選用的 `columns`。
- 之後的 engine 也套用同樣的介面：MSSQL 是 server → database → schema → …；Couchbase 是 bucket → scope → collection → index。Couchbase 的文件結果在 ④ 可能需要擴充欄位變動事件，屆時再擴充協定。

### 4.3 值編碼（`adapters/values.py`）

Cell 一律是 JSON-safe：

| 原始值 | 編碼 |
|---|---|
| NULL | `null` |
| bool | `true` / `false` |
| 整數，\|v\| ≤ 2^53−1 | JSON number |
| 更大的整數、Decimal、numeric | **字串** |
| float | JSON number（NaN 和 Infinity 改成字串） |
| 文字 | 字串；超過 64 KiB 截斷為 `{"$type":"text","preview":…,"size":n,"truncated":true}` |
| bytes | `{"$type":"bytes","size":n,"preview":"<前 64 bytes 的 hex>"}` |

PG 採 **text-first** 載入：除了 bool、int2、int4、float4、float8 與 bytea 之外，全部用 psycopg 的 text loader 載入，包括 int8、numeric、json、jsonb、日期時間、陣列。好處：
- 大數字不失真。
- JSON 保留原始格式與 key 順序。
- `infinity` 和西元前的日期這類 Python 無法表示的值也不會出錯。

`ColumnMeta`：`name`、`type_name`（DB 原生型別名）、`category`（`number` / `text` / `bool` / `datetime` / `binary` / `json` / `other`，供 UI 對齊與渲染）。

### 4.4 Session 與執行協定

- `POST /sessions {profile_id, database?}` → `{session_id}`；建立 `SessionWorker`（專屬執行緒），並在該執行緒開啟連線。
- `DELETE /sessions/{id}` 關閉；**超過 10 分鐘沒有任何 WS 連著的 session 會自動關閉**。
- `WS /sessions/{id}/ws?token=…`，JSON 訊息：

```
client → server
  {"type":"execute",    "request_id":"…", "sql":"…", "page_size":500}
  {"type":"fetch_more", "request_id":"…", "count":500}
  {"type":"cancel",     "request_id":"…"}
  {"type":"discard",    "request_id":"…"}     # 不要剩下的列了

server → client
  {"type":"started",        "request_id"}
  {"type":"result_set",     "request_id", "index", "columns":[…]}
  {"type":"rows",           "request_id", "index", "rows":[[…]]}
  {"type":"result_set_end", "request_id", "index", "row_count", "has_more":bool}
  {"type":"affected",       "request_id", "index", "rows":n}
  {"type":"message",        "request_id", "severity", "text"}
  {"type":"error",          "request_id", "message", "code"?, "position"?:{"line","column"}}
  {"type":"finished",       "request_id", "elapsed_ms", "in_transaction":bool, "cancelled":bool}
```

- **暫停中的結果集會佔住連線**（PG 用 `cursor.stream()`，MySQL 是 unbuffered，行為一致）。同一 session 上任何新指令都會先 discard 它。
- `cancel` 由 worker 以外的執行緒呼叫 `DatabaseConnection.cancel()`：PG 用 `cancel_safe()`，SQLite 用 `interrupt()`。
- 錯誤位置：PG 的 `statement_position`（字元 offset）換算成語句內的行和列，由前端加上語句在編輯器中的 offset。
- `in_transaction`：每次執行後回報（PG 用 `info.transaction_status`，SQLite 用 `in_transaction`），給 ② 的交易指示使用。連線預設 autocommit。

### 4.5 Metadata

- `POST /metadata/children {profile_id, path}` → `ObjectNode[]`
- `POST /metadata/detail {profile_id, path}` → `ObjectDetail`
- `MetadataService` 為每個 `(profile_id, database)` 維持一個 metadata session（同樣使用 `SessionWorker`），閒置 5 分鐘自動關閉；不另外開連線池。
- ① 不做 schema 快取。② 做補全與 Cmd+P 時再設計。

**PG 物件樹**：server → database（每個 database 用自己的 metadata 連線）→ schema（系統 schema 排在最後並淡化）→ 類別資料夾（Tables、Partitioned tables 併入 Tables、Views、Materialized views、Functions、Procedures、Sequences、Types；以一個彙總查詢**隱藏空資料夾**）→ 物件 → Columns、Indexes、Triggers。

**PG DDL**：
- view、materialized view：`pg_get_viewdef`
- function、procedure：`pg_get_functiondef`
- index：`pg_get_indexdef`
- trigger：`pg_get_triggerdef`
- sequence：從 `pg_sequences` 組出
- type：enum、domain、composite 分別組出
- **table：用欄位定義、`pg_get_constraintdef`、`pg_get_partkeydef`、非 constraint 的 index 與 comment 組出，標示為近似值**

**SQLite 物件樹**：檔案 → schema（main 與 ATTACH 進來的）→ Tables、Views、Indexes、Triggers → 欄位（`pragma_table_xinfo`）與 index。DDL 直接取 `sqlite_schema.sql`。

### 4.6 連線細節

- **PG**：`psycopg.connect(autocommit=True, connect_timeout=10, application_name="DbaGuy", keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3, sslmode=…, sslrootcert=…, sslcert=…, sslkey=…)`；用 `add_notice_handler` 收集 NOTICE；使用者 SQL 一律不帶參數執行，避免 `%` 跳脫陷阱。
- **SQLite**：`sqlite3.connect(uri=True, isolation_level=None)`；唯讀時用 `mode=ro`；連線後執行 `PRAGMA foreign_keys=ON`。
- **SSH tunnel**：
  - 呼叫系統的 `ssh -N -L 127.0.0.1:<free_port>:<db_host>:<db_port> [-p port] [-l user] [-i identity] <host>`，搭配 `-o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o BatchMode=<依 auth 決定>`，沿用使用者的 `~/.ssh/config`、agent 與 ProxyJump。
  - **密碼驗證**：設定 `SSH_ASKPASS=<backend 執行檔> --askpass`、`SSH_ASKPASS_REQUIRE=force`，密碼以一次性環境變數傳入 askpass 子程序。
  - 同一個 profile 的多個 session 共用一條 tunnel（ref count），最後一個 session 關閉後結束 ssh。
  - 透過輪詢本地 port 確認 tunnel 就緒，逾時 10 秒；ssh 的 stderr 收進錯誤訊息。
  - **不使用 sshtunnel 或 paramiko**（研究 §7：sshtunnel 有讀錯 key、不支援 OpenSSH 格式 key 與 2FA 等問題）。

## 5. 錯誤處理與 log

- **API 錯誤格式**一致：`{"error": {"code", "message", "detail"?}}`。由例外處理器把領域例外對應到 HTTP 狀態碼：
  - 404：`ProfileNotFound`
  - 422：驗證錯誤
  - 502：`ConnectionFailed`、`TunnelFailed`
  - 409：`SessionBusy`
- **Log 分層**（loguru）：
  - **ERROR**：未預期的例外，附 traceback。
  - **WARNING**：連線、tunnel 失敗、取消逾時。
  - **INFO**：生命週期，如啟動、session 開關、tunnel 開關。
  - **DEBUG**：SQL 文字（截斷到 2,000 字）與每次執行耗時。
- **Log 輸出**：
  - Backend：stderr，加上 `~/Library/Logs/DbaGuy/backend.log`（10MB × 5 輪替）。
  - Rust 外殼：用 `tauri-plugin-log` 寫入同一個目錄的 `shell.log`。
- **絕不記錄密碼**：profile 經 `model_dump(exclude=…)` 才進 log。
- **CORS** 只允許 `tauri://localhost` 與 `http://localhost:1420`（Vite dev）。**CSP** 的 `connect-src` 只開放 `http://127.0.0.1:*` 與 `ws://127.0.0.1:*`。

## 6. 打包

- `scripts/build-backend.sh`：
  1. `poetry install`
  2. PyInstaller **onedir**，hidden imports 包含 `keyring.backends.macOS` 與 psycopg 的 binary 實作。
  3. 輸出到 `src-tauri/resources/backend/`。
- `tauri.conf.json`：`bundle.resources` 帶入整個 backend 資料夾。研究發現 `externalBin` 只能帶單一檔案，所以不用它。Rust 從 `resource_dir()/backend/dbaguy-backend` 啟動。
- **簽章**：macOS 用 ad-hoc 簽章（`signingIdentity: "-"`），PyInstaller 也會替收集到的 binary 做 ad-hoc 簽章。不做公證（自用）。
- `keyring.set_keyring(macOS.Keyring())` 手動指定 backend，因為打包後自動偵測會失敗。
- 已知限制：每次重新 build 後簽章會變，Keychain 可能再跳一次「允許存取」。

## 7. 測試與驗證

依使用者指示，只測容易出錯的部分。

- **pytest**：
  - 值編碼
  - URL 解析
  - profile store（暫存目錄）
  - SQLite adapter（暫存檔）
  - **PG adapter 整合測試**（Docker PG；沒有 Docker 就 skip）
  - **SSH tunnel 整合測試**（Docker 的 openssh bastion，密碼驗證，順便驗證 askpass）
  - API smoke test（httpx ASGI 搭配 InMemorySecretStore）
- **前端**：TypeScript 型別檢查與 build；用內建瀏覽器手動走一遍主要流程。
- **打包**：build `.app`，啟動、連線、結束，確認沒有殘留的 sidecar 程序，並量測冷啟動與記憶體。
- **`dev/docker-compose.yml`**：
  - `postgres:17`（port 54317），seed 涵蓋：
    - 兩個 schema、FK、view、materialized view、function、procedure、sequence
    - enum、domain、composite type、trigger、partitioned table
    - jsonb、bytea、大 numeric、timestamptz、陣列
    - 10 萬列的串流測試表
  - `openssh-server` bastion（port 2222，帳號 dev/dev），和 PG 放在同一個 network。

## 8. 風險

| 風險 | 對策 |
|---|---|
| WKWebView 從 `tauri://localhost` 呼叫 `http://127.0.0.1` 可能被 mixed content 或 CORS 擋下 | 實作計畫第一批任務就驗證；不行就改由 Rust 轉送（設計上只要換掉 `lib/backend.ts`） |
| psycopg `stream()` 在 DML 或特定指令上的行為（rowcount、無法在 extended protocol 執行的指令） | 整合測試涵蓋 SELECT、DML、DDL、VACUUM 類指令；必要時依語句開頭的 keyword 改用 `execute()` |
| PyInstaller 加上 Tauri 的巢狀簽章 | 打包任務實測；失敗時改成 build 後再用 `codesign --deep -s -` 處理 |
| Keychain 存取在打包版跳出提示 | 已知、可接受（自用） |
