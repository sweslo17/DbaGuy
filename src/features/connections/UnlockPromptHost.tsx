import { cancelUnlockPrompt, finishUnlockPrompt, useUnlockPrompts } from "./unlockPrompts";
import { UnlockDialog } from "./UnlockDialog";

/**
 * The one unlock dialog on screen, mounted once by the app: the first profile in the queue (`unlockPrompts`), then the
 * next once it is answered or cancelled. Never two dialogs for one profile, however many tabs failed for it.
 */
export function UnlockPromptHost() {
  const prompt = useUnlockPrompts((state) => state.queue[0]);
  if (!prompt) return null;
  const profileId = prompt.profile.id;
  return (
    <UnlockDialog
      key={profileId}
      profile={prompt.profile}
      needs={prompt.needs}
      onCancel={() => cancelUnlockPrompt(profileId)}
      onUnlocked={() => finishUnlockPrompt(profileId)}
    />
  );
}
