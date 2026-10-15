import { AppShell } from "./app/AppShell";
import { Explorer } from "./features/explorer/Explorer";
import { Workspace } from "./features/workspace/Workspace";

export function App() {
  return <AppShell sidebar={<Explorer />} workspace={<Workspace />} />;
}
