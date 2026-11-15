import '@fontsource/ibm-plex-mono/latin-400.css';
import '@fontsource/ibm-plex-mono/latin-600.css';
import { useEffect, useEffectEvent, useMemo, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { formatPosition, parseCoordinate, parsePosition, positionInPatch, presets, type CoordinateFields, type SkyPosition } from './coordinates';
import { SkyPicker } from './SkyPicker';
import { ManualPicker } from './ManualPicker';
import { CompareImageControls, CompareMathImage, defaultVisitSelections, retainVisitSelections, type CompareMode } from './CompareImages';
import { MotionMarkers, MotionReview } from './MotionReview';
import { PixelFlags } from './PixelFlags';
import { Spectrum, spectrumDates } from './Spectrum';
import { archiveReleases, countVisits, exposureIdentity, loadSequence, stopPlayback, watchPlayback, type MotionCandidate, type MotionResult,
  type PlaybackUpdate, type SequenceSearch, type SequenceSummary } from './exposure';
import { clearPatchQuery, draftQuery, emptyDraft, fieldSizes, filterProblem, patchQuery, readPatchQuery, submittedPatch,
  type FilterField, type PatchDraft, type SubmittedPatch } from './patchQuery';
import { SharePatch, SharePatchDialog } from './SharePatch';
import './styles.css';

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
function restorePreset(): PatchDraft {
  try {
    const saved = window.sessionStorage.getItem(presetStorageKey);
    const selected = presets.find(({ name }) => name === saved);
    if (selected) return { ...emptyDraft, preset: selected.name, fields: selected.fields };
  } catch {
    // The picker is still usable when browser storage is blocked.
  }
  return emptyDraft;
}

function rememberPreset(preset: string) {
  try {
    if (preset) window.sessionStorage.setItem(presetStorageKey, preset);
    else window.sessionStorage.removeItem(presetStorageKey);
  } catch {
    // The current selection stays in memory when browser storage is blocked.
  }
}

function writePatchLocation(query: URLSearchParams, mode: 'draft' | 'applied', replace: boolean) {
  const url = new URL(window.location.href);
  clearPatchQuery(url);
  for (const [key, value] of query) url.searchParams.set(key, value);
  const state = { ...window.history.state, patchMode: mode };
  if (replace) window.history.replaceState(state, '', url);
  else window.history.pushState(state, '', url);
}

function recordDraft(draft: PatchDraft, replace = window.history.state?.patchMode === 'draft') {
  writePatchLocation(draftQuery(draft), 'draft', replace);
}

function recordPatch(patch: SubmittedPatch) {
  const current = readPatchQuery(new URLSearchParams(window.location.search));
  const query = patchQuery(patch);
  const samePatch = current.kind === 'query' && patchQuery(current.patch).toString() === query.toString();
  writePatchLocation(query, 'applied', samePatch || window.history.state?.patchMode === 'draft');
}

type WorkspaceState = { patch: SubmittedPatch; result: PlaybackUpdate; autoSelectedSources?: ReadonlySet<string> };
type ManualState = { patch: SubmittedPatch; seed?: PlaybackUpdate; autoSelectedSources?: ReadonlySet<string> };
type ManualResult = WorkspaceState & { result: PlaybackUpdate & { jobId: string } };
type SpectrumPick = { id: number; x: number; y: number; position: SkyPosition; dates: ReturnType<typeof spectrumDates> };
const comparisonTools = [
  { id: 'compare', label: 'Compare' },
  { id: 'motion', label: 'Motion' },
  { id: 'spectrum', label: 'Spectrum' },
  { id: 'pixel-flags', label: 'Pixel Flags' },
  { id: 'share', label: 'Share' },
] as const;
type ComparisonTool = typeof comparisonTools[number]['id'];

function summarizeSelection(result: PlaybackUpdate, id: string): SequenceSummary {
  return {
    id, count: result.frames.length, exposureIds: result.frames.map(exposureIdentity),
    visitCount: countVisits(result.frames) ?? (result.mode === 'exploratory' ? null : result.frames.length),
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
  const { count, visitCount = count, start, end, bandpass } = sequence;
  const visits = `${visitCount} ${visitCount === 1 ? 'visit' : 'visits'}`;
  const size = visitCount === null ? `${count} exposures` : visitCount === count ? visits : `${count} exposures across ${visits}`;
  return `${size} · ${start.slice(0, 10)}–${end.slice(0, 10)}${bandpass
    ? ` · ${bandpass[0].toFixed(3)}–${bandpass[1].toFixed(3)} µm` : ''}`;
}

const emptySearch: SequenceSearch = {
  status: 'searching', message: 'Searching the SPHEREx archive…', preview: null,
  frames: [], comparison: null, partial: false, sequences: [],
};

function App({ embed }: { embed: boolean }) {
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [draft, setDraft] = useState(emptyDraft);
  const { preset, fields, fieldSize, release, filters } = draft;
  const [errors, setErrors] = useState<Partial<CoordinateFields>>({});
  const [queryError, setQueryError] = useState('');
  const [filterError, setFilterError] = useState('');
  const [incompleteFilters, setIncompleteFilters] = useState<ReadonlySet<FilterField>>(() => new Set());
  const [view, setView] = useState<WorkspaceState | null>(null);
  const [search, setSearch] = useState<SequenceSearch | null>(null);
  const [manualResults, setManualResults] = useState<ManualResult[]>([]);
  const [pendingSequence, setPendingSequence] = useState<string>();
  const [sequenceError, setSequenceError] = useState('');
  const [failedSequence, setFailedSequence] = useState<{ id: string; automatic: boolean }>();
  const [stoppingSearch, setStoppingSearch] = useState(false);
  const [stopError, setStopError] = useState('');
  const [manual, setManual] = useState<ManualState | null>(null);
  const [frameIndex, setFrameIndex] = useState(0);
  const currentFrame = useRef<string | undefined>(undefined);
  const [playing, setPlaying] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [activeTool, setActiveTool] = useState<ComparisonTool>('compare');
  const [compareMode, setCompareMode] = useState<CompareMode>('blink');
  const [pixelFlagReadout, setPixelFlagReadout] = useState<HTMLDivElement | null>(null);
  const [visitSelections, setVisitSelections] = useState(defaultVisitSelections);
  const hasOpenedSequence = useRef(false);
  const [spectrumPick, setSpectrumPick] = useState<SpectrumPick | null>(null);
  const [markedMotion, setMarkedMotion] = useState<Record<string, MotionCandidate | null>>({});
  const [motionResults, setMotionResults] = useState<Record<string, MotionResult>>({});
  const requestRef = useRef<AbortController | null>(null);
  const selectionRef = useRef<AbortController | null>(null);
  const drawerToggle = useRef<HTMLButtonElement>(null);
  const submitAction = useRef<HTMLButtonElement>(null);
  const backAction = useRef<HTMLButtonElement>(null);
  const detailsAction = useRef<HTMLButtonElement>(null);
  const pixelFlagTarget = useRef<HTMLButtonElement>(null);
  const spectrumTarget = useRef<HTMLButtonElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const filtersRef = useRef<HTMLDetailsElement>(null);
  const draftPosition = useMemo(() => parsePosition(fields), [fields]);
  const draftPatch = draftPosition && submittedPatch(draft, draftPosition);
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
  const withdrawn = result?.jobId && search?.withdrawnSequenceIds?.includes(result.jobId);
  const activeAutoVisits = (search?.sequenceCandidates ?? search?.sequences)
    ?.find(({ id }) => id === result?.jobId)?.visits;
  const imageMathUnavailable = withdrawn
    ? 'New archive metadata changed the visit grouping. Open a current matched sequence to stack or subtract visits.'
    : !canPlay || result?.mode !== 'matched' || !result.jobId
      ? 'Stack and Difference require a matched sequence.' : '';
  const imageMathMode = activeTool === 'compare' && compareMode !== 'blink' ? compareMode : null;
  const imageMathKeys = imageMathMode && visitSelections[imageMathMode];
  const motionJobId = canPlay && result?.motionAvailable && !withdrawn ? result.jobId : undefined;
  const motionUnavailableReason = withdrawn
    ? 'New archive metadata changed the visit grouping. Choose a current sequence to check for motion.'
    : result?.motionUnavailableReason ?? (canPlay ? 'Motion analysis is unavailable for this sequence.' :
      'Open a playable sequence to check for motion.');
  const inspected = motionJobId ? motionResults[motionJobId] ?? result?.motionResult : undefined;
  const motionMarker = motionJobId && motionJobId in markedMotion
    ? markedMotion[motionJobId] : inspected?.candidates[0];

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

  function editDraft(next: PatchDraft) {
    if (view) resetComparison();
    setDraft(next);
    setQueryError('');
    recordDraft(next);
  }

  function editFilter(field: FilterField, input: HTMLInputElement) {
    const bad = input.validity.badInput;
    if (incompleteFilters.has(field) !== bad) {
      const next = new Set(incompleteFilters);
      if (bad) next.add(field);
      else next.delete(field);
      setIncompleteFilters(next);
    }
    setFilterError('');
    // Leave the last complete value in the draft and URL while the browser holds an incomplete entry.
    if (!bad && input.value !== filters[field]) editDraft({ ...draft, filters: { ...filters, [field]: input.value } });
  }

  function pickPosition(position: SkyPosition) {
    if (requestRef.current) return;
    rememberPreset('');
    editDraft({ ...draft, preset: '', fields: formatPosition(position) });
    setErrors({});
    setDrawerOpen(true);
  }

  function selectPreset(name: string) {
    const selected = presets.find((item) => item.name === name);
    rememberPreset(selected?.name ?? '');
    editDraft({ ...draft, preset: selected?.name ?? '', fields: selected?.fields ?? fields });
    if (selected) setErrors({});
  }

  function loadComparison(patch: SubmittedPatch) {
    requestRef.current?.abort();
    selectionRef.current?.abort();
    selectionRef.current = null;
    const controller = new AbortController();
    requestRef.current = controller;
    setDrawerOpen(true);
    setManual(null);
    setFrameIndex(0);
    setPlaying(false);
    setDetailsOpen(false);
    setActiveTool('compare');
    setCompareMode('blink');
    setVisitSelections(defaultVisitSelections());
    hasOpenedSequence.current = false;
    setSpectrumPick(null);
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

  function openSequence(next: WorkspaceState, resetVisits = true) {
    setFrameIndex(0);
    setPlaying(false);
    setDetailsOpen(false);
    if (resetVisits) setVisitSelections(defaultVisitSelections(next.result));
    if (next.result.mode === 'exploratory') setCompareMode('blink');
    if (next.result.status === 'ready') hasOpenedSequence.current = true;
    setView(next);
  }

  function selectSequence(patch: SubmittedPatch, id: string, automatic = false) {
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
      if (automatic && hasOpenedSequence.current) {
        const index = next.result.frames.findIndex((frame) => exposureIdentity(frame) === currentFrame.current);
        setFrameIndex(Math.max(0, index));
        if (index < 0) setPlaying(false);
        setVisitSelections((current) => retainVisitSelections(current, next.result.frames));
        setView(next);
      } else {
        openSequence(next);
      }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) {
        setFailedSequence({ id, automatic });
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
    if (failedSequence && !includes(failedSequence.id)) {
      setFailedSequence(undefined);
      setSequenceError('');
    }
    if (!sequences.length) {
      if (active) openSequence({ patch: view.patch, result: emptySearch }, false);
      return;
    }
    if (active && includes(active)) return;
    const identities = new Set(canPlay ? result?.frames.map(exposureIdentity) : []);
    const replacement = sequences.find((item) => currentFrame.current && item.exposureIds.includes(currentFrame.current))
      ?? sequences.find((item) => item.exposureIds.some((id) => identities.has(id))) ?? sequences[0];
    if (replacement.id !== failedSequence?.id) selectSequence(view.patch, replacement.id, true);
  });

  useEffect(() => {
    refreshActiveSequence();
  }, [sequences, view?.result.jobId, pendingSequence, failedSequence, manual]);

  function stopSearch() {
    const controller = requestRef.current;
    if (!controller) return;
    if (!search?.jobId) {
      controller.abort();
      requestRef.current = null;
      setSearch((current) => current && { ...current, status: 'cancelled', message: 'Search stopped.' });
      return;
    }
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
    const patch = submittedPatch(draft, draftPosition, chooseManually);
    const incomplete = Array.from(event.currentTarget.querySelectorAll<HTMLInputElement>('.patch-filters input'))
      .find((input) => (input.validity.badInput || incompleteFilters.has(input.id as FilterField)) &&
        (!chooseManually || !input.id.endsWith('GapDays')));
    const problem = incomplete ? { field: incomplete.id as FilterField, message: 'Complete this filter or clear it.' }
      : filterProblem(patch);
    setFilterError(problem?.message ?? '');
    if (problem) {
      if (filtersRef.current) filtersRef.current.open = true;
      formRef.current?.querySelector<HTMLInputElement>(`#${problem.field}`)?.focus();
      return;
    }
    if (chooseManually) {
      setManual({ patch });
    } else {
      setQueryError('');
      recordPatch(patch);
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
      setActiveTool('compare');
      setSpectrumPick(null);
      setStopError('');
      setMarkedMotion({});
      setMotionResults({});
      setManualResults([selection]);
    }
    const previous = manual.seed;
    const sameInputs = previous && previous.mode === result.mode && previous.frames.length === result.frames.length &&
      previous.frames.every((frame, index) => frame.key === result.frames[index].key);
    setQueryError('');
    recordPatch(selection.patch);
    openSequence(selection, !sameInputs);
    setManual(null);
    requestAnimationFrame(() => backAction.current?.focus({ preventScroll: true }));
  }

  function resetComparison() {
    requestRef.current?.abort();
    requestRef.current = null;
    selectionRef.current?.abort();
    selectionRef.current = null;
    setView(null);
    setCompareMode('blink');
    setVisitSelections(defaultVisitSelections());
    hasOpenedSequence.current = false;
    setSpectrumPick(null);
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
    setManual(null);
  }

  function backToSky() {
    resetComparison();
    recordDraft(draft, true);
    requestAnimationFrame(() => submitAction.current?.focus({ preventScroll: true }));
  }

  const syncLocation = useEffectEvent(() => {
    const location = readPatchQuery(new URLSearchParams(window.location.search), embed);
    const editing = !embed && window.history.state?.patchMode === 'draft';
    setDraft(location.kind === 'picker' ? restorePreset() : location.draft);
    setErrors({});
    setFilterError('');
    for (const field of incompleteFilters) {
      const input = formRef.current?.querySelector<HTMLInputElement>(`#${field}`);
      if (input?.validity.badInput) input.value = '';
    }
    if (incompleteFilters.size) setIncompleteFilters(new Set());
    setQueryError(location.kind === 'invalid' && !editing ? location.message : '');
    if (location.kind === 'query' && !editing) loadComparison(location.patch);
    else resetComparison();
  });

  useEffect(() => {
    function navigate() { syncLocation(); }
    navigate();
    window.addEventListener('popstate', navigate);
    return () => window.removeEventListener('popstate', navigate);
  }, []);

  function stepFrame(direction: number) {
    setPlaying(false);
    setFrameIndex((index) => (index + direction + frameCount) % frameCount);
  }

  function pickSpectrum(x: number, y: number) {
    if (activeTool !== 'spectrum' || !view || !exposure) return;
    setPlaying(false);
    const position = positionInPatch(view.patch.position, view.patch.fieldSize, x, y);
    const dates = spectrumDates(result?.frames.length ? result.frames : [exposure]);
    setSpectrumPick((current) => current?.x === x && current.y === y ? current :
      { id: (current?.id ?? 0) + 1, x, y, position, dates });
  }

  const drawerAction = drawerOpen ? 'Hide patch' : 'Choose patch';
  const backLabel = isLoading ? 'Cancel lookup' : 'Back to sky';
  const playbackLabel = playing ? 'Pause' : 'Play';
  const selectedSequenceId = pendingSequence ?? (canPlay ? result?.jobId : undefined);
  const playbackControls = canPlay && !imageMathMode && (
    <section className="exposure-toolbar" aria-label="Comparison playback">
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
      <input className="frame-slider" type="range" min={0} max={frameCount - 1} step={1}
        value={frameIndex} aria-label="Playback position" aria-valuetext={`Frame ${frameIndex + 1} of ${frameCount}`}
        onChange={(event) => {
          setPlaying(false);
          setFrameIndex(event.currentTarget.valueAsNumber);
        }} />
      <span className="frame-count">{frameIndex + 1} / {frameCount} {exploratory ? 'frames' : 'visits'}</span>
    </section>
  );
  const sequenceControls = view && (
    <>
      {sequences.length > (embed ? 1 : 0) && (
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
      {failedSequence && sequences.some((item) => item.id === failedSequence.id) && (
        <button type="button" className="control"
          onClick={() => selectSequence(view.patch, failedSequence.id, failedSequence.automatic)}>
          Retry loading sequence
        </button>
      )}
      {(embed || canPlay || sequences.length > 0 || (workspaceOpen && isSearching)) && search && (
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

  if (embed) {
    const fullViewer = view ? `/?${patchQuery(view.patch)}` : `/${window.location.search}`;
    return <main className="embed-viewer" aria-label="SPHEREx comparison">
      <header className="embed-header">
        <h1>SPHEREx comparison</h1>
        <a className="control" href={fullViewer} target="_blank" rel="noopener">Open full viewer</a>
      </header>
      {view && <p className="embed-coordinates">
        RA {view.patch.coordinates.ra}° · Dec {view.patch.coordinates.dec}° · {view.patch.fieldSize}′ × {view.patch.fieldSize}′
      </p>}
      <div className="embed-image">
        {exposure && <img src={exposure.imageUrl}
          alt={`North-up SPHEREx detector ${exposure.detector} exposure of the selected sky patch`} />}
      </div>
      <div className="embed-controls">
        {queryError && <p className="error" role="alert">{queryError}</p>}
        {result?.message && !canPlay && sequences.length === 0 && (
          <p className={result.status === 'error' ? 'error' : 'lookup-status'}
            role={result.status === 'error' ? 'alert' : 'status'}>{result.message}</p>
        )}
        {exposure && <div className="frame-summary">
          <time dateTime={exposure.observedAt}>{observationTime} UTC</time>
          {exposure.wavelength !== null && <span>{exposure.wavelength.toFixed(3)} µm</span>}
        </div>}
        {playbackControls}
        {sequenceControls}
        {view && (canRetry || (!canPlay && search?.status === 'cancelled')) && (
          <button type="button" className="control" onClick={() => loadComparison(view.patch)}>Retry</button>
        )}
      </div>
    </main>;
  }

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
            {queryError && <p className="error" role="alert">{queryError}</p>}
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
                    rememberPreset('');
                    editDraft({ ...draft, preset: '', fields: { ...fields, [key]: value } });
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
                value={fieldSizes.indexOf(fieldSize)} disabled={isLoading} aria-valuetext={`${fieldSize} by ${fieldSize} arcminutes`}
                onChange={(event) => {
                  const size = fieldSizes[event.currentTarget.valueAsNumber];
                  editDraft({ ...draft, fieldSize: size });
                }} />
            </div>

            <details className="patch-filters" ref={filtersRef}>
              <summary className="control details-toggle">
                {release || Object.values(filters).some(Boolean) ? 'Filters (set)' : 'Filters'}
              </summary>
              <fieldset disabled={isLoading}>
                <div className="field">
                  <label htmlFor="patch-release">Release</label>
                  <div className="preset-select">
                    <select id="patch-release" value={release ?? ''} onChange={(event) => {
                      const value = event.target.value;
                      editDraft({ ...draft, release: archiveReleases.find((name) => name === value) ?? null });
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
                          value={incompleteFilters.has(field) ? '' : filters[field]}
                          onInput={(event) => editFilter(field, event.currentTarget)} />
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
            {!workspaceOpen && drawerOpen && !queryError && !incompleteFilters.size && draftPatch &&
              !filterProblem(draftPatch) && <SharePatchDialog patch={draftPatch} />}
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
                    {imageMathMode ? (
                      !imageMathUnavailable && result?.jobId && imageMathKeys && imageMathKeys.length >= 2 && imageMathKeys.every(Boolean)
                        ? <CompareMathImage key={JSON.stringify([result.jobId, imageMathMode, imageMathKeys])}
                            jobId={result.jobId} operation={imageMathMode} keys={imageMathKeys} />
                        : <div className="sky-frame image-math-placeholder" role="status">
                            {imageMathUnavailable || `Choose ${imageMathMode === 'stack' ? 'at least two visits to stack' : 'two visits to subtract'}.`}
                          </div>
                    ) : (
                      <img className="sky-frame" src={exposure.imageUrl}
                        alt={`North-up SPHEREx detector ${exposure.detector} exposure of the selected sky patch`} />
                    )}
                    {activeTool === 'pixel-flags' && exposure.flagsUrl && pixelFlagReadout &&
                      <PixelFlags url={exposure.flagsUrl} targetRef={pixelFlagTarget} readoutHost={pixelFlagReadout} />}
                    {activeTool === 'motion' && motionMarker && <MotionMarkers candidate={motionMarker} frame={frameIndex} />}
                    {activeTool === 'spectrum' && <button className="spectrum-target" type="button" ref={spectrumTarget}
                      aria-label="Select a source for spectrum" aria-describedby="spectrum-pick-help"
                      onClick={(event) => {
                        if (event.detail === 0) {
                          pickSpectrum(spectrumPick?.x ?? .5, spectrumPick?.y ?? .5);
                          return;
                        }
                        const box = event.currentTarget.getBoundingClientRect();
                        pickSpectrum((event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height);
                      }} onKeyDown={(event) => {
                        const offsets: Record<string, [number, number]> = {
                          ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
                        };
                        const offset = offsets[event.key];
                        if (!offset) return;
                        event.preventDefault();
                        const pixels = Math.round(view.patch.fieldSize * 60 / 6.15);
                        const limit = (value: number) => Math.max(.5 / pixels, Math.min(1 - .5 / pixels, value));
                        pickSpectrum(limit((spectrumPick?.x ?? .5) + offset[0] / pixels),
                          limit((spectrumPick?.y ?? .5) + offset[1] / pixels));
                      }}>
                      {spectrumPick && <span className="spectrum-marker" aria-hidden="true"
                        style={{ left: `${spectrumPick.x * 100}%`, top: `${spectrumPick.y * 100}%` }} />}
                    </button>}
                  </div>
                </div>
                {playbackControls}
                <aside className="exposure-panel" aria-label="Comparison tools">
                  <div className="comparison-rail">
                    <button ref={backAction} type="button" className="control comparison-back"
                      aria-label={backLabel} title={backLabel} onClick={backToSky}>
                      {isLoading ? 'Cancel' : 'Back'}
                    </button>
                    <div className="comparison-tools" role="group" aria-label="Active tool">
                      {comparisonTools.map(({ id, label }) => (
                        <button key={id} type="button" className="control" aria-pressed={activeTool === id}
                          aria-controls={`comparison-tool-${id}`} onClick={() => {
                            if (id === 'compare' && compareMode !== 'blink') setPlaying(false);
                            setActiveTool(id);
                          }}>
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="comparison-tool-content">
                    <section id="comparison-tool-compare" className="comparison-tool" aria-label="Compare"
                      hidden={activeTool !== 'compare'} onKeyDown={(event) => {
                        if (event.key === 'Escape' && detailsOpen) {
                          setDetailsOpen(false);
                          detailsAction.current?.focus();
                        }
                      }}>
                      <CompareImageControls mode={compareMode} frames={result?.frames ?? []} visits={activeAutoVisits}
                        selections={visitSelections}
                        unavailable={imageMathUnavailable} onSelect={setVisitSelections} onMode={(mode) => {
                          if (mode !== 'blink') {
                            setPlaying(false);
                            setDetailsOpen(false);
                          }
                          setCompareMode(mode);
                        }} />
                      {!imageMathMode && <div className="frame-summary">
                        <time dateTime={exposure.observedAt}>{observationTime} UTC</time>
                        {exposure.wavelength !== null && (
                          <span title="Wavelength at patch center">{exposure.wavelength.toFixed(3)} µm</span>
                        )}
                        {view.autoSelectedSources?.has(exposure.sourceUrl) && <span className="auto-selected">Auto-selected</span>}
                      </div>}
                      {exploratory && <strong>Exploratory blink</strong>}
                      {result?.message && (canPlay || sequences.length === 0) && <p className="lookup-status" role="status">
                        {exploratory && 'Matched playback was unavailable. '}{result.message}
                      </p>}
                      {sequenceControls}
                      {canRetry && <button type="button" className="control" onClick={() => loadComparison(view.patch)}>Retry</button>}
                      {!isLoading && <button type="button" className="control" onClick={editComparison}>Edit exposures</button>}
                      {!imageMathMode && <button ref={detailsAction} className="control details-toggle" type="button"
                        aria-expanded={detailsOpen} aria-controls="exposure-details"
                        onClick={() => setDetailsOpen((open) => !open)}>
                        Details <span aria-hidden="true">▸</span>
                      </button>}
                      <section className="exposure-details" id="exposure-details" hidden={Boolean(imageMathMode) || !detailsOpen}
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
                              {exposure.visit != null && view.result.comparison?.coverageIncludesBandpass !== undefined && (
                                <tr><th scope="row">Visit</th><td>{exposure.visit}</td></tr>
                              )}
                              {(!view.result.comparison || view.result.comparison.coverageIncludesBandpass !== undefined) && (
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
                    </section>
                    <section id="comparison-tool-motion" className="comparison-tool" aria-label="Motion" hidden={activeTool !== 'motion'}>
                      {motionJobId && result ? <MotionReview key={motionJobId} jobId={motionJobId} frames={result.frames} exploratory={exploratory}
                        inspected={inspected} selectedCandidate={motionMarker}
                        onResult={(motionResult) => setMotionResults((items) => ({ ...items, [motionJobId]: motionResult }))}
                        onCandidate={(candidate) => setMarkedMotion((items) => ({ ...items, [motionJobId]: candidate }))} /> :
                        <p className="lookup-status" role="status">{motionUnavailableReason}</p>}
                    </section>
                    <section id="comparison-tool-spectrum" className="comparison-tool" aria-label="Spectrum controls"
                      hidden={activeTool !== 'spectrum'}>
                      <p id="spectrum-pick-help" className="lookup-status" hidden={Boolean(spectrumPick)}>
                        Select a source on the image. Use arrow keys to move the marker with the keyboard.
                      </p>
                      {spectrumPick && <Spectrum key={spectrumPick.id} position={spectrumPick.position}
                        initialDates={spectrumPick.dates} release={view.patch.release} visible={activeTool === 'spectrum'} onClear={() => {
                          setSpectrumPick(null);
                          spectrumTarget.current?.focus({ preventScroll: true });
                        }} />}
                    </section>
                    <section id="comparison-tool-pixel-flags" className="comparison-tool" aria-label="Pixel Flags"
                      hidden={activeTool !== 'pixel-flags'}>
                      {exposure.flagsUrl ? <>
                        <div className="pixel-flags-help" id="pixel-flags-help">
                          <p className="pixel-flags-legend"><span aria-hidden="true" /> Amber marks warnings</p>
                          <p className="lookup-status">Hover to inspect a pixel. Click or tap to pin it as you change visits.</p>
                        </div>
                        <div ref={setPixelFlagReadout} />
                        <button className="control pixel-flags-focus" type="button"
                          onClick={() => pixelFlagTarget.current?.focus({ preventScroll: true })}>Inspect with arrow keys</button>
                      </> : <p className="lookup-status" role="status">Pixel flags are unavailable for this exposure.</p>}
                    </section>
                    <section id="comparison-tool-share" className="comparison-tool" aria-label="Share" hidden={activeTool !== 'share'}>
                      <SharePatch key={patchQuery(view.patch).toString()} patch={view.patch} />
                    </section>
                  </div>
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

createRoot(document.getElementById('root')!).render(<App embed={/^\/embed\/?$/.test(window.location.pathname)} />);
