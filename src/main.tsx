import '@fontsource/ibm-plex-mono/latin-400.css';
import '@fontsource/ibm-plex-mono/latin-600.css';
import { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { formatPosition, parseCoordinate, parsePosition, presets, type CoordinateFields, type SkyPosition } from './coordinates';
import { SkyPicker } from './SkyPicker';
import { ManualPicker } from './ManualPicker';
import { MotionMarkers, MotionReview } from './MotionReview';
import { archiveReleases, exposureIdentity, loadSequence, stopPlayback, watchPlayback, type ArchiveRelease, type MotionCandidate, type MotionResult,
  type PlaybackPatch, type PlaybackUpdate, type SequenceSearch, type SequenceSummary } from './exposure';
import './styles.css';

const fieldSizes = [3, 6, 12, 24, 48] as const;
const emptyFields: CoordinateFields = { ra: '', dec: '' };
const presetStorageKey = 'sac26.target-preset';
const axes = [
  { key: 'ra', name: 'Right ascension (RA)', example: '133.795129° or 08h55m10.831s', range: '0° to less than 360°' },
  { key: 'dec', name: 'Declination (Dec)', example: '−7.245147° or −7d14m42.53s', range: '−90° to +90°' },
] as const;
const filterRanges = [
  { label: 'Observation dates (UTC)', fields: ['startDate', 'endDate'], bounds: ['From', 'Through'], type: 'date' },
  { label: 'Wavelength (µm)', fields: ['minWavelength', 'maxWavelength'], bounds: ['Minimum', 'Maximum'], type: 'number' },
  { label: 'Time between consecutive selected visits (days, Auto)', fields: ['minGapDays', 'maxGapDays'], bounds: ['Minimum', 'Maximum'], type: 'number' },
] as const;
const emptyFilters = { startDate: '', endDate: '', minWavelength: '', maxWavelength: '', minGapDays: '', maxGapDays: '' };
type FilterField = keyof typeof emptyFilters;

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

type SubmittedPatch = PlaybackPatch & {
  coordinates: CoordinateFields;
  fieldSize: typeof fieldSizes[number];
};

type WorkspaceState = { patch: SubmittedPatch; result: PlaybackUpdate; autoSelectedSources?: ReadonlySet<string> };
type ManualState = { patch: SubmittedPatch; seed?: PlaybackUpdate; autoSelectedSources?: ReadonlySet<string> };
type ManualResult = WorkspaceState & { result: PlaybackUpdate & { jobId: string } };

function summarizeSelection(result: PlaybackUpdate, id: string): SequenceSummary {
  return {
    id, count: result.frames.length, exposureIds: result.frames.map(exposureIdentity),
    start: result.frames[0].observedAt, end: result.frames[result.frames.length - 1].observedAt,
    bandpass: result.comparison?.bandpass ?? null,
  };
}

function availableSequences(search: SequenceSearch | null, manual: ManualResult[]) {
  if (!manual.length) return search?.sequences ?? [];
  const selected = manual.map(({ result }) => summarizeSelection(result, result.jobId));
  const used = new Set(selected.flatMap((sequence) => sequence.exposureIds));
  const auto = (search?.sequenceCandidates ?? search?.sequences ?? []).filter((sequence) => {
    if (sequence.exposureIds.some((id) => used.has(id))) return false;
    sequence.exposureIds.forEach((id) => used.add(id));
    return true;
  });
  return [...selected, ...auto];
}

function describeSequence(sequence: SequenceSummary) {
  const { count, start, end, bandpass } = sequence;
  return `${count} visits · ${start.slice(0, 10)}–${end.slice(0, 10)}${bandpass
    ? ` · ${bandpass[0].toFixed(3)}–${bandpass[1].toFixed(3)} µm` : ''}`;
}

const emptySearch: SequenceSearch = {
  status: 'searching', message: 'Searching the SPHEREx archive…', preview: null,
  frames: [], comparison: null, partial: false, sequences: [],
};

function App() {
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [{ preset, fields }, setDraft] = useState(restorePreset);
  const [errors, setErrors] = useState<Partial<CoordinateFields>>({});
  const [fieldSizeIndex, setFieldSizeIndex] = useState(1);
  const [release, setRelease] = useState<ArchiveRelease | null>(null);
  const [hasFilters, setHasFilters] = useState(false);
  const [filterError, setFilterError] = useState('');
  const [view, setView] = useState<WorkspaceState | null>(null);
  const [search, setSearch] = useState<SequenceSearch | null>(null);
  const [manualResults, setManualResults] = useState<ManualResult[]>([]);
  const [pendingSequence, setPendingSequence] = useState<string>();
  const [sequenceError, setSequenceError] = useState('');
  const [failedSequence, setFailedSequence] = useState<string>();
  const [stoppingSearch, setStoppingSearch] = useState(false);
  const [stopError, setStopError] = useState('');
  const [manual, setManual] = useState<ManualState | null>(null);
  const [frameIndex, setFrameIndex] = useState(0);
  const currentFrame = useRef<string | undefined>(undefined);
  const [playing, setPlaying] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [markedMotion, setMarkedMotion] = useState<Record<string, MotionCandidate | null>>({});
  const [motionResults, setMotionResults] = useState<Record<string, MotionResult>>({});
  const requestRef = useRef<AbortController | null>(null);
  const selectionRef = useRef<AbortController | null>(null);
  const drawerToggle = useRef<HTMLButtonElement>(null);
  const submitAction = useRef<HTMLButtonElement>(null);
  const backAction = useRef<HTMLButtonElement>(null);
  const detailsAction = useRef<HTMLButtonElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const filtersRef = useRef<HTMLDetailsElement>(null);
  const draftPosition = useMemo(() => parsePosition(fields), [fields]);
  const fieldSize = fieldSizes[fieldSizeIndex];
  const sequences = useMemo(() => availableSequences(search, manualResults), [search, manualResults]);

  const result = view?.result.status === 'ready' ? view.result : search;
  const isLoading = result?.status === 'searching';
  const isSearching = search?.status === 'searching';
  const canPlay = view?.result.status === 'ready';
  const frameCount = result?.frames.length ?? 0;
  const exposure = result?.frames[frameIndex] ?? result?.preview;
  useEffect(() => {
    currentFrame.current = exposure ? exposureIdentity(exposure) : undefined;
  }, [exposure]);
  const observationTime = exposure?.observedAt.replace('T', ' ').slice(0, 19);
  const workspaceOpen = Boolean(exposure);
  const canRetry = result?.status === 'error' || (result?.status === 'no-match' && result.partial);
  const exploratory = result?.mode === 'exploratory';
  const motionJobId = canPlay && result?.motionAvailable ? result.jobId : undefined;
  const inspected = motionJobId ? motionResults[motionJobId] ?? result?.motionResult : undefined;
  const motionMarker = motionJobId && motionJobId in markedMotion
    ? markedMotion[motionJobId] : inspected?.candidates[0];

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
    const timer = window.setInterval(() => setFrameIndex((index) => (index + 1) % frameCount), 1000);
    return () => window.clearInterval(timer);
  }, [canPlay, frameCount, playing]);

  useEffect(() => () => {
    requestRef.current?.abort();
    selectionRef.current?.abort();
  }, []);

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
    selectionRef.current?.abort();
    selectionRef.current = null;
    const controller = new AbortController();
    requestRef.current = controller;
    setDrawerOpen(true);
    setFrameIndex(0);
    setPlaying(false);
    setDetailsOpen(false);
    setView({ patch, result: emptySearch });
    setSearch(emptySearch);
    setPendingSequence(undefined);
    setSequenceError('');
    setFailedSequence(undefined);
    setStoppingSearch(false);
    setStopError('');
    setManualResults([]);
    setMarkedMotion({});
    setMotionResults({});
    const update = (result: SequenceSearch) => {
      if (controller.signal.aborted) return;
      if (result.status !== 'searching') {
        requestRef.current = null;
        setStoppingSearch(false);
        setStopError('');
      }
      setSearch(result);
    };
    void watchPlayback(patch, controller.signal, update).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      requestRef.current = null;
      setStoppingSearch(false);
      const message = error instanceof Error && !(error instanceof TypeError)
        ? error.message : 'The playback service is unavailable. Try again.';
      setSearch((current) => current && { ...current, status: 'error', message, reconnecting: false });
    });
  }

  function openSequence(next: WorkspaceState) {
    setFrameIndex(0);
    setPlaying(false);
    setDetailsOpen(false);
    setView(next);
  }

  function selectSequence(patch: SubmittedPatch, id: string, preservePosition = false) {
    selectionRef.current?.abort();
    const controller = new AbortController();
    selectionRef.current = controller;
    setPendingSequence(id);
    setSequenceError('');
    setFailedSequence(undefined);
    const existing = manualResults.find((item) => item.result.jobId === id);
    void (existing ? Promise.resolve(existing) : loadSequence(id, controller.signal).then((result) => ({
      patch, result, autoSelectedSources: new Set(result.frames.map((frame) => frame.sourceUrl)),
    }))).then((next) => {
      if (controller.signal.aborted) return;
      if (preservePosition) {
        const index = next.result.frames.findIndex((frame) => exposureIdentity(frame) === currentFrame.current);
        setFrameIndex(Math.max(0, index));
        if (index < 0) setPlaying(false);
        setView(next);
      } else {
        openSequence(next);
      }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) {
        setFailedSequence(id);
        setSequenceError(failure instanceof Error && !(failure instanceof TypeError)
          ? failure.message : 'Could not load the sequence. Try again.');
      }
    }).finally(() => {
      if (selectionRef.current !== controller) return;
      selectionRef.current = null;
      setPendingSequence(undefined);
    });
  }

  const refreshActiveSequence = useEffectEvent(() => {
    if (!view || manual) return;
    const includes = (id: string) => sequences.some((entry) => entry.id === id);
    if (pendingSequence) {
      if (includes(pendingSequence)) return;
      selectionRef.current?.abort();
      selectionRef.current = null;
      setPendingSequence(undefined);
    }
    const active = canPlay ? result?.jobId : undefined;
    if (failedSequence && !includes(failedSequence)) {
      setFailedSequence(undefined);
      setSequenceError('');
    }
    if (!sequences.length) {
      if (active) openSequence({ patch: view.patch, result: emptySearch });
      return;
    }
    if (active && includes(active)) return;
    const identities = new Set(canPlay ? result?.frames.map(exposureIdentity) : []);
    const replacement = sequences.find((item) => currentFrame.current && item.exposureIds.includes(currentFrame.current))
      ?? sequences.find((item) => item.exposureIds.some((id) => identities.has(id))) ?? sequences[0];
    if (replacement.id !== failedSequence) selectSequence(view.patch, replacement.id, Boolean(active));
  });

  useEffect(() => {
    refreshActiveSequence();
  }, [sequences, view?.result.jobId, pendingSequence, failedSequence, manual]);

  function stopSearch() {
    const controller = requestRef.current;
    if (!controller || !search?.jobId) return;
    setStoppingSearch(true);
    setStopError('');
    void stopPlayback(search.jobId).catch(() => {
      if (requestRef.current !== controller) return;
      setStoppingSearch(false);
      setStopError('Could not stop the search. Try again.');
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
    const submitter = (event.nativeEvent as SubmitEvent).submitter;
    const chooseManually = submitter instanceof HTMLButtonElement && submitter.value === 'manual';
    const formData = new FormData(event.currentTarget);
    const filters = { ...emptyFilters };
    for (const { fields } of filterRanges) {
      for (const field of fields) filters[field] = String(formData.get(field) ?? '');
    }
    const minWavelength = filters.minWavelength ? Number(filters.minWavelength) : null;
    const maxWavelength = filters.maxWavelength ? Number(filters.maxWavelength) : null;
    const minGapDays = filters.minGapDays ? Number(filters.minGapDays) : 0;
    const maxGapDays = filters.maxGapDays ? Number(filters.maxGapDays) : null;
    let invalidFilter: FilterField | undefined;
    let message = '';
    const badNumber = Array.from(event.currentTarget.querySelectorAll<HTMLInputElement>('.patch-filters input[type="number"]'))
      .find((input) => input.validity.badInput && (!chooseManually || !input.id.endsWith('GapDays')));
    if (badNumber) {
      invalidFilter = badNumber.id as FilterField;
      message = 'Enter a valid number or leave the field blank.';
    } else if (filters.startDate && filters.endDate && filters.startDate > filters.endDate) {
      invalidFilter = 'endDate';
      message = 'Enter an end date on or after the start date.';
    } else if (minWavelength !== null && minWavelength <= 0) {
      invalidFilter = 'minWavelength';
      message = 'Enter a wavelength greater than zero.';
    } else if (maxWavelength !== null && (maxWavelength <= 0 || maxWavelength < (minWavelength ?? 0))) {
      invalidFilter = 'maxWavelength';
      message = 'Enter a positive maximum wavelength at least as large as the minimum.';
    } else if (!chooseManually && minGapDays < 0) {
      invalidFilter = 'minGapDays';
      message = 'Enter a minimum time gap of zero days or more.';
    } else if (!chooseManually && maxGapDays !== null && maxGapDays < minGapDays) {
      invalidFilter = 'maxGapDays';
      message = 'Enter a maximum time gap at least as large as the minimum.';
    }
    setFilterError(message);
    if (invalidFilter) {
      if (filtersRef.current) filtersRef.current.open = true;
      formRef.current?.querySelector<HTMLInputElement>(`#${invalidFilter}`)?.focus();
      return;
    }
    const patch: SubmittedPatch = {
      position: draftPosition,
      coordinates: { ra: fields.ra.trim(), dec: fields.dec.trim() },
      fieldSize,
      release,
      startDate: filters.startDate || null,
      endDate: filters.endDate || null,
      minWavelength,
      maxWavelength,
      minGapDays: chooseManually ? 0 : minGapDays,
      maxGapDays: chooseManually ? null : maxGapDays,
    };
    if (chooseManually) {
      setManual({ patch });
    } else {
      loadComparison(patch);
    }
  }

  function editComparison() {
    if (!view || !result) return;
    selectionRef.current?.abort();
    selectionRef.current = null;
    setPendingSequence(undefined);
    setPlaying(false);
    setManual({ patch: view.patch, seed: result, autoSelectedSources: view.autoSelectedSources });
  }

  function applySelection(result: PlaybackUpdate) {
    if (!manual || !result.jobId) return;
    selectionRef.current?.abort();
    selectionRef.current = null;
    setPendingSequence(undefined);
    setSequenceError('');
    setFailedSequence(undefined);
    const existing = manual.seed && manualResults.find((item) => item.result.mode === result.mode &&
      item.result.frames.length === result.frames.length &&
      item.result.frames.every((frame, index) => frame.key === result.frames[index].key));
    const selection = existing ?? {
      patch: manual.patch, result: { ...result, jobId: result.jobId }, autoSelectedSources: manual.autoSelectedSources,
    };
    const identities = new Set(result.frames.map(exposureIdentity));
    if (manual.seed) {
      setManualResults((items) => [selection, ...items.filter(({ result }) =>
        result.frames.every((frame) => !identities.has(exposureIdentity(frame))))]);
    } else {
      setSearch(null);
      setStopError('');
      setMarkedMotion({});
      setMotionResults({});
      setManualResults([selection]);
    }
    openSequence(selection);
    setManual(null);
    requestAnimationFrame(() => backAction.current?.focus({ preventScroll: true }));
  }

  function backToSky() {
    requestRef.current?.abort();
    requestRef.current = null;
    selectionRef.current?.abort();
    selectionRef.current = null;
    setView(null);
    setSearch(null);
    setManualResults([]);
    setPendingSequence(undefined);
    setSequenceError('');
    setFailedSequence(undefined);
    setStoppingSearch(false);
    setStopError('');
    setMarkedMotion({});
    setMotionResults({});
    setPlaying(false);
    setDrawerOpen(true);
    requestAnimationFrame(() => submitAction.current?.focus({ preventScroll: true }));
  }

  function stepFrame(direction: number) {
    setPlaying(false);
    setFrameIndex((index) => (index + direction + frameCount) % frameCount);
  }

  const drawerAction = drawerOpen ? 'Hide patch' : 'Choose patch';
  const backLabel = isLoading ? 'Cancel lookup' : 'Back to sky';
  const playbackLabel = playing ? 'Pause' : 'Play';
  const selectedSequenceId = pendingSequence ?? (canPlay ? result?.jobId : undefined);
  const sequenceControls = view && (
    <>
      {sequences.length > 0 && (
        <div className="sequence-choice">
          <label htmlFor="sequence-choice">Sequence</label>
          <select id="sequence-choice" value={sequences.some((item) => item.id === selectedSequenceId) ? selectedSequenceId : ''} onChange={(event) => {
            if (event.currentTarget.value) selectSequence(view.patch, event.currentTarget.value);
          }}>
            <option value="" disabled>Choose a sequence</option>
            {sequences.map((item, index) => <option key={item.id} value={item.id}>
              {index + 1}. {describeSequence(item)}{manualResults.some(({ result }) => result.jobId === item.id) ? ' · Manual' : ''}
            </option>)}
          </select>
        </div>
      )}
      {pendingSequence && <p className="lookup-status" role="status">Loading sequence…</p>}
      {sequenceError && <p className="lookup-status" role="alert">{sequenceError}</p>}
      {failedSequence && sequences.some((item) => item.id === failedSequence) && (
        <button type="button" className="control" onClick={() => selectSequence(view.patch, failedSequence,
          Boolean(canPlay && !sequences.some((item) => item.id === result?.jobId)))}>
          Retry loading sequence
        </button>
      )}
      {(canPlay || sequences.length > 0 || (workspaceOpen && isSearching)) && search && (
        <>
          {(canPlay || sequences.length > 0) && (search.reconnecting || isSearching || search.message) && <p className="lookup-status" role="status">
            {search.reconnecting ? 'Reconnecting to the archive search…'
              : isSearching ? 'Searching for more sequences…' : search.message}
          </p>}
          {isSearching && <button type="button" className="control" disabled={stoppingSearch} onClick={stopSearch}>
            {stoppingSearch ? 'Stopping search…' : 'Stop search'}
          </button>}
          {stopError && <p className="lookup-status" role="alert">{stopError}</p>}
        </>
      )}
    </>
  );

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

            <details className="patch-filters" ref={filtersRef}>
              <summary className="control details-toggle">
                {release || hasFilters ? 'Filters (set)' : 'Filters'}
              </summary>
              <fieldset disabled={isLoading}>
                <div className="field">
                  <label htmlFor="patch-release">Release</label>
                  <div className="preset-select">
                    <select id="patch-release" value={release ?? ''} onChange={(event) => {
                      const value = event.target.value;
                      setRelease(archiveReleases.find((name) => name === value) ?? null);
                    }}>
                      <option value="">All releases</option>
                      {archiveReleases.map((name) => <option key={name} value={name}>{name.toUpperCase()}</option>)}
                    </select>
                  </div>
                </div>
                {filterRanges.map(({ label, fields, bounds, type }) => (
                  <fieldset className="patch-filter-range" key={label}>
                    <legend>{label}</legend>
                    {fields.map((field, index) => (
                      <div className="field" key={field}>
                        <label htmlFor={field}>{bounds[index]}</label>
                        <input id={field} name={field} type={type} min={type === 'number' ? 0 : undefined}
                          step={type === 'number' ? 'any' : undefined}
                          onInput={(event) => {
                            const inputs = event.currentTarget.form?.querySelectorAll<HTMLInputElement>('.patch-filters input');
                            setHasFilters(Array.from(inputs ?? []).some((input) => input.value !== ''));
                            setFilterError('');
                          }} />
                      </div>
                    ))}
                  </fieldset>
                ))}
                {filterError && <p className="error" role="alert">{filterError}</p>}
              </fieldset>
            </details>

            <div className="form-actions">
              {result?.message && !workspaceOpen && sequences.length === 0 && (
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
              {!isLoading && <button className="control" type="submit" value="manual">Choose exposures manually</button>}
              {view && canRetry && !workspaceOpen && (
                <button type="button" className="control" onClick={() => loadComparison(view.patch)}>Retry</button>
              )}
            </div>
            {!workspaceOpen && sequenceControls}
          </form>
        </aside>

        <section className="viewer" aria-label="Sky viewport">
          <SkyPicker active={!workspaceOpen} presetPosition={preset ? draftPosition : null}
            draftPosition={draftPosition} fieldSize={fieldSize} onPick={pickPosition} />

          {view && exposure && (
            <div className="image-stage">
              <div className="exposure-layout">
                <div className="exposure-frame">
                  <div className="exposure-image">
                    <img className="sky-frame" src={exposure.imageUrl}
                      alt={`North-up SPHEREx detector ${exposure.detector} exposure of the selected sky patch`} />
                    {motionMarker && <MotionMarkers candidate={motionMarker} frame={frameIndex} />}
                  </div>
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
                        {view.autoSelectedSources?.has(exposure.sourceUrl) && <span className="auto-selected">Auto-selected</span>}
                      </div>
                    </div>
                    {exploratory && <strong>Exploratory blink</strong>}
                    {result?.message && (canPlay || sequences.length === 0) && <p className="lookup-status" role="status">
                      {exploratory && 'Matched playback was unavailable. '}{result.message}
                    </p>}
                    {sequenceControls}
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
                            <button className="control symbol-control" type="button" aria-label="Previous" title="Previous" onClick={() => stepFrame(-1)}>
                              <span aria-hidden="true">⏮︎</span>
                            </button>
                            <button className="control symbol-control" type="button" aria-label={playbackLabel} title={playbackLabel}
                              onClick={() => setPlaying((active) => !active)}>
                              <span aria-hidden="true">{playing ? '⏸︎' : '▶︎'}</span>
                            </button>
                            <button className="control symbol-control" type="button" aria-label="Next" title="Next" onClick={() => stepFrame(1)}>
                              <span aria-hidden="true">⏭︎</span>
                            </button>
                          </div>
                          <span className="frame-count">{frameIndex + 1} / {frameCount} {exploratory ? 'frames' : 'visits'}</span>
                        </div>
                      </>
                    )}
                    {canRetry && (
                      <div className="exposure-control-row">
                        <button type="button" className="control" onClick={() => loadComparison(view.patch)}>Retry</button>
                      </div>
                    )}
                    {motionJobId && <MotionReview key={motionJobId} jobId={motionJobId} exploratory={exploratory}
                      inspected={inspected} selectedCandidate={motionMarker}
                      onResult={(motionResult) => setMotionResults((items) => ({ ...items, [motionJobId]: motionResult }))}
                      onCandidate={(candidate) => setMarkedMotion((items) => ({ ...items, [motionJobId]: candidate }))} />}
                    {canPlay && !motionJobId && result?.motionUnavailableReason && (
                      <p className="lookup-status">{result.motionUnavailableReason}</p>
                    )}
                    {!isLoading && <button type="button" className="control" onClick={editComparison}>Edit exposures</button>}
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
                          {view.patch.release && <tr><th scope="row">Release filter</th><td>{view.patch.release.toUpperCase()}</td></tr>}
                          {view.patch.startDate && <tr><th scope="row">From (UTC)</th><td>{view.patch.startDate}</td></tr>}
                          {view.patch.endDate && <tr><th scope="row">Through (UTC)</th><td>{view.patch.endDate}</td></tr>}
                          {view.patch.minWavelength !== null && <tr><th scope="row">Min. wavelength</th><td>{view.patch.minWavelength} µm</td></tr>}
                          {view.patch.maxWavelength !== null && <tr><th scope="row">Max. wavelength</th><td>{view.patch.maxWavelength} µm</td></tr>}
                          {view.patch.minGapDays > 0 && <tr><th scope="row">Min. Auto gap</th><td>{view.patch.minGapDays} days</td></tr>}
                          {view.patch.maxGapDays !== null && <tr><th scope="row">Max. Auto gap</th><td>{view.patch.maxGapDays} days</td></tr>}
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
                          {exposure.bandwidth != null && (
                            <tr><th scope="row">Bandwidth</th><td>{exposure.bandwidth.toFixed(4)} µm at center</td></tr>
                          )}
                          {exposure.visit != null && <tr><th scope="row">Visit</th><td>{exposure.visit}</td></tr>}
                          {(!view.result.comparison || exposure.visit != null) && (
                            <tr><th scope="row">Valid area</th><td>{(100 * exposure.coverage).toFixed(1)}%</td></tr>
                          )}
                        </tbody>
                      </table>
                      {view.result.comparison && (
                        <table>
                          <caption>{exploratory ? 'Comparison' : 'Match'}</caption>
                          <tbody>
                            <tr><th scope="row">Time span</th><td>{view.result.comparison.days < 1
                              ? `${(view.result.comparison.days * 1440).toFixed(1)} minutes`
                              : `${view.result.comparison.days.toFixed(2)} days`}</td></tr>
                            {view.result.comparison.overlap !== null && (
                              <tr><th scope="row">Center overlap</th><td>{(100 * view.result.comparison.overlap).toFixed(1)}%</td></tr>
                            )}
                            <tr><th scope="row">{view.result.comparison.coverageIncludesBandpass === false
                              ? 'Shared valid area' : 'Shared matched area'}</th><td>{(100 * view.result.comparison.coverage).toFixed(1)}%</td></tr>
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
        {manual && <ManualPicker patch={manual.patch} seed={manual.seed}
          autoSelectedSources={manual.autoSelectedSources} onClose={() => setManual(null)} onApply={applySelection} />}
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
