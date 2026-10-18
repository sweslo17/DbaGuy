import type { Engine } from "@/lib/api/types";

/** The name each engine goes by in the UI. */
export const ENGINE_LABELS: Record<Engine, string> = {
  postgres: "PostgreSQL",
  mysql: "MySQL",
  mssql: "SQL Server",
  couchbase: "Couchbase",
  sqlite: "SQLite",
};
