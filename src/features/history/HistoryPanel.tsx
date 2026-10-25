import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Ban,
  CircleAlert,
  CircleCheck,
  CircleX,
  LoaderCircle,
  Search,
  ShieldBan,
  TimerOff,
  Trash2,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { EngineIcon } from "@/components/EngineIcon";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { errorMessage } from "@/lib/api/client";
import type { Engine, HistoryEntry, HistoryStatus } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { formatDuration, formatRelativeTime, plural } from "@/lib/format";
import { isImeKeyEvent } from "@/lib/ime";
import { useNow } from "@/lib/useNow";
import { useWorkspace } from "@/stores/workspace";
import { useProfiles } from "../connections/hooks";
import { refreshHistory, useHistory, useHistoryActions } from "./useHistory";

const ROW_HEIGHT = 40;
const SEARCH_DELAY_MS = 200;
/** The next page starts loading once a row this close to the end of the list is rendered. */
const LOAD_AHEAD_ROWS = 30;
/** The row shows at most this much of the SQL's first line (the full text is in the preview). */
const LINE_CHARS = 300;
/** The preview shows this much of the SQL until "Show all". */
const PREVIEW_CHARS = 20_000;

const STATUS: Record<HistoryStatus, { icon: LucideIcon; label: string; className: string }> = {
  ok: { icon: CircleCheck, label: "Succeeded", className: "text-success" },
  error: { icon: CircleX, label: "Failed", className: "text-danger" },
  timeout: { icon: TimerOff, label: "Timed out", className: "text-warning" },
  cancelled: { icon: Ban, label: "Cancelled", className: "text-muted" },
  // spec ⑥ D-06-9: a Run SQL file or copy that has not ended, or that was running when the backend ended
  running: { icon: LoaderCircle, label: "Running", className: "text-muted" },
  interrupted: { icon: CircleAlert, label: "Interrupted", className: "text-warning" },
  // spec ⑧ OP-7: an AI tool's statement DbaGuy's gate refused (08b adds its MCP mark, the filter and Copy-only)
  refused: { icon: ShieldBan, label: "Refused", className: "text-danger" },
};

function StatusIcon({ status }: { status: HistoryStatus }) {
  const { icon: Icon, label, className } = STATUS[status];
  return (
    <span role="img" aria-label={label} title={label} className={cn("flex shrink-0", className)}>
      <Icon size={12} />
    </span>
  );
}

/** The SQL's first non-blank line, with "…" when more follows. */
function firstLine(sql: string): string {
  const match = /\S[^\r\n]*/.exec(sql);
  if (!match) return "";
  const line = match[0].trimEnd();
  const more = line.length > LINE_CHARS || /\S/.test(sql.slice(match.index + match[0].length));
  return more ? `${line.slice(0, LINE_CHARS)} …` : line;
}

const startedAt = (entry: HistoryEntry) => Date.parse(entry.started_at);
const absoluteTime = (entry: HistoryEntry) => new Date(startedAt(entry)).toLocaleString();

/** Where an entry ran, e.g. its connection's name: led by the connection's engine icon (none once it is deleted). */
function EntryConnection({ engine, where }: { engine: Engine | undefined; where: string }) {
  return (
    <span className="flex min-w-0 flex-1 items-center gap-1" title={where}>
      {engine && <EngineIcon engine={engine} size={12} />}
      <span className="truncate">{where}</span>
    </span>
  );
}

