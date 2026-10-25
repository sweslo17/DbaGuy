import { Loader2 } from "lucide-react";
import { useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Dialog } from "@/components/ui/Dialog";
import { Checkbox, FieldRow, Input, Select } from "@/components/ui/Field";
import { FilePathField } from "@/components/ui/FilePathField";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { FormatKind, RowFormatOptions } from "@/lib/api/types";
import { BROWSER_PATH_HINT, pathProblem as typedPathProblem, safeFileStem } from "@/lib/filePaths";
import { formatElapsed, plural } from "@/lib/format";
import { chooseSavePath } from "@/lib/native";
import { useCancelAgain } from "@/lib/useCancelAgain";
import { useNow } from "@/lib/useNow";
import { useEngineInfo } from "../connections/hooks";
import { INTERRUPTED_MESSAGE, type ExecutionState } from "../session/execution";
import type { RequestOptions, SessionController } from "../session/SessionController";
import { cancelAbortsTransaction, EXPORT_CANCEL_ABORTS_TRANSACTION } from "../session/transactions";
import { COPY_FORMAT_LABELS, copyFormatsFor } from "./copy";

type TableName = { schema: string | null; name: string };

/** What the export dialog writes: the statement behind a grid, run again in full on the grid's own session. */
export interface ExportSource {
  controller: SessionController;
  /** The statement that produced the result (the browse SELECT for table data). */
  sql: string;
  /**
   * What the statement was sent with (Couchbase: its query context and parameters): the export runs it the same way.
   * Absent: the session's default context, no parameters.
   */
  request?: RequestOptions;
  /** Default file name stem. */
  title: string;
  /** Prefills the INSERT target (table data). */
  tableName?: TableName | null;
  /** A line the dialog shows above its settings (table data: staged changes are not in the export). */
  note?: string | null;
}

type ExportFormat = Exclude<FormatKind, "in_list">;

interface FormatSpec {
  label: string;
  ext: string;
  /** A table of text cells: the header row and NULL text options apply. */
  textTable: boolean;
}

/** In menu order. */
const FORMATS: Record<ExportFormat, FormatSpec> = {
  csv: { label: COPY_FORMAT_LABELS.csv, ext: "csv", textTable: true },
  tsv: { label: COPY_FORMAT_LABELS.tsv, ext: "tsv", textTable: true },
  json: { label: COPY_FORMAT_LABELS.json, ext: "json", textTable: false },
  jsonl: { label: "JSONL", ext: "jsonl", textTable: false },
  markdown: { label: COPY_FORMAT_LABELS.markdown, ext: "md", textTable: true },
  insert: { label: COPY_FORMAT_LABELS.insert, ext: "sql", textTable: false },
};
const FORMAT_ORDER = Object.keys(FORMATS) as ExportFormat[];
const NULL_TEXTS = [
  { value: "", label: "Empty" },
  { value: "NULL", label: "NULL" },
];
/** The backend's own placeholder when no INSERT target is known. */
const TABLE_PLACEHOLDER = "table_name";

type Outcome = { kind: "error"; message: string; code?: string | null } | { kind: "cancelled" };

/** `<title>.<ext>` without what macOS does not take in a file name (`safeFileStem`). */
const defaultFileName = (title: string, ext: string) => `${safeFileStem(title, "export")}.${ext}`;

const qualified = (table: TableName) => (table.schema ? `${table.schema}.${table.name}` : table.name);

/**
 * The INSERT target typed as `schema.name` or `name`. The prefill is used as given, so a name containing a dot
 * survives; anything else splits at its first dot. Empty: the backend's `table_name` placeholder.
 */
function tableTarget(text: string, prefill: TableName | null): Pick<RowFormatOptions, "table_schema" | "table_name"> {
  const trimmed = text.trim();
  if (prefill && trimmed === qualified(prefill)) return { table_schema: prefill.schema, table_name: prefill.name };
  const dot = trimmed.indexOf(".");
  const [schema, name] = dot < 0 ? ["", trimmed] : [trimmed.slice(0, dot), trimmed.slice(dot + 1)];
  return { table_schema: schema || null, table_name: name || null };
}

/** The backend reports the export ("Exported 25,000 rows to /path") as its last info message. */
function successMessage(done: ExecutionState, path: string): string {
  const reported = done.messages.findLast((message) => message.severity === "info")?.text;
  return reported ?? `Exported ${plural(done.progressRows ?? 0, "row")} to ${path}`;
}

function ExportProgress({ path, rows, startedAt }: { path: string; rows: number; startedAt: number }) {
  const now = useNow(500);
  return (
    <div className="flex flex-col gap-2 text-[13px]">
      <div className="flex items-center gap-2">
        <Loader2 size={14} className="animate-spin text-muted" />
        Exporting to
      </div>
      <div className="font-mono text-[12px] break-all" data-selectable>
        {path}
      </div>
      <div className="text-muted tabular-nums">
        {plural(rows, "row")} written · {formatElapsed(now - startedAt)}
      </div>
    </div>
  );
}

