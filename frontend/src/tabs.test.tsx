import { act, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TABS } from './tabs';
import { useProjectStore } from './store/projectStore';
import type { TabId } from './types';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

// /api/settings/ answers the app's defaults, everything else 404: the tabs must still render their shell
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith('/api/settings/')) return json(useProjectStore.getState().settings);
    return json({ detail: 'not found' }, 404);
  }));
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('lazy tabs', () => {
  it.each(Object.keys(TABS) as TabId[])('%s loads and renders', async (id) => {
    const Tab = TABS[id];
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => {
      root.render(<Suspense fallback={<p>loading</p>}><Tab /></Suspense>);
    });
    // the chunk resolved and the tab replaced the fallback
    await vi.waitFor(() => expect(host.textContent).not.toBe('loading'));
    expect(host.innerHTML).not.toBe('');
    act(() => root.unmount());
    host.remove();
  });
});
