import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useComposerStore } from './composerStore';
import type { ComposerElement, ComposerPage } from '../types';

const box = (id: string, x = 10): ComposerElement => ({
  id, type: 'image-slot', x, y: 10, width: 30, height: 30,
  rotation: 0, zIndex: 0, locked: false, visible: true,
} as ComposerElement);

const page = (id: string, elements: ComposerElement[]): ComposerPage => ({
  id, name: id, templateId: 'blank', width: 600, height: 900, order: 0, elements,
});

const elementX = (id: string) =>
  useComposerStore.getState().pages[0].elements.find(e => e.id === id)!.x;

function load(pages: ComposerPage[], projectId = 'proj1') {
  useComposerStore.getState().loadProjectPages({ id: projectId, name: 'P', composerPages: pages }, 'base-1');
}

describe('composerStore undo history', () => {
  beforeEach(() => load([page('p1', [box('a')])]));

  it('records one undo step per drag gesture, however many moves it has', () => {
    const { commit, updateElement } = useComposerStore.getState();
    // what Canvas does: commit on the first real movement, then write updates
    commit();
    for (let x = 11; x <= 40; x++) updateElement('a', { x });
    expect(elementX('a')).toBe(40);
    expect(useComposerStore.getState()._past).toHaveLength(1);

    useComposerStore.getState().undo();
    expect(elementX('a')).toBe(10);
    expect(useComposerStore.getState()._past).toHaveLength(0);
    useComposerStore.getState().redo();
    expect(elementX('a')).toBe(40);
  });

  it('gives two gestures two steps', () => {
    const s = useComposerStore.getState();
    s.commit(); s.updateElement('a', { x: 20 });
    s.commit(); s.updateElement('a', { x: 30 });
    expect(useComposerStore.getState()._past).toHaveLength(2);
    useComposerStore.getState().undo();
    expect(elementX('a')).toBe(20);
  });

  describe('keyed commits', () => {
    beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1_000_000); });
    afterEach(() => vi.useRealTimers());

    it('coalesce a burst on the same key into one step', () => {
      const s = useComposerStore.getState();
      for (let x = 11; x <= 15; x++) {
        s.commit('wheel:a');
        s.updateElement('a', { x });
        vi.advanceTimersByTime(100);
      }
      expect(useComposerStore.getState()._past).toHaveLength(1);
      useComposerStore.getState().undo();
      expect(elementX('a')).toBe(10);
    });

    it('start a new step after a pause or on another key', () => {
      const s = useComposerStore.getState();
      s.commit('wheel:a'); s.updateElement('a', { x: 11 });
      vi.advanceTimersByTime(600);
      s.commit('wheel:a'); s.updateElement('a', { x: 12 });
      s.commit('text:a'); s.updateElement('a', { x: 13 });
      expect(useComposerStore.getState()._past).toHaveLength(3);
    });
  });
});

describe('composerStore savePages', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('marks a conflict on 409 and stops saving until the pages are reloaded', async () => {
    load([page('p1', [box('a')])], 'proj-409');
    useComposerStore.getState().commit();
    useComposerStore.getState().updateElement('a', { x: 50 });

    const fetchMock = vi.fn(async () => new Response('{"detail":"conflict"}', { status: 409 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(useComposerStore.getState().savePages()).resolves.toBe('conflict');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/projects/proj-409/composer-pages');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string).base).toBe('base-1');
    expect(useComposerStore.getState().saveConflict).toBe(true);

    // further saves (autosave, forced) don't overwrite the other copy
    await expect(useComposerStore.getState().savePages({ force: true })).resolves.toBe('conflict');
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // reloading the project's pages clears the conflict
    load([page('p1', [box('a')])], 'proj-409');
    expect(useComposerStore.getState().saveConflict).toBe(false);
  });

  it('stores the new base after a successful save', async () => {
    load([page('p1', [box('a')])], 'proj-ok');
    useComposerStore.getState().commit();
    useComposerStore.getState().updateElement('a', { x: 50 });
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ success: true, base: 'base-2' }), { status: 200 })));

    await expect(useComposerStore.getState().savePages()).resolves.toBe('saved');
    const st = useComposerStore.getState();
    expect(st.pagesBase).toBe('base-2');
    expect(st._savedPages).toBe(st.pages);
    // nothing changed since: the next autosave is skipped
    await expect(useComposerStore.getState().savePages()).resolves.toBe('skipped');
  });
});
