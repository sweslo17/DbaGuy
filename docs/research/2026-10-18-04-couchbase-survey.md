# ④ Couchbase — technical survey (groundwork for the design spec)

> Date: 2026-10-18. Scope: self-hosted Couchbase Server 7.x (feature-priorities §4, §7 "Couchbase", §8).
> Everything marked **[probe]** was measured on this Mac (Apple Silicon, 16 GB, macOS 26, OrbStack 7.8 GiB) against a
> temporary container `dbaguy-cb-probe` running `couchbase/server:community-7.6.2` and then
> `couchbase/server:enterprise-7.6.12`, with Python 3.13.5 and `couchbase==4.6.3` in a scratch venv. The container was
> removed afterwards. Probe scripts lived in the session's scratchpad (`cb-probe/*.py`) and are not in the repo.
> Nothing in the repo was changed.

---

## 0. Top findings and recommendations

1. **Do not run queries through the Python SDK.** Its query path (4.6.3) is not usable for an IDE:
   - **It buffers the whole response before the first row.** 1M rows / 226 MB: first row after **5.1 s**, RSS **1.15 GB**
     (raw HTTP: first byte after 10 ms). 24k travel-sample routes: first row after 638 ms, RSS stays at about 1.1 GB.
   - **It re-encodes every row** through its C++ JSON library. Object keys come out sorted alphabetically, the
     `signature` comes out sorted too (so it no longer gives column order), decimals become doubles (`1.10` → `1.1`,
     `1E+3` → `1000.0`), and integers past uint64 become doubles.
   - **It aborts the whole process** (`libc++abi: terminating … std::runtime_error: invalid double value`) when a row
     contains a number outside double range, such as `1e400`, `-1e309` or a 400-digit integer. That breaks Q2: one
     odd document would kill every session.
   - **Signature and metrics only arrive after the last row**, so the grid could not use them for the first page.
   - **Cancel from another thread is weak.** The internal `cancel()` leaves the request running on the server.
     `DELETE FROM system:active_requests` needs `admin` (CE) or `query_system_catalog` (EE). Only closing the whole
     `Cluster` stops the server-side request.
2. **Recommended: the SQL++ REST API over stdlib `http.client`, with an incremental response parser** (prototype in
   `cb-probe/httpq.py`, about 120 lines):
   - First row in 5–23 ms; 1M rows / 226 MB in 3.7 s (272k rows/s); RSS flat at about 21 MB; no new dependencies.
   - Rows arrive as the server's exact text, in the server's key order, and `signature` arrives *before* `results`.
   - Cancel = shut down the socket from any thread: 0 ms, and the server marks the request `closed`. This works for
     every user.
   - Pausing after a page relies on TCP backpressure, and memory stays flat.
3. **Opening and saving documents.** Two workable designs. **This needs a user decision (see §6.4).**
   - **(B) SQL++ only, recommended for v1:**
     - Open: `SELECT META(d).cas, META(d).expiration, META(d).type, d FROM ks d USE KEYS $id`. This returns exact bytes
       and needs no index.
     - Save: `UPDATE ks AS d USE KEYS $id SET d = $doc WHERE META(d).cas = $cas RETURNING META(d).cas` with
       `preserve_expiry=true`.
     - Side effects of saving: the stored document's keys get sorted, decimals are normalized, and integers past int64
       are corrupted. A guard has to block that last case. Binary documents are read-only.
   - **(C) Lazy-load the SDK for document open and save only:**
     - Saves are byte-exact and binary-safe.
     - Costs about 15 MB in the frozen app and about 24 MB RSS once used.
     - KV does not work through an SSH tunnel to a multi-node cluster.
4. **Server-side read-only works:** send `readonly=true` (§3.6 lists what it rejects). Couchbase is a
   `server_read_only` engine.
5. **v1 has no transactions.** Without a `txid`, `BEGIN WORK` opens a transaction that nothing else joins, and
   `COMMIT`/`ROLLBACK`/`SAVEPOINT` fail with error 17002. Classify them as TRANSACTION and refuse them with a clear
   message.
6. **Server bugs and pitfalls to design around:**
   - On 7.6.2 CE, `INFER` on a collection that holds binary documents **crashed the query service** (Go panic, process
     restart).
   - On 7.6.12 EE, the same `INFER` silently returns 0 rows, and `ADVISE` returns error 5001 "Panic: interface
     conversion".
   - Since 7.6, a collection without any index is read with a sequential scan instead of failing. EXPLAIN shows
     `index: "#sequentialscan"`, and in EE the user needs the `query_use_sequential_scans` role.
7. **Edition matters for the dev environment.** Community Edition lacks `ADVISE` and `UPDATE STATISTICS` (error 3230
   "enterprise level feature"), CBO cost estimates, `profile` output (silently ignored), and all RBAC roles except
   `admin`, `ro_admin` and `bucket_full_access`. To develop the EXPLAIN/ADVISE and limited-user flows we need the EE
   image. **This needs a user decision (licensing, §9).**
8. **sqlglot has no N1QL/SQL++ dialect** (30.23.0 is the latest). Classify with keyword rules. The frontend's
   `sql-formatter` 15.9 has an `n1ql` language, but **it breaks `'it''s'` into `'it' 's'`**. A token-equality guard
   after formatting is needed.

---

## 1. SDK choice and footprint (Q1)

### 1.1 The Python SDK

| Item | Value | Source |
|---|---|---|
| Latest stable | **4.6.3, 2026-08-25** (4.6.0: 2026-03-31; there is also a 4.5.1 backport from 2026-08-21) | PyPI JSON `pypi.org/pypi/couchbase/json` |
| Wheel | `couchbase-4.6.3-cp313-cp313-macosx_11_0_arm64.whl`, 4.94 MB; also cp314 | PyPI |
| Dependencies on 3.13 | **none** (`typing-extensions` only below 3.13; `opentelemetry` only with the `otel` extra) | PyPI `requires_dist` |
| Installed size | 18 MB: `couchbase/logic/pycbc_core/_core.so` is 13.5 MB (BoringSSL statically linked; links only libz, libSystem, libc++), plus 556 KB of tests | [probe] `du`, `otool -L` |
| Import time | about 115–135 ms warm (`couchbase.cluster` + options + auth); 830 ms on the first run while .pyc files are written | [probe] `-X importtime` |
| RSS | bare Python 14 MB → 38 MB after import (+24 MB) → 39.7 MB after connect and one query (+2 MB) | [probe] `connect_probe.py` |
| Threads | 5 OS threads per process after `Cluster()` (C++ IO) | [probe] |
| Connect | `Cluster()` 32 ms, `wait_until_ready` 46 ms, first query 9 ms (localhost) | [probe] |
| Default query timeout | 75 s (C++ `timeout_defaults::query_timeout`) | `couchbase/logic/cluster_impl.py:241` |

### 1.2 How the SDK's query path behaves [probe]

