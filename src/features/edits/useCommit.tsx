import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { toast } from "@/components/ui/Toast";
import type { Dialect, Profile } from "@/lib/api/types";
import { dispatchStaged, readStagedEdits } from "@/stores/stagedEdits";
import type { TableTabState } from "@/stores/workspace";
import type { DataGridHandle } from "../grid/DataGrid";
import { refreshHistory } from "../history/useHistory";
import type { ExecutionState } from "../session/execution";
import type { SessionController, SessionSnapshot } from "../session/SessionController";
import { saveWorkspaceNow } from "../workspace/workspaceSync";
import {
  afterglowNow,
  afterglowOf,
  applyingStep,
  browseWasPaused,
  closable,
  commitBlocked,
  commitStep,
  NO_CHANGES,
  NOT_SHOWN,
  nextSqlModeRefusals,
  outcomeView,
  previewed,
  previewFailed,
  previewing,
  refreshBy,
  resultStep,
  type Afterglow,
  type ApplyingStep,
  type BrowseNow,
  type CommitDialogState,
} from "./commitDialog";
import {
  abandonedCommit,
  commitState,
  NOT_SENT_REPORT,
  problemsOf,
  rerunAfter,
  ROLLBACK_UNCONFIRMED,
  rollbackToast,
  stagedOutcome,
  type CommitAction,
  type SettledCommit,
} from "./commitFlow";
import { editsErrorMessage, previewEdits } from "./editsApi";
import { PreviewDialog } from "./PreviewDialog";
import { changeCount, stagingOf, toChanges } from "./staging";
import type { TableEdits } from "./useTableEdits";

/*
 * Committing a table tab's staged changes (spec ⑤ §3.5, §3.6.5, R-05-6, R-05-7): ⌘S or Commit… previews them (the
 * backend writes the statements), Commit applies that preview's plan on the tab's own session, and the outcome goes
 * back into the staging. Nothing is ever sent twice: an apply whose outcome did not arrive is unknown, and Commit stays
 * off until fresh rows were read. Only the plan of the preview on screen is applied (05a M7): its id lives in the
 * dialog's state alone, and a preview whose staging changed since is made again (H8). What is decided when is in the
 * pure `commitDialog.ts`; this hook does the I/O. Console lines carry counts and outcomes only, never a value or a key.
 */

/**
 * The commit dialog's state and the afterglow (pre-flight M5), held by the Data view above its edits: the edits need
 * them in the same render (a running commit locks the staging; the afterglow is drawn instead of the staging), and the
 * commit needs the edits. `browse` is the browse now: the afterglow goes once another request has delivered its rows,
 * or a new sort or filter started (`afterglowHolds`), and is never drawn again.
 */
export function useCommitState(browse: BrowseNow) {
  const [dialog, setDialogState] = useState<CommitDialogState | null>(null);
  const [afterglow, setAfterglow] = useState<Afterglow | null>(null);
  // Every step goes through `setDialog`: `read()` is the step as last set, even before React renders it.
  const latest = useRef<CommitDialogState | null>(null);
  const setDialog = useCallback((next: CommitDialogState | null) => {
    latest.current = next;
    setDialogState(next);
  }, []);
  const read = useCallback(() => latest.current, []);
  const kept = afterglowNow(afterglow, browse);
  if (kept !== afterglow) setAfterglow(kept); // gone for good: a later load (deliveredRequest null) must not bring it back
  return {
    dialog,
    setDialog,
    read,
    setAfterglow,
    /** A commit is running: nothing is staged meanwhile (the edits' `locked`) and the tab does not close. */
    applying: dialog?.step === "applying",
    /** What the grid draws instead of the staging until the re-read's rows are in, or null. */
    afterglow: kept?.staged ?? null,
  };
}

export type CommitHolder = ReturnType<typeof useCommitState>;

export interface CommitInput {
  tab: TableTabState;
  /** The table as the dialog and the toast name it (schema.name). */
  table: string;
  profile: Profile | undefined;
  dialect: Dialect | null;
  controller: SessionController | null;
  snapshot: SessionSnapshot | null;
  browse: BrowseNow & {
    execution: ExecutionState | null;
    /** The browse's rows are being read: held while a re-run loads them, or not there yet. */
    pending: boolean;
    /** Runs the browse again keeping the position (D-05-12): the re-read after an outcome. */
    reread: () => void;
    /** The user's Refresh (the result's Refresh): the browse again, and the edit target. */
    refresh: () => void;
  };
  edits: Pick<TableEdits, "staged" | "readOnlyReason" | "locate" | "flushEditor">;
  exporting: boolean;
  gridRef: RefObject<DataGridHandle | null>;
  /** Reconnect: a new session for the tab. */
  retrySession: () => void;
  /** `useCommitState()`'s, called above the edits. */
  holder: CommitHolder;
}

