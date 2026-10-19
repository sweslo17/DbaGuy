import { useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { errorMessage } from "@/lib/api/client";
import type { Keyspace, ObjectNode } from "@/lib/api/types";
import { isImeKeyEvent } from "@/lib/ime";
import { useAppSettings } from "@/stores/settings";
import { useWorkspace } from "@/stores/workspace";
import { NumberFieldRow } from "../settings/NumberFieldRow";
import { NUMBER_FIELDS, numberProblem } from "../settings/settingsForm";
import { keyspaceLabel, keyspaceOfPath, useIsSampling, useSampleFields } from "./fieldSamples";

const SIZE_FIELD = NUMBER_FIELDS.sample_size;

function SampleFieldsBody({
  profileId,
  node,
  keyspace,
  onOpenChange,
}: {
  profileId: string;
  node: ObjectNode;
  keyspace: Keyspace;
  onOpenChange: (open: boolean) => void;
}) {
  const openObject = useWorkspace((s) => s.openObject);
  const { settings } = useAppSettings();
  const [size, setSize] = useState(() => String(settings.couchbase.sample_size));
  const sample = useSampleFields();
  // Also a sample of this collection that an earlier dialog started and walked away from: one at a time.
  const busy = useIsSampling(profileId, keyspace);
  const problem = numberProblem(SIZE_FIELD, size);
  const canSample = problem === null && !busy;

  const submit = () => {
    if (!canSample) return;
    sample.mutate(
      { profileId, keyspace, size: Number(size.trim()) },
      {
        // Only while the dialog is open: after Cancel the sample still lands in the cache, and nothing else happens.
        onSuccess: () => {
          openObject(profileId, node);
          onOpenChange(false);
        },
      },
    );
  };
  const onEnter = (e: KeyboardEvent) => {
    if (e.key !== "Enter" || isImeKeyEvent(e)) return;
    e.preventDefault();
    submit();
  };

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title="Sample fields"
      width={440}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={!canSample}>
            {busy ? "Sampling…" : "Sample"}
          </Button>
        </>
      }
    >
      <p className="mb-3 text-[13px] font-medium" data-selectable>
        {keyspaceLabel(keyspace)}
      </p>
      <NumberFieldRow field={SIZE_FIELD} text={size} onChange={setSize} autoFocus onKeyDown={onEnter} />
      {busy && (
        <p className="mt-3 text-[11px] text-muted">Cancel closes this window; the sample finishes in the background.</p>
      )}
      {sample.isError && (
        <p className="mt-3 text-[12px] whitespace-pre-wrap text-danger" role="alert" data-selectable>
          {errorMessage(sample.error)}
        </p>
      )}
    </Dialog>
  );
}

/**
 * The tree's "Sample fields…" (R-04-11): asks how many documents to read (the setting's size to begin with), reads
 * them, and opens the collection's detail tab, where the field paths are. A server error stays in the dialog, in the
 * server's words. Cancel closes the dialog at any time; a sample already running finishes in the background and is
 * cached as usual.
 */
export function SampleFieldsDialog({
  open,
  profileId,
  node,
  onOpenChange,
}: {
  open: boolean;
  profileId: string;
  node: ObjectNode;
  onOpenChange: (open: boolean) => void;
}) {
  const keyspace = keyspaceOfPath(node.path);
  if (!open || keyspace === null) return null;
  return (
    <SampleFieldsBody
      key={`${profileId}:${keyspaceLabel(keyspace)}`}
      profileId={profileId}
      node={node}
      keyspace={keyspace}
      onOpenChange={onOpenChange}
    />
  );
}