- **Buffering** (`sdk_ttfr.py`, `sdk_bench.py`):

  | Rows (≈220 B each) | SDK first row | SDK total | SDK RSS at first row | curl first byte | curl total |
  |---|---|---|---|---|---|
  | 10k | 51 ms | 62 ms | 57 MB | 8 ms | 47 ms |
  | 100k | 460 ms | 506 ms | 180 MB | 3 ms | 303 ms |
  | 300k | 1,324 ms | 1,440 ms | 452 MB | 10 ms | 849 ms |
  | 1M | 5,095 ms | 5.5 s | **1,153 MB** | 10 ms | 3.4 s |

  The time to the first row grows with the size of the whole result, so the SDK reads everything before it yields a
  row. RSS stayed at 0.9–1.1 GB after the result was consumed.
- **Re-encoding.** Rows reach Python as bytes that the C++ core has *re-serialized*. `RawSerializer` (a deserializer
  that returns its input unchanged) shows this:
  - Server sent: `{"name":"Ann",…,"dec":1.10,"pi":3.14159265358979323846…,"sci":1E+3,"key order":{"z":1,"a":2}}`
  - SDK gave: `{"beyond":12345678901234567890,"big":…,"dec":1.1,…,"key order":{"a":2,"z":1},…,"pi":3.141592653589793,"sci":1000.0,…}`

  Every object is key-sorted, and the signature dict is sorted the same way (`{'cas','expiration','id','name'}` for
  `SELECT META(u).id, META(u).cas, …`).
- **Process abort.** `SELECT *` or `SELECT RAW` of a document containing `1e400`, `-1e309` or a 400-digit integer
  aborts the Python process. `1e-400` silently becomes `0.0`, and a 40-digit integer becomes `1.2345678901234568e39`.
- **Metadata timing.** `QueryMetaData` (signature, metrics, warnings) is set only on `StopIteration`
  (`couchbase/logic/streaming.py::stream_next`).
- **Metrics are off by default.** The SDK sets `metrics=False` unless `QueryOptions(metrics=True)`
  (`couchbase/logic/n1ql.py::create_query_object`).
- **The KV path is fine.** `collection.get(..., transcoder=RawJSONTranscoder())` returns the stored bytes untouched,
  even for `1e400`.

### 1.3 Alternatives

| Option | Size and RSS | Rows: streaming and fidelity | Cancel | KV with CAS | DNS SRV | TLS | SSH tunnel |
|---|---|---|---|---|---|---|---|
| **A. SDK for everything** | +15 MB frozen, +24 MB RSS | ✗ buffered, re-encoded, can abort the process | close the Cluster, or DELETE (admin only) | ✓ exact | ✓ | BoringSSL, own trust settings | ✗ multi-node (cluster map gives internal addresses; needs server-side alternate addresses) |
| **B. REST for query and metadata, SQL++ for documents** | **0 MB**, no deps | ✓ streaming, exact bytes, key order, signature first | socket close (any user) | emulated with `UPDATE … WHERE META().cas = $cas` (keys sorted, numbers normalized) | ✗ (would need dnspython) | stdlib `ssl` + profile CA (tested on 18093 with the cluster CA, TLS 1.3) | ✓ forward one query node (8093/18093), as for PG/MySQL |
| **C. B + SDK lazily for open/save by ID** | +15 MB frozen; +24 MB RSS only after the first document is opened | as B | as B | ✓ exact, binary-safe | — | two TLS stacks | query ✓; KV ✗ multi-node |
| D. Pure REST, including the console's `/pools/default/buckets/<b>/scopes/<s>/collections/<c>/docs/<id>` | 0 MB | as B | as B | **✗ the endpoint ignores `cas`** (a stale `cas=1` still wrote; [probe]); internal and undocumented | ✗ | stdlib | needs 8091 too |

**Recommendation: B for v1**, keeping documents behind a small `DocumentStore` interface so that C can replace it if
byte-exact saves turn out to matter (§6.4).

Numbers for B: 0 bytes added to the bundle. Import cost is nil (`http.client`, `ssl`, `json`, `codecs` are already
loaded). First row 5–23 ms; 272k rows/s on 1M × 226 MB; RSS about 21 MB with no growth while paused. DNS SRV (mainly
Capella) is out of scope per §8.

---

## 2. PyInstaller packaging [probe]

- `pyinstaller-hooks-contrib 2026.8` has **no couchbase hook**, and none is needed. A plain onedir build of a probe that
  imports `couchbase.cluster`, connects, runs a query and does a KV get **worked frozen** with no hidden imports
  (`cb-probe/freeze/`).
- Size: baseline (stdlib json/http/ssl) 18 MB → with couchbase **33 MB (+15 MB)**. The only binary is `_core.so`
  (13.5 MB); the Python code goes into the PYZ. There are no extra dylibs because BoringSSL is linked in statically.
  `codesign -v` passes on the ad-hoc signed `.so`.
- Frozen import: 2.8 s on the first launch after a build (the usual first-launch signature check, as in CLAUDE.md),
  then 100–113 ms.
- §7's note about adding `pycbc_core` as a hidden import is outdated for 4.6: the module is now
  `couchbase.logic.pycbc_core._core` and is found by normal analysis.
- If option C is chosen: import the SDK lazily so that non-Couchbase users never pay the ~110 ms import or the 24 MB.
  Exclude `couchbase.tests`, `acouchbase`, `txcouchbase` and the otel tracer in the spec, the way `_MYSQL_LEFT_OUT`
  does today. Then verify by letting the frozen sidecar open a document from the dev cluster, as is done for MySQL.

---

## 3. Query execution

### 3.1 Wire format and streaming [probe]

`POST /query/service` (8093, or 18093 with TLS) with a JSON body. The response is one streamed (chunked) JSON object:

```
{"requestID":…, "clientContextID":…, "signature": {…} | "json" | …, "results": [ row, row, … ],
 "errors": [ {code, msg, line?, column?, reason?, retry?} ], "warnings": […], "status": "success|stopped|timeout|fatal|errors|…",
 "metrics": {"elapsedTime":"12.96s","executionTime":…,"resultCount":…,"resultSize":…,"mutationCount"?,"sortCount"?,"errorCount"?,"warningCount"?},
 "profile"?: {…}}
```

- `signature` always came **before** `results`, including on failing requests where a projection was known (for
  example `NEXT VALUE FOR` in read-only mode).
- **Errors can come after rows.** With `timeout=3s` and a 6 s client pause, the client got 53,363 rows, then
  `errors:[{code:1080,"Timeout 3s exceeded",retry:true}]`, `status:"fatal"`.
- Durations in the REST metrics are Go duration strings, which need parsing. The SDK converts them to `timedelta`.
- Prototype parser (`httpq.py`):
  - Uses an incremental UTF-8 decoder.
  - Scans the top-level object field by field.
  - Decodes each row with `json.JSONDecoder(parse_float=Decimal).raw_decode`.
  - Keeps the exact row text by slicing the buffer at the start and end positions.
  - Accepts a value only when a delimiter follows it, so a number split across chunks is never cut.
  - Handles chunked encoding through `HTTPResponse.read1()`.

### 3.2 Signature → columns [probe]

