import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPersistedWrites, useProjectStore } from './projectStore';
import type { Panel } from '../types';

const KEY = 'automanga-storage';
const panel = (id: string, scene: string): Panel => ({
  id, order: 0, scene, characters: [], dialogue: '', prompt: {}, image: 'data:image/png;base64,AAAA',
} as Panel);

describe('projectStore persistence', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    flushPersistedWrites();
    localStorage.clear();
  });
  afterEach(() => vi.useRealTimers());

  it('writes localStorage at most every 500 ms, with the latest state', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const s = useProjectStore.getState();
    s.setPanels([panel('panel_1', 'a')]);
    for (let i = 0; i < 50; i++) s.updatePanel('panel_1', { scene: `scene ${i}` });
    expect(setItem).not.toHaveBeenCalled();

    vi.advanceTimersByTime(500);
    expect(setItem).toHaveBeenCalledTimes(1);
    const saved = JSON.parse(localStorage.getItem(KEY)!);
    expect(saved.state.panels).toEqual([{ id: 'panel_1', order: 0, scene: 'scene 49', characters: [], dialogue: '', prompt: {} }]);
    setItem.mockRestore();
  });

  it('writes at once when the page is hidden', () => {
    useProjectStore.getState().setPromptMode('tags');
    expect(localStorage.getItem(KEY)).toBeNull();
    const vis = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(JSON.parse(localStorage.getItem(KEY)!).state.promptMode).toBe('tags');
    vis.mockRestore();
  });
});
