import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { FieldRow, Input } from "@/components/ui/Field";
import type { Keyspace } from "@/lib/api/types";
import { isImeKeyEvent } from "@/lib/ime";
import { useWorkspace } from "@/stores/workspace";
import { childrenQuery } from "../explorer/useTreeRows";
import {
  closeOpenDocument,
  lastCollection,
  rememberCollection,
  useOpenDocumentRequest,
  type OpenDocumentRequest,
} from "./openDocument";

/** Couchbase's limit for a document's key, in UTF-8 bytes (`DOCUMENT_ID_MAX_BYTES` in the backend). */
const ID_MAX_BYTES = 250;
/** The collections offered follow the bucket and scope typed, once typing pauses: a name half typed asks nothing. */
const LOOKUP_DELAY_MS = 300;

const WORDING = {
  open: { title: "Open document", action: "Open" },
  new: { title: "New document", action: "Create" },
} as const;

/** `value` as it was `delayMs` ago: it follows once it has stayed the same that long. */
function useSettled<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

/** The names of a scope's collections, for the Collection field's suggestions; none until bucket and scope are known. */
function useCollectionNames(profileId: string, bucket: string, scope: string): string[] {
  const settled = useSettled({ bucket, scope }, LOOKUP_DELAY_MS);
  const known = settled.bucket !== "" && settled.scope !== "";
  const path = [
    { kind: "bucket", name: settled.bucket },
    { kind: "scope", name: settled.scope },
  ];
  const children = useQuery({ ...childrenQuery(profileId, path), enabled: known });
  return (known ? children.data : undefined)?.filter((node) => node.kind === "collection").map((node) => node.name) ?? [];
}

function OpenDocumentForm({ request }: { request: OpenDocumentRequest }) {
  const { profileId, mode } = request;
  const openDocument = useWorkspace((s) => s.openDocument);
  const [bucket, setBucket] = useState(request.keyspace.bucket ?? "");
  const [scope, setScope] = useState(request.keyspace.scope ?? "");
  const [collection, setCollection] = useState(
    () => request.keyspace.collection ?? lastCollection(profileId, bucket, scope) ?? "",
  );
  const [id, setId] = useState(request.id ?? "");
  const listId = useId();
  const collections = useCollectionNames(profileId, bucket, scope);

  const idBytes = new TextEncoder().encode(id).length;
  const idTooLong = idBytes > ID_MAX_BYTES;
  const canOpen = bucket !== "" && scope !== "" && collection !== "" && id !== "" && !idTooLong;

  const submit = () => {
    if (!canOpen) return;
    const keyspace: Keyspace = { bucket, scope, collection };
    rememberCollection(profileId, keyspace);
    openDocument(profileId, keyspace, id, { isNew: mode === "new" });
    closeOpenDocument();
  };
  // Enter submits, except in the Collection field: its suggestions take Enter to pick one, and the page sees the key
  // before they do, so the form would go with the half-typed name.
  const onEnter = (e: KeyboardEvent) => {
    if (e.key !== "Enter" || isImeKeyEvent(e)) return;
    e.preventDefault();
    submit();
  };
  const wording = WORDING[mode];

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && closeOpenDocument()}
      title={wording.title}
      width={480}
      footer={
        <>
          <Button variant="ghost" onClick={closeOpenDocument}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={!canOpen}>
            {wording.action}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2.5">
        <FieldRow label="Bucket">
          <Input value={bucket} onChange={(e) => setBucket(e.target.value)} onKeyDown={onEnter} />
        </FieldRow>
        <FieldRow label="Scope">
          <Input value={scope} onChange={(e) => setScope(e.target.value)} onKeyDown={onEnter} />
        </FieldRow>
        <FieldRow label="Collection">
          <Input value={collection} onChange={(e) => setCollection(e.target.value)} list={listId} />
          <datalist id={listId}>
            {collections.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </FieldRow>
        <FieldRow label="Document ID" error={idTooLong ? `At most ${ID_MAX_BYTES} bytes` : null}>
          <Input
            value={id}
            onChange={(e) => setId(e.target.value)}
            onKeyDown={onEnter}
            aria-invalid={idTooLong}
            className="font-mono"
            autoFocus
          />
        </FieldRow>
      </div>
    </Dialog>
  );
}

/**
 * The "Open document" dialog (spec §3.13), asked for by `askOpenDocument` (the tree's collection menu, a grid's cell
 * menu): the collection's three names, prefilled from what the request knows and the collection last opened in the
 * scope, and the document's ID, which has the focus. Open opens the document's tab, or shows the one already open.
 * Mounted once, in the app shell.
 */
export function OpenDocumentDialogHost() {
  const request = useOpenDocumentRequest();
  return request && <OpenDocumentForm request={request} />;
}