function HistoryRow({
  entry,
  engine,
  now,
  selected,
}: {
  entry: HistoryEntry;
  engine: Engine | undefined;
  now: number;
  selected: boolean;
}) {
  return (
    <div
      role="option"
      aria-selected={selected}
      className={cn(
        "flex h-full flex-col justify-center gap-0.5 px-2 text-[12px]",
        selected ? "bg-selected" : "hover:bg-hover",
      )}
    >
      <div className="flex min-w-0 items-center gap-1.5">
        <StatusIcon status={entry.status} />
        <span className="min-w-0 flex-1 truncate font-mono">{firstLine(entry.sql)}</span>
      </div>
      <div className="flex min-w-0 items-center gap-1 pl-[18px] text-[11px] text-muted">
        <span className="shrink-0" title={absoluteTime(entry)}>
          {formatRelativeTime(startedAt(entry), now)}
        </span>
        <span className="shrink-0">·</span>
        <EntryConnection engine={engine} where={entry.profile_name} />
        <span className="shrink-0 tabular-nums">{formatDuration(entry.elapsed_ms)}</span>
      </div>
    </div>
  );
}

/** The first `length` UTF-16 units of `text`, never ending inside a surrogate pair. */
function cut(text: string, length: number): string {
  const end = /[\uD800-\uDBFF]/.test(text.charAt(length - 1)) ? length - 1 : length;
  return text.slice(0, end);
}

/**
 * The selected entry, below the list: its SQL (the first 20,000 characters until "Show all") and error can be
 * selected and copied.
 */
