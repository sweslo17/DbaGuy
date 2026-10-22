import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { FieldRow, Input } from "@/components/ui/Field";
import type { Keyspace } from "@/lib/api/types";
import { isImeKeyEvent } from "@/lib/ime";
import { useDebouncedValue } from "@/lib/useDebouncedValue";
import { useWorkspace } from "@/stores/workspace";
import { childrenQuery } from "../explorer/useTreeRows";
import {
  closeOpenDocument,
  lastCollection,
  rememberCollection,
  useOpenDocumentAsk,
  type OpenDocumentAsk,
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

/** The names of a scope's collections, for the Collection field's suggestions; none until bucket and scope are known. */
function useCollectionNames(profileId: string, bucket: string, scope: string): string[] {
  const settled = useDebouncedValue({ bucket, scope }, LOOKUP_DELAY_MS);
  const known = settled.bucket !== "" && settled.scope !== "";
  const path = [
    { kind: "bucket", name: settled.bucket },
    { kind: "scope", name: settled.scope },
  ];
  const children = useQuery({ ...childrenQuery(profileId, path), enabled: known });
  return (known ? children.data : undefined)?.filter((node) => node.kind === "collection").map((node) => node.name) ?? [];
}

/** The form's four fields, in the order they are asked. */
const FIELD_NAMES = ["bucket", "scope", "collection", "id"] as const;
type FieldName = (typeof FIELD_NAMES)[number];
type Fields = Record<FieldName, string>;

/** What a request knows of the fields, and the collection last opened in its scope for the rest. */
function initialFields({ profileId, keyspace, id }: OpenDocumentRequest): Fields {
  const bucket = keyspace.bucket ?? "";
  const scope = keyspace.scope ?? "";
  return { bucket, scope, collection: keyspace.collection ?? lastCollection(profileId, bucket, scope) ?? "", id: id ?? "" };
}

/** The field the focus starts in: the first one still empty, else the document ID (every other field is known). */
const firstEmpty = (fields: Fields): FieldName => FIELD_NAMES.find((name) => fields[name] === "") ?? "id";

/**
 * The inputs. Keyed by the ask by its parent, so a second ask while the dialog is open starts them over and moves the
 * focus again, without the dialog itself being rebuilt (it would forget where to return the focus to once closed).
 */
function DocumentFields({
  fields,
  collections,
  idTooLong,
  onChange,
  onEnter,
}: {
  fields: Fields;
  collections: string[];
  idTooLong: boolean;
  onChange: (name: FieldName, value: string) => void;
  onEnter: (e: KeyboardEvent) => void;
}) {
  const ids = { bucket: useId(), scope: useId(), collection: useId(), id: useId() };
  const listId = useId();
  const inputs = useRef<Partial<Record<FieldName, HTMLInputElement | null>>>({});
  // Decided once, as the dialog opens: what the user types later never moves the focus.
  const [target] = useState(() => firstEmpty(fields));
  // A menu that opened the dialog (the tree's, a grid's) hands the focus back to its trigger in a timeout once it closes,
  // and the dialog then pulls it to its own box: the field takes it back a frame after that timeout, unless the user has
  // already put the focus in a field of their own choosing.
  useEffect(() => {
    let frame = 0;
    const timer = window.setTimeout(() => {
      frame = requestAnimationFrame(() => {
        const focused = document.activeElement;
        const inField = Object.values(inputs.current).some((input) => input != null && input === focused);
        if (!inField) inputs.current[target]?.focus();
      });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      cancelAnimationFrame(frame);
    };
  }, [target]);

  const field = (name: FieldName) => ({
    id: ids[name],
    ref: (element: HTMLInputElement | null) => void (inputs.current[name] = element),
    value: fields[name],
    autoFocus: name === target,
    onChange: (e: { target: { value: string } }) => onChange(name, e.target.value),
  });

  return (
    <div className="flex flex-col gap-2.5">
      <FieldRow label="Bucket" htmlFor={ids.bucket}>
        <Input {...field("bucket")} onKeyDown={onEnter} />
      </FieldRow>
      <FieldRow label="Scope" htmlFor={ids.scope}>
        <Input {...field("scope")} onKeyDown={onEnter} />
      </FieldRow>
      <FieldRow label="Collection" htmlFor={ids.collection}>
        <Input {...field("collection")} list={listId} />
        <datalist id={listId}>
          {collections.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </FieldRow>
      <FieldRow label="Document ID" htmlFor={ids.id} error={idTooLong ? `At most ${ID_MAX_BYTES} bytes` : null}>
        <Input {...field("id")} onKeyDown={onEnter} aria-invalid={idTooLong} className="font-mono" />
      </FieldRow>
    </div>
  );
}

function OpenDocumentForm({ ask }: { ask: OpenDocumentAsk }) {
  const { request, serial } = ask;
  const { profileId, mode } = request;
  const openDocument = useWorkspace((s) => s.openDocument);
  const [fields, setFields] = useState(() => initialFields(request));
  // Another ask while the dialog is open: the fields start over from it (the dialog stays; its fields component is keyed).
  const [seen, setSeen] = useState(serial);
  if (serial !== seen) {
    setSeen(serial);
    setFields(initialFields(request));
  }
  const { bucket, scope, collection, id } = fields;
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
      <DocumentFields
        key={serial}
        fields={fields}
        collections={collections}
        idTooLong={idTooLong}
        onChange={(name, value) => setFields((current) => ({ ...current, [name]: value }))}
        onEnter={onEnter}
      />
    </Dialog>
  );
}

/**
 * The "Open document" dialog (spec §3.13), asked for by `askOpenDocument` (the tree's collection menu, a grid's cell
 * menu): the collection's three names, prefilled from what the request knows and the collection last opened in the
 * scope, and the document's ID. The focus starts in the first field that is still empty (the ID when the collection is
 * known). Open opens the document's tab, or shows the one already open; in "new" mode it is "Create". Mounted once, in the
 * app shell.
 */
export function OpenDocumentDialogHost() {
  const ask = useOpenDocumentAsk();
  return ask && <OpenDocumentForm ask={ask} />;
}
