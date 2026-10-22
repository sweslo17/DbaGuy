import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useEffect, useEffectEvent, useId, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Checkbox, FieldRow, Radio } from "@/components/ui/Field";
import { FilePathField } from "@/components/ui/FilePathField";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import type { DumpObjectKinds, DumpPlan, Engine, ObjectNode } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { BROWSER_PATH_HINT, pathProblem } from "@/lib/filePaths";
import { chooseSavePath } from "@/lib/native";
import { useDebouncedValue } from "@/lib/useDebouncedValue";
import { useStagedEdits } from "@/stores/stagedEdits";
import { useEngineInfo, useProfiles } from "../connections/hooks";
import { RecoverableError } from "../connections/RecoverableError";
import { isPasswordRequired } from "../connections/UnlockDialog";
import { changeCount } from "../edits/staging";
import { NOT_IN_DUMP } from "../edits/tableEditing";
import { childrenQuery } from "../explorer/useTreeRows";
import { closeDataDialog, openJobDialog } from "./dataDialogs";
import {
  dumpChoices,
  dumpCommand,
  dumpDialogTitle,
  dumpPlanRequest,
  dumpProblem,
  dumpTitle,
  initialDumpForm,
  planLineText,
  planSummary,
  planTables,
  planWarnings,
  type Choice,
  type DumpForm,
} from "./dumpForm";
import type { DumpEntry } from "./entryPoints";
import { dumpFileName } from "./fileNames";
import { fetchDumpPlan } from "./jobsApi";
import { startJob } from "./jobsStore";
import { ConnectionLine } from "./JobViews";

/** The plan is read again once the typed path has stayed the same this long (its warnings are about the target). */
const PLAN_PATH_DEBOUNCE_MS = 400;

const DROP_FIRST_HINT = "A DROP … IF EXISTS before each object the file creates; nothing else is dropped";
const FILTER_TITLE_PREFIX = "WHERE ";

/** The reasons of a group's greyed choices, each once: shown under the group (a `title` lands on the input only). */
const reasonsOf = (choices: readonly Choice<unknown>[]) =>
  [...new Set(choices.flatMap((choice) => (choice.disabled ? [choice.disabled] : [])))].join(" · ");

/** A read that failed: a missing password asks for it (then reads again); another error says why, with Retry. */
function ReadProblem({ profileId, error, retry }: { profileId: string; error: Error; retry: () => void }) {
  if (isPasswordRequired(error)) return <RecoverableError profileId={profileId} error={error} onRetry={retry} />;
  return (
    <div className="flex items-start gap-2 text-[12px] text-danger">
      <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
        {errorMessage(error)}
      </span>
      <Button size="sm" onClick={retry}>
        Retry
      </Button>
    </div>
  );
}

