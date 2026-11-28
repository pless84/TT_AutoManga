// Pieces of the Comics tab that update on their own clock (the run's elapsed
// time, its log, the editor's text), so a tick or a keystroke re-renders only
// them and not the whole tab.
import { memo, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { usePoll } from '../../hooks/usePoll';
import { useComicsDrafts, useDraftText } from './scriptDrafts';

/** Renders `children` with the live editor text, so its parent needn't subscribe to it. */
export function LiveText({ children }: { children: (text: string) => ReactNode }) {
  return <>{children(useDraftText())}</>;
}

/** m:ss since the run started, ticking once a second while it runs. */
export const RunClock = memo(function RunClock({ started, ended }: { started: number; ended: number | null }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  usePoll(() => setNow(Date.now() / 1000), 1000, { enabled: ended == null });
  const elapsed = Math.round((ended ?? Math.max(now, started)) - started);
  return <>{Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, '0')}</>;
});

/** The run's log, kept scrolled to the end as lines arrive. */
export const RunLog = memo(function RunLog({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines.length]);
  return <pre className="comics-log" ref={ref}>{lines.join('\n')}</pre>;
});

/** The script editor. It reads and writes the draft store itself; give it stable handlers. */
export const ScriptEditor = memo(function ScriptEditor({ editorRef, label, onSelect, onKeyDown }: {
  editorRef: RefObject<HTMLTextAreaElement | null>;
  label: string;
  onSelect: () => void;
  onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
}) {
  const text = useDraftText();
  const setDraft = useComicsDrafts(s => s.setDraft);
  return (
    <textarea
      ref={editorRef}
      className="comics-editor"
      spellCheck={false}
      value={text}
      aria-label={label}
      onChange={e => {
        const { selected } = useComicsDrafts.getState();
        if (selected) setDraft(selected, { text: e.target.value });
      }}
      onSelect={onSelect}
      onKeyDown={onKeyDown}
    />
  );
});
