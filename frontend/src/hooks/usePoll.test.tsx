import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { usePoll, type PollOptions } from './usePoll';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let visibility: DocumentVisibilityState = 'visible';
function setVisibility(v: DocumentVisibilityState) {
  visibility = v;
  document.dispatchEvent(new Event('visibilitychange'));
}

function Poller({ fn, ms, opts }: { fn: (s: AbortSignal) => unknown; ms: number; opts?: PollOptions }) {
  usePoll(fn, ms, opts);
  return null;
}

let root: Root;
let host: HTMLElement;
const render = (fn: (s: AbortSignal) => unknown, ms: number, opts?: PollOptions) =>
  act(() => { root.render(<Poller fn={fn} ms={ms} opts={opts} />); });

/** Advance fake time and let the awaited callbacks settle. */
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

beforeEach(() => {
  vi.useFakeTimers();
  visibility = 'visible';
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
  host = document.createElement('div');
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('usePoll', () => {
  it('ticks every ms, the first one after ms', async () => {
    const fn = vi.fn();
    render(fn, 1000);
    expect(fn).not.toHaveBeenCalled();
    await advance(999);
    expect(fn).not.toHaveBeenCalled();
    await advance(1);
    expect(fn).toHaveBeenCalledTimes(1);
    await advance(3000);
    expect(fn).toHaveBeenCalledTimes(4);
  });

  it('ticks at once with immediate', async () => {
    const fn = vi.fn();
    render(fn, 1000, { immediate: true });
    await advance(0);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('does nothing while disabled, and stops when disabled', async () => {
    const fn = vi.fn();
    render(fn, 1000, { enabled: false });
    await advance(5000);
    expect(fn).not.toHaveBeenCalled();
    render(fn, 1000, { enabled: true });
    await advance(1000);
    expect(fn).toHaveBeenCalledTimes(1);
    render(fn, 1000, { enabled: false });
    await advance(5000);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('never overlaps: the next tick waits for a slow one to settle', async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const fn = vi.fn(() => {
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      return new Promise<void>(resolve => setTimeout(() => { inFlight--; resolve(); }, 2500));
    });
    render(fn, 1000);
    await advance(1000);   // tick 1 starts, settles at 3500
    await advance(2500);
    expect(fn).toHaveBeenCalledTimes(1);
    await advance(1000);   // tick 2 at 4500
    expect(fn).toHaveBeenCalledTimes(2);
    expect(maxInFlight).toBe(1);
  });

  it('pauses while the page is hidden and ticks at once when visible again', async () => {
    const fn = vi.fn();
    render(fn, 1000);
    await advance(1000);
    expect(fn).toHaveBeenCalledTimes(1);
    act(() => setVisibility('hidden'));
    await advance(10_000);
    expect(fn).toHaveBeenCalledTimes(1);
    act(() => setVisibility('visible'));
    await advance(0);
    expect(fn).toHaveBeenCalledTimes(2);
    await advance(1000);
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it('does not start while hidden', async () => {
    visibility = 'hidden';
    const fn = vi.fn();
    render(fn, 1000, { immediate: true });
    await advance(5000);
    expect(fn).not.toHaveBeenCalled();
    act(() => setVisibility('visible'));
    await advance(0);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('calls the latest fn and keeps polling after a rejected tick', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const first = vi.fn(() => Promise.reject(new Error('boom')));
    const second = vi.fn();
    render(first, 1000);
    await advance(1000);
    expect(first).toHaveBeenCalledTimes(1);
    render(second, 1000);
    await advance(1000);
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).toHaveBeenCalledTimes(1);
  });

  it('aborts the signal when the poll stops', async () => {
    let signal: AbortSignal | null = null;
    render(s => { signal = s; }, 1000);
    await advance(1000);
    expect(signal!.aborted).toBe(false);
    act(() => root.unmount());
    expect(signal!.aborted).toBe(true);
    root = createRoot(host);   // for afterEach
  });
});
