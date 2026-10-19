import { Boxes } from "lucide-react";
import { DropdownMenu, type MenuItem } from "@/components/ui/ContextMenu";
import { errorMessage } from "@/lib/api/client";
import type { ContextOption, QueryContext } from "@/lib/api/types";
import { cn } from "@/lib/cn";
import { PickerTrigger } from "./PickerTrigger";
import { effectiveContext, sameContext, useContextOptions, type TabContext } from "./queryContext";

const NO_CONTEXT = "No context";
const TOOLTIP = "Query context: unqualified names resolve in it";

export interface ContextPickerProps {
  /** The tab's own choice: undefined follows the profile's default, "none" is No context. */
  choice: TabContext;
  /** The context the session opened with (the profile's database); null when it has none. */
  sessionDefault: QueryContext | null;
  /** The session has not reported its default yet: a tab that follows it cannot tell its context. */
  loading?: boolean;
  /** Set while the context cannot change (a run is active): the picker is disabled with this tooltip. */
  blockedReason?: string;
  onChange: (next: QueryContext | "none") => void;
}

const contextOf = ({ bucket, scope }: ContextOption): QueryContext => (scope == null ? { bucket } : { bucket, scope });

/** One option as a menu row: checked when it is the tab's context, with a note on the right where one applies. */
function optionItem(
  option: ContextOption,
  current: QueryContext | null,
  sessionDefault: QueryContext | null,
  onChange: ContextPickerProps["onChange"],
): MenuItem {
  const notes = [sameContext(option, sessionDefault) && "default", !option.listed && "not in the schema"];
  return {
    label: option.text,
    checked: sameContext(option, current),
    shortcut: notes.filter(Boolean).join(" · ") || undefined,
    onSelect: () => onChange(contextOf(option)),
  };
}

/**
 * The tab's Couchbase query context (spec §3.11.1): where unqualified names resolve. The menu lists the contexts of
 * the connection's schema (the profile's default marked, the tab's own one too when the schema lacks it) and "No
 * context"; a name is then written in full. The names shown are the backend's text for each context, never composed
 * here.
 */
export function ContextPicker({
  profileId,
  choice,
  sessionDefault,
  loading = false,
  blockedReason,
  onChange,
}: ContextPickerProps & { profileId: string }) {
  // A tab that follows the default cannot tell its context until the session reports it (None would be a wrong guess).
  const known = choice !== undefined || !loading;
  const current = known ? effectiveContext(choice, sessionDefault) : null;
  const noContext = known && current === null;
  // Asked once the session is open, as it is by then unlocked; the menu asks again whenever it opens.
  const options = useContextOptions(profileId, current, !loading);

  let items: MenuItem[];
  if (options.isPending) items = [{ label: "Loading…", disabled: true }];
  else if (options.isError) items = [{ label: errorMessage(options.error), disabled: true }];
  else if (options.data.length === 0) items = [{ label: "No contexts", disabled: true }];
  else items = options.data.map((option) => optionItem(option, current, sessionDefault, onChange));
  items.push({ label: NO_CONTEXT, checked: noContext, onSelect: () => onChange("none"), separatorBefore: true });

  // The trigger's text: the backend's text of the context (not known while the options load: "…").
  const text = noContext ? NO_CONTEXT : options.data?.find((option) => sameContext(option, current))?.text;
  const refreshWhenOpened = (open: boolean) => {
    if (open && !loading) void options.refetch();
  };
  return (
    <DropdownMenu items={items} onOpenChange={refreshWhenOpened} className="max-h-[60vh] max-w-[360px] overflow-y-auto">
      <PickerTrigger
        disabled={!!blockedReason}
        title={blockedReason ?? TOOLTIP}
        className="max-w-[200px] enabled:hover:bg-hover"
      >
        <Boxes size={12} className="shrink-0 text-muted" />
        <span className={cn("truncate", (noContext || text === undefined) && "text-muted")}>{text ?? "…"}</span>
      </PickerTrigger>
    </DropdownMenu>
  );
}
