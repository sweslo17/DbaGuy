import { createTabActionRegistry, type TabAction } from "@/lib/tabActions";

/** A table tab's own actions besides grid editing (⌘⇧P): Dump table… with the tab's filter (spec ⑥ §3.6). */
export type TableTabActionId = "dumpTable";
export type TableTabActions = Record<TableTabActionId, TabAction>;

const registry = createTabActionRegistry<TableTabActionId>();
export const readTableTabActions = registry.read;
export const useTableTabActions = registry.use;
