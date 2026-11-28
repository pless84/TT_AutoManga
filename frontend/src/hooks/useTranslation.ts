import { useEffect, useSyncExternalStore } from 'react';
import { useProjectStore } from '../store/projectStore';
import { loadTranslations, subscribeTranslations, translationsFor } from '../i18n';
import type { TranslationKeys } from '../i18n';

/** A language's UI text: English until a lazily loaded language arrives, then a re-render. */
export function useTranslationsFor(language: string): TranslationKeys {
  const t = useSyncExternalStore(subscribeTranslations, () => translationsFor(language));
  useEffect(() => {
    if (language !== 'en') loadTranslations(language).catch(console.error);
  }, [language]);
  return t;
}

export function useTranslation() {
  const language = useProjectStore((state) => state.settings.language) || 'en';
  const t = useTranslationsFor(language);

  return {
    t,
    language,
  };
}

/** Fills {name} placeholders in a translated string: fill(t.prompt.allFailed, { n: 3 }). */
export function fill(text: string, values: Record<string, string | number>) {
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in values ? String(values[k]) : m));
}
