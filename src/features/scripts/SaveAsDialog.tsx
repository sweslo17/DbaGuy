import { useMemo, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { FieldRow, Input, Select } from "@/components/ui/Field";
import { toast } from "@/components/ui/Toast";
import { errorMessage } from "@/lib/api/client";
import { isImeKeyEvent } from "@/lib/ime";
import { baseName, joinPath, nameProblem, parentPath, stemLength, withSqlSuffix } from "@/lib/scriptPaths";
import { scriptTab } from "@/stores/workspace";
import { folderPaths, scriptExists, useScriptTree } from "./useScripts";

/**
 * Save As (⌘⇧S, or ⌘S on a tab without a file): a folder from the scripts tree and a file name (`.sql` appended when
 * missing). Only an empty name and a "/" are refused here; the backend judges the rest (its message is a toast). An
 * existing file is replaced only after a second confirmation. Mounted only while open.
 */
export function SaveAsDialog({
  tabId,
  boundPath,
  suggestedName,
  onSave,
  onClose,
  onCloseAutoFocus,
}: {
  /** The tab being saved. */
  tabId: string;
  /** The file the tab is bound to now, or null: its folder and name are proposed. */
  boundPath: string | null;
  /** The name proposed for a tab without a file. */
  suggestedName: string;
  /** Saves the tab's text to the path; resolves to null once saved, or to a sentence saying why it was not. */
  onSave: (path: string) => Promise<string | null>;
  onClose: () => void;
  /** Where the focus goes once the dialog has closed. */
  onCloseAutoFocus: (event: Event) => void;
}) {
  const tree = useScriptTree();
  const [folder, setFolder] = useState(() => (boundPath ? parentPath(boundPath) : ""));
  const [name, setName] = useState(() => (boundPath ? baseName(boundPath) : withSqlSuffix(suggestedName)));
  const [problem, setProblem] = useState<string | null>(null);
  /** The existing file the user was told about: saving there again replaces it. */
  const [replacing, setReplacing] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const nameSelected = useRef(false);

  // The name field has the focus when the dialog opens, with the name selected without its suffix, so typing replaces
  // just the name. (`autoFocus`: the dialog's content mounts a render after this component.)
  const selectStem = (e: FocusEvent<HTMLInputElement>) => {
    if (nameSelected.current) return;
    nameSelected.current = true;
    e.currentTarget.setSelectionRange(0, stemLength(e.currentTarget.value));
  };

  const folders = useMemo(() => {
    const listed = folderPaths(tree.data?.items ?? []);
    return folder && !listed.includes(folder) ? [folder, ...listed] : listed; // a folder that is gone stays choosable
  }, [tree.data, folder]);

  const trimmed = name.trim();
  const target = nameProblem(trimmed) === null ? joinPath(folder, withSqlSuffix(trimmed)) : null;

  const submit = async () => {
    if (pending) return;
    const invalid = nameProblem(trimmed);
    if (invalid || target === null) return setProblem(invalid);
    const other = scriptTab(target);
    if (other && other.id !== tabId) return setProblem(`${target} is open in another tab (${other.title})`);
    setProblem(null);
    setPending(true);
    try {
      if (target !== boundPath && target !== replacing && (await scriptExists(target))) {
        setReplacing(target);
        return;
      }
      const failure = await onSave(target);
      if (failure === null) {
        toast("success", `Saved as ${target}`);
        onClose();
      } else toast("error", failure);
    } catch (error) {
      toast("error", `Could not save ${target}: ${errorMessage(error)}`);
    } finally {
      setPending(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key !== "Enter" || isImeKeyEvent(e)) return; // Enter may belong to the input method
    e.preventDefault();
    void submit();
  };

  const replace = target !== null && target === replacing;
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Save As"
      width={460}
      onCloseAutoFocus={onCloseAutoFocus}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={replace ? "danger" : "primary"} onClick={() => void submit()} disabled={pending}>
            {pending ? "Saving…" : replace ? "Replace" : "Save"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2.5" onKeyDown={onKeyDown}>
        <FieldRow label="Folder" hint={tree.data ? `In ${joinPath(tree.data.root, folder)}` : undefined}>
          <Select value={folder} onChange={(e) => setFolder(e.target.value)}>
            <option value="">/ (scripts folder)</option>
            {folders.map((path) => (
              <option key={path} value={path}>
                {path}
              </option>
            ))}
          </Select>
        </FieldRow>
        <FieldRow label="File name">
          <Input
            autoFocus
            onFocus={selectStem}
            value={name}
            placeholder="name.sql"
            onChange={(e) => {
              setName(e.target.value);
              setProblem(null);
            }}
          />
        </FieldRow>
        {tree.isError && (
          <p className="text-[12px] text-muted">Could not list the folders: {errorMessage(tree.error)}</p>
        )}
        {problem && (
          <p className="text-[12px] text-danger" data-selectable>
            {problem}
          </p>
        )}
        {replace && (
          <p className="text-[12px] text-warning" data-selectable>
            {target} already exists. Replace it with this tab’s text?
          </p>
        )}
      </div>
    </Dialog>
  );
}
