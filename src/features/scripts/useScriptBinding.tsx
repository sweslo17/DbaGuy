import type { Text } from "@codemirror/state";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { toast } from "@/components/ui/Toast";
import { ApiError, errorMessage } from "@/lib/api/client";
import { isDialogOpen } from "@/lib/dialogs";
import { isImeKeyEvent } from "@/lib/ime";
import { baseName } from "@/lib/scriptPaths";
import { reportTabStatus } from "@/stores/tabStatus";
import { useWorkspace, type QueryTabState } from "@/stores/workspace";
import type { CodeEditorHandle } from "../editor/CodeEditor";
import { CLOSE_GUARD_ORDER, useCloseGuard } from "../workspace/closeGuards";
import { SaveAsDialog } from "./SaveAsDialog";
import { readScript, scriptTreeKey, writeScript } from "./useScripts";

/** Typing that keeps the length the same is compared with the file only once it pauses this long. */
const DIRTY_CHECK_DELAY_MS = 300;
/** A restored tab's file read gives up after this long (closing waits for it): the tab then counts as unsaved. */
const READ_TIMEOUT_MS = 5000;

/** The line break a file uses: CodeMirror joins lines with "\n", so a CRLF file is compared and saved with CRLF. */
const lineBreakOf = (text: string) => (text.includes("\r\n") ? "\r\n" : "\n");

/** The editor's text as the file would hold it. */
const fileText = (doc: Text, lineBreak: string) => doc.sliceString(0, doc.length, lineBreak);

/** The length of `fileText`, without building it. */
const fileLength = (doc: Text, lineBreak: string) => doc.length + (lineBreak.length - 1) * (doc.lines - 1);

/** Why closing would lose something: unsaved edits, a file that is gone, or a file that could not be read. */
type Unsaved = "changes" | "missing" | "unknown";

interface ClosePrompt {
  path: string;
  unsaved: Unsaved;
  saving: boolean;
  error: string | null;
}

type CloseChoice = "save" | "discard" | "cancel";

export interface ScriptBinding {
  /** ⌘S: saves to the bound file, or asks where (Save As) when there is none. */
  save: () => void;
  /** ⌘⇧S. */
  saveAs: () => void;
  /** The editor's text changed (every keystroke: cheap). */
  docChanged: () => void;
  /** The Save As and close dialogs; the tab renders them. */
  dialogs: ReactNode;
}

/**
 * A query tab's script file (spec §4.7). The `•` follows whether the editor differs from the file as last read or
 * saved: a length change shows it at once, an equal length is compared once typing pauses, and an unchanged document
 * is never compared twice. Nothing here is React state of the tab: the dot goes through the tab status store. A
 * restored tab does not know its file's content: it is read again the first time the tab is shown; a file that is gone
 * marks the tab "file missing" (⌘S writes it again). Closing a tab with unsaved edits, a missing file or a file not read
 * yet asks Save / Discard / Cancel, before the session's guards.
 */
