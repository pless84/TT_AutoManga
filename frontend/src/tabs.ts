// The app's tabs, each its own chunk: only the open tab's code is downloaded,
// the others when first opened. App renders one at a time (switching unmounts
// the previous tab; state that must survive lives in stores).
import { lazy, type ComponentType, type LazyExoticComponent } from 'react';
import type { TabId } from './types';

export const TAB_LOADERS: Record<TabId, () => Promise<ComponentType>> = {
  wizard: () => import('./components/wizard/WizardTab').then(m => m.WizardTab),
  storyboard: () => import('./components/storyboard/StoryboardTab').then(m => m.StoryboardTab),
  prompt: () => import('./components/prompt/PromptLabTab').then(m => m.PromptLabTab),
  assets: () => import('./components/assets/AssetTab').then(m => m.AssetTab),
  composer: () => import('./components/composer/ComposerTab').then(m => m.ComposerTab),
  showcase: () => import('./components/showcase/ShowcaseTab').then(m => m.ShowcaseTab),
  settings: () => import('./components/settings/SettingsTab').then(m => m.SettingsTab),
  characters: () => import('./components/characters/CharacterTab').then(m => m.CharacterTab),
  comics: () => import('./components/comics/ComicsTab').then(m => m.ComicsTab),
};

export const TABS = Object.fromEntries(
  Object.entries(TAB_LOADERS).map(([id, load]) => [id, lazy(() => load().then(c => ({ default: c })))]),
) as Record<TabId, LazyExoticComponent<ComponentType>>;
