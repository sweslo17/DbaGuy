import type { ChangeCounts, DocumentChange, SavePreview } from "@/lib/api/types";
import { plural } from "@/lib/format";

/** What the preview's first line says: the value changes apart (they are what to look at), then the rest by kind. */
export interface PreviewSummary {
  /** "1 value changes", "3 values change", "at least 3 values change"; null when no listed change alters a value. */
  values: string | null;
  /** The other kinds, from `counts`, the value changes taken out: "2 keys reordered", "1 number written differently". */
  others: string[];
}

type Kind = keyof ChangeCounts;

/** One kind's phrase ("2 keys reordered"); the kinds in the order the API lists them. */
const PHRASES: Record<Kind, (count: number) => string> = {
  key_order: (count) => `${plural(count, "key")} reordered`,
  number: (count) => `${plural(count, "number")} written differently`,
  duplicate_key: (count) => plural(count, "duplicate key"),
  other: (count) => plural(count, "other change"),
};

/** Every change the server found, listed or not. */
export const totalChanges = (counts: ChangeCounts): number => Object.values(counts).reduce((sum, count) => sum + count, 0);

/**
 * The summary of a preview. `counts` has every change by kind but not which of them change a value, and `changes` lists
 * value changes first but not all changes, so the value changes are counted from the list: exact when the list is
 * complete, and "at least" when it is not (the server keeps a limited number of each kind and lists a limited number in
 * all, so an unlisted change may be one that alters a value). Each kind's remaining count leaves out its listed value
 * changes: a number that changes value is named once.
 */
export function summarizePreview({ changes, counts }: Pick<SavePreview, "changes" | "counts">): PreviewSummary {
  const incomplete = totalChanges(counts) > changes.length;
  const changing = changes.filter((change: DocumentChange) => change.value_changed);
  const values = changing.length;
  const exact = values === 1 ? "1 value changes" : `${values} values change`;
  const phrase = incomplete ? `at least ${exact}` : exact;
  const others = (Object.keys(PHRASES) as Kind[])
    .map((kind) => ({ kind, count: counts[kind] - changing.filter((change) => change.kind === kind).length }))
    .filter(({ count }) => count > 0)
    .map(({ kind, count }) => PHRASES[kind](count));
  return { values: values > 0 ? phrase : null, others };
}
