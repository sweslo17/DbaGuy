import type { components } from "./schema";

type S = components["schemas"];

export type ProfileEntry = S["ProfileEntry"];
export type ServerProfile = S["ServerProfile"];
export type SqliteProfile = S["SqliteProfile"];
export type Profile = ServerProfile | SqliteProfile;
export type ServerProfileDraft = S["ServerProfileDraft"];
export type SqliteProfileDraft = S["SqliteProfileDraft"];
export type ProfileDraft = ServerProfileDraft | SqliteProfileDraft;
export type SslConfig = S["SslConfig"];
export type SshConfig = S["SshConfig"];
export type ProfileSecretsInput = S["ProfileSecretsInput"];
export type ConnectionTestResult = S["ConnectionTestResult"];
export type EngineInfo = S["EngineInfo"];
export type Engine = EngineInfo["engine"];
export type PathSegment = S["PathSegment"];
export type ObjectNode = S["ObjectNode"];
export type ObjectDetail = S["ObjectDetail"];
/** A table or view found by `GET /metadata/search`, with the explorer path that opens it. */
export type ObjectSearchHit = S["ObjectSearchHit"];
export type ColumnInfo = S["ColumnInfo"];
export type SessionInfo = S["SessionInfo"];
/** Couchbase: the bucket (and scope) that unqualified keyspace names resolve against, sent with execute and export. */
export type QueryContext = S["QueryContext"];
export type ColumnMeta = S["ColumnMeta"];
export type ColumnCategory = S["ColumnCategory"];
export type ServerEvent = S["ExecutionSchema"]["event"];
export type ClientCommand = S["ExecutionSchema"]["command"];
export type SessionStateEvent = S["SessionStateEvent"];
export type GuardReason = S["GuardReason"];
export type GuardAction = S["GuardAction"];
export type GuardDecision = S["GuardDecision"];
export type Classification = S["Classification"];
export type Dialect = S["Dialect"];
export type FormatKind = S["FormatKind"];
export type RowFormatOptions = S["RowFormatOptions"];
export type FormatRowsRequest = S["FormatRowsRequest"];
export type FormatResult = S["FormatResult"];
export type AppSettings = S["AppSettings"];
export type EditorSettings = S["EditorSettings"];
export type CompletionItem = S["CompletionItem"];
export type DescribeResult = S["DescribeResult"];
export type DescribeColumn = S["DescribeColumn"];
/** `workspace.json` (spec §4.8): the open tabs, as `GET`/`PUT /workspace` exchange them. */
export type SavedWorkspace = S["WorkspaceState"];
export type SavedTab = S["WorkspaceTab"];
export type TextSelection = S["TextSelection"];
/** The scripts folder (spec §4.7): its tree, and one `.sql` file with its content. */
export type ScriptTree = S["ScriptTree"];
export type ScriptNode = S["ScriptNode"];
export type ScriptFile = S["ScriptFile"];
/** The query history (spec §4.6): one executed statement, and a page of them (newest first). */
export type HistoryEntry = S["HistoryEntry"];
export type HistoryPage = S["HistoryPage"];
export type HistoryStatus = HistoryEntry["status"];
/** Couchbase (spec §3.11.1): one entry of the query-context picker. `text` is the backend's display text for it. */
export type ContextOption = S["ContextOption"];
/** Couchbase (spec §3.10): an EXPLAIN, ADVISE or profile result as an operator tree with flags, advice and timings. */
export type PlanView = S["PlanView"];
export type PlanNode = S["PlanNode"];
export type PlanFinding = S["PlanFinding"];
export type PlanDetail = S["PlanDetail"];
export type PlanSubquery = S["PlanSubquery"];
export type IndexAdvice = S["IndexAdvice"];
/** Couchbase (spec §3.11.2): the typed parameters of one statement, JSON literal texts by name or position. */
export type StatementParams = S["StatementParams"];
/** Couchbase: a collection by its three names (the key of a field sample). */
export type Keyspace = S["Keyspace"];
/**
 * Couchbase (R-04-11): one field path of a manual sample. `path` runs from the document's root: a key, `null` for the
 * elements of an array, `[]` for the document itself; `types` counts the documents by JSON kind ("binary" included).
 * `display` is the path as the backend writes it for the user (SQL++ quoting): show it, never build one from `path`.
 */
