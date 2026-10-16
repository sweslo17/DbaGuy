import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/Button";

interface ErrorBoundaryProps {
  /** Names the boundary in the log. */
  name: string;
  /** The heading shown in place of the content that threw. */
  title: string;
  /** "reload": the page loads again (the workspace is flushed on unload); "retry": the content is rendered again. */
  recover: "reload" | "retry";
  children: ReactNode;
}

/**
 * Catches a render or effect error below it (ruling R-02c-14): the content that threw is replaced by its message and a
 * way back, instead of React unmounting the whole app. Editors hand over their text as they unmount, so nothing typed
 * is lost. React only offers this as a class component.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: unknown): { error: Error } {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error(`${this.props.name} hit an error`, error, info.componentStack);
  }

  private recover = () => {
    if (this.props.recover === "reload") window.location.reload();
    else this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    // The app-wide one replaces the title bar too, so the window stays movable by its background (as while restoring).
    const drag = this.props.recover === "reload" ? { "data-tauri-drag-region": true } : {};
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center gap-2 bg-bg p-6 text-center"
        {...drag}
      >
        <p className="text-[13px] font-semibold">{this.props.title}</p>
        <p className="max-w-[560px] text-[12px] break-words whitespace-pre-wrap text-danger" data-selectable>
          {error.message || String(error)}
        </p>
        <Button onClick={this.recover}>{this.props.recover === "reload" ? "Reload" : "Try again"}</Button>
      </div>
    );
  }
}
