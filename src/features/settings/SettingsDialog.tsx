import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { create } from "zustand";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { Checkbox, FieldRow, Input, Select } from "@/components/ui/Field";
import { errorMessage } from "@/lib/api/client";
import type { AppSettings } from "@/lib/api/types";
import { isDialogOpen } from "@/lib/dialogs";
import { isImeKeyEvent } from "@/lib/ime";
import { canChooseFiles, chooseFolder } from "@/lib/native";
import { isSettingsKey } from "@/lib/shortcuts";
import { useAppSettings } from "@/stores/settings";
import { scriptsFolderChanged } from "../scripts/useScripts";
import { NumberFieldRow } from "./NumberFieldRow";
import {
  hasNumberProblem,
  NUMBER_FIELDS,
  numberTexts,
  saveErrorText,
  scriptsDirValue,
  withNumbers,
  type NumberId,
  type NumberTexts,
} from "./settingsForm";

const useSettingsDialog = create<{ open: boolean }>(() => ({ open: false }));

/** Opens the Settings dialog (⌘,, the gear in the title bar, and the command palette). */
export function openSettings(): void {
  useSettingsDialog.setState({ open: true });
}

const close = () => useSettingsDialog.setState({ open: false });

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5 [&+&]:mt-5">
      <h3 className="text-[11px] font-semibold tracking-wide text-muted uppercase">{title}</h3>
      {children}
    </section>
  );
}

/** The whole-number box of one numeric setting. */
function NumberRow({ id, texts, onChange }: { id: NumberId; texts: NumberTexts; onChange: (text: string) => void }) {
  return <NumberFieldRow field={NUMBER_FIELDS[id]} text={texts[id]} onChange={onChange} />;
}

/**
 * The form, built from the stored settings when it opens. Saving writes them all (`PUT /settings`): every open editor
 * and the next run use them at once. A refused save keeps the dialog open with the backend's reasons.
 */