export type FieldPath = S["FieldPath"];
/** Couchbase (R-04-11): the field paths of a keyspace's sampled documents, as `POST /couchbase/fields/sample` answers. */
export type FieldSample = S["FieldSample"];
/** Couchbase (spec §3.9.2): a document as stored, its exact text (null when binary) and META; `cas` in decimal. */
export type DocumentView = S["DocumentView"];
/** Couchbase (spec §3.9.3): a write that happened, with the document's new CAS and its expiration. */
export type DocumentSaved = S["DocumentSaved"];
/** Couchbase (spec §3.9.3): what the server would store instead of the edited text, written only once confirmed. */
export type SavePreview = S["SavePreview"];
/** Couchbase (ruling C2): one token change of a preview; value changes come first. */
export type DocumentChange = S["DocumentChange"];
/** Couchbase (ruling C2): every change of a preview by kind, listed or not. */
export type ChangeCounts = S["ChangeCounts"];
/** Couchbase: what `POST /couchbase/documents/save` and `/insert` answer, told apart by `status`. */
export type SaveAnswer = DocumentSaved | SavePreview;
/** Grid editing (spec ⑤ §3.2): how a table's rows are identified and written, as `POST /edits/target` answers. */
export type EditTarget = S["EditTarget"];
/** Grid editing: one column of an edit target, with the `kind` that picks its cell editor and how its original is checked. */
export type EditColumn = S["EditColumn"];
/** Grid editing: how a column's new values are read ("number", "text", "bool", "datetime", "binary", "json", "other"). */
export type EditKind = EditColumn["kind"];
/** Grid editing (spec ⑤ §3.3.1): a staged cell value: typed text, NULL or DEFAULT (a new row only). */
export type CellInput = S["CellInput"];
/** Grid editing: a staged UPDATE: the row's key and original cells as loaded, and the cells to set. */
export type RowUpdate = S["RowUpdate"];
/** Grid editing: a staged INSERT; a column left out takes its default. */
export type RowInsert = S["RowInsert"];
/** Grid editing: a staged DELETE, by the row's key as loaded. */
export type RowDelete = S["RowDelete"];
/** Grid editing: every staged change of one commit, as `POST /edits/preview` takes them. */
export type EditChanges = S["EditChanges"];
/** Grid editing (spec ⑤ §3.3.3): one cell or change the preview refuses, with a fixed message (never the value). */
export type EditProblem = S["EditProblem"];
/** Grid editing: why the preview refuses a cell or change ("invalid_number", "key_incomplete", "key_collision", …). */
export type EditProblemCode = EditProblem["code"];
/** Grid editing (spec ⑤ §3.3.3): a set column whose original value the UPDATE does not check, and why. */
export type UncheckedColumn = S["UncheckedColumn"];
/** Grid editing (spec ⑤ §3.4): one statement of a commit, exactly as the worker sends it, with the change it belongs to. */
export type PlannedStatement = S["PlannedStatement"];
/** Grid editing: something to know before committing (columns whose original is not checked, a non-transactional table). */
export type EditWarning = S["EditWarning"];
/** Grid editing: "unchecked_columns" (one per statement), "non_transactional" or "missing_required" (one per new row). */
export type EditWarningCode = EditWarning["code"];
/** Grid editing: how many updates, inserts and deletes a preview holds. */
export type EditCounts = S["EditCounts"];
/** Grid editing (spec ⑤ §3.4): the preview of a commit: its statements, problems, warnings and, when none, a one-time plan id. */
export type EditPreview = S["EditPreview"];
/** Grid editing (spec ⑤ §3.5): apply a previewed plan on the tab's session, its statements exactly as previewed, in one transaction. */
export type ApplyEditsCommand = S["ApplyEditsCommand"];
/** Grid editing (spec ⑤ R-05-6): how an apply ended, sent once right before its `session_state` and `finished`. */
export type EditOutcomeEvent = S["EditOutcomeEvent"];
/** "committed", "rolled_back" (nothing was applied), "partial" (a table that cannot roll back) or "unknown". */
export type EditOutcome = EditOutcomeEvent["outcome"];
/** Grid editing (spec ⑤ §3.7): a table tab's staged edits as `workspace.json` keeps them. */
export type StagedEditsFile = S["StagedEditsFile"];
/** Data tools (spec ⑥ D-06-11): how far a job is, at most every 250 ms (cumulative counts). */
export type JobProgressEvent = S["JobProgressEvent"];
/** Data tools (spec ⑥ R-06-12): how a job ended, sent once right before its `session_state` and `finished`. */
export type JobOutcomeEvent = S["JobOutcomeEvent"];
/** "completed", "failed", "rolled_back", "partial", "stopped", "unknown" or "not_started"; a cancel is `cancelled`. */
export type JobOutcomeKind = JobOutcomeEvent["outcome"];
/** Data tools: a statement that failed (line, statement number, the server's code and text). */
export type JobError = S["JobError"];
/** Data tools (spec ⑥ D-06-10): a job of this backend run, as `GET /data/jobs` lists it. */
export type JobInfo = S["JobInfo"];
/** Run SQL file (spec ⑥ §3.4): run a file on a job session, all or nothing or statement by statement. */
export type RunFileCommand = S["RunFileCommand"];
/** Run SQL file (spec ⑥ §3.2): the size, first lines, DbaGuy header and warnings of a file, before it runs. */
export type FileInspection = S["FileInspection"];
/** The header of a dump DbaGuy wrote, read back by the inspection: a hint, never a permission. */
export type DumpHeader = S["DumpHeader"];
/** Dump (spec ⑥ §3.3): dump a database, a schema or named tables of this session to a SQL file. */
export type DumpCommand = S["DumpCommand"];
/** Dump: what it covers ("database", "schema", "tables" with their names). */
export type DumpScope = S["DumpScope"];
/** Dump: which objects besides tables it writes (views, sequences, types, routines, triggers). */
export type DumpObjectKinds = S["DumpObjectKinds"];
/** Dump (POST /data/dump/plan): the objects, what the header leaves out, the consistency and the warnings. */
export type DumpPlan = S["DumpPlan"];
/** Dump plan: one object the dump writes under its own name (a table with its rows estimate). */
export type DumpPlanObject = S["DumpPlanObject"];
/** Dump plan request (POST /data/dump/plan): the dump command's fields, and the profile and database to read. */
export type DumpPlanRequest = S["DumpPlanRequest"];
/** Data tools: where a run stopped, or where a run again starts (`resume_at`, 06b B1): a line and a statement number. */
export type StoppedAt = S["StoppedAt"];
/** Run SQL file (06b B2): a file's lines around one line, for the error list. */
export type FileExcerpt = S["FileExcerpt"];
/** Copy data (spec ⑥ §3.5.1, POST /data/transfer/plan): the source, the target and how. */
export type TransferRequest = S["TransferRequest"];
/** Copy data: tables (tree paths) of one connection, or a query tab's result; a filter for one table. */
export type SourceSpec = S["SourceSpec"];
/** Copy data: where the rows go, the mode, and per table and column the types, identities and columns left out. */
export type TargetSpec = S["TargetSpec"];
/** Copy data: a query tab's statement (run again on its session) and the result's columns the tab already has. */
export type QuerySource = S["QuerySource"];
/** Copy data's preview: each table's mapping, statements, warnings and problems; `plan_id` null while any problem. */
export type TransferPlan = S["TransferPlan"];
/** Copy data's preview: one table, in the order the copy runs. */
export type TableTransfer = S["TableTransfer"];
/** Copy data's preview: one source column and where it goes. */
export type ColumnMapping = S["ColumnMapping"];
/** Copy data's preview: why it cannot run as asked (spec §4). */
export type TransferProblem = S["TransferProblem"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
}
