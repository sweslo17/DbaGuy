import {
  Braces,
  Columns3,
  Database,
  Eye,
  Folder,
  FunctionSquare,
  Hash,
  KeyRound,
  Layers,
  ListTree,
  Shapes,
  Split,
  Table2,
  Workflow,
  Zap,
  type LucideIcon,
} from "lucide-react";

const KIND_ICONS: Record<string, LucideIcon> = {
  database: Database,
  schema: Braces,
  table: Table2,
  partition: Split,
  view: Eye,
  materialized_view: Layers,
  function: FunctionSquare,
  procedure: Workflow,
  sequence: Hash,
  type: Shapes,
  index: KeyRound,
  trigger: Zap,
  column: Columns3,
};
/** Folder nodes show the icon of the objects they contain. */
const FOLDER_ICONS: Record<string, LucideIcon> = {
  tables: Table2,
  views: Eye,
  materialized_views: Layers,
  functions: FunctionSquare,
  procedures: Workflow,
  sequences: Hash,
  types: Shapes,
  columns: Columns3,
  indexes: KeyRound,
  triggers: Zap,
  partitions: Split,
};
const FOLDER_LABELS: Record<string, string> = {
  tables: "Tables",
  views: "Views",
  materialized_views: "Materialized Views",
  functions: "Functions",
  procedures: "Procedures",
  sequences: "Sequences",
  types: "Types",
  columns: "Columns",
  indexes: "Indexes",
  triggers: "Triggers",
  partitions: "Partitions",
};
const KIND_LABELS: Record<string, string> = {
  database: "Database",
  schema: "Schema",
  table: "Table",
  partition: "Partition",
  view: "View",
  materialized_view: "Materialized View",
  function: "Function",
  procedure: "Procedure",
  sequence: "Sequence",
  type: "Type",
  index: "Index",
  trigger: "Trigger",
  column: "Column",
};

/** Icon for an object kind; for `folder` nodes pass the folder name to get the icon of its contents. */
export function kindIcon(kind: string, folderName?: string): LucideIcon {
  if (kind === "folder") return (folderName && FOLDER_ICONS[folderName]) || Folder;
  return KIND_ICONS[kind] ?? ListTree;
}
export function folderLabel(name: string): string {
  return FOLDER_LABELS[name] ?? name;
}
export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}
