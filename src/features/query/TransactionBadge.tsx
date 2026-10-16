import type { SessionStateView } from "../session/SessionController";

/** The session's transaction state, from `session_state` only. */
export function TransactionBadge({ state }: { state: SessionStateView | null }) {
  if (state?.transactionFailed) {
    return (
      <span
        className="rounded bg-danger/15 px-1.5 text-[11px] font-medium text-danger"
        title="The transaction failed: only ROLLBACK is accepted"
      >
        Rollback required
      </span>
    );
  }
  if (!state?.inTransaction) return null;
  return (
    <span className="rounded bg-warning/15 px-1.5 text-[11px] text-warning" title="A transaction is open">
      Transaction
    </span>
  );
}
