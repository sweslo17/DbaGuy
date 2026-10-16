import type { GuardReason } from "@/lib/api/types";

/** The one place where safety-policy reason codes become text. */
const REASON_TEXT: Record<GuardReason, string> = {
  update_without_where: "UPDATE without WHERE",
  delete_without_where: "DELETE without WHERE",
  drop: "Drops an object (DROP)",
  truncate: "Empties a table (TRUNCATE)",
  alter_drop: "Drops part of a table (ALTER … DROP)",
  strict_write: "Changes data (strict safety level)",
  strict_ddl: "Changes the schema (strict safety level)",
  strict_transaction: "Controls the transaction (strict safety level)",
  strict_session: "Changes session settings (strict safety level)",
  strict_unknown: "Unrecognised statement (strict safety level)",
  read_only: "Not allowed on a read-only connection",
};

export function reasonText(reason: GuardReason): string {
  return REASON_TEXT[reason] ?? reason; // a code a newer backend added: still better than nothing
}

/** Why a blocked statement cannot run at all. */
export const BLOCKED_TEXT = "Blocked on a read-only connection";
