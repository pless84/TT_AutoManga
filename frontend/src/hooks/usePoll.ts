import { useEffect, useRef } from 'react';

export interface PollOptions {
  /** Poll only while true (default true). Turning it off stops the chain. */
  enabled?: boolean;
  /** Run the first tick right away instead of after `ms` (default false). */
  immediate?: boolean;
}

const isHidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden';

/**
 * Calls `fn` every `ms` while `enabled`, as a setTimeout chain: the next tick
 * is scheduled only after the previous one's promise settles, so a slow
 * response never overlaps the next request. Pauses while the page is hidden
 * (no requests from a background tab) and ticks at once when it is visible
 * again. `fn` may change on every render: the latest one is called. Its
 * `signal` is aborted when the poll stops (disabled, `ms` changed, unmounted),
 * so a response that lands afterwards can be dropped (or the fetch aborted).
 */
export function usePoll(
  fn: (signal: AbortSignal) => unknown,
  ms: number,
  { enabled = true, immediate = false }: PollOptions = {},
) {
  const fnRef = useRef(fn);
  useEffect(() => { fnRef.current = fn; });

  useEffect(() => {
    if (!enabled) return;
    const ctl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let running = false;

    const schedule = () => {
      clearTimeout(timer);
      timer = undefined;
      if (!ctl.signal.aborted && !isHidden()) timer = setTimeout(tick, ms);
    };
    const tick = async () => {
      timer = undefined;
      if (ctl.signal.aborted || running) return;
      running = true;
      try {
        await fnRef.current(ctl.signal);
      } catch (e) {
        if (!ctl.signal.aborted) console.error(e);
      } finally {
        running = false;
      }
      schedule();
    };
    const onVisibility = () => {
      if (isHidden()) { clearTimeout(timer); timer = undefined; }
      // back in view: catch up now (a tick still in flight schedules the next itself)
      else if (!running) { clearTimeout(timer); void tick(); }
    };

    document.addEventListener('visibilitychange', onVisibility);
    if (immediate && !isHidden()) void tick();
    else schedule();
    return () => {
      ctl.abort();
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [enabled, ms, immediate]);
}