function OutcomeNote({ outcome }: { outcome: Outcome }) {
  if (outcome.kind === "cancelled") return <p className="text-[12px] text-muted">Export cancelled</p>;
  return (
    <div
      className="rounded-md border border-danger/40 bg-danger/10 p-2 text-[12px] whitespace-pre-wrap text-danger"
      data-selectable
    >
      {outcome.message}
      {outcome.code && <div className="mt-1 text-[11px] opacity-80">{outcome.code}</div>}
    </div>
  );
}

/**
 * Writes the whole result behind a grid to a file (spec §3.3, §4.4): the backend runs `source.sql` again on the same
 * session and streams the raw values through the shared formatter, which also ends the grid's paused page. While the
 * export runs the dialog shows its progress and can only be left by cancelling it — or closed while the session is
 * not connected, since the export is interrupted then anyway; an error or a cancel keeps the dialog open with the
 * settings, so the path can be changed and the export retried. Inside an open transaction on an engine where cancelling
 * aborts it (PostgreSQL), the dialog says so beside Cancel and asks before cancelling. Cancel can be clicked again a
 * second later, or at once when the export gets a warning, which the dialog shows (SQL Server: the cancel cannot stop
 * it safely, and what another Cancel does). Hosted by the tab, so a remounting grid never ends it.
 */