export interface Commit {
  /** ⌘S, Commit…, the palette's Commit changes…: an open editor's text is staged first. */
  open: () => void;
  /** An editor's ⌘S (`onRequestCommit`): it staged its text and closed itself already. */
  openAfterEdit: () => void;
  /** Why Commit… cannot start now, or null. */
  disabledReason: string | null;
  /** A commit is running: nothing is staged meanwhile and the tab does not close. */
  applying: boolean;
  dialog: ReactNode;
}

export function useCommit(input: CommitInput): Commit {
  const { tab, holder, controller, snapshot, edits } = input;
  const queryClient = useQueryClient();
  // The latest render's input, for the handlers and the outcome (they run after the render they were made in).
  const latest = useRef(input);
  useLayoutEffect(() => {
    latest.current = input;
  });
  const tickets = useRef(0);
  const keys = useRef(0);
  /** This tab's `sql_mode_changed` refusals in a row (a hint to reconnect once it repeats). */
  const sqlModeRefusals = useRef(0);
  /** The browse request shown when an outcome was unknown: rows of any other request are the refresh it waits for. */
  const unknownSince = useRef<string | null>(null);
  /** The session an apply was sent on: its outcome is read there, also once the tab has another. */
  const applyController = useRef<SessionController | null>(null);
  /** The apply whose outcome has been taken: it is taken once. */
  const settledRequest = useRef<string | null>(null);
  /** The dialog closes onto the grid (a commit, Show in grid, an action), not onto what opened it (L9). */
  const focusGrid = useRef(false);

  const dialog = holder.dialog;
  const disabledReason = commitBlocked({
    applying: holder.applying,
    sessionOpen: !!controller && snapshot?.phase === "open",
    exporting: input.exporting,
    unknownPending: edits.staged?.unknownOutcome === "pending",
    rowsLoading: input.browse.pending,
    afterglow: holder.afterglow !== null,
    readOnlyReason: edits.readOnlyReason,
  });

  /** Previews what is staged now; the answer shows only if the dialog still waits for this very preview (M7). */
  const startPreview = () => {
    const current = readStagedEdits(tab.id);
    if (!current || changeCount(current) === 0) {
      holder.setDialog(null);
      toast("info", NO_CHANGES);
      return;
    }
    const ticket = ++tickets.current;
    holder.setDialog(previewing(ticket));
    const staging = stagingOf(current);
    const unknownBefore = current.unknownOutcome === "refreshed";
    console.info(`Grid edits: previewing ${changeCount(current)} change(s)`);
    previewEdits(tab.profileId, tab.path, toChanges(current)).then(
      (preview) => {
        const before = holder.read();
        const next = previewed(before, ticket, { preview, staging, key: ++keys.current, unknownBefore });
        if (next === before) return; // closed, or another preview asked for since: this plan is never applied
        dispatchStaged(tab.id, { type: "setProblems", problems: problemsOf(preview) }); // they mark the cells
        console.info(
          `Grid edits: preview of ${preview.statements.length} statement(s), ${preview.problems.length} problem(s)`,
        );
        holder.setDialog(next);
      },
      (error: unknown) => {
        const before = holder.read();
        const next = previewFailed(before, ticket, error, editsErrorMessage(error));
        if (next === before) return;
        console.warn("Grid edit preview failed", error instanceof Error ? error.name : typeof error);
        holder.setDialog(next);
      },
    );
  };

  /** `editorEnded`: an editor's ⌘S, which has staged its text and closed it already. */
  const open = (editorEnded = false) => {
    if (holder.read()) return; // a dialog is up (a held ⌘S)
    // An open editor's text is staged first, never into a change a preview plans (Task 9's carry).
    if (!editorEnded) latest.current.edits.flushEditor();
    const current = readStagedEdits(tab.id);
    if (!current || changeCount(current) === 0) {
      toast("info", NO_CHANGES);
      return;
    }
    if (disabledReason) {
      toast("info", disabledReason);
      return;
    }
    startPreview();
  };

  const close = () => {
    if (!closable(holder.read())) return; // never while a commit runs
    holder.setDialog(null); // a preview still on its way is dropped (`previewed`): its plan is never applied
  };
  const closeOntoGrid = () => {
    focusGrid.current = true;
    close();
  };

  const commit = () => {
    const current = holder.read();
    const step = commitStep(current, readStagedEdits(tab.id));
    if (step.kind === "none" || current?.step !== "preview") return;
    if (step.kind === "repreview") {
      startPreview(); // the staging changed since this preview (H8): preview what is staged now
      return;
    }
    const { controller: session, browse } = latest.current;
    if (!session) {
      holder.setDialog(resultStep(current, NOT_SENT_REPORT, [])); // no session at all: nothing sent, nothing happened
      return;
    }
    const paused = browseWasPaused(browse.execution);
    holder.setAfterglow(null); // the last commit's: this apply takes over
    // H1: unknown until its outcome arrives, and saved so at once: a reload, crash or quit meanwhile restores it as
    // unknown, never as unsent (Commit would send it again: new rows twice).
    dispatchStaged(tab.id, { type: "applying" });
    saveWorkspaceNow("A grid commit is being sent");
    applyController.current = session;
    const requestId = session.applyEdits(step.planId);
    console.info(`Grid edits: applying ${current.preview.statements.length} statement(s)`);
    holder.setDialog(applyingStep(current, requestId, paused));
  };

  /**
   * The apply is over, one way or the other: its outcome goes into the staging (every settled apply ends with exactly
   * one `applyOutcome`, a command that never left included: H1), History is read again, the apply's execution is
   * forgotten, the browse is read again when the outcome asks for it, and the dialog says what happened.
   */
  const settle = (held: ApplyingStep, commit: SettledCommit) => {
    if (settledRequest.current === held.requestId) return;
    settledRequest.current = held.requestId;
    const { browse, table } = latest.current;
    applyController.current?.forget(held.requestId);
    refreshHistory(queryClient);
    if (commit.kind === "ended") {
      // Before the staging changes: the grid keeps drawing what it drew until the re-read is in (M5). Both updates
      // are taken in the same render (this runs in a layout effect or an event), so nothing flips back meanwhile.
      holder.setAfterglow(afterglowOf(commit.outcome, readStagedEdits(tab.id), browse));
      if (commit.outcome === "unknown") unknownSince.current = browse.deliveredRequest;
    }
    sqlModeRefusals.current = nextSqlModeRefusals(sqlModeRefusals.current, commit);
    dispatchStaged(tab.id, { type: "applyOutcome", outcome: stagedOutcome(commit) });
    if (commit.kind === "ended" && rerunAfter(commit.outcome, held.browseWasPaused)) browse.reread();
    console.info(`Grid edits: ${commit.kind === "ended" ? commit.outcome : "not sent"}`);
    const view = outcomeView(commit, { table, counts: held.preview.counts, sqlModeRefusals: sqlModeRefusals.current });
    if (view.kind === "toasts") {
      for (const { kind, text } of view.toasts) toast(kind, text);
      focusGrid.current = true;
      holder.setDialog(null);
      return;
    }
    holder.setDialog(resultStep(held, view.report, view.messages));
  };

  // The apply's events: once it is over, its outcome is taken. A layout effect: the afterglow and the staging change
  // in the same render (a store update and a state update made here are both rendered before the next paint).
  const held = dialog?.step === "applying" ? dialog : null;
  const execution = held ? (snapshot?.executions[held.requestId] ?? null) : null;
  const latestSettle = useRef(settle);
  useLayoutEffect(() => {
    latestSettle.current = settle;
  });
  useLayoutEffect(() => {
    if (!held) return;
    // Read where it was sent: an outcome that arrived before that session closed stands. Gone from it (a closed or
    // replaced session forgot it): its outcome can no longer arrive.
    const current = applyController.current?.store.getState().executions[held.requestId] ?? null;
    if (!current) {
      latestSettle.current(held, abandonedCommit(null));
      return;
    }
    const progress = commitState(current);
    if (progress.kind !== "running") latestSettle.current(held, progress);
  }, [held, execution, controller]);

  // R-05-6: after an unknown outcome, the first rows another request delivers let a commit start again (after a
  // restore: the first rows read at all). A re-read cut short delivers nothing (K17).
  const pendingUnknown = edits.staged?.unknownOutcome === "pending";
  const delivered = input.browse.deliveredRequest;
  useEffect(() => {
    if (!pendingUnknown || delivered === null || delivered === unknownSince.current) return;
    unknownSince.current = null;
    dispatchStaged(tab.id, { type: "refreshed" });
  }, [pendingUnknown, delivered, tab.id]);

  const showInGrid = (changeId: string, column: string | null) => {
    const { edits: now, gridRef } = latest.current;
    const pos = now.locate(changeId, column);
    closeOntoGrid();
    if (!pos) {
      toast("info", NOT_SHOWN);
      return;
    }
    gridRef.current?.selectCell(pos.row, pos.col);
  };

  /** Roll back (`transaction_open`): the session's ROLLBACK; the toast once it has answered (L22). */
  const rollBack = () => {
    const session = latest.current.controller;
    closeOntoGrid();
    if (!session) return;
    const requestId = session.rollback();
    session
      .whenDone(requestId)
      .then(
        (done) => {
          const { kind, text } = rollbackToast(done);
          toast(kind, text);
        },
        () => toast("warning", ROLLBACK_UNCONFIRMED),
      )
      .finally(() => session.forget(requestId));
  };

  const onAction = (action: CommitAction) => {
    const current = holder.read();
    if (current?.step !== "result") return;
    switch (action) {
      case "show_in_grid":
        if (current.report.failedChange) showInGrid(current.report.failedChange, null);
        return;
      case "preview_again":
        startPreview();
        return;
      case "rollback":
        rollBack();
        return;
      case "reconnect":
        closeOntoGrid();
        latest.current.retrySession();
        return;
      case "refresh": {
        closeOntoGrid();
        // A session that is gone (a backend restart) reads nothing: the browse is sent again on a new one, which counts
        // as fresh rows too (R-05-6).
        const { snapshot: now, controller: session } = latest.current;
        if (refreshBy(session ? (now?.phase ?? null) : null) === "reconnect") latest.current.retrySession();
        else latest.current.browse.refresh();
        return;
      }
    }
  };

  /**
   * K11: the outcome may never come; it is unknown, nothing is sent again, Commit stays off until fresh rows. The result
   * says the user stopped waiting (K21), not that the connection was lost.
   */
  const closeUnknown = () => {
    const current = holder.read();
    if (current?.step !== "applying") return;
    const execution = applyController.current?.store.getState().executions[current.requestId] ?? null;
    settle(current, abandonedCommit(execution, true));
  };

  const cancelApply = () => {
    const current = holder.read();
    if (current?.step === "applying") applyController.current?.cancel(current.requestId);
  };

  // The dialog's handlers keep their identity (it is memoized and renders again only when its step changes).
  const handlers = useRef({ close, commit, cancelApply, closeUnknown, onAction, showInGrid, startPreview });
  useLayoutEffect(() => {
    handlers.current = { close, commit, cancelApply, closeUnknown, onAction, showInGrid, startPreview };
  });
  const stable = useMemo(
    () => ({
      onClose: () => handlers.current.close(),
      onCommit: () => handlers.current.commit(),
      onCancelApply: () => handlers.current.cancelApply(),
      onCloseUnknown: () => handlers.current.closeUnknown(),
      onAction: (action: CommitAction) => handlers.current.onAction(action),
      onShowInGrid: (changeId: string, column: string | null) => handlers.current.showInGrid(changeId, column),
      onPreviewAgain: () => handlers.current.startPreview(),
      onClosed: (event: Event) => {
        if (!focusGrid.current) return; // back to what opened it
        focusGrid.current = false;
        event.preventDefault();
        latest.current.gridRef.current?.focus();
      },
    }),
    [],
  );

  const store = held ? (applyController.current?.store ?? null) : null;
  const dialogNode = dialog ? (
    <PreviewDialog
      state={dialog}
      table={input.table}
      profile={input.profile}
      dialect={input.dialect}
      store={store}
      {...stable}
    />
  ) : null;

  return {
    open: () => open(),
    openAfterEdit: () => open(true),
    disabledReason,
    applying: holder.applying,
    dialog: dialogNode,
  };
}
