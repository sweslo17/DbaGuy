import { Prec, StateEffect, StateField, type Extension } from "@codemirror/state";
import { EditorView, keymap, showTooltip, ViewPlugin, type Tooltip, type ViewUpdate } from "@codemirror/view";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import type { DescribeResult } from "@/lib/api/types";
import { isImeKeyEvent } from "@/lib/ime";
import { kindLabel } from "../explorer/kinds";
import { statementWindow } from "./statements";

/*
 * Quick describe (spec §4.11): ⌥-click on an identifier, or F1 at the cursor, shows the columns of the table it names
 * (directly or through an alias) in a popover: name, type, PK badge and FK target, from `POST /sql/describe` with the
 * statement around that position. Escape, an edit, a cursor move or leaving the editor closes it. ⌥-click belongs to
 * this alone (ruling R-02c-5).
 */

/** What the query tab knows when a describe starts; read each time, so it never needs reconfiguring. */
export interface DescribeTarget {
  profileId: string | null;
  database: string | null;
}

type Content =
  | { kind: "loading" }
  | { kind: "relation"; relation: DescribeResult }
  | { kind: "note"; text: string };

interface Popover {
  id: number;
  pos: number;
  content: Content;
}

const NOTE_MS = 1_500;
const ERROR_MS = 3_000;

const setPopover = StateEffect.define<Popover | null>();

const popoverField = StateField.define<Popover | null>({
  create: () => null,
  update(value, tr) {
    let next = value && (tr.docChanged || tr.selection) ? null : value;
    for (const effect of tr.effects) if (effect.is(setPopover)) next = effect.value;
    return next;
  },
  provide: (field) => showTooltip.from(field, (popover) => (popover ? tooltip(popover) : null)),
});

let popoverCount = 0;

function shown(view: EditorView, id: number): boolean {
  return view.state.field(popoverField, false)?.id === id;
}

/** Closes the popover; false when none is open (Escape then goes on to other handlers). */
function closePopover(view: EditorView): boolean {
  if (!view.state.field(popoverField, false)) return false;
  view.dispatch({ effects: setPopover.of(null) });
  return true;
}

/**
 * Once a focus change settles (never inside an editor update): closes the popover when the focus left the editor and
 * the popover. `source`, the popover that lost the focus, no longer being in the page means it was closed or replaced.
 */
function closeIfFocusLeft(view: EditorView, source?: HTMLElement): void {
  window.setTimeout(() => {
    if (source && !source.isConnected) return;
    const active = document.activeElement;
    if (!active || !view.dom.contains(active)) closePopover(view);
  }, 0);
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderRelation(dom: HTMLElement, relation: DescribeResult): void {
  const title = relation.schema_name ? `${relation.schema_name}.${relation.name}` : relation.name;
  dom.setAttribute("aria-label", `Columns of ${title}`);
  const head = element("div", "cm-describe-head");
  head.append(element("span", "cm-describe-name", title), element("span", "cm-describe-kind", kindLabel(relation.kind)));
  dom.append(head);
  if (!relation.columns.length) {
    dom.append(element("div", "cm-describe-note", "No columns"));
    return;
  }
  const body = element("tbody", "");
  for (const column of relation.columns) {
    const row = element("tr", "");
    const keys = element("td", "cm-describe-keys");
    if (column.is_primary_key) keys.append(element("span", "cm-describe-pk", "PK"));
    if (column.references) {
      const target = element("span", "cm-describe-ref", `→ ${column.references}`);
      target.title = `Foreign key to ${column.references}`;
      keys.append(target);
    }
    row.append(
      element("td", "cm-describe-column", column.name),
      element("td", "cm-describe-type", column.type_name),
      keys,
    );
    body.append(row);
  }
  const table = element("table", "cm-describe-columns");
  table.append(body);
  dom.append(table);
}

function tooltip(popover: Popover): Tooltip {
  return {
    pos: popover.pos,
    create(view) {
      const dom = element("div", "cm-describe");
      dom.setAttribute("data-selectable", "");
      dom.setAttribute("role", "dialog");
      dom.tabIndex = -1; // a click inside takes the focus, so Escape still reaches it
      dom.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || isImeKeyEvent(event)) return;
        event.preventDefault();
        closePopover(view);
        view.focus();
      });
      dom.addEventListener("focusout", () => closeIfFocusLeft(view, dom));
      const { content } = popover;
      if (content.kind === "relation") renderRelation(dom, content.relation);
      else {
        // A quick answer never flashes "Loading…": it fades in only after a moment.
        if (content.kind === "loading") dom.classList.add("cm-describe-pending");
        dom.append(element("div", "cm-describe-note", content.kind === "loading" ? "Loading…" : content.text));
      }
      return { dom };
    },
  };
}

