import { useEffect, useEffectEvent, useState, type ReactNode } from 'react';
import type { Exposure, ImageMathOperation, PlaybackUpdate } from './exposure';

const modes = { blink: 'Blink', stack: 'Stack', difference: 'Difference' } as const;
export type CompareMode = keyof typeof modes;
export type VisitSelections = { stack: string[]; difference: [string, string] };

export function defaultVisitSelections(result?: PlaybackUpdate): VisitSelections {
  const frames = result?.mode === 'matched' ? result.frames : [];
  return { stack: frames.map((frame) => frame.key), difference: [frames[0]?.key ?? '', frames.at(-1)?.key ?? ''] };
}

export function retainVisitSelections(selections: VisitSelections, frames: Exposure[]): VisitSelections {
  const available = new Set(frames.map((frame) => frame.key));
  return {
    stack: selections.stack.every((key) => available.has(key)) ? selections.stack : [],
    difference: selections.difference.every((key) => !key || available.has(key)) ? selections.difference : ['', ''],
  };
}

function visitLabel(frame: Exposure, visit = frame.visit) {
  const time = frame.observedAt.replace('T', ' ').slice(0, 19);
  return `${visit == null ? '' : `Visit ${visit} · `}${time} UTC`;
}

type ControlsProps = {
  mode: CompareMode;
  frames: Exposure[];
  visits?: number[];
  selections: VisitSelections;
  unavailable: string;
  onMode: (mode: CompareMode) => void;
  onSelect: (selections: VisitSelections) => void;
};

export function CompareImageControls({ mode, frames, visits, selections, unavailable, onMode, onSelect }: ControlsProps) {
  const [earlier, later] = selections.difference;
  const earlierIndex = frames.findIndex((frame) => frame.key === earlier);
  const laterIndex = frames.findIndex((frame) => frame.key === later);
  return (
    <>
      <div className="compare-modes" role="group" aria-label="Compare image mode">
        {(Object.keys(modes) as CompareMode[]).map((item) => (
          <button key={item} className="control" type="button" aria-pressed={mode === item}
            disabled={item !== 'blink' && Boolean(unavailable)} onClick={() => onMode(item)}>
            {modes[item]}
          </button>
        ))}
      </div>
      {unavailable ? mode === 'blink' && <p className="lookup-status">{unavailable}</p> : mode === 'stack' ? (
        <fieldset className="stack-visits">
          <legend>Stack visits · {selections.stack.length} selected</legend>
          {frames.map((frame, index) => {
            const checked = selections.stack.includes(frame.key);
            return (
              <label key={frame.key}>
                <input type="checkbox" checked={checked} onChange={() => onSelect({
                  ...selections,
                  stack: frames.filter(({ key }) => key === frame.key ? !checked : selections.stack.includes(key))
                    .map(({ key }) => key),
                })} />
                {visitLabel(frame, visits?.[index])}
              </label>
            );
          })}
        </fieldset>
      ) : mode === 'difference' && (
        <>
          <div className="sequence-choice">
            <label htmlFor="difference-earlier">Earlier visit</label>
            <select id="difference-earlier" value={earlier}
              onChange={(event) => onSelect({ ...selections, difference: [event.currentTarget.value, later] })}>
              <option value="" disabled>Choose an earlier visit</option>
              {frames.slice(0, -1).map((frame, index) => (
                <option key={frame.key} value={frame.key} disabled={laterIndex >= 0 && index >= laterIndex}>
                  {visitLabel(frame, visits?.[index])}
                </option>
              ))}
            </select>
            <label htmlFor="difference-later">Later visit</label>
            <select id="difference-later" value={later}
              onChange={(event) => onSelect({ ...selections, difference: [earlier, event.currentTarget.value] })}>
              <option value="" disabled>Choose a later visit</option>
              {frames.slice(1).map((frame, index) => (
                <option key={frame.key} value={frame.key} disabled={index + 1 <= earlierIndex}>
                  {visitLabel(frame, visits?.[index + 1])}
                </option>
              ))}
            </select>
          </div>
          <div className="difference-legend" aria-label="Difference scale">
            <span className="difference-negative">− Earlier brighter</span>
            <span className="difference-zero">0 Equal</span>
            <span className="difference-positive">+ Later brighter</span>
          </div>
        </>
      )}
      {!unavailable && mode === 'difference' && (
        <p className="lookup-status"><strong>Quick look</strong> · Later minus earlier. Differences in sharpness, alignment, or background can leave residuals.</p>
      )}
    </>
  );
}

type ImageState = { url: string } | { error: string } | null;

export function CompareMathImage({ operation, keys, load, fallback }: {
  operation: ImageMathOperation;
  keys: string[];
  load: (signal: AbortSignal) => Promise<Blob>;
  fallback?: ReactNode;
}) {
  const [image, setImage] = useState<ImageState>(null);
  const [attempt, setAttempt] = useState(0);
  const request = useEffectEvent(load);
  useEffect(() => {
    const controller = new AbortController();
    let url: string | undefined;
    void request(controller.signal).then(async (blob) => {
      controller.signal.throwIfAborted();
      url = URL.createObjectURL(blob);
      const preview = new Image();
      preview.src = url;
      await preview.decode();
      if (!controller.signal.aborted) setImage({ url });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setImage({
        error: error instanceof Error && !(error instanceof TypeError)
          ? error.message : 'Could not load the comparison image. Try again.',
      });
    });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [operation, keys, attempt]);

  if (image && 'url' in image) {
    const alt = operation === 'stack' ? `North-up average of ${keys.length} SPHEREx visits`
      : 'North-up SPHEREx difference image, later visit minus earlier visit';
    return <img className="sky-frame" src={image.url} alt={alt} />;
  }
  if (image && 'error' in image) {
    return (
      <div className="sky-frame image-math-placeholder">
        <p role="alert">{image.error}</p>
        <button className="control" type="button" onClick={() => {
          setImage(null);
          setAttempt((value) => value + 1);
        }}>Retry image</button>
      </div>
    );
  }
  return fallback ?? <div className="sky-frame image-math-placeholder" role="status">Calculating {modes[operation].toLowerCase()}…</div>;
}
