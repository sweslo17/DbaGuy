import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { toast } from "@/components/ui/Toast";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api/client";
import type { ScriptFile, ScriptNode, ScriptTree } from "@/lib/api/types";
import { baseName, isWithin, joinPath, nameProblem, parentPath, withSqlSuffix } from "@/lib/scriptPaths";
import { mountedEditor, rememberEditorBuffer } from "@/stores/editorBuffers";
import { scriptTab, startSelection, useWorkspace, type QueryTabState } from "@/stores/workspace";

/*
 * The scripts folder (spec §4.7): the backend's `/scripts` API, the tree as one shared query, and the actions the
 * Scripts panel offers. Every failure is shown as a toast with the backend's message (dot names, conflicts, `.sql`).
 */

export const scriptTreeKey = ["scripts", "tree"] as const;

async function fetchScriptTree(): Promise<ScriptTree> {
  return unwrap(await (await api()).GET("/scripts"));
}

/**
 * The scripts folder's tree (folders first), shared by the panel, the Save As dialog and quick open. `cached`: a copy
 * already loaded is used as it is, not asked for again (quick open, on every ⌘P); it is read only if there is none.
 */
export function useScriptTree({ cached = false }: { cached?: boolean } = {}) {
  return useQuery({ queryKey: scriptTreeKey, queryFn: fetchScriptTree, ...(cached && { staleTime: Infinity }) });
}

export async function readScript(path: string, signal?: AbortSignal): Promise<ScriptFile> {
  return unwrap(await (await api()).GET("/scripts/file", { params: { query: { path } }, signal }));
}

/** Creates or replaces the file. */
export async function writeScript(path: string, content: string): Promise<ScriptFile> {
  return unwrap(await (await api()).PUT("/scripts/file", { body: { path, content } }));
}

/**
 * Whether a file exists at `path` (writing it would replace it). Any answer but "found" or "not found" is thrown: the
 * name is not usable (a dot, a folder there) or the file cannot be checked, and then nothing should be written.
 */
export async function scriptExists(path: string): Promise<boolean> {
  try {
    await readScript(path);
    return true;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return false;
    throw error;
  }
}

/**
 * A tab waiting for its file's text (restored without text of its own, ruling R-02c-21) gets `content`: known as saved
 * first, so it is no edit (no dot), and not an undo step. Its mounted editor takes it, else the tab mounts with it.
 */
export function loadFileText(tab: QueryTabState, content: string): void {
  useWorkspace.getState().updateTab(tab.id, "query", { savedText: content, textFromFile: false });
  const selection = startSelection(tab);
  const editor = mountedEditor(tab.id);
  if (editor) editor.setText(content, selection);
  else rememberEditorBuffer(tab.id, { text: content, selection: selection ?? { anchor: 0, head: 0 } });
}

/**
 * The tabs bound at or below `path` that still wait for their file's text get it now, read from the file: deleting
 * it must not leave them empty. Throws if one cannot be read.
 */
async function loadWaitingTabs(path: string): Promise<void> {
  for (const tab of useWorkspace.getState().tabs) {
    if (tab.kind !== "query" || !tab.textFromFile) continue;
    if (tab.scriptPath === null || !isWithin(tab.scriptPath, path)) continue;
    let file: ScriptFile;
    try {
      file = await readScript(tab.scriptPath);
    } catch (error) {
      throw new Error(`its open tab could not get the file's text first (${errorMessage(error)})`);
    }
    const now = scriptTab(file.path); // read meanwhile (its tab was shown), or typed into: nothing to give it
    if (now?.id === tab.id && now.textFromFile) loadFileText(now, file.content);
  }
}

/**
 * The scripts folder is now another one (rulings R-02c-12, R-02c-22): the tree is read again. A tab bound to a script
 * keeps its text but is unbound, since its path would now name a file in the new folder (⌘S would overwrite it); a tab
 * still showing just its file, never read, is closed instead (the file is intact in the old folder).
 */
export function scriptsFolderChanged(qc: QueryClient): void {
  void qc.invalidateQueries({ queryKey: scriptTreeKey });
  const { closed, unbound } = useWorkspace.getState().leaveScriptsFolder();
  console.info(`Scripts folder changed; closed ${closed.length} and unbound ${unbound.length} script tab(s)`);
  const parts: string[] = [];
  if (closed.length) parts.push(`Closed ${closed.join(", ")}: unchanged, still in the old folder.`);
  if (unbound.length) {
    const kept = unbound.length === 1 ? `${unbound[0]} keeps its text` : `${unbound.join(", ")} keep their text`;
    parts.push(`${kept}, no longer saved to a file (Save As saves to the new folder).`);
  }
  if (parts.length) toast("info", `The scripts folder changed. ${parts.join(" ")}`);
}

/** Every folder in the tree, depth first (parents before their children). */
export function folderPaths(items: ScriptNode[]): string[] {
  return items.flatMap((node) => (node.kind === "folder" ? [node.path, ...folderPaths(node.children)] : []));
}

