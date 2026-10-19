import type { ReactNode } from "react";
import type { Keyspace } from "@/lib/api/types";
import { formatBytes } from "@/lib/cells";
import { formatTimeUntil } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { keyspaceLabel } from "../explorer/fieldSamples";
import type { DocumentLoad, DocumentMeta } from "./documentState";

/** The expiration is a countdown in years, days or minutes: a minute's resolution is enough. */
const NOW_INTERVAL_MS = 60_000;

/** "Expires 1/1/2100, 12:00:00 AM (in 73 yr.)", or "No expiry". `expiration` is Unix seconds; 0 is none. */
function ExpiryText({ expiration, now }: { expiration: number; now: number }) {
  if (expiration === 0) return <span>No expiry</span>;
  const at = expiration * 1000;
  return (
    <span>
      Expires {new Date(at).toLocaleString()} ({formatTimeUntil(at, now)})
    </span>
  );
}

/** What the server holds of the document besides its text (its META), as selectable text. */
function MetaText({ meta, now }: { meta: DocumentMeta; now: number }) {
  return (
    <>
      <span data-selectable>CAS {meta.cas}</span>
      <ExpiryText expiration={meta.expiration} now={now} />
      <span>{meta.type === "json" ? "json" : "binary"}</span>
      <span>{formatBytes(meta.size)}</span>
    </>
  );
}

/**
 * The line above a document's editor: its ID and collection, then what the server holds besides the text (CAS,
 * expiration, type, size) or, for a new document, that it is not saved yet. `children` are the actions, at the right.
 */
export function DocumentBar({
  id,
  keyspace,
  load,
  isNew,
  children,
}: {
  id: string;
  keyspace: Keyspace;
  load: DocumentLoad;
  isNew: boolean;
  children: ReactNode;
}) {
  const meta = load.status === "ready" ? load.meta : null;
  const now = useNow(NOW_INTERVAL_MS, meta !== null && meta.expiration !== 0);
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-1.5 text-[12px]">
      <span className="max-w-full min-w-0 truncate font-mono text-[13px] font-medium" data-selectable title={id}>
        {id}
      </span>
      <span className="text-muted">{keyspaceLabel(keyspace)}</span>
      <div className="flex flex-wrap items-center gap-x-3 text-muted">
        {isNew ? <span>New document — not saved yet</span> : meta && <MetaText meta={meta} now={now} />}
      </div>
      <div className="ml-auto flex items-center gap-1.5">{children}</div>
    </div>
  );
}
