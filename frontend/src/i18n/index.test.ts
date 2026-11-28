import { describe, expect, it, vi } from 'vitest';
import { en, loadTranslations, subscribeTranslations, translationsFor } from '.';

describe('translations', () => {
  it('has English at once and loads Chinese on demand', async () => {
    expect(translationsFor('en')).toBe(en);
    expect(translationsFor('zh')).toBe(en);   // not loaded yet: English stands in
    expect(translationsFor('fr')).toBe(en);   // unknown: English

    const listener = vi.fn();
    const unsubscribe = subscribeTranslations(listener);
    const zh = await loadTranslations('zh');
    unsubscribe();

    expect(listener).toHaveBeenCalledTimes(1);
    expect(zh.tabs.storyboard).toBe('分镜');
    expect(translationsFor('zh')).toBe(zh);
    await expect(loadTranslations('zh')).resolves.toBe(zh);   // once
  });
});
