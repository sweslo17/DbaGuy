import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./lib/api/client";

export function App() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: async () => unwrap(await (await api()).GET("/system/health")),
  });
  return (
    <div className="flex h-full flex-col">
      <div data-tauri-drag-region className="h-[38px] shrink-0 border-b border-border bg-panel" />
      <div className="p-4">
        {health.isPending && <span className="text-muted">Connecting to backend…</span>}
        {health.isError && <span className="text-danger">Backend error: {String(health.error)}</span>}
        {health.data && <span className="text-success">Backend: {health.data.status} (v{health.data.version})</span>}
      </div>
    </div>
  );
}
