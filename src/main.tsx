import '@fontsource/ibm-plex-mono/latin-400.css';
import '@fontsource/ibm-plex-mono/latin-600.css';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { formatPosition, parseCoordinate, parsePosition, presets, type CoordinateFields, type SkyPosition } from './coordinates';
import { SkyPicker } from './SkyPicker';
import { watchPlayback, type PlaybackUpdate } from './exposure';
import './styles.css';

const fieldSizes = [3, 6, 12, 24, 48] as const;
const emptyFields: CoordinateFields = { ra: '', dec: '' };
const presetStorageKey = 'sac26.target-preset';
const axes = [
  { key: 'ra', name: 'Right ascension (RA)', example: '133.795129° or 08h55m10.831s', range: '0° to less than 360°' },
  { key: 'dec', name: 'Declination (Dec)', example: '−7.245147° or −7d14m42.53s', range: '−90° to +90°' },
] as const;

type PatchDraft = { preset: string; fields: CoordinateFields };

function restorePreset(): PatchDraft {
  try {
    const saved = window.sessionStorage.getItem(presetStorageKey);
    const selected = presets.find(({ name }) => name === saved);
    if (selected) return { preset: selected.name, fields: selected.fields };
  } catch {
    // The picker is still usable when browser storage is blocked.
  }
  return { preset: '', fields: emptyFields };
}

type SubmittedPatch = {
  position: SkyPosition;
  coordinates: CoordinateFields;
  fieldSize: typeof fieldSizes[number];
  target?: 'wise-0855';
};

type WorkspaceState = { patch: SubmittedPatch; result: PlaybackUpdate };

