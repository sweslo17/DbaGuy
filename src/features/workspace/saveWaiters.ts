/**
 * Who waits for the workspace to be saved (`saveWorkspaceNow`): each caller waits for a save that started after it
 * asked, since a save already running serialized the state from before the call (a grid commit's "applying" would not
 * be in it, 05b pre-flight H1). Pure bookkeeping: the writer numbers its saves and says when each has ended.
 */
export class SaveWaiters {
  /** Saves started so far. */
  private started = 0;
  /** Each waiter with the number of saves started when it asked: a later one answers it. */
  private waiting: { after: number; done: (saved: boolean) => void }[] = [];

  /** Resolves once a save started after this call has ended: true when it saved, false when it failed. */
  wait(): Promise<boolean> {
    return new Promise((done) => this.waiting.push({ after: this.started, done }));
  }

  /** A save starts, serializing the state as it is now: its number, for `ended`. */
  start(): number {
    this.started += 1;
    return this.started;
  }

  /** Save `save` (from `start`) has ended: whoever asked before it started is told whether it saved. */
  ended(save: number, saved: boolean): void {
    const answered = this.waiting.filter((waiter) => waiter.after < save);
    if (answered.length === 0) return;
    this.waiting = this.waiting.filter((waiter) => waiter.after >= save);
    for (const waiter of answered) waiter.done(saved);
  }
}