| Query | `signature` | Row shape |
|---|---|---|
| `SELECT a, b` | `{"a":"json","b":"json"}` in projection order | object, projection order |
| `SELECT *` (one keyspace) | `{"*":"*"}` | `{"<alias>": {document, stored key order}}` (passed through unparsed) |
| `SELECT u.*` | `{"*":"*"}` | document keys, **sorted by the server** |
| `SELECT META(u).id, u.*, o.amount` | `{"id":"json","*":"*","amount":"json"}` (marks where the star sits) | merged |
| `SELECT RAW x` | `"json"` / `"number"` / `"object"` (a string, not an object) | scalar, array or object |
| `SELECT 1 AS a UNION ALL SELECT 2 AS b` | `{"a":…, "b":…}` (union of the branches) | each row has only its own key |
| `SELECT 1+1` | `{"$1":"number"}` | `{"$1":2}` |
| Duplicate alias | rejected: 3000 "Duplicate result alias" | — |

Rows can have absent keys (MISSING) as well as `null`.

### 3.3 Errors and warnings [probe]

- Errors are objects with a `code` and `msg`. Since 7.6, syntax and resolution errors also carry **`line` and
  `column` fields** (1-based, **column counted in code points**: `SELECT '中文😀' AS x WHER` reports column 19, where
  UTF-16 would say 20). Map them through `sql/offsets.py`.
- Nested causes come in `reason` (for example `{"code":19114,"key":"datastore.sequence.read_only",…}`).
- RBAC errors carry `reason.missing_role` (for example `query_system_catalog`, `admin`).
- Codes seen:

  | Code | Meaning |
  |---|---|
  | 3000 | syntax |
  | 12003 | keyspace not found |
  | 13014 | missing credentials |
  | 1000 | read-only violation |
  | 1080 | timeout |
  | 3230 | EE-only feature |
  | 17002 | COMMIT/SAVEPOINT outside a transaction |
  | 5001 | panic |
  | 5010 | error evaluating projection |

- Warnings arrive in `warnings` (same shape). Report them as `ServerMessage(WARNING)`, as MySQL reports its warning
  count.

### 3.4 Request parameters relevant to DbaGuy

