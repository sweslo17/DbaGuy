import { cn } from "@/lib/cn";
import { useNow } from "@/lib/useNow";
import type { SessionStateView } from "../session/SessionController";

/** "02:13", or "1:02:13" past an hour. */
function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const pad = (n: number) => String(n).padStart(2, "0");
  const [hours, minutes, seconds] = [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60];
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
}

/**
 * The session's transaction state, from `session_state` only (spec §4.2): "Transaction · 02:13" counting from the
 * server's `transaction_started_at`, or "Aborted — Rollback required" (the full wording in its tooltip). It never
 * shrinks: the connection's names give way first. The clock ticks once a second only
 * while `visible`; the elapsed time is always computed from the start, never accumulated. `pulse`: a session message
 * (e.g. an idle warning) just arrived.
 */
export function TransactionBadge({
  state,
  visible,
  pulse,
}: {
  state: SessionStateView | null;
  visible: boolean;
  pulse: boolean;
}) {
  const startedAt = state?.inTransaction && !state.transactionFailed ? state.transactionStartedAt : null;
  const now = useNow(1000, visible && startedAt !== null);
  if (state?.transactionFailed) {
    return (
      <span
        className={cn(
          "shrink-0 rounded bg-danger/15 px-1.5 text-[11px] font-medium whitespace-nowrap text-danger",
          pulse && "animate-pulse",
        )}
        title="Transaction aborted — Rollback required: a statement failed inside the transaction, only ROLLBACK is accepted"
      >
        Aborted — Rollback required
      </span>
    );
  }
  if (!state?.inTransaction) return null;
  const elapsed = startedAt ? ` · ${clock(now - Date.parse(startedAt))}` : "";
  return (
    <span
      className={cn(
        "shrink-0 rounded bg-warning/15 px-1.5 text-[11px] font-medium whitespace-nowrap text-warning tabular-nums",
        pulse && "animate-pulse",
      )}
      title={
        startedAt ? `A transaction is open since ${new Date(startedAt).toLocaleTimeString()}` : "A transaction is open"
      }
    >
      Transaction{elapsed}
    </span>
  );
}
