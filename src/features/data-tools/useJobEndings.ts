import { useEffect, useRef } from "react";
import { dismissToast, toast } from "@/components/ui/Toast";
import { appIsFocused, notify } from "@/lib/native";
import { useAppSettings } from "@/stores/settings";
import { openJobDialog, shownJob } from "./dataDialogs";
import { endingNotice, endingTeller, endingToastKey } from "./jobEndings";
import { onJobEnded, useJobs } from "./jobsStore";

/**
 * Says when a data-tools job ends (spec ⑥ R-06-15, ruling V11), for the app shell's whole life: a system notification
 * per S8 (`notifications.long_query_seconds`, only while DbaGuy is in the background) and, when its view is not open, a
 * toast with Open. Each ending once (`endingTeller`): a corrected one takes its toast's place (one toast per job), or
 * dismisses it when the correction is not news (the job ended cancelled after all) or its view is open now.
 */
export function useJobEndings(): void {
  const { settings } = useAppSettings();
  // The latest threshold, read as a job ends: the listener stays registered for the shell's life
  const threshold = useRef(settings.notifications.long_query_seconds);
  useEffect(() => {
    threshold.current = settings.notifications.long_query_seconds;
  }, [settings]);

  useEffect(() => {
    const teller = endingTeller(
      (requestId) => useJobs.getState().jobs[requestId],
      (record) => {
        const { requestId } = record;
        const notice = endingNotice(record, { shown: shownJob(), focused: appIsFocused(), thresholdS: threshold.current });
        if (notice.notification) {
          console.info(`Notifying: ${notice.notification.title}`);
          void notify(notice.notification.title, notice.notification.body);
        }
        const key = endingToastKey(requestId);
        if (notice.toast) toast(notice.toast.kind, notice.toast.text, { label: "Open", run: () => openJobDialog(requestId) }, key);
        else dismissToast(key); // a guessed ending's toast, if one is shown, no longer says what happened
      },
    );
    const stop = onJobEnded((record) => teller.ended(record));
    return () => {
      stop();
      teller.stop();
    };
  }, []);
}