export function useScriptBinding({
  tab,
  shown,
  active,
  editor,
}: {
  tab: QueryTabState;
  /** The tab has been shown: a restored binding is checked from then on. */
  shown: boolean;
  /** ⌘S also works while the focus is outside the editor (e.g. in the results). */
  active: boolean;
  editor: RefObject<CodeEditorHandle | null>;
}): ScriptBinding {
  const qc = useQueryClient();
  const tabId = tab.id;
  const mounted = useRef(false);
  /** The bound path whose file was found missing (until it is saved again). */
  const missingPath = useRef<string | null>(null);
  const verifying = useRef<{ path: string; done: Promise<string | null> } | null>(null);
  /** The last full comparison: the same document against the same saved text gives the same answer. */
  const lastCheck = useRef<{ doc: Text; saved: string; dirty: boolean } | null>(null);
  const lineBreak = useRef<{ saved: string; lineBreak: string } | null>(null);
  const timer = useRef<number | null>(null);
  /** Saves run one after another. */
  const saves = useRef<Promise<unknown>>(Promise.resolve());
  const [saveAsOpen, setSaveAsOpen] = useState(false);
  const [prompt, setPrompt] = useState<ClosePrompt | null>(null);
  const answer = useRef<((close: boolean) => void) | null>(null);

  /** The tab as the store has it now: what an async step finds when it resumes, not what a render saw. */
  const current = () =>
    useWorkspace.getState().tabs.find((t): t is QueryTabState => t.id === tabId && t.kind === "query");
  const doc = () => editor.current?.view.state.doc ?? null;

  const lineBreakFor = (saved: string) => {
    if (lineBreak.current?.saved !== saved) lineBreak.current = { saved, lineBreak: lineBreakOf(saved) };
    return lineBreak.current.lineBreak;
  };

  const isMissing = (t: QueryTabState) =>
    t.scriptPath !== null && t.savedText === null && missingPath.current === t.scriptPath;

  /** Whether the editor differs from the file's known content; false when unbound or not known (yet). */
  const differs = (t: QueryTabState): boolean => {
    const text = doc();
    const saved = t.savedText;
    if (t.scriptPath === null || saved === null || !text) return false;
    const last = lastCheck.current;
    if (last && last.doc === text && last.saved === saved) return last.dirty;
    const breaks = lineBreakFor(saved);
    const dirty = fileLength(text, breaks) !== saved.length || fileText(text, breaks) !== saved;
    lastCheck.current = { doc: text, saved, dirty };
    return dirty;
  };

  const clearTimer = () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  };

  /** Reports the dot and the missing state now (a full comparison). */
  const reportStatus = () => {
    clearTimer();
    const t = current();
    if (!t || !mounted.current) return;
    const gone = isMissing(t);
    reportTabStatus(tabId, { missing: gone, dirty: gone || differs(t) });
  };

  const docChanged = () => {
    const t = current();
    const text = doc();
    if (!t || t.scriptPath === null || t.savedText === null || !text || !mounted.current) return;
    if (fileLength(text, lineBreakFor(t.savedText)) !== t.savedText.length) {
      clearTimer();
      reportTabStatus(tabId, { dirty: true });
      return;
    }
    clearTimer();
    timer.current = window.setTimeout(reportStatus, DIRTY_CHECK_DELAY_MS);
  };

  /**
   * Reads the bound file when its content is not known (a restored tab). Resolves once it is known or found missing
   * (null), or to the reason it could not be read: the tab then counts as unsaved, and the next call reads it again. A
   * file known to be missing is not read again.
   */
  const verify = (): Promise<string | null> => {
    const t = current();
    if (!t || t.scriptPath === null || t.savedText !== null || isMissing(t)) return Promise.resolve(null);
    const path = t.scriptPath;
    if (verifying.current?.path === path) return verifying.current.done;
    const done = (async (): Promise<string | null> => {
      try {
        const file = await readScript(path, AbortSignal.timeout(READ_TIMEOUT_MS));
        const now = current();
        if (now?.scriptPath !== path || now.savedText !== null) return null; // saved, renamed or unbound meanwhile
        useWorkspace.getState().updateTab(tabId, "query", { savedText: file.content });
        console.info(`Read script ${path} again for its restored tab`);
        return null;
      } catch (error) {
        if (current()?.scriptPath !== path) return null;
        if (error instanceof ApiError && error.status === 404) {
          missingPath.current = path;
          console.warn(`Script ${path} no longer exists; its tab keeps the text`);
          toast("warning", `${path} no longer exists. Its tab keeps the text; ⌘S saves it there again.`);
          reportStatus();
          return null;
        }
        console.warn(`Could not read script ${path}`, error);
        return errorMessage(error);
      } finally {
        if (verifying.current?.path === path) verifying.current = null;
      }
    })();
    verifying.current = { path, done };
    return done;
  };

  /**
   * Writes the editor's text to `path` and binds the tab to it; resolves to null once saved, or to a sentence saying
   * why it was not. A restored tab's file is read first, so its line breaks (CRLF) are kept: if it cannot be read, the
   * file is left as it is. A missing file is written again with "\n".
   */
  const saveTo = (path: string): Promise<string | null> => {
    const run = saves.current.then(async (): Promise<string | null> => {
      const unreadable = await verify(); // bounded by its timeout; not queued behind other saves
      const t = current();
      const text = doc();
      if (!t || !text) return `${path} was not saved: the tab is closed`;
      if (t.scriptPath === path && t.savedText === null && !isMissing(t)) {
        console.warn(`Not saving script ${path}: its line breaks are unknown (${unreadable ?? "not read"})`);
        return `Could not read ${path} to check its line endings — save cancelled (${unreadable ?? "not read"})`;
      }
      const content = fileText(text, t.savedText !== null ? lineBreakFor(t.savedText) : "\n");
      try {
        const file = await writeScript(path, content);
        const sameFile = t.scriptPath === file.path;
        if (!sameFile || t.savedText === null) void qc.invalidateQueries({ queryKey: scriptTreeKey }); // a new file
        if (missingPath.current === file.path) missingPath.current = null;
        if (current()) {
          useWorkspace
            .getState()
            .updateTab(
              tabId,
              "query",
              sameFile ? { savedText: content } : { scriptPath: file.path, savedText: content, title: baseName(file.path) },
            );
        }
        console.info(`Saved script ${file.path}`);
        return null;
      } catch (error) {
        console.warn(`Saving script ${path} failed`, error);
        return `Could not save ${path}: ${errorMessage(error)}`;
      }
    });
    saves.current = run;
    return run;
  };

  const saveAs = () => setSaveAsOpen(true);

  const save = () => {
    const path = current()?.scriptPath ?? null;
    if (path === null) return saveAs();
    void saveTo(path).then((failure) => {
      if (failure !== null) toast("error", failure);
    });
  };

  const ask = (path: string, unsaved: Unsaved) =>
    new Promise<boolean>((resolve) => {
      answer.current?.(false); // closes are serialized, so this is only a safety net
      const settle = (close: boolean) => {
        if (answer.current !== settle) return;
        answer.current = null;
        setPrompt(null);
        resolve(close);
      };
      answer.current = settle;
      setPrompt({ path, unsaved, saving: false, error: null });
    });

  const choose = async (choice: CloseChoice) => {
    const settle = answer.current;
    if (!settle || !prompt) return;
    if (choice !== "save") return settle(choice === "discard");
    if (prompt.saving) return;
    setPrompt({ ...prompt, saving: true, error: null });
    const failure = await saveTo(prompt.path);
    if (answer.current !== settle) {
      if (failure !== null) toast("error", failure); // dismissed meanwhile
      return;
    }
    if (failure === null) return settle(true);
    setPrompt((p) => p && { ...p, saving: false, error: failure });
  };

  // Closing: before the session's guards do anything (Cancel here leaves the run and the transaction alone), a bound
  // tab whose text may not be in its file asks first. A restored tab is read now if it never was, so an unchanged one
  // closes without asking.
  useCloseGuard(tabId, CLOSE_GUARD_ORDER.script, async () => {
    await verify();
    const t = current();
    if (!t || t.scriptPath === null) return true;
    const unsaved: Unsaved | null = isMissing(t)
      ? "missing"
      : t.savedText === null
        ? "unknown"
        : differs(t)
          ? "changes"
          : null;
    return unsaved === null || ask(t.scriptPath, unsaved);
  });

  // The helpers below read only refs and the store, so the effects depend on the tab fields that matter alone.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      clearTimer();
      answer.current?.(false);
    };
  }, []);

  // Opened, saved, renamed, unbound or read again: the dot follows at once.
  useEffect(reportStatus, [tab.scriptPath, tab.savedText]);

  useEffect(() => {
    const path = tab.scriptPath;
    if (!shown || path === null || tab.savedText !== null) return;
    void verify().then((unreadable) => {
      if (unreadable !== null) toast("warning", `Could not read ${path}: ${unreadable}. The tab counts as unsaved.`);
    });
  }, [shown, tab.scriptPath, tab.savedText]);

  // ⌘S / ⌘⇧S while the focus is elsewhere in the tab (the editor's own keymap handles them inside it). Never the
  // browser's "Save page"; nothing while a dialog has the keyboard.
  const commands = useRef({ save, saveAs });
  useLayoutEffect(() => {
    commands.current = { save, saveAs };
  });
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (isImeKeyEvent(e) || e.defaultPrevented) return;
      if (!e.metaKey || e.ctrlKey || e.altKey || e.key.toLowerCase() !== "s") return;
      e.preventDefault();
      if (isDialogOpen()) return;
      if (e.shiftKey) commands.current.saveAs();
      else commands.current.save();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);

  // A closed dialog hands the keyboard back to the editor (it has no trigger for Radix to return the focus to).
  const refocus = (event: Event) => {
    event.preventDefault();
    editor.current?.focus();
  };
  const dialogs = (
    <>
      {saveAsOpen && (
        <SaveAsDialog
          tabId={tabId}
          boundPath={tab.scriptPath}
          suggestedName={tab.title}
          onSave={saveTo}
          onClose={() => setSaveAsOpen(false)}
          onCloseAutoFocus={refocus}
        />
      )}
      {prompt && (
        <ClosePromptDialog prompt={prompt} onChoose={(choice) => void choose(choice)} onCloseAutoFocus={refocus} />
      )}
    </>
  );
  return { save, saveAs, docChanged, dialogs };
}

