import { useQuery } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Checkbox, FieldRow, Input, Radio } from "@/components/ui/Field";
import type { TransferPlan } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { isImeKeyEvent } from "@/lib/ime";
import { useStagedEdits } from "@/stores/stagedEdits";
import { findEngineInfo, useEngines, useProfiles } from "../connections/hooks";
import { changeCount } from "../edits/staging";
import { NOT_IN_COPY } from "../edits/tableEditing";
import { childrenQuery } from "../explorer/useTreeRows";
import { DatabasePicker } from "../query/ConnectionPicker";
import {
  copyCommand,
  copyDialogTitle,
  copyJobTitle,
  copyProblem,
  copyRequest,
  defaultSchema,
  initialCopyForm,
  isOne,
  MODE_HINTS,
  MODE_LABELS,
  passwordOwner,
  planTargets,
  statementFirstLine,
  tablesProblem,
  targetRefusal,
  withTarget,
  type CopyForm,
  type CopyMode,
} from "./copyForm";
import { closeDataDialog, openJobDialog } from "./dataDialogs";
import type { CopyEntry } from "./entryPoints";
import { fetchTransferPlan } from "./jobsApi";
import { startJob } from "./jobsStore";
import { ConnectionLine, FilterCode, ignoreHeldReturn, ReadProblem } from "./JobViews";
import { previewProblems, tablesToDraw } from "./mappingRows";
import { MappingTable } from "./MappingTable";
import { databaseNodes } from "./runForm";
import { TableChecks } from "./TableChecks";
import { TargetPicker } from "./TargetPicker";
import { useSettledRead } from "./useSettledRead";

const MODES: CopyMode[] = ["create", "append", "replace_rows"];
/** A preview draws this many tables' mappings at first (Q1: a copy may hold 500); the rest on request. */
const PREVIEW_TABLES = 40;
const NO_DATABASE_FOR_SCHEMAS = "Choose the database first: its schemas are listed from it";

/**
 * The preview: what blocks the copy (each problem once, in red), each table's mapping and statements, then the warnings
 * once each. `stale`: it is the preview of the choices before, dimmed until the next one arrives.
 */
