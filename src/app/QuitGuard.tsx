import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import { listJobs } from "@/features/data-tools/jobsApi";
import { onQuitRequested, onWindowCloseRequested, quitApp, revealWindow } from "@/lib/native";
import { quitDecider, runningNow, type QuitQuestion } from "./quitQuestion";

/** How long ⌘Q and closing the window wait for the backend's list (its address included): then nothing runs. */
const LIST_TIMEOUT_MS = 1_000;

/**
 * ⌘Q and closing the window ask while data-tools jobs run (spec ⑥ CR-5, A12); app only. It asks the backend's list,
 * not this window's jobs: right after a reload too (ruling V8). Return and Esc keep the jobs running.
 */
export function QuitGuard() {
  const [asking, setAsking] = useState<{ question: QuitQuestion; answer: (quit: boolean) => void } | null>(null);
  useEffect(() => {
    const decide = quitDecider(
      () => runningNow(() => listJobs(LIST_TIMEOUT_MS), LIST_TIMEOUT_MS),
      (question) => {
        // Only when it asks: a ⌘Q with no job quits without unminimizing the window first (06b review K1).
        revealWindow().catch((error: unknown) =>
          console.warn("Quit: the window could not be brought to the front", error),
        );
        return new Promise<boolean>((resolve) =>
          setAsking({
            question,
            answer: (quit) => {
              setAsking(null);
              resolve(quit);
            },
          }),
        );
      },
    );
    const stopQuit = onQuitRequested(() => {
      void decide("quit").then((quit) => {
        if (quit) void quitApp();
      });
    });
    const stopClose = onWindowCloseRequested(() => decide("close"));
    return () => {
      void stopQuit.then((stop) => stop());
      void stopClose.then((stop) => stop());
    };
  }, []);
  if (!asking) return null;
  const { question, answer } = asking;
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && answer(false)}
      title={question.title}
      width={460}
      footer={
        <>
          <Button variant="danger" onClick={() => answer(true)}>
            {question.confirm}
          </Button>
          <Button variant="primary" onClick={() => answer(false)} autoFocus>
            {question.keep}
          </Button>
        </>
      }
    >
      <p className="text-[13px]" data-selectable>
        {question.message}
      </p>
    </Dialog>
  );
}
