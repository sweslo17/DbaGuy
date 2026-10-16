import { useRef, useState, type KeyboardEvent } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/cn";
import type { ConsoleTab } from "@/stores/workspace";
import { useProfiles } from "../connections/hooks";
import { secretsNeeded, UnlockDialog } from "../connections/UnlockDialog";
import { PAGE_SIZE, type SessionPhase, type SessionStateView } from "../session/SessionController";
import { useSession } from "../session/useSession";
import { ResultTable } from "./ResultTable";

/** Error code of a statement whose database connection dropped: the session must be reopened. */
const CONNECTION_LOST = "connection_lost";
const INDENT = "  ";
const PHASE_DOT: Record<SessionPhase, string> = {
  opening: "bg-yellow-400",
  open: "bg-success",
  reconnecting: "bg-yellow-400",
  failed: "bg-danger",
  closed: "bg-danger",
};

/** Where the executed text starts inside the editor: 0-based line and 0-based column (in characters). */
interface Origin {
  line: number;
  column: number;
}
const WHOLE_TEXT: Origin = { line: 0, column: 0 };

function originOf(text: string, offset: number): Origin {
  const before = text.slice(0, offset);
  const lineStart = before.lastIndexOf("\n") + 1;
  return { line: before.split("\n").length - 1, column: Array.from(before.slice(lineStart)).length };
}