function Preview({
  entry,
  engine,
  onClose,
}: {
  entry: HistoryEntry;
  engine: Engine | undefined;
  onClose: () => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const shortened = !showAll && entry.sql.length > PREVIEW_CHARS;
  const facts = [STATUS[entry.status].label, formatDuration(entry.elapsed_ms)];
  if (entry.status === "ok") facts.push(plural(entry.rows, "row"));
  const where = entry.database ? `${entry.profile_name} · ${entry.database}` : entry.profile_name;
  return (
    <div className="flex max-h-[45%] min-h-0 shrink-0 flex-col border-t border-border">
      <div className="flex shrink-0 items-start gap-1.5 px-2 pt-1.5 text-[11px] text-muted">
        <span className="pt-px">
          <StatusIcon status={entry.status} />
        </span>
        <div className="min-w-0 flex-1" data-selectable>
          <EntryConnection engine={engine} where={where} />
          <p>
            {absoluteTime(entry)} · {facts.join(" · ")}
          </p>
        </div>
        <button
          type="button"
          aria-label="Close the preview"
          title="Close the preview"
          className="flex h-4 w-4 shrink-0 items-center justify-center rounded hover:bg-hover hover:text-fg"
          onClick={onClose}
        >
          <X size={12} />
        </button>
      </div>
      <div className="min-h-0 overflow-auto px-2 pt-1 pb-2">
        <pre className="font-mono text-[12px] break-words whitespace-pre-wrap" data-selectable>
          {shortened ? `${cut(entry.sql, PREVIEW_CHARS)}…` : entry.sql}
        </pre>
        {shortened && (
          <button
            type="button"
            className="mt-1 text-[11px] text-accent hover:underline"
            onClick={() => setShowAll(true)}
          >
            Show all ({entry.sql.length.toLocaleString()} characters)
          </button>
        )}
        {entry.error && (
          <p className="mt-1.5 text-[12px] break-words whitespace-pre-wrap text-danger" data-selectable>
            {entry.error}
          </p>
        )}
      </div>
    </div>
  );
}

/** "All" or "This connection": the active tab's connection, else the one selected in the explorer. */
function ScopeToggle({
  only,
  scopeName,
  disabled,
  onChange,
}: {
  only: boolean;
  scopeName: string | null;
  disabled: boolean;
  onChange: (only: boolean) => void;
}) {
  const option = (value: boolean, label: string, title: string) => (
    <button
      type="button"
      aria-pressed={only === value}
      title={title}
      disabled={value && disabled}
      onClick={() => onChange(value)}
      className={cn(
        "min-w-0 flex-1 truncate rounded px-2 py-0.5 outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        only === value ? "bg-selected text-fg" : "text-muted enabled:hover:text-fg",
        "disabled:opacity-40",
      )}
    >
      {label}
    </button>
  );
  const scopeTitle = disabled
    ? "Open a tab or select a connection to show only its statements"
    : `Only the statements run on ${scopeName ?? "the active tab's connection"}`;
  return (
    <div
      role="group"
      aria-label="Connections shown"
      className="flex gap-0.5 rounded-md border border-border p-0.5 text-[11px]"
    >
      {option(false, "All", "The statements run on every connection")}
      {option(true, "This connection", scopeTitle)}
    </div>
  );
}

/**
 * The sidebar's History view (spec §4.6): the executed statements, newest first, searchable (substring of the SQL)
 * and filterable to the active tab's connection, loading 100 more as the list nears its end. A click previews the
 * entry below the list; a double-click (or Enter) inserts its SQL at the cursor of the active query tab, ⌥ opens it in
 * a new query tab on its connection. The list refreshes (its newest page) after every run while it is shown, and
 * whenever the view is shown again.
 */
export function HistoryPanel({ shown }: { shown: boolean }) {
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setQ(search.trim()), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [search]);

  // The scope follows the active tab (any kind); without one, the explorer's selected connection.
  const scopeId = useWorkspace(
    (s) => s.tabs.find((tab) => tab.id === s.activeTabId)?.profileId ?? s.selectedProfileId,
  );
  const profiles = useProfiles();
  const scopeName = profiles.data?.find((item) => item.profile.id === scopeId)?.profile.name ?? null;
  const engines = useMemo(
    () => new Map(profiles.data?.map(({ profile }) => [profile.id, profile.engine])),
    [profiles.data],
  );
  const [onlyScope, setOnlyScope] = useState(false);
  const profileFilter = onlyScope ? scopeId : null;

  const history = useHistory(q, profileFilter);
  const actions = useHistoryActions();
  const entries = useMemo(() => history.data?.pages.flatMap((page) => page.items) ?? [], [history.data]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = useMemo(() => entries.find((entry) => entry.id === selectedId) ?? null, [entries, selectedId]);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const now = useNow(60_000);

  // Shown again: whatever was recorded without a refresh (e.g. a paused result whose tab closed) appears. Not when
  // first shown: the list is loading then.
  const qc = useQueryClient();
  const wasShown = useRef(shown);
  useEffect(() => {
    if (shown && !wasShown.current) refreshHistory(qc);
    wasShown.current = shown;
  }, [shown, qc]);

  const listRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
  });
  const visible = virtualizer.getVirtualItems();

  // A new search or scope starts at the top with nothing selected, so a hidden entry's preview never comes back later.
  useEffect(() => {
    listRef.current?.scrollTo({ top: 0 });
    setSelectedId(null);
  }, [q, profileFilter]);

  // The next page, once the rendered rows near the end: never while a page or a refetch is loading (it would cancel
  // it), and not again after a failure until Retry.
  const lastRendered = visible.at(-1)?.index ?? -1;
  const { hasNextPage, isFetching, isError, fetchNextPage } = history;
  useEffect(() => {
    if (hasNextPage && !isFetching && !isError && lastRendered >= entries.length - LOAD_AHEAD_ROWS) {
      void fetchNextPage();
    }
  }, [hasNextPage, isFetching, isError, fetchNextPage, lastRendered, entries.length]);

  const activate = (entry: HistoryEntry, newTab: boolean) => {
    if (newTab) actions.openInNewTab(entry);
    else actions.insert(entry);
  };

  const select = (index: number) => {
    const target = Math.max(0, Math.min(entries.length - 1, index));
    const entry = entries[target];
    if (!entry) return;
    setSelectedId(entry.id);
    virtualizer.scrollToIndex(target);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (isImeKeyEvent(e)) return;
    const index = selected ? entries.indexOf(selected) : -1;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      select(index + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      select(index < 0 ? 0 : index - 1);
    } else if (e.key === "Enter" && selected) {
      e.preventDefault();
      activate(selected, e.altKey);
    }
  };

  const clear = async () => {
    setClearing(true);
    try {
      if (await actions.clear()) setSelectedId(null);
    } finally {
      setClearing(false);
      setConfirmClear(false);
    }
  };

  const retry = () => void (history.isFetchNextPageError ? history.fetchNextPage() : history.refetch());
  const failure = history.isError && !history.isFetching && (
    <div className="shrink-0 p-3 text-[12px]">
      <p className="text-danger" data-selectable>
        {errorMessage(history.error)}
      </p>
      <button className="mt-1 text-accent" onClick={retry}>
        Retry
      </button>
    </div>
  );
  const filtered = q !== "" || profileFilter !== null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 flex-col gap-1.5 border-b border-border p-2">
        <div className="flex items-center gap-1">
          <label
            className={cn(
              "flex h-6 min-w-0 flex-1 items-center gap-1 rounded-md border border-border bg-elevated pr-0.5 pl-1.5",
              "focus-within:border-accent",
            )}
          >
            <Search size={12} className="shrink-0 text-muted" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              spellCheck={false}
              autoCorrect="off"
              autoCapitalize="off"
              aria-label="Search the history"
              placeholder="Search SQL"
              className="h-full min-w-0 flex-1 bg-transparent text-[12px] outline-none placeholder:text-muted/70"
            />
            {search && (
              <button
                type="button"
                aria-label="Clear the search"
                title="Clear the search"
                className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-muted hover:bg-hover hover:text-fg"
                onClick={() => setSearch("")}
              >
                <X size={12} />
              </button>
            )}
          </label>
          <Button
            variant="ghost"
            size="sm"
            title="Clear history…"
            aria-label="Clear history"
            onClick={() => setConfirmClear(true)}
          >
            <Trash2 size={13} />
          </Button>
        </div>
        <ScopeToggle only={profileFilter !== null} scopeName={scopeName} disabled={!scopeId} onChange={setOnlyScope} />
      </div>
      {/* Notes and Retry stay outside the listbox: its keys (Enter, arrows) are not theirs. */}
      {history.isPending && <p className="animate-delayed-show p-3 text-[12px] text-muted">Loading…</p>}
      {history.isSuccess && entries.length === 0 && (
        <p className="p-4 text-[12px] text-muted">
          {filtered ? "No statements match." : "No statements yet. Statements run in query tabs show up here."}
        </p>
      )}
      {entries.length === 0 && failure}
      <div
        ref={listRef}
        role="listbox"
        aria-label="Query history"
        tabIndex={0}
        onKeyDown={onKeyDown}
        className={cn(
          "min-h-0 flex-1 overflow-x-hidden overflow-y-auto outline-none",
          "focus-visible:outline-solid focus-visible:outline-2 focus-visible:-outline-offset-2",
          "focus-visible:outline-accent/50",
        )}
      >
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {visible.map((item) => {
            const entry = entries[item.index];
            return (
              <div
                key={entry.id}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  right: 0,
                  height: item.size,
                  transform: `translateY(${item.start}px)`,
                }}
                onClick={() => setSelectedId(entry.id)}
                onDoubleClick={(e) => activate(entry, e.altKey)}
              >
                <HistoryRow
                  entry={entry}
                  engine={engines.get(entry.profile_id)}
                  now={now}
                  selected={entry.id === selectedId}
                />
              </div>
            );
          })}
        </div>
      </div>
      {history.isFetchingNextPage && <p className="shrink-0 px-3 py-1.5 text-[11px] text-muted">Loading more…</p>}
      {/* Below the rows: a page that failed to load more is where the list ends. */}
      {entries.length > 0 && failure}
      {selected && (
        <Preview
          key={selected.id}
          entry={selected}
          engine={engines.get(selected.profile_id)}
          onClose={() => setSelectedId(null)}
        />
      )}
      {confirmClear && (
        <ConfirmDialog
          open
          title="Clear history"
          message="Delete every statement in the query history? This cannot be undone."
          confirmLabel="Clear"
          pending={clearing}
          onOpenChange={(open) => !open && !clearing && setConfirmClear(false)}
          onConfirm={() => void clear()}
        />
      )}
    </div>
  );
}
