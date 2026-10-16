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
export type ColumnInfo = S["ColumnInfo"];
export type SessionInfo = S["SessionInfo"];
export type ColumnMeta = S["ColumnMeta"];
export type ColumnCategory = S["ColumnCategory"];
export type ServerEvent = S["ExecutionSchema"]["event"];
export type ClientCommand = S["ExecutionSchema"]["command"];
export type SessionStateEvent = S["SessionStateEvent"];
export type GuardReason = S["GuardReason"];
export type Classification = S["Classification"];
export type Dialect = S["Dialect"];
export type FormatKind = S["FormatKind"];
export type RowFormatOptions = S["RowFormatOptions"];
export type FormatRowsRequest = S["FormatRowsRequest"];
export type FormatResult = S["FormatResult"];

export function pathKey(path: PathSegment[]): string {
  return path.map((s) => `${s.kind}:${s.name}`).join("/");
}