export function ExportDialog({
  source,
  open,
  onOpenChange,
}: {
  source: ExportSource;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { controller, sql, request, title } = source;
  const engine = useEngineInfo(controller.profileId);
  const prefill = source.tableName ?? null;
  // The formats of the engine's dialect: SQL++ has no INSERT by columns, and `copyFormatsFor` is where that is decided.
  const dialect = engine?.dialect ?? null;
  const formats = useMemo(
    () => FORMAT_ORDER.filter((kind) => kind !== "insert" || copyFormatsFor(dialect).includes("insert")),
    [dialect],
  );
  const [format, setFormat] = useState<ExportFormat>("csv");
  const [header, setHeader] = useState(true);
  const [nullText, setNullText] = useState("");
  const [table, setTable] = useState(() => (prefill ? qualified(prefill) : TABLE_PLACEHOLDER));
  const [path, setPath] = useState("");
  const [running, setRunning] = useState<{ requestId: string; path: string; startedAt: number } | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const execution = useStore(controller.store, (s) => (running ? s.executions[running.requestId] : undefined));
  const connected = useStore(controller.store, (s) => s.phase === "open");
  const inTransaction = useStore(controller.store, (s) => !!s.state?.inTransaction && !s.state.transactionFailed);
  const abortsTransaction = inTransaction && cancelAbortsTransaction(engine);
  const warnings = execution?.messages.filter((message) => message.severity === "warning") ?? [];
  // Cancel comes back a second after a click, or as soon as the export gets a warning (`useCancelAgain`)
  const cancelAgain = useCancelAgain(warnings.length);
  const cancelling = cancelAgain.cancelling;
  const pathInput = useRef<HTMLInputElement>(null);
  const isOpen = useRef(open);
  useEffect(() => {
    isOpen.current = open;
  }, [open]);
  // After an error or a cancel the form is back: the path is what usually changes before a retry.
  useEffect(() => {
    if (outcome) pathInput.current?.focus();
  }, [outcome]);

  const spec = FORMATS[format];
  const target = path.trim();
  const pathProblem = typedPathProblem(target);
  const canStart = !running && !!target && !pathProblem;

  /** A file name ending in the old format's extension follows the format. */
  const changeFormat = (next: ExportFormat) => {
    const from = `.${spec.ext}`;
    if (path.endsWith(from)) setPath(`${path.slice(0, -from.length)}.${FORMATS[next].ext}`);
    setFormat(next);
  };

  // The engine arrives after the dialog may have been opened (or the tab's connection changed): a format it cannot
  // write is not kept; going through `changeFormat` also remaps a file name's extension.
  const dropUnsupportedFormat = useEffectEvent(() => {
    if (!formats.includes(format)) changeFormat("csv");
  });
  useEffect(() => {
    dropUnsupportedFormat();
  }, [formats, format]);

  const choose = async (): Promise<string | null> => {
    try {
      const filters = [{ name: spec.label, extensions: [spec.ext] }];
      return await chooseSavePath(defaultFileName(title, spec.ext), filters); // null: the user cancelled the panel
    } catch (error) {
      setOutcome({ kind: "error", message: `Could not open the save panel: ${errorMessage(error)}` });
      return null;
    }
  };

  const start = () => {
    if (!canStart) return;
    const options: RowFormatOptions = {
      header: spec.textTable ? header : true,
      null_text: spec.textTable ? nullText : "",
      ...(format === "insert" ? tableTarget(table, prefill) : {}),
    };
    const requestId = controller.exportResult(sql, format, target, options, request);
    setRunning({ requestId, path: target, startedAt: Date.now() });
    cancelAgain.reset();
    setOutcome(null);
    const settle = (next: Outcome | null) => {
      controller.forget(requestId);
      setRunning(null);
      cancelAgain.reset();
      setConfirmCancel(false);
      if (next?.kind === "error" && !isOpen.current) {
        // Closed while disconnected: say how it ended now, not in a note waiting for the next opening.
        toast("error", `Export failed: ${next.message}`);
        return;
      }
      setOutcome(next);
    };
    controller.whenDone(requestId).then(
      (done) => {
        if (done.error) settle({ kind: "error", message: done.error.message, code: done.error.code });
        else if (done.interrupted) settle({ kind: "error", message: INTERRUPTED_MESSAGE });
        else if (done.cancelled) settle({ kind: "cancelled" });
        else {
          settle(null);
          toast("success", successMessage(done, target));
          onOpenChange(false);
        }
      },
      (error: unknown) => settle({ kind: "error", message: errorMessage(error) }),
    );
  };

  const cancel = () => {
    setConfirmCancel(false);
    if (!running) return;
    controller.cancel(running.requestId);
    cancelAgain.sent();
  };
  const askCancel = () => (abortsTransaction ? setConfirmCancel(true) : cancel());

  /**
   * A running export keeps the dialog open (Escape and outside clicks do nothing, Cancel stops it) while the session is
   * connected; without a connection Cancel cannot reach the backend and the export ends as interrupted, so it closes.
   */
  const changeOpen = (next: boolean) => {
    if (running && connected) return;
    if (!next) setOutcome(null);
    onOpenChange(next);
  };

  // Export keeps its slot (disabled while running), so a double click on it never lands on Cancel.
  const footer = (
    <>
      {running && connected && abortsTransaction && (
        <span className="mr-auto self-center text-[12px] text-warning">{EXPORT_CANCEL_ABORTS_TRANSACTION}</span>
      )}
      {running && connected ? (
        <Button onClick={askCancel} disabled={cancelling}>
          {cancelling ? "Cancelling…" : "Cancel"}
        </Button>
      ) : (
        <Button variant="ghost" onClick={() => changeOpen(false)}>
          Close
        </Button>
      )}
      <Button variant="primary" onClick={start} disabled={!canStart}>
        Export
      </Button>
    </>
  );

  return (
    <Dialog open={open} onOpenChange={changeOpen} title="Export result" width={520} footer={footer}>
      <ConfirmDialog
        open={confirmCancel && abortsTransaction && !!running && connected}
        onOpenChange={setConfirmCancel}
        title="Cancel the export?"
        message={`${EXPORT_CANCEL_ABORTS_TRANSACTION}: afterwards it can only be rolled back.`}
        confirmLabel="Cancel export"
        cancelLabel="Keep exporting"
        onConfirm={cancel}
      />
      {running ? (
        <div className="flex flex-col gap-2">
          <ExportProgress path={running.path} rows={execution?.progressRows ?? 0} startedAt={running.startedAt} />
          {warnings.length > 0 && (
            <p className="text-[12px] whitespace-pre-wrap text-warning" data-selectable>
              {warnings[warnings.length - 1].text}
            </p>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {source.note && (
            <p className="text-[12px] text-warning" data-selectable>
              {source.note}
            </p>
          )}
          <FieldRow label="Format">
            <Select value={format} onChange={(e) => changeFormat(e.target.value as ExportFormat)}>
              {formats.map((kind) => (
                <option key={kind} value={kind}>
                  {FORMATS[kind].label}
                </option>
              ))}
            </Select>
          </FieldRow>
          {spec.textTable && (
            <>
              <FieldRow label="Header">
                <Checkbox
                  label="Column names in the first row"
                  checked={header}
                  onChange={(e) => setHeader(e.target.checked)}
                />
              </FieldRow>
              <FieldRow label="NULL as">
                <Select value={nullText} onChange={(e) => setNullText(e.target.value)}>
                  {NULL_TEXTS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              </FieldRow>
            </>
          )}
          {format === "insert" && (
            <FieldRow label="Table name" hint="schema.name or name">
              <Input
                value={table}
                onChange={(e) => setTable(e.target.value)}
                placeholder={TABLE_PLACEHOLDER}
                className="font-mono"
              />
            </FieldRow>
          )}
          <FilePathField
            inputRef={pathInput}
            value={path}
            onChange={setPath}
            onEnter={start}
            choose={choose}
            placeholder={`/path/to/${defaultFileName(title, spec.ext)}`}
            browserHint={BROWSER_PATH_HINT}
            error={pathProblem}
          />
          {outcome && <OutcomeNote outcome={outcome} />}
        </div>
      )}
    </Dialog>
  );
}