const CLOSE_MESSAGES: Record<Unsaved, (path: string) => { title: string; message: string }> = {
  changes: (path) => ({
    title: `Save changes to ${baseName(path)}?`,
    message: `${path} has unsaved changes. They are lost if the tab closes without saving.`,
  }),
  missing: (path) => ({
    title: `${baseName(path)} no longer exists`,
    message: `${path} was deleted or moved outside DbaGuy. Save writes the tab’s text there again; Discard loses it.`,
  }),
  unknown: (path) => ({
    title: `Save ${baseName(path)}?`,
    message: `${path} could not be read, so it is not known whether the tab’s text is saved. Save reads it again, then writes the text.`,
  }),
};

function ClosePromptDialog({
  prompt,
  onChoose,
  onCloseAutoFocus,
}: {
  prompt: ClosePrompt;
  onChoose: (choice: CloseChoice) => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const { title, message } = CLOSE_MESSAGES[prompt.unsaved](prompt.path);
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onChoose("cancel")}
      title={title}
      width={460}
      onCloseAutoFocus={onCloseAutoFocus}
      footer={
        <>
          <Button variant="ghost" autoFocus onClick={() => onChoose("cancel")}>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => onChoose("discard")} disabled={prompt.saving}>
            Discard
          </Button>
          <Button variant="primary" onClick={() => onChoose("save")} disabled={prompt.saving}>
            {prompt.saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <p className="text-[13px]" data-selectable>
        {message}
      </p>
      {prompt.error && (
        <p className="mt-2 text-[12px] whitespace-pre-wrap text-danger" data-selectable>
          {prompt.error}
        </p>
      )}
    </Dialog>
  );
}