function SettingsForm({ initial }: { initial: AppSettings }) {
  const qc = useQueryClient();
  const { save } = useAppSettings();
  const [draft, setDraft] = useState(initial);
  const [texts, setTexts] = useState(() => numberTexts(initial));
  const [scriptsDir, setScriptsDir] = useState(initial.scripts_dir ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    errorRef.current?.scrollIntoView({ block: "nearest" }); // newer engines return a promise: never the effect's
  }, [error]);

  const folder = scriptsDirValue(scriptsDir);
  const invalid = hasNumberProblem(texts) || folder.problem !== null;
  const setText = (id: NumberId) => (text: string) => setTexts((t) => ({ ...t, [id]: text }));
  const patch = <S extends "editor" | "run">(section: S, changes: Partial<AppSettings[S]>) =>
    setDraft((d) => ({ ...d, [section]: { ...d[section], ...changes } }));

  const choose = async () => {
    try {
      const picked = await chooseFolder(folder.value?.startsWith("/") ? folder.value : undefined);
      if (picked) setScriptsDir(picked);
    } catch (e) {
      console.warn("The folder picker failed", e);
      setError(`Could not open the folder picker: ${errorMessage(e)}`);
    }
  };

  const onSave = async () => {
    if (invalid || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await save({ ...withNumbers(draft, texts), scripts_dir: folder.value });
      console.info("Settings saved");
      if ((saved.scripts_dir ?? null) !== (initial.scripts_dir ?? null)) scriptsFolderChanged(qc);
      close();
    } catch (e) {
      console.warn("Saving the settings failed", e);
      setError(saveErrorText(e));
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && close()}
      title="Settings"
      width={540}
      footer={
        <>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void onSave()} disabled={invalid || saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <Section title="Editor">
        <NumberRow id="font_size" texts={texts} onChange={setText("font_size")} />
        <NumberRow id="tab_size" texts={texts} onChange={setText("tab_size")} />
        <FieldRow label="Keyword case" hint="How completion inserts keywords and how ⌘⇧F formats them">
          <Select
            value={draft.editor.keyword_case}
            onChange={(e) => patch("editor", { keyword_case: e.target.value as AppSettings["editor"]["keyword_case"] })}
          >
            <option value="upper">UPPER CASE</option>
            <option value="preserve">As typed</option>
          </Select>
        </FieldRow>
        <FieldRow label="">
          <div className="flex flex-col gap-1.5">
            <Checkbox
              label="Vim key bindings"
              checked={draft.editor.vim}
              onChange={(e) => patch("editor", { vim: e.target.checked })}
            />
            <Checkbox
              label="Wrap long lines"
              checked={draft.editor.word_wrap}
              onChange={(e) => patch("editor", { word_wrap: e.target.checked })}
            />
          </div>
        </FieldRow>
      </Section>

      <Section title="Run">
        <FieldRow label="⌘↵ runs" hint="With a selection, ⌘↵ runs the selection; ⌘⇧↵ always runs every statement">
          <Select
            value={draft.run.default}
            onChange={(e) => patch("run", { default: e.target.value as AppSettings["run"]["default"] })}
          >
            <option value="statement">The statement under the cursor</option>
            <option value="all">Every statement</option>
          </Select>
        </FieldRow>
        <NumberRow id="page_size" texts={texts} onChange={setText("page_size")} />
        <NumberRow id="statement_timeout_s" texts={texts} onChange={setText("statement_timeout_s")} />
        <FieldRow label="">
          <Checkbox
            label="Keep running the next statements after an error"
            checked={draft.run.continue_on_error}
            onChange={(e) => patch("run", { continue_on_error: e.target.checked })}
          />
        </FieldRow>
      </Section>

      <Section title="Safety">
        <NumberRow id="idle_transaction_warn_minutes" texts={texts} onChange={setText("idle_transaction_warn_minutes")} />
        <NumberRow
          id="idle_transaction_rollback_minutes"
          texts={texts}
          onChange={setText("idle_transaction_rollback_minutes")}
        />
      </Section>

      <Section title="Scripts folder">
        <FieldRow
          label="Path"
          error={folder.problem}
          hint="Empty: the default folder. Changing it unbinds open script tabs: they keep their text, no longer saved to a file."
        >
          <div className="flex gap-2">
            <Input
              placeholder="Default: inside DbaGuy’s data folder"
              aria-label="Scripts folder"
              aria-invalid={folder.problem !== null}
              value={scriptsDir}
              onChange={(e) => setScriptsDir(e.target.value)}
            />
            {canChooseFiles() && <Button onClick={() => void choose()}>Choose…</Button>}
          </div>
        </FieldRow>
      </Section>

      <Section title="Notifications">
        <NumberRow id="long_query_seconds" texts={texts} onChange={setText("long_query_seconds")} />
      </Section>

      <Section title="Couchbase">
        <NumberRow id="sample_size" texts={texts} onChange={setText("sample_size")} />
      </Section>

      {error && (
        <p ref={errorRef} className="mt-4 text-[12px] whitespace-pre-wrap text-danger" role="alert" data-selectable>
          {error}
        </p>
      )}
    </Dialog>
  );
}

/**
 * Before the stored settings are known: loading, or why they could not be read, with Retry (which shows "Loading…"
 * again while it asks).
 */
function SettingsUnavailable({ error, reload }: { error: Error | null; reload: () => Promise<unknown> }) {
  return (
    <Dialog open onOpenChange={(next) => !next && close()} title="Settings" width={540}>
      {error ? (
        <div className="flex flex-col items-start gap-2">
          <p className="text-[13px]">Could not load the settings</p>
          <p className="text-[12px] whitespace-pre-wrap text-danger" data-selectable>
            {errorMessage(error)}
          </p>
          <Button onClick={() => void reload()}>Retry</Button>
        </div>
      ) : (
        <p className="text-[13px] text-muted">Loading the settings…</p>
      )}
    </Dialog>
  );
}

/**
 * The Settings dialog (spec §4.9), mounted once by the app shell; ⌘, opens it unless another dialog has the keyboard.
 * Until the stored settings have loaded it shows nothing to edit, so the defaults are never saved over them.
 */
export function SettingsDialogHost() {
  const open = useSettingsDialog((s) => s.open);
  const { settings, loaded, loadError, reload } = useAppSettings();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isImeKeyEvent(e) || e.defaultPrevented || !isSettingsKey(e)) return;
      e.preventDefault();
      if (isDialogOpen()) return; // the palette opens Settings itself (as its Settings command)
      openSettings();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!open) return null;
  return loaded ? <SettingsForm initial={settings} /> : <SettingsUnavailable error={loadError} reload={reload} />;
}
