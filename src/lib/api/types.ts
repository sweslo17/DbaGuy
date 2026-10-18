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

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
}