/** The Tables folder's tables, every one checked the first time they arrive (§3.3.1); All, None and the count. */
function TablePicker({
  tables,
  checked,
  onChange,
}: {
  tables: readonly ObjectNode[];
  checked: readonly string[];
  onChange: (names: string[]) => void;
}) {
  const on = new Set(checked);
  // The tree's order, whatever the order of the clicks
  const toggle = (name: string, value: boolean) =>
    onChange(tables.map((table) => table.name).filter((n) => (n === name ? value : on.has(n))));
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1 text-[12px]">
        <Button size="sm" variant="ghost" onClick={() => onChange(tables.map((table) => table.name))}>
          All
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onChange([])}>
          None
        </Button>
        <span className="ml-auto text-muted tabular-nums">
          {checked.length.toLocaleString()} of {tables.length.toLocaleString()} checked
        </span>
      </div>
      <div className="flex max-h-48 flex-col gap-0.5 overflow-y-auto rounded-md border border-border px-2 py-1 [scrollbar-gutter:stable]">
        {tables.length === 0 && <p className="text-[12px] text-muted">No tables</p>}
        {tables.map((table) => (
          <div key={table.name} className="flex min-w-0 items-center gap-2">
            <Checkbox
              label={<span className="[overflow-wrap:anywhere]">{table.name}</span>}
              checked={on.has(table.name)}
              onChange={(e) => toggle(table.name, e.target.checked)}
            />
            {table.detail && <span className="ml-auto shrink-0 text-[11px] text-muted">{table.detail}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

/** What the file will hold, from the plan (`/data/dump/plan`): counts by kind, the objects, the snapshot, what is left out. */
function PlanSummary({ plan, stale }: { plan: DumpPlan; stale: boolean }) {
  const [listed, setListed] = useState(false);
  const lines = useMemo(() => planSummary(plan.objects), [plan.objects]);
  return (
    <div className={cn("flex flex-col gap-1 text-[12px] transition-opacity", stale && "opacity-60")}>
      {lines.length === 0 && <p className="text-muted">No objects</p>}
      {lines.map((line) => (
        <p key={line.kind} className="tabular-nums">
          {planLineText(line)}
        </p>
      ))}
      {plan.objects.length > 0 && (
        <div>
          <button
            type="button"
            className="-ml-0.5 flex items-center gap-0.5 rounded text-muted outline-none hover:text-fg focus-visible:ring-2 focus-visible:ring-accent/50"
            aria-expanded={listed}
            onClick={() => setListed((open) => !open)}
          >
            {listed ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            {listed ? "Hide the objects" : "Show the objects"}
          </button>
          {listed && (
            <ul className="mt-1 max-h-48 overflow-y-auto rounded-md border border-border px-2 py-1 [scrollbar-gutter:stable]">
              {plan.objects.map((object, i) => (
                <li key={i} className="py-0.5" data-selectable>
                  <span className="text-muted">{object.kind}</span> <span className="font-mono">{object.name}</span>
                  {object.notes.map((note, j) => (
                    <span key={j} className="block pl-3 text-[11px] text-muted">
                      {note}
                    </span>
                  ))}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <p className={plan.consistency === "snapshot" ? undefined : "text-warning"}>
        {plan.consistency === "snapshot"
          ? "One consistent snapshot"
          : "Not one consistent snapshot: each table is read in its turn"}
      </p>
      {plan.not_included.length > 0 && (
        <p className="text-muted [overflow-wrap:anywhere]" data-selectable>
          Not included: {plan.not_included.join(" · ")}
        </p>
      )}
    </div>
  );
}

/** The plan's warnings: a filtered dump that drops its table first, in red (06a F2); the backend's others below it. */
function PlanWarnings({ warnings }: { warnings: readonly string[] }) {
  const { prominent, other } = planWarnings(warnings);
  if (warnings.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5 text-[12px]">
      {prominent.map((warning, i) => (
        <p
          key={i}
          className="rounded-md border border-danger/40 bg-danger/10 p-2 font-medium whitespace-pre-wrap text-danger"
          data-selectable
        >
          {warning}
        </p>
      ))}
      {other.length > 0 && (
        <ul className="flex list-disc flex-col gap-1 pl-4 text-warning">
          {other.map((warning, i) => (
            <li key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
              {warning}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Dump… (spec ⑥ §3.6 DumpDialog): what to dump, how, and to which file, with the backend's plan of what the file will
 * hold and its warnings, read again as the choices change. Dump starts the job on a session of its own and turns this
 * dialog into the job's view. `form`: "Dump again…" starts from the job's own choices.
 */
export function DumpDialog({ entry, form: preset }: { entry: DumpEntry; form: DumpForm | null }) {
  const engine = useEngineInfo(entry.profileId);
  const profiles = useProfiles();
  const profile = profiles.data?.find((item) => item.profile.id === entry.profileId)?.profile ?? null;
  const engineKind = engine?.engine ?? profile?.engine ?? null;
  const [form, setForm] = useState<DumpForm>(() => preset ?? initialDumpForm(entry, ""));
  const patch = (changes: Partial<DumpForm>) => setForm((current) => ({ ...current, ...changes }));
  const contentGroup = useId();

  // The Tables folder: its tables, all checked the first time they arrive unless the form came with its own (again)
  const folder = entry.target.kind === "tables" ? entry.target.folder : null;
  const children = useQuery({ ...childrenQuery(entry.profileId, folder ?? []), enabled: folder !== null });
  const tables = useMemo(() => children.data?.filter((node) => node.kind === "table") ?? null, [children.data]);
  const [seeded, setSeeded] = useState(preset !== null || folder === null);
  if (!seeded && tables) {
    setSeeded(true);
    setForm((current) => ({ ...current, checked: tables.map((table) => table.name) }));
  }

  // The plan: the same fields as the command, the path once typing pauses (the warnings about the target follow it)
  const settledPath = useDebouncedValue(form.path, PLAN_PATH_DEBOUNCE_MS);
  const body = useMemo(() => dumpPlanRequest(entry, { ...form, path: settledPath }), [entry, form, settledPath]);
  const plan = useQuery({
    queryKey: ["dumpPlan", body],
    queryFn: ({ signal }) => fetchDumpPlan(body!, signal),
    enabled: body !== null,
    retry: false,
    gcTime: 0,
    // The plan of the choices before stays (dimmed) while the new one is read: the dialog does not jump at each click
    placeholderData: keepPreviousData,
  });
  // A disabled query (no scope: no table checked) still hands back the previous plan as its placeholder: not shown
  const shownPlan = body !== null && !plan.isError ? (plan.data ?? null) : null;
  const readPlan = plan.isPlaceholderData ? null : shownPlan;

  const staged = useStagedEdits(entry.tabId ?? "");
  const stagedNote = staged && changeCount(staged) > 0 ? NOT_IN_DUMP : null;

  const problem = dumpProblem(entry, form);
  // M6: React's autoFocus acts as an element mounts only. A form that opens complete ("Dump again…") puts the focus on
  // Dump; else the path takes it (Choose… in the app), and a file chosen there moves it to Dump once the form is complete.
  const [opensComplete] = useState(() => problem === null);
  const dumpButton = useRef<HTMLButtonElement>(null);
  const [chosen, setChosen] = useState(0);
  const focusDumpIfReady = useEffectEvent(() => {
    if (dumpProblem(entry, form) === null) dumpButton.current?.focus();
  });
  useEffect(() => {
    if (chosen > 0) focusDumpIfReady();
  }, [chosen]);

  const sqlitePath = profile?.engine === "sqlite" ? profile.path : null;
  const fileName = engineKind
    ? dumpFileName(entry, engineKind, sqlitePath, form.checked?.length ?? 0, new Date())
    : "dump.sql";
  const choose = async (): Promise<string | null> => {
    try {
      return await chooseSavePath(fileName, [{ name: "SQL file", extensions: ["sql"] }]); // null: the panel was cancelled
    } catch (error) {
      toast("error", `Could not open the save panel: ${errorMessage(error)}`);
      return null;
    }
  };

  const start = () => {
    if (dumpProblem(entry, form) !== null || !profile) return;
    const requestId = crypto.randomUUID();
    const command = dumpCommand(entry, form, requestId);
    startJob({
      kind: "dump",
      profileId: entry.profileId,
      database: entry.database,
      title: dumpTitle(command.scope, profile.name, command.path),
      command,
      entry,
      tables: readPlan ? planTables(readPlan.objects) : null,
    });
    openJobDialog(requestId);
  };

  const footer = (
    <>
      <Button variant="ghost" onClick={closeDataDialog}>
        Close
      </Button>
      <Button
        ref={dumpButton}
        variant="primary"
        onClick={start}
        disabled={problem !== null}
        title={problem ?? undefined}
        autoFocus={opensComplete}
      >
        Dump
      </Button>
    </>
  );

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && closeDataDialog()}
      title={dumpDialogTitle(entry)}
      width={600}
      footer={footer}
    >
      <div className="flex flex-col gap-3">
        <ConnectionLine profileId={entry.profileId} database={entry.database} />
        {stagedNote && (
          <p className="text-[12px] text-warning" data-selectable>
            {stagedNote}
          </p>
        )}
        {!engineKind ? (
          <p className="text-[12px] text-muted">Loading the connection…</p>
        ) : (
          <DumpChoices
            entry={entry}
            engineKind={engineKind}
            form={form}
            patch={patch}
            contentGroup={contentGroup}
            tables={
              folder === null
                ? null
                : {
                    nodes: tables,
                    error: children.error,
                    retry: () => void children.refetch(),
                  }
            }
          />
        )}
        {body !== null && (
          <FieldRow label="Writes">
            {plan.error ? (
              <ReadProblem profileId={entry.profileId} error={plan.error} retry={() => void plan.refetch()} />
            ) : shownPlan ? (
              <PlanSummary plan={shownPlan} stale={plan.isPlaceholderData} />
            ) : (
              <p className="animate-delayed-show text-[12px] text-muted">Reading the catalog…</p>
            )}
          </FieldRow>
        )}
        {shownPlan && shownPlan.warnings.length > 0 && (
          <FieldRow label="">
            <PlanWarnings warnings={shownPlan.warnings} />
          </FieldRow>
        )}
        <FilePathField
          value={form.path}
          onChange={(path) => patch({ path })}
          onEnter={start}
          choose={choose}
          onChosen={() => setChosen((n) => n + 1)}
          placeholder={`/path/to/${fileName}`}
          browserHint={BROWSER_PATH_HINT}
          error={pathProblem(form.path)}
          autoFocus={!opensComplete}
        />
      </div>
    </Dialog>
  );
}

/** The form's choices: the Tables folder's tables, the content, the object kinds and the options. */
function DumpChoices({
  entry,
  engineKind,
  form,
  patch,
  contentGroup,
  tables,
}: {
  entry: DumpEntry;
  engineKind: Engine;
  form: DumpForm;
  patch: (changes: Partial<DumpForm>) => void;
  contentGroup: string;
  tables: { nodes: readonly ObjectNode[] | null; error: Error | null; retry: () => void } | null;
}) {
  const choices = dumpChoices(entry, engineKind, form);
  const contentReasons = reasonsOf(choices.contents);
  const kindReasons = reasonsOf(choices.kinds);
  const setKind = (kind: keyof DumpObjectKinds, on: boolean) => patch({ objects: { ...form.objects, [kind]: on } });
  return (
    <>
      {tables && (
        <FieldRow label="Tables">
          {tables.nodes ? (
            <TablePicker tables={tables.nodes} checked={form.checked ?? []} onChange={(checked) => patch({ checked })} />
          ) : tables.error ? (
            <ReadProblem profileId={entry.profileId} error={tables.error} retry={tables.retry} />
          ) : (
            <p className="animate-delayed-show text-[12px] text-muted">Loading the tables…</p>
          )}
        </FieldRow>
      )}
      <FieldRow label="Content">
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {choices.contents.map((choice) => (
              <Radio
                key={choice.value}
                name={contentGroup}
                label={choice.label}
                checked={form.content === choice.value}
                disabled={choice.disabled !== null}
                title={choice.disabled ?? undefined}
                onChange={() => patch({ content: choice.value })}
              />
            ))}
          </div>
          {contentReasons && <span className="text-[11px] text-muted">{contentReasons}</span>}
        </div>
      </FieldRow>
      {choices.kinds.length > 0 && (
        <FieldRow label="Objects">
          <div className="flex flex-col gap-1">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {choices.kinds.map((choice) => (
                <Checkbox
                  key={choice.value}
                  label={choice.label}
                  // A greyed kind is not written: shown unchecked, the choice kept for when it can be written again
                  checked={choice.disabled === null && form.objects[choice.value]}
                  disabled={choice.disabled !== null}
                  title={choice.disabled ?? undefined}
                  onChange={(e) => setKind(choice.value, e.target.checked)}
                />
              ))}
            </div>
            {kindReasons && <span className="text-[11px] text-muted">{kindReasons}</span>}
          </div>
        </FieldRow>
      )}
      <FieldRow label="Options">
        <div className="flex flex-col gap-1">
          <Checkbox
            label="Drop objects first"
            checked={choices.dropFirst === null && form.dropFirst}
            disabled={choices.dropFirst !== null}
            title={choices.dropFirst ?? undefined}
            onChange={(e) => patch({ dropFirst: e.target.checked })}
          />
          <span className="pl-5 text-[11px] text-muted">{choices.dropFirst ?? DROP_FIRST_HINT}</span>
          {choices.filter.shown && entry.filter !== null && (
            <>
              <div className="flex min-w-0 items-center gap-2">
                <Checkbox
                  label="Only rows matching the filter"
                  checked={choices.filter.disabled === null && form.onlyFiltered}
                  disabled={choices.filter.disabled !== null}
                  title={choices.filter.disabled ?? undefined}
                  onChange={(e) => patch({ onlyFiltered: e.target.checked })}
                />
                <code
                  className="min-w-0 truncate font-mono text-[12px] text-muted"
                  title={`${FILTER_TITLE_PREFIX}${entry.filter}`}
                  data-selectable
                >
                  {FILTER_TITLE_PREFIX}
                  {entry.filter}
                </code>
              </div>
              {choices.filter.disabled && <span className="pl-5 text-[11px] text-muted">{choices.filter.disabled}</span>}
            </>
          )}
        </div>
      </FieldRow>
    </>
  );
}
