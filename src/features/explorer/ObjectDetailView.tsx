import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CopyButton } from "@/components/ui/CopyButton";
import { api, unwrap } from "@/lib/api/client";
import { pathKey, type ColumnInfo, type ObjectDetail } from "@/lib/api/types";
import type { ObjectTabState } from "@/stores/workspace";
import { RecoverableError } from "../connections/RecoverableError";
import { isPasswordRequired } from "../connections/UnlockDialog";
import { keyspaceOfPath } from "./fieldSamples";
import { kindLabel } from "./kinds";
import { SampledFields } from "./SampledFields";

/** The object a detail view shows: an object tab, or the table tab's Columns and DDL views. */
type ObjectRef = Pick<ObjectTabState, "profileId" | "path">;

/** The app's one automatic retry of a failed read, except a missing password: that is asked for, not retried. */
const retryOnce = (failures: number, error: Error) => failures < 1 && !isPasswordRequired(error);

/**
 * Loads an object's detail (`/metadata/detail`) and renders it with `children`; shows loading and errors itself. A
 * failure offers Retry, and a missing password (the backend forgets it on restart) asks for it: the profile unlocked
 * anywhere reads this detail again (`useUnlockProfile`).
 */
function WithDetail({ tab, children }: { tab: ObjectRef; children: (detail: ObjectDetail) => ReactNode }) {
  const detail = useQuery({
    queryKey: ["detail", tab.profileId, pathKey(tab.path)],
    queryFn: async () =>
      unwrap(await (await api()).POST("/metadata/detail", { body: { profile_id: tab.profileId, path: tab.path } })),
    retry: retryOnce,
  });
  if (detail.isPending) return <p className="p-4 text-muted">Loading…</p>;
  if (detail.isError) {
    // `cancelRefetch: false`: an unlock has started this read already, and asking again must not restart it.
    const retry = () => void detail.refetch({ cancelRefetch: false });
    return <RecoverableError profileId={tab.profileId} error={detail.error} onRetry={retry} />;
  }
  return children(detail.data);
}

/** "approximate" next to a DDL rebuilt from the catalog; nothing for the original statement. */
function DdlBadge({ detail }: { detail: ObjectDetail }) {
  if (!detail.ddl || !detail.ddl_is_generated) return null;
  return (
    <span
      className="rounded bg-hover px-1.5 text-[11px] text-muted"
      title="Rebuilt from the catalog; may differ from the original statement"
    >
      approximate
    </span>
  );
}

function Comment({ text }: { text: string }) {
  return (
    <p className="px-4 pt-2 text-[12px] text-muted" data-selectable>
      {text}
    </p>
  );
}

function ColumnsTable({ columns }: { columns: ColumnInfo[] }) {
  return (
    <table className="mx-4 mt-3 border-collapse text-[12px]" data-selectable>
      <thead>
        <tr className="text-left text-muted">
          {["Name", "Type", "Nullable", "Default", "PK", "Comment"].map((h) => (
            <th key={h} className="border-b border-border px-2 py-1 font-medium">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {columns.map((c) => (
          <tr key={c.name} className="hover:bg-hover">
            <td className="px-2 py-0.5 font-medium">{c.name}</td>
            <td className="px-2 py-0.5 font-mono">{c.type_name}</td>
            <td className="px-2 py-0.5">{c.nullable ? "yes" : "no"}</td>
            <td className="px-2 py-0.5 font-mono text-muted">{c.default ?? ""}</td>
            <td className="px-2 py-0.5">{c.is_primary_key ? "●" : ""}</td>
            <td className="px-2 py-0.5 text-muted">{c.comment ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DdlText({ ddl }: { ddl: string }) {
  return (
    <pre
      className="m-4 whitespace-pre-wrap rounded-md border border-border bg-panel p-3 font-mono text-[12px]"
      data-selectable
    >
      {ddl}
    </pre>
  );
}

/** A table data tab's Columns view: the object's comment and its columns. */
export function ColumnsView({ tab }: { tab: ObjectRef }) {
  return (
    <WithDetail tab={tab}>
      {(d) => (
        <div className="h-full overflow-auto pb-4">
          {d.comment && <Comment text={d.comment} />}
          {d.columns?.length ? <ColumnsTable columns={d.columns} /> : <p className="p-4 text-muted">No columns</p>}
        </div>
      )}
    </WithDetail>
  );
}

/** A table data tab's DDL view. */
export function DdlView({ tab }: { tab: ObjectRef }) {
  return (
    <WithDetail tab={tab}>
      {(d) =>
        d.ddl ? (
          <div className="flex h-full flex-col">
            <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-2">
              <DdlBadge detail={d} />
              <div className="flex-1" />
              <CopyButton text={d.ddl} label="Copy DDL" />
            </div>
            <div className="min-h-0 flex-1 overflow-auto">
              <DdlText ddl={d.ddl} />
            </div>
          </div>
        ) : (
          <p className="p-4 text-muted">No DDL available</p>
        )
      }
    </WithDetail>
  );
}

/** The sampled fields of a Couchbase collection's detail page; nothing for any other object. */
function CollectionFields({ tab, kind }: { tab: ObjectRef; kind: string }) {
  const keyspace = kind === "collection" ? keyspaceOfPath(tab.path) : null;
  return keyspace && <SampledFields profileId={tab.profileId} keyspace={keyspace} />;
}

/**
 * Detail tab of an object without rows (function, sequence, index, …): its comment, columns and DDL on one page. A
 * Couchbase collection adds its sampled fields under the DDL.
 */
export function ObjectDetailView({ tab }: { tab: ObjectRef }) {
  return (
    <WithDetail tab={tab}>
      {(d) => (
        <div className="flex h-full flex-col overflow-auto">
          <div className="flex items-center gap-2 border-b border-border px-4 py-2">
            <span className="text-[11px] uppercase tracking-wide text-muted">{kindLabel(d.kind)}</span>
            <span className="font-semibold" data-selectable>
              {d.name}
            </span>
            <DdlBadge detail={d} />
            <div className="flex-1" />
            {d.ddl && <CopyButton text={d.ddl} label="Copy DDL" />}
          </div>
          {d.comment && <Comment text={d.comment} />}
          {d.columns && <ColumnsTable columns={d.columns} />}
          {d.ddl && <DdlText ddl={d.ddl} />}
          <CollectionFields tab={tab} kind={d.kind} />
        </div>
      )}
    </WithDetail>
  );
}
