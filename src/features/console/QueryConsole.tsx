import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import type { SocketStatus } from "@/lib/api/session-socket";
import { cn } from "@/lib/cn";
import type { ConsoleTab } from "@/stores/workspace";
import { useProfiles } from "../connections/hooks";
import { UnlockDialog } from "../connections/UnlockDialog";
import { applyEvent, interruptExecution, newExecution, type ExecutionState } from "./execution";
import { ResultTable } from "./ResultTable";
import { useConsoleSession } from "./useConsoleSession";

const PAGE = 500;
const INDENT = "  ";
const STATUS_DOT: Record<SocketStatus, string> = { open: "bg-success", connecting: "bg-yellow-400", closed: "bg-danger" };

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

export function QueryConsole({ tab }: { tab: ConsoleTab }) {
  const { session, socket, status, error, retry } = useConsoleSession(tab.profileId, tab.database);
  const profiles = useProfiles();
  const [sql, setSql] = useState("");
  const [execution, setExecution] = useState<ExecutionState | null>(null);
  const [origin, setOrigin] = useState<Origin>(WHOLE_TEXT);
  const [unlockDismissed, setUnlockDismissed] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);

  const connectionLost = useRef(false);
  /** The last `in_transaction` the backend reported for this console's session. */
  const inTransaction = useRef(false);
  const connection: SocketStatus = error ? "closed" : status;

  useEffect(() => {
    setExecution(null); // results belong to the session that produced them
    connectionLost.current = false;
    inTransaction.current = false; // a fresh session starts outside any transaction
    if (!socket) return;
    return socket.subscribe((event) => {
      if (event.type === "finished") inTransaction.current = event.in_transaction;
      setExecution((prev) => (prev && event.request_id === prev.requestId ? applyEvent(prev, event) : prev));
    });
  }, [socket]);

  const latestExecution = useRef(execution);
  useEffect(() => {
    latestExecution.current = execution;
  }, [execution]);

  // Events sent while the socket was down are gone for good, so an unfinished request can never complete: end it
  // when the socket is back (or when the session turns out to be gone) instead of leaving it "Running…".
  useEffect(() => {
    if (status === "closed") connectionLost.current = true;
    const reconnected = status === "open" && connectionLost.current;
    if (reconnected) connectionLost.current = false;
    if (!reconnected && !error) return;
    const orphan = latestExecution.current;
    // Cancelling it stops it from blocking the next statement, but inside a transaction a cancel would abort the
    // whole transaction: there the request is only marked interrupted and the user decides.
    if (reconnected && orphan && orphan.status !== "done" && !inTransaction.current) {
      socket?.send({ type: "cancel", request_id: orphan.requestId });
    }
    setExecution((prev) => (prev ? interruptExecution(prev) : prev));
  }, [status, error, socket]);

  // Only while the socket is open: a command sent into a dropped connection would be queued and replayed late.
  const canRun = !!socket && connection === "open" && execution?.status !== "running";
  const run = () => {
    const el = editor.current;
    if (!canRun || !el) return;
    const hasSelection = el.selectionEnd > el.selectionStart;
    const text = hasSelection ? sql.slice(el.selectionStart, el.selectionEnd) : sql;
    if (!text.trim()) return;
    setOrigin(hasSelection ? originOf(sql, el.selectionStart) : WHOLE_TEXT);
    const requestId = crypto.randomUUID();
    setExecution(newExecution(requestId));
    socket.send({ type: "execute", request_id: requestId, sql: text, page_size: PAGE });
  };
  const cancel = () => {
    if (execution) socket?.send({ type: "cancel", request_id: execution.requestId });
  };
  const loadMore = () => {
    if (!execution || !socket) return;
    setExecution({ ...execution, status: "running" });
    socket.send({ type: "fetch_more", request_id: execution.requestId, count: PAGE });
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
        <Button size="sm" onClick={cancel} disabled={execution?.status !== "running"}>
          Cancel
        </Button>
        <div className="flex-1" />
        <span className={cn("h-2 w-2 rounded-full", STATUS_DOT[connection])} title={connection} />
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
                      {set.hasMore && execution.status === "paused" && connection === "open" && (
                        <Button size="sm" onClick={loadMore}>
                          Load {PAGE} more
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
                  {execution.transactionFailed ? (
                    <span className="rounded bg-danger/15 px-1.5 font-medium text-danger">
                      Transaction aborted — ROLLBACK required
                    </span>
                  ) : (
                    execution.inTransaction && (
                      <span className="rounded bg-orange-500/20 px-1.5 text-orange-600 dark:text-orange-400">
                        In transaction
                      </span>
                    )
                  )}
                </div>
              </div>
            )}
          </Panel>
        </Group>
      </div>
    </div>
  );
}