Docs: [Query REST API](https://docs.couchbase.com/server/7.6/n1ql/n1ql-rest-api/index.html); behaviour confirmed by
[probe].

| Parameter | Notes |
|---|---|
| `client_context_id` | Echoed back. **Max 64 characters (longer is cut); `/` or `"` is rejected with error 1110.** Use `dbaguy-<request_id>`. |
| `timeout` | Server-side. The node default is **0 = none** (7.6.12 `/admin/settings`). The clock **keeps running while the client is paused**, so a paused result resumed after the timeout ends with 1080. Do not send it while results can pause; let the worker's timer close the socket instead (§3.7). |
| `query_context` | `default:bucket.scope`, `bucket.scope`, or `default:bucket` (then `users` resolves to `bucket._default.users`). Back-ticked names work: `default:\`dbaguy\`.\`app\``. |
| Named params | `"$name": <json>` or `"@name"` (both spellings bind `$x` and `@x`). |
| Positional params | `"args": [...]`, used as `$1` or `?`. |
| Parameter types | Values are JSON, so types come from the JSON. Integers are exact within int64 (`9007199254740993 + 1` exact). Decimals and integers past int64 become float64 on the server (`1.10` → `1.1`, `18446744073709551615` → `18446744073709552000`). |
| `readonly` | §3.6 |
| `scan_consistency` | `not_bounded` (default) / `request_plus` / `at_plus`. A per-tab "read your writes" toggle would map to `request_plus` (Should, not probed). |
| `preserve_expiry` | Keeps the expiration on DML (§6). Not allowed inside transactions. |
| `profile` | `phases` / `timings`. **EE only in practice:** CE 7.6.2 silently returned no `profile`. EE `timings` returns `executionTimings`, the plan with per-operator `#stats` (`#itemsIn/Out`, `execTime`, `servTime`): a usable "EXPLAIN ANALYZE" for EE. |
| `metrics` | Defaults to true over REST. |
| `pretty=false` | The server default is already false. |

### 3.5 Timeouts

- SDK: 75 s per query by default.
- REST: no client timeout of its own; the server's node and cluster `timeout` is 0 by default.
- DbaGuy already applies `timeout_s` with a worker timer that calls `cancel()`. For Couchbase, `cancel()` = close the
  socket, and the timer covers `run()` plus the first page, as today.
- The feature-priorities note "show both timeouts" becomes: show the tab timeout, and show the server's raw error 1080
  if the server's own timeout hits.

### 3.6 `readonly=true` (server enforced) [probe, CE and EE]

| Rejected with 1000 "read-only" | Allowed (tested with `readonly=true`) |
|---|---|
| INSERT, UPSERT, UPDATE, DELETE, MERGE | SELECT |
| CREATE/DROP/BUILD INDEX, CREATE PRIMARY INDEX | EXPLAIN (including `EXPLAIN DELETE …`) |
| CREATE SCOPE, CREATE COLLECTION | PREPARE (even `PREPARE p FROM DELETE …`), but **`EXECUTE p` of a write is rejected** |
| BEGIN WORK; CREATE FUNCTION; **EXECUTE FUNCTION** (even of an inline function) | `SELECT f()` with a user-defined function |
| `DELETE FROM system:active_requests` | INFER |
| `NEXT VALUE FOR seq` (error 5010/19114) | — |

Not tested with `readonly=true`: CTEs and `WITH RECURSIVE` (they run without it), EXPLAIN FUNCTION, and ADVISE. On
CE, ADVISE failed with 3230 (EE only) before any read-only check.

The docs list only CREATE INDEX, DROP INDEX, INSERT, MERGE, UPDATE and UPSERT; the probe shows the server goes further.
With `readonly=true` sent on every request of a read-only profile, plus the existing `SafetyPolicy` allow-list, the
read-only story matches PG and MySQL.

### 3.7 Cancellation [probe]

Test statement: a 245 s blocking aggregate (`COUNT(*)` over `ARRAY_RANGE` × `UNNEST`). Cancel sent after 2 s.

| Method | Client sees | Latency | Server state afterwards | Who can do it |
|---|---|---|---|---|
| HTTP: `sock.shutdown()` from another thread | `RemoteDisconnected` (reader ends) | **0 ms** | `completed_requests.state = "closed"`, elapsed 2.0 s (stopped at once, even mid-aggregation) | anyone |
| HTTP: `DELETE FROM system:active_requests WHERE clientContextID=…` | stream ends **cleanly** with `status:"stopped"` and no error | 15 ms | `"stopped"` | CE: `admin` only; EE: `query_system_catalog` (error 13014 `missing_role`). A `bucket_full_access` or `query_select` user cannot even see their own requests. |
| SDK: DELETE as above | `InternalServerFailureException` "Streaming operation failed" | 5 ms | `"stopped"` | as above |
| SDK: internal `_request._streaming_result.cancel()` | `InternalSDKException` | 0 ms | **still `running`** | — |
| SDK: `cluster.close()` from another thread | `RequestCanceledException` | 4 ms | `"closed"` | anyone (drops the whole connection) |

→ **Cancel = socket shutdown.** Treat `status:"stopped"` as cancelled too (someone else killed the request). Reconnect
lazily: there is no session state, and no transactions in v1.

### 3.8 Pause and backpressure [probe]

- After 500 rows, the reader slept 10 s. Client RSS did not grow (17–21 MB), and the server's `elapsedTime` included
  the pause. That is TCP backpressure.
- While paused, `system:active_requests` shows the request as **`running`**. It holds one of the node's **servicers**
  (EE 7.6.12: `servicers=32`, which is 4 × cores; CE showed 16).
- So a paused grid result costs a server thread until it is drained or discarded, much like MySQL holding its
  connection.
- **Open question for the spec:** close paused Couchbase results after some idle time, to protect production query
  nodes?

---

## 4. Number fidelity [probe]

Document stored through KV as raw bytes:
`{"big":9007199254740993,"i64max":9223372036854775807,"u64max":18446744073709551615,"beyond":12345678901234567890,"dec":1.10,"pi":3.141592653589793238462643383279,"sci":1E+3,…}`

| Path | Integers ≤ int64 (incl. CAS) | uint64 | > uint64 | `1.10` / long decimals | `1e400` | Key order |
|---|---|---|---|---|---|---|
| KV get (SDK raw transcoder) | exact | exact | exact | exact text | exact | stored |
| REST `SELECT *` / `SELECT RAW alias` (passed through) | exact | exact | exact | **exact text** | exact | stored (whitespace too) |
| REST projection (`SELECT u.dec`) | exact | → float (`18446744073709552000`) | → float | `1.1`, `3.141592653589793` | **dropped (MISSING)** | projection order |
| `SELECT u.*` | exact | — | — | — | doc with `1e400` → **`{}`** (whole object lost) | sorted |
| SDK query (any) | exact | exact | → double | `1.1` | **process abort** | sorted |
| SQL++ write (`SET d = $doc`, `VALUES`) | exact | → float | → float (**value changes**) | normalized | — | **sorted, compacted** |

- `META().cas` is a uint64 (for example `1792297840852140032`, under 2^63 today). It needs no conversion beyond what
  `values.encode_value` already does: ints past ±(2^53−1) become strings.
- Today `encode_value` turns `dict`/`list` into `json.dumps(...)`. For Couchbase, JSON cells should carry the
  **server's raw text** for that value. The frontend's `src/features/grid/json.ts` already parses losslessly (keeps
  number literals, key order and duplicate keys).
  - Slicing each top-level field of a row costs a second `raw_decode` per field. The cost is acceptable: 80k rows/s on
    27 MB of travel-sample routes, including the HTTP read.
- Scalars:
  - ints → Python int, already exact.
  - Floats: `float` when `repr(float(text))` gives the same text back, else keep the text (as a string or a typed cell).
- Raw row text also allows a byte-exact JSON / JSON Lines export.

---

## 5. Metadata

### 5.1 Buckets → scopes → collections [probe]

| Source | Notes | Speed (300 + 4 collections) | Access for limited users (EE) |
|---|---|---|---|
| `system:buckets` / `system:scopes` / `system:keyspaces` (SQL++) | **Filtered by the user's privileges** (`query_select[dbaguy:app:users]` sees only `app.users`). **Leaves out the `_default` scope.** The `_default._default` collection appears as a bucket-level keyspace (the row has no `bucket`/`scope` field). Order is arbitrary: add ORDER BY. | 2–45 ms; 300 names in 18 ms | works for any user with a query role; a `data_reader`-only user sees nothing |
| REST `/pools/default/buckets/<b>/scopes` (manifest; the SDK's `collections().get_all_scopes()`) | everything incl. `_default`, `_system` (`_mobile`, `_query`), `maxTTL`, `history`, uid | 18 ms, 16.8 KB | **403** for `query_select`/`data_reader` users; needs 8091 |
| REST `/pools/default/buckets?skipMap=true` | bucket list | 14 ms | 200 (own buckets only) |

The "45 collections, only 30 shown" complaint does not reproduce with either source (300 listed). Recommendation:
system catalogs, which also work through a single forwarded query port. Add a `_default` scope node when the bucket
row exists, and hide `_system` by default.

### 5.2 Indexes [probe]

- `system:indexes` rows include `name`, `bucket_id`, `scope_id`, `keyspace_id`, `index_key[]` (expressions as
  strings, for example ``"`amount` DESC"``, `"(distinct (array \`t\` for \`t\` in \`tags\` end))"`), `condition`,
  `is_primary`, `state`, `using` (`gsi`), `metadata.num_replica` and `last_scan_time`. The `partition` field is EE only.
  - Observed `state` values: `online`, `deferred`. Docs also list building, pending, offline, scheduled for creation
    and error.
  - Indexes on `_default._default`: `bucket_id`/`scope_id` absent, `keyspace_id` = the bucket name.
  - Primary indexes: `is_primary:true`, `index_key: []`.
  - Probably filtered by privileges like `system:keyspaces`. Not verified: the probe bucket had no indexes when the
    limited users were tested. `ro_admin` saw all 23 travel-sample indexes.
- **DDL text:** `/indexStatus` (8091) gives the full `definition`
  (`CREATE INDEX … WHERE … WITH {"defer_build":true}`) but returns **403** to limited users. Build the DDL from
  `system:indexes` (`CREATE [PRIMARY] INDEX name ON ks(keys) [PARTITION BY …] [WHERE cond] [WITH {"defer_build":…,"num_replica":…}]`),
  and mark it as generated (`ObjectDetail.ddl_is_generated`).
- **"No primary index" no longer means "cannot list documents" on 7.6:** a sequential scan is used. EXPLAIN shows
  `PrimaryScan3 {index:"#sequentialscan", using:"sequentialscan"}`. EE users need `query_use_sequential_scans` (error
  13014 on SELECT *and* EXPLAIN). On 7.0–7.2 the old rule still holds.
  ([Query without indexes](https://docs.couchbase.com/server/current/indexes/query-without-index.html))

### 5.3 INFER and sampling

- `INFER ks WITH {"sample_size":N, "num_sample_values":K, "similarity_metric":0.6}`. Defaults: 1000 documents, 5
  values. ([INFER](https://docs.couchbase.com/server/7.6/n1ql/n1ql-language-reference/infer.html))
- Output: an array of "flavors", each a JSON-schema-like object (`#docs`, `%docs`, `samples`, `type`, nested
  `properties`). 50 airline documents took 112 ms. Allowed with `readonly=true`.
- **Danger:** on 7.6.2 CE, INFER on `dbaguy.app.users` (holding binary and string documents) crashed cbq-engine
  (`panic: interface conversion: interface {} is []uint8, not map[string]interface {}` in
  `inferencer.NewSchemaFromValue`; both attempts; the query service restarted). On 7.6.12 EE it returned success with
  0 results, and ADVISE on the same collection returned 5001 "Panic: …". No matching MB ticket found.
- → Keep INFER manual (§4 Won't: no automatic INFER). Consider sampling documents on the client instead
  (`SELECT RAW d FROM ks d LIMIT n`, then derive the field paths in Python), which cannot crash a server. At least warn
  before running INFER on servers older than 7.6.12.
- Completion of field paths can also learn from result sets already fetched in the session, at no cost.

---

## 6. Document editing by ID

### 6.1 SDK KV path [probe `kv_edit.py`]

- `get(id, GetOptions(transcoder=…, with_expiry=True))` → `cas`, `expiryTime` (a naive datetime in local time),
  value. A small pass-through transcoder returns `(bytes, flags)` for every format.
  - JSON: flags `0x02000000`.
  - Binary: `0x03000002`.
  - String: `0x04000004`.
  - The default transcoder raises `ValueFormatException` on binary and string documents.
- `replace(id, bytes, ReplaceOptions(cas=…, preserve_expiry=True))` took 6.5 ms and **kept the expiry**. Without
  `preserve_expiry` the expiry was cleared.
- A stale CAS raises `CasMismatchException`; a missing document raises `DocumentNotFoundException`. A 21 MB value
  raises a generic `CouchbaseException ec=104` (the limit is 20 MB). Getting a 5 MB document took 166 ms.

### 6.2 SQL++ path [probe]

- **Open:** `SELECT META(d).cas, META(d).expiration, META(d).type, d FROM ks d USE KEYS $id`.
  - Returns the document's **exact stored text** (key order, whitespace, `1e400`).
  - `type` is `json` or `base64`. Binary documents show as `"<binary (9 b)>"`, so they are read-only.
  - `META().flags` read 0 even for binary documents.
  - No index is needed (`KeyScan`). A missing ID gives 0 rows.
- **Save:**
  `UPDATE ks AS d USE KEYS $id SET d = $doc WHERE META(d).cas = $cas RETURNING META(d).cas AS cas`, with
  `"preserve_expiry": true`.
  - It replaced the whole document and returned the new CAS, and the expiry was kept.
  - A stale CAS gives `success` with 0 rows and no `mutationCount`, which the editor reports as a conflict.
  - The check is atomic: the query service writes with the CAS it fetched, and a concurrent change in between gives
    error 12009.
- **Side effects of saving:**
  - The stored keys get **sorted** (`{"zeta","alpha"{"z","a"}}` → `{"alpha":{"a","z"},…,"zeta"}`).
  - Whitespace is compacted.
  - `1.10` → `1.1`.
  - **`12345678901234567890` → `12345678901234567000`**: the value changes.
  - int64 values (including `9223372036854775807`) stay exact.
- Before saving: parse the edited text with `parse_float=Decimal`. **Block** integers past int64. **Warn** when other
  numbers will be normalized.

### 6.3 Rejected: the console's REST docs endpoint

GET returns exact `json` plus `meta.rev`, `expiration` and `flags`, and POST writes raw values exactly. But a POST with
`cas=1` still wrote, so it has no optimistic check, and it is an internal API on 8091. Useful only for seeding the dev
cluster (§9).

### 6.4 Decision for the user

| | B: SQL++ only | C: SDK for open/save |
|---|---|---|
| Must 6 (META, CAS check, keep expiry) | ✓ | ✓ |
| Byte-exact save (key order, decimals, > int64) | ✗ keys sorted, decimals normalized; > int64 blocked | ✓ |
| Binary or string documents | view only (size) | editable as text or hex |
| Bundle and memory | 0 | +15 MB app; +24 MB RSS after the first open |
| Works through SSH to a multi-node cluster | ✓ | ✗ (KV needs every node's 11210) |
| Code | one HTTP client | second client, second TLS config, connection lifecycle |

**Recommendation: B in v1**, behind a `DocumentStore` interface (get / replace / insert / remove).

### 6.5 Other observations

- Renaming a key = insert + remove; a key cannot be changed in place.
- §4 says to always show `META().id`, cas and expiration in results. Those must be projected explicitly. The grid
  cannot add them to an arbitrary query; open-by-ID can show them.

---

## 7. EXPLAIN and ADVISE [probe]

- **EXPLAIN** returns one row `{plan: <operator tree>, text}`. EE with CBO adds `cost` and `cardinality` at the top and
  `optimizer_estimates {cost, cardinality, fr_cost, size}` on operators.
  - Children sit under `~children` (Sequence), `~child` (Parallel, Authorize), `scans` (IntersectScan, UnionScan),
    `first`/`second` (joins), and `~subqueries`.
  - The sample tree is saved in `cb-probe/explain_sample.json`.
- **Detection rules (all four seen):**

  | Anti-pattern | Rule |
  |---|---|
  | Primary scan | `PrimaryScan3` (or `PrimaryScan`) whose `index` is not `#sequentialscan` |
  | Sequential scan | `PrimaryScan3` with `index:"#sequentialscan"` or `using:"sequentialscan"` |
  | Not covered | `IndexScan3` without `covers`, followed by `Fetch`. A Fetch is normal for `SELECT *`, so show it as a hint rather than an error. A covered scan has `covers:["cover ((\`airport\`.\`faa\`))",…]` and no Fetch. |
  | IntersectScan | the `IntersectScan` operator with child `IndexScan3`s |

  Also seen: `KeyScan` (USE KEYS), `Filter`, `InitialProject`, `Order`, `Limit`.
- **ADVISE (EE only; CE returns 3230):** returns one row
  `{"#operator":"Advise","advice":{"#operator":"IndexAdvice","adviseinfo":{"current_indexes":[{index_statement,keyspace_alias}],"recommended_indexes":{"indexes":[{index_statement:"CREATE INDEX adv_… ON …",keyspace_alias,recommending_rule,update_statistics}],"covering_indexes"?:[…]}}},"query":…}`.
  Show `index_statement` with a copy button. Detect EE through `/pools` `isEnterprise`, or try ADVISE and treat 3230
  as "not available". 7.6.12 can still panic (5001) on collections with binary documents (§5.3).
- **Permissions:** EXPLAIN of a query that would use a sequential scan needs `query_use_sequential_scans` (EE). ADVISE
  needs the same privileges as the query.
- **EXPLAIN never executes:** there is no ANALYZE form, so the classifier's EXPLAIN-ANALYZE unwrapping does not apply.
  The executed-plan equivalent is `profile=timings` (EE).

---

## 8. SQL++ text processing

### 8.1 Lexical rules [probe `lex_probe.py` against 7.6.2]

| Aspect | SQL++ |
|---|---|
| Strings | `'…'` and `"…"` (**double quotes are strings, never identifiers**). Backslash escapes in both (`\'`, `\"`, `\n`, `\t`, `é`). `''` is accepted inside `'…'`; `""` inside `"…"` is *not* (two strings next to each other), but either reading gives the same token end for splitting. |
| Identifiers | Unquoted: ASCII `[A-Za-z_][A-Za-z0-9_]*` (`a$b` and `中文` fail). Back-ticked: `` `…` `` with **both `` `` `` and `` \` `` escapes**. Case-sensitive; keywords are not. |
| Comments | `--` to end of line (**no space needed**: `1--x` is a comment); `/* … */` **not nested**; `#` is **not** a comment. `/*+ … */` is an optimizer hint, valid only after SELECT/INSERT/…, otherwise error 3000. |
| Parameters | `$name`, `@name`, `$1`, `?` |
| Numbers | `1`, `1.5`, `1e3`, `1.5E-2`; **`.5` is invalid**; no hex |
| Statements | **One statement per request**: `SELECT 1; SELECT 2` gives 3000, while a trailing `;` is fine. Splitting on the client is required. There is no DELIMITER, no `GO`, no dollar quoting (`AS $$…$$` → 3000). JavaScript UDF bodies are string literals; inline functions use `{ expr }`. → no body rule. |
| Unterminated tokens | the server reports "missing closing quote" (3000) |
| Literals | `TRUE`, `FALSE`, `NULL`, `MISSING`; array `[…]` and object `{…}` constructors (`{` and `}` inside a statement never split it) |

Mapping onto `LexRules`:

- Same as MySQL: `backslash_escapes=True`, `string_quotes={' "}`, `identifier_quotes=(("`","`"),)`,
  `nested_block_comments=False`.
- New values: `dash_comment_needs_space=False`, `hash_comments=False`, `executable_comments=False`.
- New flag: `identifier_backslash_escapes` (the current quoted-identifier scanner handles only the doubled closing
  quote).
- Parameters: `variable_prefixes` = `$` and `@`, plus `?` as a parameter.
- `body_rule`: a new "none" value. `client_command=None`.
- Error positions: line + code-point column → UTF-16 via `sql/offsets.py`.

Reserved words: 225 in the
[7.6 list](https://docs.couchbase.com/server/7.6/n1ql/n1ql-language-reference/reservedwords.html), case-insensitive,
used as names when back-ticked. They include `VALUE`, `USER`, `USERS`, `KEY`, `KEYS`, `PATH`, `ROLE`, `SCOPE`,
`COLLECTION`, `BUCKET`, `INDEX`, `FIRST`, `OBJECT`, `ARRAY`, `SET`, `SELF`, `WORK`, `FUNCTION`; not `NAME`, `TYPE`,
`STATUS`, `DATE`. `quote_ident` must also quote non-ASCII names and any name containing `-` (`travel-sample`).
`FROM dbaguy.app.users` worked without quotes even though `USERS` is listed, so quoting extra words is harmless, as
for the other dialects.

### 8.2 Statement kinds for the classifier

Keyword rules only. sqlglot 30.23.0, the latest, has no N1QL or SQL++ dialect; the dialect directory lists athena …
tsql and nothing for Couchbase.

| Kind | SQL++ statements |
|---|---|
| READ | SELECT, WITH [RECURSIVE] … SELECT, EXPLAIN, EXPLAIN FUNCTION, ADVISE, INFER, PREPARE (does not execute) |
| WRITE | INSERT, UPSERT, UPDATE, DELETE, MERGE, `UPDATE STATISTICS` / `ANALYZE` (maintenance, EE), `DELETE FROM system:…` (kills requests, drops prepared statements) |
| DDL | CREATE/DROP/ALTER/BUILD INDEX, CREATE PRIMARY INDEX, CREATE/DROP SCOPE, CREATE/DROP COLLECTION, CREATE/DROP FUNCTION, CREATE/DROP/ALTER SEQUENCE, GRANT, REVOKE |
| TRANSACTION | BEGIN WORK / BEGIN TRANSACTION / START TRANSACTION, COMMIT, ROLLBACK [TO SAVEPOINT], SAVEPOINT, SET TRANSACTION. **Refuse in v1** (no txid handling). |
| UNKNOWN (fail closed) | EXECUTE <prepared> (the server's read-only check still catches it), EXECUTE FUNCTION (a JavaScript UDF may write) |

- **Danger reasons.**
  - Flag UPDATE and DELETE only when they have neither WHERE nor `USE KEYS`. `DELETE FROM ks USE KEYS 'k'` is targeted
    and should not be flagged.
  - DROP INDEX / SCOPE / COLLECTION → `DROP`.
  - `MERGE … WHEN MATCHED THEN DELETE` and `DELETE FROM system:…` are worth a confirmation too.
- **No hidden DML in SELECT:** a DML subquery inside SELECT is a syntax error (`DELETE (reserved word)`), so a SELECT
  cannot write. The exception is `NEXT VALUE FOR` (sequences advance; the read-only check blocks it on the server).

### 8.3 Formatter

The frontend already ships `sql-formatter` 15.9 with an **`n1ql` language**: map `sqlpp → "n1ql"` in
`src/features/editor/format.ts`. It handles `ANY … SATISFIES … END`, `UNNEST`, `USE KEYS`, `$params`, back-ticks and
`RETURNING`.

**Bug [probe]:** `'it''s'` comes out as `'it' 's'`. Add a guard: re-lex the original and the formatted text with
DbaGuy's lexer, compare the non-space token texts, and refuse to replace the text when they differ. This guard is worth
having for every dialect.

---

## 9. Dev environment

### 9.1 Image

- `couchbase/server:community-7.6.2` is the latest 7.x Community: arm64 native, 1.17 GB unpacked, pulled in 27 s here.
  `community-8.0.2` also exists, but 8.0 is not the target.
- `couchbase/server:enterprise-7.6.12` is the latest 7.6 EE: arm64, 0.88 GB compressed, pulled in 48 s.
- Memory in OrbStack: CE with travel-sample + 2 buckets + 300 collections: **1.29 GiB**. EE with travel-sample + 1
  bucket + 300 collections: **1.17 GiB**. Quotas: data 512 MB, index 256 MB. travel-sample takes a 200 MB quota.
- **CE vs EE (user decision):**
  - CE lacks ADVISE, UPDATE STATISTICS, CBO estimates in EXPLAIN, `profile` output, the Query Monitor, and every RBAC
    role except `admin`, `ro_admin` and `bucket_full_access`. CE's `GRANT query_select` fails with 5250 "Role
    query_select is not valid".
  - EE is free for development and testing per Couchbase's FAQ, with caveats in the license wording
    ([forum](https://couchbase.com/forums/t/licensing-and-usage-guidance-for-different-editions/4420),
    [blog](https://www.couchbase.com/blog/?p=702)). The public repo would only reference the image tag.
  - Recommendation: **EE 7.6.x in `dev/docker-compose.yml`**, since the company cluster is most likely EE. Add a note
    that CE works too but EE-only features will say "not available".

### 9.2 Scripted init

All via REST, about 1 minute in total ([probe]):

```sh
# wait for http://127.0.0.1:8091/ui/index.html → 200 (1–5 s)
curl -X POST :8091/clusterInit -d hostname=127.0.0.1 -d services=kv,n1ql,index \
  -d memoryQuota=512 -d indexMemoryQuota=256 -d indexerStorageMode=plasma   # CE: forestdb
  -d username=Administrator -d password=<fake> -d port=SAME                 # 0.2 s
curl -u … -X POST :8091/sampleBuckets/install -d '["travel-sample"]'        # ~40 s incl. indexes
curl -u … -X POST :8091/pools/default/buckets -d name=dbaguy -d ramQuota=100 -d bucketType=couchbase
# scopes/collections: SQL++ "CREATE SCOPE dbaguy.app" / "CREATE COLLECTION dbaguy.app.users" via :8093
# (300 collections through the SDK took 22 s; via SQL++ similar)
curl -u … -X PUT :8091/settings/rbac/users/local/reader -d password=<fake> \
  --data-urlencode 'roles=query_select[dbaguy:app:users],data_reader[dbaguy:app:users]'   # EE roles
```

`couchbase-cli`, `cbq`, `cbimport` and `cbc` are all inside the image. A one-shot `couchbase-init` service (same
pattern as `mssql-init`) can run the script. The healthcheck can mean "seeded": for example, a SQL++ check that a
marker document `dbaguy._default._default::seed_done` exists.

### 9.3 Seeding odd data byte-exact without the SDK

The console endpoint
`POST /pools/default/buckets/<b>/scopes/<s>/collections/<c>/docs/<id> --data-urlencode 'value=<raw json>'` stored
`{"beyond":12345678901234567890,"dec":1.10,"key order":{"z":1,"a":2}}` exactly. SQL++ `INSERT` would normalize it
(§4).

Suggested seed set (all used in the probe):

- An int64 boundary document: 2^53+1, int64 max/min, uint64 max, 20 digits, `1.10`, a long π, `1E+3`, `1e-400`,
  `1e400`.
- Mixed-shape documents (missing fields, nested arrays and objects, null).
- Non-object documents: a string, an array, a number.
- A binary and a text document (they need the KV or REST endpoint and a non-JSON flag; the REST endpoint can't set
  binary). Note: these documents trigger the INFER and ADVISE bugs in §5.3.
- A document with an expiry; a 5 MB document.
- 20k plain rows for paging; a collection named `value` and one named `odd-name`; a bucket named `value`.
- A scope with 300 collections; travel-sample for realistic indexes (primary, `faa`, `city` → IntersectScan) and
  EXPLAIN.
- Leave one collection without indexes to exercise the sequential scan path.

---

## 10. Fit with DbaGuy's adapter interface

These are observations for the spec to decide on.

| Area | Relational assumption today | Couchbase reality → suggested change |
|---|---|---|
| Connection | One driver connection per session (`DatabaseConnection`), `Endpoint(host, port, tls_server_name)` | An HTTP/1.1 keep-alive connection to one query node per session fits SessionWorker's one-thread model. Nodes come from `GET :8091/pools/default/nodeServices` (`n1ql`/`n1qlSSL` ports; works for limited users, 401 anonymous). With SSH: tunnel one node's 8093/18093 and skip discovery. The metadata connection is just another HTTP connection. |
| URL import | `parse_url` per scheme | `couchbase://h1,h2[:port]` → query port 8093 (`couchbases://` → 18093, TLS). The port in `couchbase://` URLs is KV 11210, so map it rather than reuse it. Accept `http(s)://h:8093` too. The profile's `database` field → default query context (`bucket.scope`). |
| Database / schema | `per_database_connection`, `current_database()`, schema = namespace | No database switch: **query context** (`bucket.scope`) is per tab, sent on every request. Needs a per-tab query context (a new command, or an `ExecuteCommand` field) and a picker. `ServerInfo.current_database` → the context string. Version: `SELECT ds_version()` → `7.6.12-8946-enterprise`. |
| Tree | `NodeKind` database/schema/folder/table/index… | bucket → scope → collection → indexes. Either new kinds (`bucket`, `scope`, `collection`) or reuse database/schema/table; `RelationKind` lacks "collection". `_default` scope and `_system` quirks (§5.1). |
| Columns | `ResultSetEvent(columns)` sent before rows, fixed | Columns come from the signature, in its order: no `*` → fixed; scalar signature (RAW) → one value column. **With `*`**, keys are only known from rows and can appear later → peek a page to build the columns, plus a way to add columns mid-stream (re-send `result_set` with more columns, or a catch-all "other fields" JSON column). `ColumnMeta.type_name` = the signature type (`json`, `number`, `string`, `object`); `ColumnCategory` mostly JSON/NUMBER/TEXT. |
| Cells | `None` = NULL, `encode_value` | Absent keys (**MISSING**) differ from `null` → needs a cell marker (for example `{"$type":"missing"}`) and grid styling. Objects and arrays as **raw JSON text** from the server, never `json.dumps` of a parsed value. Floats that would not survive `float()` as text. |
| Transactions | autocommit, `in_transaction`, commit/rollback, idle warnings | v1: `in_transaction()=False`, `autocommit()=True`, `set_autocommit(False)` → error; commit/rollback do nothing; the TRANSACTION kind is refused. New capability `transactions: bool` so the UI hides the commit/rollback/auto-commit controls. Later (v1.x): REST transactions work with `txid` + the same node (`BEGIN WORK` returned `{"txid":…, "nodeUUID":…}`), which this HTTP design can pin. |
| Read-only | `server_read_only` | ✓ (`readonly=true` on every request). |
| Cancel | `cancel()` from any thread | ✓ socket shutdown. Treat `status:"stopped"` (and 1080 when the server's own timeout fired) as cancelled or timed out. `cancel_aborts_transaction=False`. |
| Pause | A paused result holds the connection | Also holds a **server servicer** (§3.8). The server timeout must be off for paused results. Optional idle cap. |
| Statement API | `run(sql)` without parameters | Typed named and positional parameters (Must 3) need `run(sql, params)` or a Couchbase-only path, and the protocol needs params on `ExecuteCommand`. The parameter UI is the general S6 feature. |
| Messages | `take_messages()` | `warnings[]`, plus a metrics summary line (elapsed, execution, results, size, mutations, warnings) → `MessageEvent`s, or a new metrics event if the grid status bar should show them (Must 4). |
| Affected rows | `affected_rows()` | `metrics.mutationCount` (absent when 0). DML with `RETURNING` returns rows *and* mutations. |
| Errors | `StatementError(code, position)` | `code` = the Couchbase number as a string; position from `line` and `column` (code points → UTF-16); keep `msg` raw (M10). |
| Schema snapshot (completion) | Columns of every table, from catalogs, a few queries | No columns without sampling, and INFER must stay manual → the snapshot holds buckets, scopes and collections from catalogs (cheap; 300 in 18 ms). Field paths come from (a) manual INFER or client-side sampling, cached, and (b) keys seen in this session's result sets. |
| Export | Writer gets raw values | JSON / JSON Lines can write the raw row text (exact). CSV needs flattening of nested values (as JSON text). |
| Explain | per-dialect | A Couchbase plan renderer (React tree) + anti-pattern rules (§7). ADVISE and `profile=timings` gated on EE. |
| Safety | `SafetyPolicy` + classifier | The SQL++ classifier (§8.2). DELETE/UPDATE with `USE KEYS` is not "without WHERE". |

Suggested new `EngineInfo`/capability fields (the spec decides):

- `transactions` (False for Couchbase).
- `query_context` (True: show the context picker).
- `document_ids` (True: open-by-ID editor).
- `json_rows` (rows are documents: star expansion, MISSING, JSON-first cells).
- `explain_kind` (`"couchbase"`).

The existing `server_read_only` (True), `per_database_connection` (False) and `cancel_aborts_transaction` (False) keep
their meaning.

---

## 11. Pain-point check (feature-priorities §2, §4, §8)

| Need | Effect of the recommended design |
|---|---|
| Q1 light | No new dependencies, flat about 21 MB RSS even for 1M rows (the SDK reached 1.15 GB), no 13.5 MB native library unless option C. |
| Q2 stable | No in-process C++ abort path. Server-side INFER and ADVISE bugs are contained by keeping INFER manual and warning. Paused results cannot balloon memory. |
| Q3 trusted | Exact values: raw text, int64 and CAS exact, key order kept. Server-enforced read-only. Raw server errors with positions. |
| Q4 focus | Respects the §4 Won't list: no cluster management, no Analytics or FTS, no automatic INFER, no Capella extras (DNS SRV and the Capella CA are left out per §8). |
| Q5 multi-engine | Fits the adapter and worker model. The gaps are listed in §10: columns that grow, MISSING, query context, no transactions. |
| §4 Must 1–8 | Connection ✓ (minus Capella items, per §8). Tree with indexes and state ✓. Editor: splitting ✓, cancel ✓, per-tab timeout ✓, typed parameters (protocol work), context picker. Results: streaming ✓, signature order ✓ (only over REST), union of keys, int64/CAS ✓, metrics ✓. Export ✓ (raw). Open by ID with META, CAS, keeping expiry ✓ (B with caveats, or C exact). EXPLAIN with anti-patterns ✓ (+ADVISE on EE). History: existing. |

---

## 12. Risks and open questions

1. **Document save fidelity (user decision, §6.4):** B (zero dependencies, saved documents get sorted keys and
   normalized decimals, > int64 blocked, binary read-only) or C (SDK lazily, exact, +15 MB, no KV over SSH to
   multi-node).
2. **CE vs EE for the dev environment (user decision, §9.1).** Also: which edition and patch version does the company
   cluster run? The INFER and ADVISE bugs depend on it, and so do ADVISE, CBO and profile.
3. **Paused results hold a query servicer on the server.** Add an idle cap or auto-close for paused Couchbase results?
   And how should the UI explain it?
4. **Columns that grow for `SELECT *` / `u.*`**: needs a protocol decision (re-send columns, or an overflow JSON
   column) and frontend work in the grid.
5. **MISSING vs NULL** needs a cell representation across the grid, copy and export.
6. **Multi-node clusters:** choosing a query node (round-robin per session?), failover when that node dies mid-session
   (a "Reconnect" UX like MySQL), and `alternateAddresses` for NAT. None of this was probed on a real multi-node
   cluster.
7. **TLS on macOS:** stdlib `ssl` with the profile CA was verified. Using the macOS system trust store for publicly
   signed cluster certificates follows whatever PG/MySQL already do.
8. **INFER safety:** client-side sampling instead of INFER, or INFER with a version-gated warning?
9. **Report upstream (optional):** the SDK abort on out-of-range numbers (couchbase 4.6.3); the INFER panic on binary
   documents (7.6.2 CE crash, 7.6.12 EE empty result); `SELECT u.*` returning `{}` for a document containing `1e400`.
10. **Not probed:** a real company cluster (latency, sizes, LDAP/SSO users), the 8.0 server, `scan_consistency`
    behaviour, sub-document APIs, ephemeral buckets (no sequential scans there).

---

## 13. Suggested sub-plan split

- **04a — Dialect + HTTP query engine (backend first):**
  - `Dialect.SQLPP`: lexer rules incl. back-tick backslash escapes, splitter with no body rule, classifier keyword
    table and danger rules, quoting with the reserved words, error positions code point → UTF-16.
  - Formatter mapping and the token-equality guard.
  - Couchbase profile and URL import (couchbase://, couchbases://, TLS CA, read-only → `readonly=true`), node
    discovery.
  - HTTP client with the streaming parser, cancel by socket close, pause and backpressure, timeouts.
  - Columns from the signature, star expansion and MISSING, raw JSON cells, metrics and warnings messages, refusing
    transactions.
  - Export JSON / JSON Lines raw.
  - `dev/` compose service + init + seed; integration tests against it.
- **04b — Couchbase workflow (frontend-heavy):**
  - Keyspace tree from system catalogs (`_default`/`_system` handling, indexes with state, generated DDL) and the
    query context picker.
  - Open by ID / save with CAS and keeping the expiry (B or C per decision) with the number guard.
  - EXPLAIN tree with anti-pattern badges, ADVISE on EE.
  - Manual INFER or sampling with a sample size, and field-path completion fed by INFER and seen results.
  - Typed named and positional parameters (the general S6 UI plus the protocol field).
- Option C, if chosen: a separate small 04c "SDK document store" (lazy import, spec excludes, frozen-sidecar check).

---

## Sources

- PyPI couchbase metadata: https://pypi.org/pypi/couchbase/json (4.6.3 released 2026-08-25; wheel list and sizes).
- Docker Hub tags: https://hub.docker.com/r/couchbase/server/tags (community-7.6.2, enterprise-7.6.12, arm64).
- SDK source (couchbase 4.6.3 wheel): `couchbase/n1ql.py`, `couchbase/logic/n1ql.py` (metrics default, metadata at
  end, options), `couchbase/logic/streaming.py`, `couchbase/serializer.py`, `couchbase/logic/pycbc_core/_core.pyi`
  (`pycbc_streamed_result.cancel`), `couchbase/logic/cluster_impl.py:241` (75 s default).
- Query REST API (parameters, response fields, readonly, client_context_id limits):
  https://docs.couchbase.com/server/7.6/n1ql/n1ql-rest-api/index.html
- INFER: https://docs.couchbase.com/server/7.6/n1ql/n1ql-language-reference/infer.html
- Reserved words (225): https://docs.couchbase.com/server/7.6/n1ql/n1ql-language-reference/reservedwords.html
- Query without indexes (sequential scans):
  https://docs.couchbase.com/server/current/indexes/query-without-index.html
- Manage and monitor queries (active_requests, DELETE):
  https://docs.couchbase.com/server/current/manage/monitor/monitoring-n1ql-query.html
- EE licensing for dev and test: https://couchbase.com/forums/t/licensing-and-usage-guidance-for-different-editions/4420,
  https://couchbase.com/forums/t/free-enterprise-edition/22968, https://www.couchbase.com/blog/?p=702
- Similar but non-matching panic tickets: https://issues.couchbase.com/browse/MB-59545,
  https://issues.couchbase.com/browse/MB-40779
- sqlglot 30.23.0 dialect directory (no N1QL): `backend/.venv/lib/python3.13/site-packages/sqlglot/dialects/`
- sql-formatter `n1ql`: `node_modules/sql-formatter/dist/esm/languages/n1ql`; probe `cb-probe/fmt.mjs`
- Probe scripts (this session's scratchpad `cb-probe/`):
  - `connect_probe.py`, `probe_sig.py`, `one_query.py`, `seed_nums.py`, `seed_u1b.py`, `kv_raw.py`
  - `sdk_stream.py`, `sdk_ttfr.py`, `sdk_bench.py`, `sdk_cancel.py`, `sdk_close_cancel.py`
  - `httpq.py`, `http_stream.py`, `http_timeout.py`, `http_cancel.py`, `http_bench.py`, `own_cancel.py`,
    `pause_probe.py`
  - `readonly_probe.py`, `params_probe.py`, `lex_probe.py`, `explain_probe.py`, `rbac_probe.py`, `kv_edit.py`
  - `setup_data.py`, `freeze/`
