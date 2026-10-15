import { useQuery } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { pathKey } from "@/lib/api/types";
import type { ObjectTab } from "@/stores/workspace";
import { kindLabel } from "./kinds";

const FEEDBACK_MS = 1500;

function CopyDdlButton({ ddl }: { ddl: string }) {
  const [result, setResult] = useState<"copied" | "failed" | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(ddl);
      setResult("copied");
    } catch {
      setResult("failed"); // e.g. the webview denied clipboard access
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setResult(null), FEEDBACK_MS);
  };
  return (
    <Button size="sm" onClick={() => void copy()}>
      {result === "copied" ? <Check size={12} /> : <Copy size={12} />}{" "}
      {result === "copied" ? "Copied" : result === "failed" ? "Copy failed" : "Copy DDL"}
    </Button>
  );
}

export function ObjectDetailView({ tab }: { tab: ObjectTab }) {
  const detail = useQuery({
    queryKey: ["detail", tab.profileId, pathKey(tab.path)],
    queryFn: async () =>
      unwrap(await (await api()).POST("/metadata/detail", { body: { profile_id: tab.profileId, path: tab.path } })),
  });
  if (detail.isPending) return <p className="p-4 text-muted">Loading…</p>;
  if (detail.isError) {
    return (
      <p className="whitespace-pre-wrap p-4 text-danger" data-selectable>
        {errorMessage(detail.error)}
      </p>
    );
  }
  const d = detail.data;
  return (
    <div className="flex h-full flex-col overflow-auto">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <span className="text-[11px] uppercase tracking-wide text-muted">{kindLabel(d.kind)}</span>
        <span className="font-semibold" data-selectable>
          {d.name}
        </span>
        {d.ddl_is_generated && (
          <span
            className="rounded bg-hover px-1.5 text-[11px] text-muted"
            title="Rebuilt from the catalog; may differ from the original statement"
          >
            approximate
          </span>
        )}
        <div className="flex-1" />
        {d.ddl && <CopyDdlButton ddl={d.ddl} />}
      </div>
      {d.comment && (
        <p className="px-4 pt-2 text-[12px] text-muted" data-selectable>
          {d.comment}
        </p>
      )}
      {d.columns && (
        <table className="mx-4 mt-3 border-collapse text-[12px]" data-selectable>
          <thead>
            <tr className="text-left text-muted">
              {["Name", "Type", "Nullable", "Default", "PK", "Comment"].map((h) => (
                <th key={h} className="border-b border-border px-2 py-1 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.columns.map((c) => (
              <tr key={c.name} className="hover:bg-hover">
                <td className="px-2 py-0.5 font-medium">{c.name}</td>
                <td className="px-2 py-0.5 font-mono">{c.type_name}</td>
                <td className="px-2 py-0.5">{c.nullable ? "yes" : "no"}</td>
                <td className="px-2 py-0.5 font-mono text-muted">{c.default ?? ""}</td>
                <td className="px-2 py-0.5">{c.is_primary_key ? "●" : ""}</td>
                <td className="px-2 py-0.5 text-muted">{c.comment ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {d.ddl && (
        <pre className="m-4 whitespace-pre-wrap rounded-md border border-border bg-panel p-3 font-mono text-[12px]" data-selectable>
          {d.ddl}
        </pre>
      )}
    </div>
  );
}
