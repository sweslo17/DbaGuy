import { useVirtualizer } from "@tanstack/react-virtual";
import { memo, useEffect, useRef } from "react";
import { tabClass } from "@/components/ui/Tabs";
import { cn } from "@/lib/cn";

export interface StripTab {
  value: string;
  label: string;
  /** The tooltip (e.g. the statement, shortened). */
  title?: string;
}

/** A tab's width until it is measured, and the space between two tabs (as in `Tabs`). */
const TAB_ESTIMATE = 160;
const GAP = 4;

/**
 * The run's result tabs, one per result set, in a strip that scrolls sideways (a mouse wheel scrolls it too), then a
 * tab pinned after it (Messages), always in reach. Only the tabs in view are rendered, so a script with thousands of
 * result sets costs no more on each event than a few.
 */
export const ResultTabStrip = memo(function ResultTabStrip({
  tabs,
  pinned,
  value,
  onValueChange,
}: {
  tabs: readonly StripTab[];
  pinned: StripTab;
  value: string;
  onValueChange: (value: string) => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    horizontal: true,
    count: tabs.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => TAB_ESTIMATE,
    getItemKey: (index) => tabs[index].value,
    gap: GAP,
    overscan: 4,
  });
  const selectedIndex = tabs.findIndex((tab) => tab.value === value);
  // The selected tab is scrolled into view (e.g. the first result of a new run).
  useEffect(() => {
    if (selectedIndex >= 0) virtualizer.scrollToIndex(selectedIndex);
  }, [selectedIndex, virtualizer]);

  return (
    <div role="tablist" className="flex shrink-0 border-b border-border px-2">
      {/* `pb-px -mb-px`: the selected tab's underline sits on the border below, as in `Tabs`. */}
      <div
        ref={scroller}
        className="-mb-px min-w-0 overflow-x-auto overflow-y-hidden pb-px [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        onWheel={(e) => {
          if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) e.currentTarget.scrollLeft += e.deltaY;
        }}
      >
        {/* The tabs in view, in the flow: the first is pushed to where the strip has it. */}
        <div className="flex" style={{ width: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((item, i) => {
            const { value: key, label, title } = tabs[item.index];
            return (
              <button
                key={item.key}
                ref={virtualizer.measureElement}
                data-index={item.index}
                type="button"
                role="tab"
                aria-selected={key === value}
                title={title}
                onClick={() => onValueChange(key)}
                className={tabClass(key === value)}
                style={{ marginLeft: i === 0 ? item.start : GAP }}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>
      <button
        type="button"
        role="tab"
        aria-selected={pinned.value === value}
        title={pinned.title}
        onClick={() => onValueChange(pinned.value)}
        className={cn(tabClass(pinned.value === value), tabs.length > 0 && "ml-1")}
      >
        {pinned.label}
      </button>
    </div>
  );
});