const plural = (count: number, noun: string) => `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;

/** The session's transaction state, from `session_state` only. */
function TransactionBadge({ state }: { state: SessionStateView | null }) {
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
    <span
      className="rounded bg-orange-500/20 px-1.5 text-[11px] text-orange-600 dark:text-orange-400"
      title="A transaction is open"
    >
      Transaction
    </span>
  );
}

export function QueryConsole({ tab }: { tab: ConsoleTab }) {
  const { controller, snapshot, retry } = useSession(tab.profileId, tab.database);
  const profiles = useProfiles();
  const [sql, setSql] = useState("");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [origin, setOrigin] = useState<Origin>(WHOLE_TEXT);
  const [unlockDismissed, setUnlockDismissed] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);

  const phase: SessionPhase = snapshot?.phase ?? "opening";
  const session = snapshot?.info ?? null;
  const error = snapshot?.error ?? null;
  // Results belong to the session that produced them: a new session knows none of the old request ids.
  const execution = requestId ? (snapshot?.executions[requestId] ?? null) : null;

  const canRun = !!controller && phase === "open" && execution?.status !== "running";
  const run = () => {
    const el = editor.current;
    if (!canRun || !controller || !el) return;
    const hasSelection = el.selectionEnd > el.selectionStart;
    const text = hasSelection ? sql.slice(el.selectionStart, el.selectionEnd) : sql;
    if (!text.trim()) return;
    setOrigin(hasSelection ? originOf(sql, el.selectionStart) : WHOLE_TEXT);
    if (requestId) controller.forget(requestId); // only the latest result is shown: do not keep the old rows
    setRequestId(controller.execute(text, { pageSize: PAGE_SIZE }));
  };
  const cancel = () => {
    if (execution) controller?.cancel(execution.requestId);
  };
  const loadMore = () => {
    if (execution) controller?.fetchMore(execution.requestId, PAGE_SIZE);
  };
  const retrySession = () => {
    setUnlockDismissed(false);
    retry();
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && e.metaKey) {
      e.preventDefault();
      run();
    } else if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      const el = e.currentTarget;
      el.setRangeText(INDENT, el.selectionStart, el.selectionEnd, "end");
      setSql(el.value);
    }
  };

  const profile = profiles.data?.find((p) => p.profile.id === tab.profileId)?.profile;
  const needsPassword = error instanceof ApiError && error.code === "password_required" && !!profile;
  const errorPosition = execution?.error?.position;

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-2">
        <Button variant="primary" size="sm" onClick={run} disabled={!canRun}>
          Run ⌘↵
        </Button>
        <Button size="sm" onClick={cancel} disabled={phase !== "open" || execution?.status !== "running"}>
          Cancel
        </Button>
        <div className="flex-1" />
        <TransactionBadge state={snapshot?.state ?? null} />
        <span className={cn("h-2 w-2 rounded-full", PHASE_DOT[phase])} title={phase} />
        <span className="truncate text-[11px] text-muted">
          {session
            ? `${session.server.version}${session.database ? ` · ${session.database}` : ""}`
            : error
              ? "Not connected"
              : "Connecting…"}
        </span>
      </div>
      {error && (!needsPassword || unlockDismissed) && (
        <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-[12px] text-danger" data-selectable>
          <span className="flex-1 whitespace-pre-wrap">{error.message}</span>
          <Button size="sm" onClick={retrySession}>
            Retry
          </Button>
        </div>
      )}
      {needsPassword && profile && (
        <UnlockDialog
          open={!unlockDismissed}
          profile={profile}
          needs={secretsNeeded(error instanceof ApiError ? error.detail : undefined)}
          onOpenChange={(open) => !open && setUnlockDismissed(true)}
          onUnlocked={retrySession}
        />
      )}
      <div className="min-h-0 flex-1">
        <Group orientation="vertical">
          <Panel id="editor" defaultSize="40%" minSize="15%">
            <textarea
              ref={editor}
              value={sql}
              onChange={(e) => setSql(e.target.value)}
              onKeyDown={onKeyDown}
              spellCheck={false}
              placeholder="SELECT …  (⌘↵ to run)"
              className="block h-full w-full resize-none bg-bg p-3 font-mono text-[13px] outline-none"
            />
          </Panel>
          <Separator className="h-px bg-border outline-none data-[separator=active]:bg-accent data-[separator=hover]:bg-accent data-[separator=focus]:bg-accent" />
          <Panel id="results" minSize="20%" className="overflow-auto">
            {execution && (
              <div className="flex flex-col gap-3 p-3 text-[12px]">
                {execution.error && (
                  <div className="rounded-md border border-danger/40 bg-danger/10 p-2 text-danger" data-selectable>
                    <div className="whitespace-pre-wrap">{execution.error.message}</div>
                    {errorPosition && (
                      <div className="mt-1 text-[11px]">
                        Line {errorPosition.line + origin.line}, column{" "}
                        {errorPosition.column + (errorPosition.line === 1 ? origin.column : 0)}
                        {execution.error.code ? ` · ${execution.error.code}` : ""}
                      </div>
                    )}
                    {execution.error.code === CONNECTION_LOST && (
                      <Button size="sm" className="mt-2" onClick={retrySession}>
                        Reconnect
                      </Button>
                    )}
                  </div>
                )}
                {execution.resultSets.map((set) => (
                  <div key={set.index} className="flex flex-col gap-1">
                    <ResultTable set={set} />
                    <div className="flex items-center gap-2 text-muted">
                      <span>
                        {plural(set.rows.length, "row")}
                        {set.hasMore ? " loaded" : ""}
                      </span>
                      {set.hasMore && execution.status === "paused" && phase === "open" && (
                        <Button size="sm" onClick={loadMore}>
                          Load {PAGE_SIZE} more
                        </Button>
                      )}
                    </div>
                  </div>
                ))}
                {execution.affected.map((a) => (
                  <div key={a.index}>{plural(a.rows, "row")} affected</div>
                ))}
                {execution.messages.map((m, i) => (
                  <div
                    key={i}
                    className={m.severity === "warning" ? "text-yellow-600 dark:text-yellow-400" : "text-muted"}
                    data-selectable
                  >
                    {m.text}
                  </div>
                ))}
                <div className="flex items-center gap-2 text-muted">
                  {execution.status === "running" && <span>Running…</span>}
                  {execution.elapsedMs !== null && <span>{execution.elapsedMs} ms</span>}
                  {execution.cancelled && <span>Cancelled</span>}
                </div>
              </div>
            )}
          </Panel>
        </Group>
      </div>
    </div>
  );
}
