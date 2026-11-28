// UI text. English is bundled and renders on first paint; other languages are
// separate chunks loaded on demand (loadTranslations), English standing in
// until they arrive. Components use hooks/useTranslation.
import { en, type TranslationKeys } from './en';

export { en };
export type { TranslationKeys };
export type Language = 'en' | 'zh';

const loaders: Record<Exclude<Language, 'en'>, () => Promise<TranslationKeys>> = {
  zh: () => import('./zh').then(m => m.zh),
};

const loaded: Partial<Record<Language, TranslationKeys>> = { en };
const pending: Partial<Record<Language, Promise<TranslationKeys>>> = {};
const listeners = new Set<() => void>();

export const isLanguage = (lang: string): lang is Language => lang === 'en' || lang in loaders;

/** The language's text if it is loaded, else English. */
export function translationsFor(lang: string): TranslationKeys {
  return (isLanguage(lang) && loaded[lang]) || en;
}

/** Loads a language's text (once); subscribers are told when it arrives. */
export function loadTranslations(lang: string): Promise<TranslationKeys> {
  if (!isLanguage(lang)) return Promise.resolve(en);
  const have = loaded[lang];
  if (have) return Promise.resolve(have);
  const load = loaders[lang as Exclude<Language, 'en'>];
  pending[lang] ??= load().then(
    (t) => {
      loaded[lang] = t;
      listeners.forEach(l => l());
      return t;
    },
    (e) => {
      delete pending[lang];   // a later call may try again
      throw e;
    },
  );
  return pending[lang];
}

export function subscribeTranslations(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