function Preview({
  plan,
  stale,
  form,
  onChange,
}: {
  plan: TransferPlan;
  stale: boolean;
  form: CopyForm;
  onChange: (form: CopyForm) => void;
}) {
  const [all, setAll] = useState(false);
  const problems = previewProblems(plan);
  const warnings = [...new Set([...plan.warnings, ...plan.tables.flatMap((table) => table.warnings)])];
  const drawn = tablesToDraw(plan.tables, all ? Infinity : PREVIEW_TABLES);
  return (
    <div className={cn("flex flex-col gap-3 transition-opacity", stale && "opacity-60")}>
      {problems.length > 0 && (
        <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto rounded-md border border-danger/40 bg-danger/10 p-2 text-[12px] text-danger [scrollbar-gutter:stable]">
          {problems.map((problem, i) => (
            <li key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]" data-selectable>
              {problem}
            </li>
          ))}
        </ul>
      )}
      {drawn.map((table) => (
        <div key={table.source_key} className="flex flex-col gap-1">
          <p className="text-[12px] font-medium [overflow-wrap:anywhere]" data-selectable>
            {table.source} → {table.target}
          </p>
          <MappingTable table={table} form={form} onChange={onChange} />
        </div>
      ))}
      {drawn.length < plan.tables.length && (
        <p className="flex items-center gap-2 text-[12px] text-muted">
          Showing the first {drawn.length.toLocaleString()} of {plan.tables.length.toLocaleString()} tables
          <Button size="sm" onClick={() => setAll(true)}>
            Show all
          </Button>
        </p>
      )}
      {warnings.length > 0 && (
        <ul className="flex max-h-48 list-disc flex-col gap-1 overflow-y-auto pl-4 text-[12px] text-warning [scrollbar-gutter:stable]">
          {warnings.map((warning, i) => (
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
 * Copy data to… (spec ⑥ §3.6 CopyDataDialog): the source as the entry gave it, where the rows go, how, and the backend's
 * preview of every column and statement, read again as the choices change. Copy runs exactly the preview shown (its
 * plan id; D-06-8) on a session of the target's own, and turns this dialog into the job's view. A red target's Copy is a
 * danger button (⑤ H24; no box, C9: every statement is shown first); a preview with a problem has no Copy. `form`:
 * "Copy again…" and "Copy the rest…".
 */
export function CopyDataDialog({ entry, form: preset }: { entry: CopyEntry; form: CopyForm | null }) {
  const profiles = useProfiles();
  const engines = useEngines();
  const entries = profiles.data ?? [];
  const sourceProfile = entries.find((item) => item.profile.id === entry.profileId)?.profile ?? null;
  const [form, setForm] = useState<CopyForm>(() => preset ?? initialCopyForm(entry));
  const patch = (changes: Partial<CopyForm>) => setForm((current) => ({ ...current, ...changes }));
  const modeGroup = useId();
  const tableId = useId();
  const target = entries.find((item) => item.profile.id === form.targetProfileId)?.profile ?? null;
  const targetEngine = findEngineInfo(entries, engines.data, form.targetProfileId);

  // A schema's tables: listed from its Tables folder, all checked the first time they arrive (unless "again")
  const folder = entry.source.kind === "tables" ? entry.source.folder : null;
  const children = useQuery({ ...childrenQuery(entry.profileId, folder ?? []), enabled: folder !== null });
  const tables = useMemo(() => children.data?.filter((node) => node.kind === "table") ?? null, [children.data]);
  const [seeded, setSeeded] = useState(preset !== null || folder === null);
  if (!seeded && tables) {
    setSeeded(true);
    setForm((current) => ({ ...current, checked: tables.map((table) => table.name) }));
  }

  // The preview: asked once the choices settle; the one of the choices before stays, dimmed, while the next is read, and is
  // never what Copy runs (`current` is the only door to the job). Keyed by content: a form change the preview does not
  // read keeps the same request, so nothing dims or waits.
  const requestKey = useMemo(() => {
    const fresh = copyRequest(entry, form, tables);
    return fresh === null ? null : JSON.stringify(fresh);
  }, [entry, form, tables]);
  const { request, read: plan, shown: shownPlan, behind, current: readPlan } = useSettledRead("copyPlan", requestKey, fetchTransferPlan);
  const problem = copyProblem(entry, form, tables, readPlan, behind);

  const staged = useStagedEdits(entry.tabId ?? "");
  const stagedNote = staged && changeCount(staged) > 0 ? NOT_IN_COPY : null;
  const red = target?.color === "red";
  const one = isOne(entry);

  const pickTarget = (profileId: string, database: string | null) => {
    const profile = entries.find((item) => item.profile.id === profileId)?.profile;
    const engine = findEngineInfo(entries, engines.data, profileId);
    if (!profile || !engine) return;
    if (profileId !== form.targetProfileId) {
      setForm((current) => withTarget(current, profile, engine));
      return;
    }
    // the same connection, another database: MySQL's "database" is the schema itself
    if (databaseNodes(engine) === "schema") patch({ targetSchema: database });
    else patch({ targetDatabase: database, targetSchema: defaultSchema(engine.engine, database) });
  };

  const start = () => {
    if (problem !== null || !readPlan || !target || !sourceProfile || !form.targetProfileId) return;
    const requestId = crypto.randomUUID();
    const command = copyCommand(readPlan, entry, requestId);
    if (!command) return; // a preview without a plan id is never sent (it has problems; `problem` says so already)
    startJob({
      kind: "copy_data",
      profileId: form.targetProfileId,
      sourceProfileId: entry.profileId,
      database: form.targetDatabase,
      title: copyJobTitle(readPlan, sourceProfile.name, target.name),
      command,
      entry,
      tables: planTargets(readPlan),
      // the source tables' names in the copy's order: "Copy the rest…" names the ones not copied by position
      copy: { form, sources: readPlan.tables.map((table) => table.source_path?.at(-1)?.name ?? table.source) },
    });
    openJobDialog(requestId);
  };

  const footer = (
    <>
      <Button variant="ghost" onClick={closeDataDialog}>
        Close
      </Button>
      <Button
        variant={red ? "danger" : "primary"}
        onClick={start}
        // a Return still held from a choice made in the form never presses Copy as the focus lands here (06b, L-g)
        onKeyDown={ignoreHeldReturn}
        disabled={problem !== null}
        title={problem ?? undefined}
      >
        Copy
      </Button>
    </>
  );

  const source = entry.source;
  // PostgreSQL's and SQL Server's schemas are the chosen database's children; MySQL's database is the schema itself
  const schemaInDatabase = targetEngine?.per_database_connection === true;
  const targetNodes = databaseNodes(targetEngine);
  return (
    <Dialog open onOpenChange={(open) => !open && closeDataDialog()} title={copyDialogTitle(entry)} width={760} footer={footer}>
      <div className="flex flex-col gap-3">
        <FieldRow label="From">
          <ConnectionLine profileId={entry.profileId} database={entry.database} />
        </FieldRow>
        {source.kind === "query" && (
          // the statement runs again on its tab when the copy starts (the preview warns): its first line is shown
          <FieldRow label="Query">
            <code className="block min-w-0 truncate font-mono text-[12px] text-muted" title={statementFirstLine(source.sql, 1000)} data-selectable>
              {statementFirstLine(source.sql)}
            </code>
          </FieldRow>
        )}
        {stagedNote && (
          <p className="text-[12px] text-warning" data-selectable>
            {stagedNote}
          </p>
        )}
        {source.kind === "tables" && (
          <FieldRow label="Tables">
            {tables ? (
              <TableChecks
                tables={tables}
                checked={form.checked ?? []}
                problem={tablesProblem(entry, form, tables)}
                onChange={(checked) => patch({ checked })}
              />
            ) : children.error ? (
              <ReadProblem profileId={entry.profileId} error={children.error} retry={() => void children.refetch()} />
            ) : (
              <p className="animate-delayed-show text-[12px] text-muted">Loading the tables…</p>
            )}
          </FieldRow>
        )}
        {source.kind === "relation" && source.filter !== null && (
          <FieldRow label="Rows">
            <div className="flex min-w-0 items-center gap-2">
              <Checkbox
                label="Only rows matching the filter"
                checked={form.onlyFiltered}
                onChange={(e) => patch({ onlyFiltered: e.target.checked })}
              />
              <FilterCode filter={source.filter} />
            </div>
          </FieldRow>
        )}
        <FieldRow label="To">
          <TargetPicker
            profileId={form.targetProfileId}
            database={targetNodes === "schema" ? form.targetSchema : form.targetDatabase}
            onChange={pickTarget}
            tool="copy_data"
            refusal={targetRefusal}
          >
            {form.targetProfileId && schemaInDatabase && (
              <DatabasePicker
                profileId={form.targetProfileId}
                current={form.targetSchema}
                nodeKind="schema"
                excludeSystem
                under={form.targetDatabase ? [{ kind: "database", name: form.targetDatabase }] : []}
                blockedReason={form.targetDatabase ? undefined : NO_DATABASE_FOR_SCHEMAS}
                onPick={(schema) => patch({ targetSchema: schema })}
              />
            )}
          </TargetPicker>
        </FieldRow>
        {one && (
          <FieldRow
            label="Table"
            htmlFor={tableId}
            hint={form.mode === "create" ? "The new table's name" : "The existing table's name"}
          >
            <Input
              id={tableId}
              value={form.table}
              onChange={(e) => patch({ table: e.target.value })}
              onKeyDown={(e) => {
                if (!isImeKeyEvent(e) && e.key === "Enter") e.preventDefault(); // Return never copies from here
              }}
              className="h-7 w-full max-w-[320px] font-mono text-[12px]"
              spellCheck={false}
            />
          </FieldRow>
        )}
        <FieldRow label="How">
          <div className="flex flex-col gap-1">
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {MODES.map((mode) => (
                <Radio
                  key={mode}
                  name={modeGroup}
                  label={MODE_LABELS[mode]}
                  checked={form.mode === mode}
                  onChange={() => patch({ mode })}
                />
              ))}
            </div>
            <span className={cn("text-[11px]", form.mode === "replace_rows" ? "font-medium text-danger" : "text-muted")}>
              {MODE_HINTS[form.mode]}
            </span>
          </div>
        </FieldRow>
        {request !== null && (
          <FieldRow label="Preview">
            {plan.error ? (
              // two connections are read: a missing password is asked for on the one the backend names
              <ReadProblem
                profileId={passwordOwner(plan.error, entry.profileId)}
                error={plan.error}
                retry={() => void plan.refetch()}
              />
            ) : shownPlan ? (
              <Preview plan={shownPlan} stale={behind} form={form} onChange={setForm} />
            ) : (
              <p className="animate-delayed-show text-[12px] text-muted">Reading both connections…</p>
            )}
          </FieldRow>
        )}
      </div>
    </Dialog>
  );
}