/** Paths being read to open: a second double-click while the first read is underway does not open a second tab. */
const opening = new Set<string>();

/**
 * Opens a script in a query tab bound to it (spec §4.7), or activates the tab that has it already. A new tab starts on
 * the connection selected in the explorer; afterwards it keeps its own (the workspace remembers it). A file that is
 * gone refreshes the tree, so its stale entry goes.
 */
export async function openScript(path: string, qc: QueryClient): Promise<void> {
  const open = scriptTab(path);
  if (open) return useWorkspace.getState().activate(open.id);
  if (opening.has(path)) return;
  opening.add(path);
  try {
    const file = await readScript(path);
    const again = scriptTab(file.path); // opened meanwhile (e.g. saved there from another tab)
    if (again) return useWorkspace.getState().activate(again.id);
    const { selectedProfileId, openQuery } = useWorkspace.getState();
    openQuery(selectedProfileId, null, { text: file.content, scriptPath: file.path, title: baseName(file.path) });
    console.info(`Opened script ${file.path}`);
  } catch (error) {
    toast("error", `Could not open ${path}: ${errorMessage(error)}`);
    if (error instanceof ApiError && error.status === 404) void qc.invalidateQueries({ queryKey: scriptTreeKey });
  } finally {
    opening.delete(path);
  }
}

/** A trimmed name, or null after telling why it cannot be used. */
function usableName(raw: string): string | null {
  const name = raw.trim();
  const problem = nameProblem(name);
  if (problem) toast("error", problem);
  return problem ? null : name;
}

/** Runs one change to the scripts folder: toasts its failure, refreshes the tree after a success. */
async function change(failure: string, run: () => Promise<void>, refresh: () => Promise<void>): Promise<boolean> {
  try {
    await run();
    return true;
  } catch (error) {
    toast("error", `${failure}: ${errorMessage(error)}`);
    return false;
  } finally {
    void refresh();
  }
}

/**
 * The Scripts panel's actions: `open` (see `openScript`) and the changes to the folder. Each change resolves to the
 * resulting path (true for a delete) when done, or null (false) after a failure, which was shown as a toast. Tabs bound
 * to a renamed file follow it; tabs bound to a deleted file keep their text, unbound.
 */
export function useScriptActions() {
  const qc = useQueryClient();
  return useMemo(() => {
    const refresh = () => qc.invalidateQueries({ queryKey: scriptTreeKey });

    /** Creates an empty file in `folder` (never over an existing one) and opens it. */
    const createFile = async (folder: string, rawName: string): Promise<string | null> => {
      const name = usableName(rawName);
      if (name === null) return null;
      const path = joinPath(folder, withSqlSuffix(name));
      const created = await change(
        `Could not create ${path}`,
        async () => {
          if (await scriptExists(path)) throw new Error(`${path} already exists`);
          await writeScript(path, "");
          console.info(`Created script ${path}`);
        },
        refresh,
      );
      if (!created) return null;
      await openScript(path, qc);
      return path;
    };

    const createFolder = async (parent: string, rawName: string): Promise<string | null> => {
      const name = usableName(rawName);
      if (name === null) return null;
      const path = joinPath(parent, name);
      const created = await change(
        `Could not create the folder ${path}`,
        async () => {
          unwrap(await (await api()).POST("/scripts/folder", { body: { path } }));
          console.info(`Created script folder ${path}`);
        },
        refresh,
      );
      return created ? path : null;
    };

    /** Renames in place (same folder); a file keeps (or gets) its `.sql` suffix. */
    const rename = async (node: ScriptNode, rawName: string): Promise<string | null> => {
      const name = usableName(rawName);
      if (name === null) return null;
      const to = joinPath(parentPath(node.path), node.kind === "file" ? withSqlSuffix(name) : name);
      if (to === node.path) return to;
      const renamed = await change(
        `Could not rename ${node.path}`,
        async () => {
          unwrap(await (await api()).POST("/scripts/rename", { body: { from: node.path, to } }));
          useWorkspace.getState().moveScripts(node.path, to);
          console.info(`Renamed script ${node.path} to ${to}`);
        },
        refresh,
      );
      return renamed ? to : null;
    };

    /** Deletes a file or an empty folder (the backend refuses a folder with anything in it). */
    const remove = async (node: ScriptNode): Promise<boolean> =>
      change(
        `Could not delete ${node.path}`,
        async () => {
          await loadWaitingTabs(node.path); // nothing is deleted if a tab cannot keep the text
          unwrap(await (await api()).DELETE("/scripts/file", { params: { query: { path: node.path } } }));
          console.info(`Deleted script ${node.path}`);
          const titles = useWorkspace.getState().unbindScripts(node.path);
          if (titles.length) {
            const tabs = titles.length === 1 ? "Its open tab keeps" : `${titles.length} open tabs keep`;
            toast("info", `${node.path} was deleted. ${tabs} the text, no longer saved to a file.`);
          }
        },
        refresh,
      );

    const open = (path: string) => openScript(path, qc);

    return { refresh, open, createFile, createFolder, rename, remove };
  }, [qc]);
}
