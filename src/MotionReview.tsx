import { useEffect, useRef, useState } from 'react';
import { inspectMotion, type MotionCandidate, type MotionResult } from './exposure';

export function MotionMarkers({ candidate, frame }: { candidate: MotionCandidate; frame: number }) {
  const points = candidate.measurements.map(({ frameIndex, imagePosition: [x, y] }) =>
    ({ frameIndex, x: x * 100, y: y * 100 }));
  const current = points.find(({ frameIndex }) => frameIndex === frame);
  return (
    <svg className="motion-markers" viewBox="0 0 100 100" aria-label="Measured candidate positions" role="img">
      <polyline points={points.map(({ x, y }) => `${x},${y}`).join(' ')} />
      {points.map(({ frameIndex, x, y }) => (
        <circle key={frameIndex} cx={x} cy={y} r={frameIndex === frame ? 1.8 : 1.25}
          className={frameIndex === frame ? 'current' : undefined} />
      ))}
      {current && <circle cx={current.x} cy={current.y} r="2.3" className="marker-outline" />}
    </svg>
  );
}

export function MotionReview({ jobId, exploratory, inspected, selectedCandidate, onResult, onCandidate }: {
  jobId: string;
  exploratory: boolean;
  inspected?: MotionResult;
  selectedCandidate?: MotionCandidate | null;
  onResult: (result: MotionResult) => void;
  onCandidate: (candidate: MotionCandidate | null) => void;
}) {
  const [state, setState] = useState<MotionResult | 'loading' | null>(inspected ?? null);
  const [error, setError] = useState('');
  const [index, setIndex] = useState(() => selectedCandidate ? Math.max(0, inspected?.candidates.findIndex((candidate) =>
    JSON.stringify(candidate.measurements) === JSON.stringify(selectedCandidate.measurements)) ?? 0) : 0);
  const [visible, setVisible] = useState(selectedCandidate !== null);
  const request = useRef<AbortController | null>(null);
  const candidates = state && state !== 'loading' ? state.candidates : [];
  const visits = state && state !== 'loading' ? state.visits : [];
  const skipped = visits.flatMap((issue, i) => issue ? [i + 1] : []);
  const frameLabel = exploratory ? 'exposures' : 'visits';

  useEffect(() => () => request.current?.abort(), []);

  function start() {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setState('loading');
    onCandidate(null);
    setError('');
    void inspectMotion(jobId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setState(result);
      setIndex(0);
      setVisible(true);
      onResult(result);
      onCandidate(result.candidates[0] ?? null);
    }).catch((failure: unknown) => {
      if (controller.signal.aborted) return;
      setState(null);
      setError(failure instanceof Error ? failure.message : 'Motion analysis failed. Try again.');
    }).finally(() => {
      if (request.current === controller) request.current = null;
    });
  }

  function cancel() {
    request.current?.abort();
    request.current = null;
    setState(null);
  }

  const candidate = candidates[index];
  return (
    <div className="motion-review">
      {!state && <button type="button" className="control" onClick={start}>{error ? 'Retry motion analysis' : 'Check for motion'}</button>}
      {error && <p className="lookup-status" role="alert">{error}</p>}
      {state === 'loading' && (
        <>
          <p className="lookup-status" role="status">Checking the sequence for motion…</p>
          <button type="button" className="control" onClick={cancel}>Cancel motion analysis</button>
        </>
      )}
      {state && state !== 'loading' && state.status !== 'inspected' && (
        <p className="lookup-status" role="status">{state.reason}</p>
      )}
      {skipped.length > 0 && <p className="lookup-status" role="status">Could not evaluate {frameLabel} {skipped.join(', ')}.</p>}
      {state && state !== 'loading' && (state.status === 'incomplete' || skipped.length > 0) && (
        <button type="button" className="control" onClick={start}>Retry motion analysis</button>
      )}
      {state && state !== 'loading' && state.status === 'inspected' && !candidate && (
        <p className="lookup-status" role="status">No motion candidates met the alert threshold in this sequence.</p>
      )}
      {candidate && (
        <>
          {candidates.length > 1 && (
            <label className="motion-choice">
              Motion candidate
              <select value={index} onChange={(event) => {
                const next = Number(event.currentTarget.value);
                setIndex(next);
                onCandidate(visible ? candidates[next] : null);
              }}>
                {candidates.map((item, i) => <option value={i} key={i}>{i + 1} · {item.displacement.toFixed(2)}″</option>)}
              </select>
            </label>
          )}
          <p role="status">Possible motion · {candidate.displacement.toFixed(2)} ± {candidate.uncertainty.toFixed(2)}″</p>
          <p className="lookup-status">Detected in {candidate.measurements.length} of {visits.length} {frameLabel}</p>
          <button type="button" className="control" aria-pressed={visible} onClick={() => {
            setVisible(!visible);
            onCandidate(visible ? null : candidate);
          }}>{visible ? 'Hide motion markers' : 'Show motion markers'}</button>
        </>
      )}
    </div>
  );
}
