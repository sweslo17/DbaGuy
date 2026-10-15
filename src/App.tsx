import { AppShell } from "./app/AppShell";

export function App() {
  return (
    <AppShell
      sidebar={<div className="p-3 text-muted">Explorer (Task 13)</div>}
      workspace={<div className="p-3 text-muted">Workspace (Task 13)</div>}
    />
  );
}