function App() {
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [{ preset, fields }, setDraft] = useState(restorePreset);
  const [errors, setErrors] = useState<Partial<CoordinateFields>>({});
  const [fieldSizeIndex, setFieldSizeIndex] = useState(1);
  const [view, setView] = useState<WorkspaceState | null>(null);
  const [frameIndex, setFrameIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  const drawerToggle = useRef<HTMLButtonElement>(null);
  const submitAction = useRef<HTMLButtonElement>(null);
  const backAction = useRef<HTMLButtonElement>(null);
  const detailsAction = useRef<HTMLButtonElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const draftPosition = useMemo(() => parsePosition(fields), [fields]);
  const fieldSize = fieldSizes[fieldSizeIndex];

  const result = view?.result;
  const isLoading = result?.status === 'searching';
  const canPlay = result?.status === 'ready';
  const frameCount = result?.frames.length ?? 0;
  const exposure = result?.frames[frameIndex] ?? result?.preview;
  const observationTime = exposure?.observedAt.replace('T', ' ').slice(0, 19);
  const workspaceOpen = Boolean(exposure);
  const canRetry = result?.status === 'error' || (result?.status === 'no-match' && result.partial);

  useEffect(() => {
    try {
      if (preset) window.sessionStorage.setItem(presetStorageKey, preset);
      else window.sessionStorage.removeItem(presetStorageKey);
    } catch {
      // The current selection stays in memory when browser storage is blocked.
    }
  }, [preset]);

  useEffect(() => {
    if (workspaceOpen) backAction.current?.focus({ preventScroll: true });
  }, [workspaceOpen]);

  useEffect(() => {
    if (!playing || !canPlay) return;
    const timer = window.setInterval(() => setFrameIndex((index) => 1 - index), 1000);
    return () => window.clearInterval(timer);
  }, [canPlay, playing]);

  useEffect(() => () => requestRef.current?.abort(), []);

  const pickPosition = useCallback((position: SkyPosition) => {
    if (requestRef.current) return;
    setDraft({ preset: '', fields: formatPosition(position) });
    setErrors({});
    setDrawerOpen(true);
  }, []);

  function selectPreset(name: string) {
    const selected = presets.find((item) => item.name === name);
    setDraft((current) => ({ preset: selected?.name ?? '', fields: selected?.fields ?? current.fields }));
    if (selected) setErrors({});
  }

  function loadComparison(patch: SubmittedPatch) {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setDrawerOpen(true);
    setFrameIndex(0);
    setPlaying(false);
    setDetailsOpen(false);
    setView({ patch, result: {
      status: 'searching', message: 'Searching the SPHEREx archive…', preview: null,
      frames: [], comparison: null, partial: false,
    } });
    void watchPlayback(patch.position, patch.fieldSize, controller.signal, (result) => {
      if (controller.signal.aborted) return;
      if (result.status !== 'searching') requestRef.current = null;
      setView({ patch, result });
    }, patch.target).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      requestRef.current = null;
      const message = error instanceof Error && !(error instanceof TypeError)
        ? error.message : 'The playback service is unavailable. Try again.';
      setView((current) => current && ({
        ...current, result: { ...current.result, status: 'error', message },
      }));
    });
  }

  function applyPatch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestRef.current) return;
    const invalid: Partial<CoordinateFields> = {};
    for (const { key, name, range } of axes) {
      if (parseCoordinate(fields[key], key) === null) {
        invalid[key] = fields[key].trim() ? `Enter a coordinate from ${range}.` : `Enter ${name.toLowerCase()}.`;
      }
    }
    setErrors(invalid);
    const firstInvalid = invalid.ra ? 'ra' : invalid.dec ? 'dec' : null;
    if (firstInvalid) {
      formRef.current?.querySelector<HTMLInputElement>(`#${firstInvalid}`)?.focus();
      return;
    }

    if (!draftPosition) return;
    loadComparison({
      position: draftPosition,
      coordinates: { ra: fields.ra.trim(), dec: fields.dec.trim() },
      fieldSize,
      target: preset === 'WISE 0855' ? 'wise-0855' : undefined,
    });
  }

  function backToSky() {
    requestRef.current?.abort();
    requestRef.current = null;
    setView(null);
    setPlaying(false);
    setDrawerOpen(true);
    requestAnimationFrame(() => submitAction.current?.focus({ preventScroll: true }));
  }

  function stepFrame() {
    setPlaying(false);
    setFrameIndex((index) => 1 - index);
  }

  const drawerAction = drawerOpen ? 'Hide patch' : 'Choose patch';
  const backLabel = isLoading ? 'Cancel lookup' : 'Back to sky';
  const playbackLabel = playing ? 'Pause' : 'Play';

  return (
    <div className="app">
      <header className="title-overlay" hidden={workspaceOpen}>
        <button ref={drawerToggle} type="button" className="control drawer-toggle" aria-label={drawerAction} title={drawerAction}
          aria-expanded={drawerOpen} aria-controls="patch-drawer"
          onClick={() => setDrawerOpen((open) => !open)}>
          <span className="patch-icon" aria-hidden="true" />
        </button>
        <h1>Planet X and SPHEREx</h1>
      </header>

      <main className="workspace">
        <aside id="patch-drawer" className="drawer" hidden={workspaceOpen || !drawerOpen} aria-labelledby="patch-title" onKeyDown={(event) => {
          if (event.key === 'Escape') {
            setDrawerOpen(false);
            drawerToggle.current?.focus();
          }
        }}>
          <h2 id="patch-title">Choose a sky patch</h2>
          <form ref={formRef} onSubmit={applyPatch} noValidate>
            <div className="field">
              <label htmlFor="preset">Target preset</label>
              <div className="preset-select">
                <select id="preset" value={preset} disabled={isLoading} onChange={(event) => selectPreset(event.target.value)}>
                  <option value="">Choose a preset</option>
                  {presets.map(({ name }) => <option key={name}>{name}</option>)}
                </select>
              </div>
            </div>

            {axes.map(({ key, name, example }) => (
              <div className="field" key={key}>
                <label htmlFor={key}>{name}</label>
                <input id={key} value={fields[key]} placeholder={example} autoComplete="off" spellCheck={false}
                  disabled={isLoading}
                  aria-invalid={Boolean(errors[key])}
                  aria-describedby={errors[key] ? `${key}-error` : undefined}
                  onChange={(event) => {
                    const value = event.target.value;
                    setDraft((current) => ({ preset: '', fields: { ...current.fields, [key]: value } }));
                    setErrors({ ...errors, [key]: undefined });
                  }} />
                {errors[key] && <span id={`${key}-error`} className="error">{errors[key]}</span>}
              </div>
            ))}

            <div className="field field-size">
              <div className="field-size-heading">
                <label htmlFor="field-size">Field size</label>
                <span>{fieldSize}′ × {fieldSize}′</span>
              </div>
              <input id="field-size" type="range" min={0} max={fieldSizes.length - 1} step={1}
                value={fieldSizeIndex} disabled={isLoading} aria-valuetext={`${fieldSize} by ${fieldSize} arcminutes`}
                onChange={(event) => setFieldSizeIndex(event.currentTarget.valueAsNumber)} />
            </div>

            <div className="form-actions">
              {result?.message && !workspaceOpen && (
                <p className={result.status === 'error' ? 'error' : 'lookup-status'}
                  role={result.status === 'error' ? 'alert' : 'status'}>{result.message}</p>
              )}
              <button ref={submitAction} className="control primary" type={isLoading ? 'button' : 'submit'}
                onClick={isLoading ? (event) => {
                  event.preventDefault();
                  backToSky();
                } : undefined}>
                {isLoading ? 'Cancel' : 'Compare visits'}
              </button>
              {view && canRetry && !workspaceOpen && (
                <button type="button" className="control" onClick={() => loadComparison(view.patch)}>Retry</button>
              )}
            </div>
          </form>
        </aside>

        <section className="viewer" aria-label="Sky viewport">
          <SkyPicker active={!workspaceOpen} presetPosition={preset ? draftPosition : null}
            draftPosition={draftPosition} fieldSize={fieldSize} onPick={pickPosition} />

          {view && exposure && (
            <div className="image-stage">
              <div className="exposure-layout">
                <div className="exposure-frame">
                  <img className="sky-frame" src={exposure.imageUrl}
                    alt={`North-up SPHEREx detector ${exposure.detector} exposure of the selected sky patch`} />
                </div>
                <aside className="exposure-panel" aria-label="Comparison controls" onKeyDown={(event) => {
                  if (event.key === 'Escape' && detailsOpen) {
                    setDetailsOpen(false);
                    detailsAction.current?.focus();
                  }
                }}>
                  <div className="exposure-toolbar">
                    <div className="exposure-heading">
                      <button ref={backAction} type="button" className="control symbol-control"
                        aria-label={backLabel} title={backLabel} onClick={backToSky}>
                        <span aria-hidden="true">←</span>
                      </button>
                      <div className="frame-summary">
                        <time dateTime={exposure.observedAt}>{observationTime} UTC</time>
                        {exposure.wavelength !== null && (
                          <span title="Wavelength at patch center">{exposure.wavelength.toFixed(3)} µm</span>
                        )}
                      </div>
                    </div>
                    {view.result.message && (
                      <p className="lookup-status" role="status">{view.result.message}</p>
                    )}
                    {canPlay && (
                      <>
                        <input className="frame-slider" type="range" min={0} max={frameCount - 1} step={1}
                          value={frameIndex} aria-label="Playback position" aria-valuetext={`Frame ${frameIndex + 1} of ${frameCount}`}
                          onChange={(event) => {
                            setPlaying(false);
                            setFrameIndex(event.currentTarget.valueAsNumber);
                          }} />
                        <div className="exposure-control-row">
                          <div className="playback-controls" role="group" aria-label="Playback controls">
                            <button className="control symbol-control" type="button" aria-label="Previous" title="Previous" onClick={stepFrame}>
                              <span aria-hidden="true">⏮︎</span>
                            </button>
                            <button className="control symbol-control" type="button" aria-label={playbackLabel} title={playbackLabel}
                              onClick={() => setPlaying((active) => !active)}>
                              <span aria-hidden="true">{playing ? '⏸︎' : '▶︎'}</span>
                            </button>
                            <button className="control symbol-control" type="button" aria-label="Next" title="Next" onClick={stepFrame}>
                              <span aria-hidden="true">⏭︎</span>
                            </button>
                          </div>
                          <span className="frame-count">{frameIndex + 1} / {frameCount}</span>
                        </div>
                      </>
                    )}
                    {canRetry && (
                      <div className="exposure-control-row">
                        <button type="button" className="control" onClick={() => loadComparison(view.patch)}>Retry</button>
                      </div>
                    )}
                    <button ref={detailsAction} className="control details-toggle" type="button"
                      aria-expanded={detailsOpen} aria-controls="exposure-details"
                      onClick={() => setDetailsOpen((open) => !open)}>
                      Details <span aria-hidden="true">▸</span>
                    </button>
                  </div>
                  <section className="exposure-details" id="exposure-details" hidden={!detailsOpen}
                    aria-label="Comparison details" tabIndex={0}>
                    <div className="exposure-metadata">
                      <table>
                        <caption>Sky patch</caption>
                        <tbody>
                          <tr><th scope="row">RA</th><td>{view.patch.coordinates.ra}</td></tr>
                          <tr><th scope="row">Dec</th><td>{view.patch.coordinates.dec}</td></tr>
                          <tr><th scope="row">Field size</th><td>{view.patch.fieldSize}′ × {view.patch.fieldSize}′</td></tr>
                        </tbody>
                      </table>
                      <table>
                        <caption>Observation</caption>
                        <tbody>
                          <tr>
                            <th scope="row">Time (UTC)</th>
                            <td><time dateTime={exposure.observedAt}>{observationTime}</time></td>
                          </tr>
                          <tr><th scope="row">Release</th><td>{exposure.release}</td></tr>
                          <tr><th scope="row">Detector</th><td>D{exposure.detector}</td></tr>
                          <tr><th scope="row">ID</th><td>{exposure.observationId}</td></tr>
                          {exposure.wavelength !== null && (
                            <tr><th scope="row">Wavelength</th><td>{exposure.wavelength.toFixed(3)} µm at center</td></tr>
                          )}
                          {!view.result.comparison && <tr><th scope="row">Valid area</th><td>{(100 * exposure.coverage).toFixed(1)}%</td></tr>}
                        </tbody>
                      </table>
                      {view.result.comparison && (
                        <table>
                          <caption>Match</caption>
                          <tbody>
                            <tr><th scope="row">Time apart</th><td>{view.result.comparison.days.toFixed(2)} days</td></tr>
                            <tr><th scope="row">Center overlap</th><td>{(100 * view.result.comparison.overlap).toFixed(1)}%</td></tr>
                            <tr><th scope="row">Shared area</th><td>{(100 * view.result.comparison.coverage).toFixed(1)}%</td></tr>
                          </tbody>
                        </table>
                      )}
                    </div>
                    <a className="control source-fits" href={exposure.sourceUrl} target="_blank" rel="noreferrer">Source FITS</a>
                  </section>
                </aside>
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