const describeTheme = EditorView.theme({
  ".cm-tooltip.cm-describe": {
    maxHeight: "320px",
    maxWidth: "min(560px, 90vw)",
    overflow: "auto",
    fontFamily: "var(--font-sans)",
    fontSize: "12px",
    lineHeight: "18px",
    cursor: "auto",
    outline: "none",
    userSelect: "text",
    WebkitUserSelect: "text",
    boxShadow: "0 4px 16px rgba(0, 0, 0, 0.12)",
  },
  ".cm-describe-pending": { animation: "cm-describe-show 150ms ease-out 250ms both" },
  "@keyframes cm-describe-show": { from: { opacity: "0" }, to: { opacity: "1" } },
  ".cm-describe-head": {
    position: "sticky",
    top: "0",
    display: "flex",
    alignItems: "baseline",
    gap: "8px",
    padding: "6px 10px 5px",
    backgroundColor: "var(--elevated)",
    borderBottom: "1px solid var(--border)",
  },
  ".cm-describe-name": { fontFamily: "var(--font-mono)", fontWeight: "600" },
  ".cm-describe-kind": { color: "var(--muted)", fontSize: "11px" },
  ".cm-describe-columns": { borderCollapse: "collapse", margin: "4px 0 6px" },
  ".cm-describe-columns td": { padding: "1px 12px 1px 0", whiteSpace: "nowrap", verticalAlign: "baseline" },
  ".cm-describe-columns td:first-child": { paddingLeft: "10px" },
  ".cm-describe-column": { fontFamily: "var(--font-mono)" },
  ".cm-describe-type": { fontFamily: "var(--font-mono)", color: "var(--muted)" },
  ".cm-describe-pk": {
    display: "inline-block",
    marginRight: "6px",
    padding: "0 4px",
    border: "1px solid var(--accent)",
    borderRadius: "3px",
    color: "var(--accent)",
    fontSize: "10px",
    fontWeight: "600",
    lineHeight: "14px",
  },
  ".cm-describe-ref": { fontFamily: "var(--font-mono)", color: "var(--muted)" },
  ".cm-describe-note": { padding: "6px 10px", color: "var(--muted)" },
});

/**
 * Describe on demand: ⌥-click on an identifier or F1 at the cursor. `target` (the tab's connection and database) is
 * read at each describe.
 */
export function describeOnDemand(target: () => DescribeTarget): Extension {
  const describer = ViewPlugin.fromClass(
    class {
      private request: { id: number; controller: AbortController } | null = null;
      private timer: number | null = null;

      constructor(private readonly view: EditorView) {}

      update(update: ViewUpdate): void {
        // Closed or replaced while loading: the answer is no longer wanted.
        if (this.request && !shown(update.view, this.request.id)) this.abort();
      }

      destroy(): void {
        this.abort();
        this.clearTimer();
      }

      describeAt(pos: number): void {
        const { view } = this;
        const { profileId, database } = target();
        const id = ++popoverCount;
        const anchor = view.state.wordAt(pos)?.from ?? pos;
        this.abort();
        this.clearTimer();
        if (!profileId) {
          this.note(id, anchor, "Choose a connection first", NOTE_MS);
          return;
        }
        const bounds = statementWindow(view.state, pos);
        const body = {
          profile_id: profileId,
          database,
          text: view.state.sliceDoc(bounds.from, bounds.to),
          cursor: pos - bounds.from,
        };
        view.dispatch({ effects: setPopover.of({ id, pos: anchor, content: { kind: "loading" } }) });
        const controller = new AbortController();
        this.request = { id, controller };
        void (async () => {
          try {
            const client = await api();
            const relation = unwrap(await client.POST("/sql/describe", { body, signal: controller.signal }));
            if (!shown(view, id)) return;
            if (relation) view.dispatch({ effects: setPopover.of({ id, pos: anchor, content: { kind: "relation", relation } }) });
            else this.note(id, anchor, "No table here", NOTE_MS);
          } catch (error) {
            if (!controller.signal.aborted && shown(view, id)) {
              this.note(id, anchor, `Could not describe: ${errorMessage(error)}`, ERROR_MS);
            }
          } finally {
            if (this.request?.id === id) this.request = null;
          }
        })();
      }

      /** A short message at `pos` that closes by itself after `ms`. */
      private note(id: number, pos: number, text: string, ms: number): void {
        this.view.dispatch({ effects: setPopover.of({ id, pos, content: { kind: "note", text } }) });
        this.timer = window.setTimeout(() => {
          this.timer = null;
          if (shown(this.view, id)) closePopover(this.view);
        }, ms);
      }

      private abort(): void {
        this.request?.controller.abort();
        this.request = null;
      }

      private clearTimer(): void {
        if (this.timer !== null) window.clearTimeout(this.timer);
        this.timer = null;
      }
    },
    {
      eventHandlers: {
        mousedown(event, view) {
          if (!event.altKey || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return false;
          const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
          if (pos === null) return false;
          event.preventDefault(); // no cursor move, no selection
          view.focus();
          this.describeAt(pos);
          return true;
        },
        blur(_event, view) {
          // Focus going into the popover (to select or scroll its text) keeps it open.
          if (view.state.field(popoverField, false)) closeIfFocusLeft(view);
          return false;
        },
      },
    },
  );

  return [
    popoverField,
    describer,
    Prec.highest(
      keymap.of([
        {
          key: "F1",
          run: (view) => {
            view.plugin(describer)?.describeAt(view.state.selection.main.head);
            return true;
          },
        },
        // Before vim's own Escape, but only while the popover is open.
        { key: "Escape", run: closePopover },
      ]),
    ),
    describeTheme,
  ];
}
