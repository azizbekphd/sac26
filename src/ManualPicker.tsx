import { useEffect, useRef, useState, type FormEvent } from 'react';
import {
  exposureIdentity, loadBandpasses, loadExposurePreview, maxManualFrames, watchInventory, watchSelection,
  type ArchiveExposure, type ArchivePatch, type Bandpass, type ExposureIssue, type InventoryUpdate, type PlaybackUpdate,
} from './exposure';

const pageSize = 12;
const networkError = 'The playback service is unavailable. Try again.';
const exposureIssueLabels: Record<ExposureIssue, string> = {
  'no-usable-pixels': 'No usable pixels in this patch',
  'read-failed': 'Could not load this exposure. Try again.',
};

type Props = {
  patch: ArchivePatch;
  seed: PlaybackUpdate | undefined;
  autoSelectedSources?: ReadonlySet<string>;
  onClose: () => void;
  onApply: (result: PlaybackUpdate) => void;
};

type PreviewSource = {
  playback?: { url: string; matched: boolean };
  archive?: { inventoryId: string; key: string };
};

function observationTime(exposure: ArchiveExposure) {
  return exposure.observedAt.slice(0, 23).replace('T', ' ') + ' UTC';
}

function bandLabel(exposure: ArchiveExposure, band: Bandpass | null | undefined) {
  const wavelength = band?.wavelength ?? exposure.wavelength;
  const bandwidth = band?.bandwidth ?? exposure.bandwidth;
  if (wavelength === null) return band === null ? 'Center wavelength unavailable' : 'Center wavelength pending';
  const width = bandwidth == null ? '' : ` · width ${bandwidth.toFixed(4)} µm`;
  return `${wavelength.toFixed(4)} µm${width}`;
}

function errorMessage(error: unknown) {
  return error instanceof Error && !(error instanceof TypeError) ? error.message : networkError;
}

function ExposurePreview({ exposure, source, onClose }: {
  exposure: ArchiveExposure;
  source: PreviewSource;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [playbackFailed, setPlaybackFailed] = useState(false);
  const [archiveUrl, setArchiveUrl] = useState<string>();
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const playback = playbackFailed ? undefined : source.playback;
  const imageUrl = playback?.url ?? archiveUrl;
  const inventoryId = source.archive?.inventoryId;
  const key = source.archive?.key;
  const previewError = error || (playbackFailed && !inventoryId ? 'Could not display this exposure preview.' : '');

  useEffect(() => { dialog.current?.showModal(); }, []);

  useEffect(() => {
    if (playback || !inventoryId || !key) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    void loadExposurePreview(inventoryId, key, controller.signal).then((png) => {
      if (controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(png);
      setArchiveUrl(objectUrl);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setError(errorMessage(error));
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [playback?.url, inventoryId, key, attempt]);

  function close() {
    dialog.current?.close();
    onClose();
  }

  return (
    <dialog ref={dialog} className="manual-picker manual-preview" aria-labelledby="manual-preview-title"
      onCancel={(event) => { event.preventDefault(); event.stopPropagation(); close(); }}>
      <header className="manual-heading">
        <div>
          <h2 id="manual-preview-title">Exposure preview</h2>
          <p>{observationTime(exposure)} · {exposure.release} · D{exposure.detector}</p>
        </div>
        <button type="button" className="control" onClick={close}>Close preview</button>
      </header>
      {previewError ? (
        <div className="manual-actions">
          <p role="alert">{previewError}</p>
          {inventoryId && <button className="control" type="button" onClick={() => {
            setArchiveUrl(undefined); setError(''); setAttempt((current) => current + 1);
          }}>Retry preview</button>}
        </div>
      ) : imageUrl ? (
        <img className="sky-frame" src={imageUrl} alt={`North-up SPHEREx detector ${exposure.detector} exposure`}
          onError={() => {
            if (playback) setPlaybackFailed(true);
            else setError('Could not display this exposure preview.');
          }} />
      ) : <p role="status">Loading preview…</p>}
      {imageUrl && !previewError && playback?.matched && <p>Shows the shared mask from matched playback.</p>}
    </dialog>
  );
}

export function ManualPicker({ patch, seed, autoSelectedSources, onClose, onApply }: Props) {
  const { position, fieldSize, release: patchRelease } = patch;
  const dialog = useRef<HTMLDialogElement>(null);
  const request = useRef<AbortController | null>(null);
  const revealedExposure = useRef<HTMLInputElement>(null);
  const [listing, setListing] = useState<InventoryUpdate | null>(null);
  const [listingError, setListingError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<ArchiveExposure[]>(seed?.frames ?? []);
  const [bands, setBands] = useState<Record<string, Bandpass | null>>({});
  const [release, setRelease] = useState('');
  const [detector, setDetector] = useState('');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [wavelength, setWavelength] = useState('');
  const [page, setPage] = useState(0);
  const [listJump, setListJump] = useState<{ key: string } | null>(null);
  const [status, setStatus] = useState('');
  const [failure, setFailure] = useState('');
  const [exposureIssues, setExposureIssues] = useState<Record<string, ExposureIssue>>({});
  const [exploratory, setExploratory] = useState<PlaybackUpdate | null>(null);
  const [preview, setPreview] = useState<ArchiveExposure | null>(null);

  const exposures = listing?.exposures ?? [];
  const inventoryId = listing?.status === 'ready' ? listing.jobId : undefined;
  const wave = Number(wavelength);
  const visible = exposures.filter((item) => {
    const date = item.observedAt.slice(0, 10);
    const band = bands[item.key];
    const bounds = band ? [band.wavelength - band.bandwidth / 2, band.wavelength + band.bandwidth / 2]
      : item.wavelengthBounds;
    return (!release || item.release === release) && (!detector || String(item.detector) === detector)
      && (!startDate || date >= startDate) && (!endDate || date <= endDate)
      && (!wavelength || (!bounds && band !== null) || Boolean(bounds && wave >= bounds[0] && wave <= bounds[1]));
  });
  const pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const pageExposures = visible.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
  const calibrationKeys = JSON.stringify(pageExposures.map(({ key }) => key));
  const selectedSources = new Set(selected.map(({ sourceUrl }) => sourceUrl));
  const inventoryKeys = new Map(exposures.map(({ sourceUrl, key }) => [sourceUrl, key]));
  const unavailable = inventoryId && selected.some(({ sourceUrl }) => !inventoryKeys.has(sourceUrl));
  function previewSource(item: ArchiveExposure): PreviewSource | undefined {
    const exploratoryFrame = exploratory?.frames.find((frame) => frame.sourceUrl === item.sourceUrl);
    const frame = seed?.frames.find((frame) => frame.sourceUrl === item.sourceUrl);
    const playback = exploratoryFrame ? { url: exploratoryFrame.imageUrl, matched: false }
      : frame ? { url: frame.imageUrl, matched: seed?.mode !== 'exploratory' }
      : seed?.preview?.sourceUrl === item.sourceUrl ? { url: seed.preview.imageUrl, matched: false }
      : undefined;
    const key = inventoryKeys.get(item.sourceUrl);
    const archive = inventoryId && key ? { inventoryId, key } : undefined;
    if (playback || archive) return { playback, archive };
  }

  const activePreview = preview && previewSource(preview);

  function previewButton(item: ArchiveExposure) {
    return <button className="control" type="button" onClick={() => setPreview(item)}
      disabled={Boolean(status) || !previewSource(item)}
      aria-label={`Preview ${item.release} detector ${item.detector} exposure at ${observationTime(item)}`}>Preview</button>;
  }

  useEffect(() => {
    dialog.current?.showModal();
    return () => request.current?.abort();
  }, []);

  useEffect(() => {
    if (!listJump) return;
    const checkbox = revealedExposure.current;
    checkbox?.focus({ preventScroll: true });
    checkbox?.scrollIntoView({ block: 'center', inline: 'nearest' });
  }, [listJump]);

  useEffect(() => {
    const controller = new AbortController();
    void watchInventory(patch, seed?.jobId, controller.signal, (update) => {
      setListing(update);
      if (update.status === 'error') setListingError(update.message);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setListingError(errorMessage(error));
    });
    return () => controller.abort();
  }, [patch, seed?.jobId, attempt]);

  useEffect(() => {
    if (!inventoryId) return;
    const keys = (JSON.parse(calibrationKeys) as string[]).filter((key) => !(key in bands));
    if (!keys.length) return;
    const controller = new AbortController();
    void loadBandpasses(inventoryId, keys, controller.signal).then((values) => {
      if (controller.signal.aborted) return;
      setBands((current) => ({ ...current, ...Object.fromEntries(keys.map((key) => [key, values[key] ?? null])) }));
    }).catch(() => {
      if (!controller.signal.aborted) {
        setBands((current) => ({ ...current, ...Object.fromEntries(keys.map((key) => [key, null])) }));
      }
    });
    return () => controller.abort();
  }, [inventoryId, calibrationKeys, bands]);

  function toggle(exposure: ArchiveExposure) {
    setSelected((current) => current.some(({ sourceUrl }) => sourceUrl === exposure.sourceUrl)
      ? current.filter(({ sourceUrl }) => sourceUrl !== exposure.sourceUrl)
      : [...current.filter((item) => exposureIdentity(item) !== exposureIdentity(exposure)), exposure]);
    setExploratory(null);
    setFailure('');
  }

  function changePage(nextPage: number) {
    setListJump(null);
    setPage(nextPage);
  }

  function showInList(key: string) {
    const index = exposures.findIndex((item) => item.key === key);
    if (index < 0) return;
    setRelease('');
    setDetector('');
    setStartDate('');
    setEndDate('');
    setWavelength('');
    setPage(Math.floor(index / pageSize));
    setListJump({ key });
  }

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const keys = selected.map(({ sourceUrl }) => inventoryKeys.get(sourceUrl)).filter((key) => key !== undefined);
    if (!inventoryId || selected.length < 2 || request.current || keys.length !== selected.length) return;
    const controller = new AbortController();
    request.current = controller;
    setFailure('');
    setExploratory(null);
    setStatus('Preparing selected exposures…');
    void watchSelection(inventoryId, keys, controller.signal, (update) => {
      if (update.status === 'searching') return;
      setExposureIssues((current) => {
        const issues = { ...current };
        for (const key of keys) delete issues[key];
        return { ...issues, ...update.exposureIssues };
      });
      if (update.status !== 'ready') setFailure(update.message);
      else if (update.mode === 'exploratory') setExploratory(update);
      else onApply(update);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setFailure(errorMessage(error));
    }).finally(() => {
      request.current = null;
      if (!controller.signal.aborted) setStatus('');
    });
  }

  return (
    <dialog ref={dialog} className="manual-picker" aria-labelledby="manual-title" onCancel={(event) => {
      event.preventDefault();
      onClose();
    }}>
      <form onSubmit={apply}>
        <header className="manual-heading">
          <div>
            <h2 id="manual-title">Select exposures</h2>
            <p>{position.ra.toFixed(5)}°, {position.dec.toFixed(5)}° · {fieldSize}′ × {fieldSize}′{patchRelease && ` · ${patchRelease.toUpperCase()}`}</p>
            {(patch.startDate || patch.endDate) && <p>
              UTC dates {patch.startDate ?? 'any start'} through {patch.endDate ?? 'any end'}
            </p>}
            {(patch.minWavelength !== null || patch.maxWavelength !== null) && <p>
              Archive wavelength {patch.minWavelength !== null && `≥ ${patch.minWavelength} µm`}
              {patch.minWavelength !== null && patch.maxWavelength !== null && ' and '}
              {patch.maxWavelength !== null && `≤ ${patch.maxWavelength} µm`}
            </p>}
          </div>
          <button className="control" type="button" onClick={onClose}>Close</button>
        </header>

        {listingError ? (
          <div className="manual-actions">
            <p role="alert">{listingError}</p>
            <button className="control" type="button" onClick={() => {
              setListingError(''); setListing(null); setBands({}); setAttempt((current) => current + 1);
            }}>Retry</button>
          </div>
        ) : !inventoryId && <p role="status">Searching the SPHEREx archive…</p>}

        {inventoryId && (
          <>
            <fieldset className="manual-filters" disabled={Boolean(status)} onChange={() => changePage(0)}>
              {!patchRelease && <label className="field">Release
                <select value={release} onChange={(event) => setRelease(event.target.value)}>
                  <option value="">All releases</option>
                  {[...new Set(exposures.map((item) => item.release))].sort().map((name) => <option key={name}>{name}</option>)}
                </select>
              </label>}
              <label className="field">Detector
                <select value={detector} onChange={(event) => setDetector(event.target.value)}>
                  <option value="">All detectors</option>
                  {[...new Set(exposures.map((item) => item.detector))].sort().map((number) => (
                    <option key={number} value={number}>D{number}</option>
                  ))}
                </select>
              </label>
              <label className="field">From (UTC)
                <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
              </label>
              <label className="field">Through (UTC)
                <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
              </label>
              <label className="field">Wavelength (µm)
                <input type="number" min="0.75" max="5" step="any" value={wavelength}
                  onChange={(event) => setWavelength(event.target.value)} />
              </label>
            </fieldset>
            {listing?.partial && <p role="status">{listing.message}</p>}
            {wavelength && <p>Uses archive wavelength ranges for exposures whose center bandpass is unknown.</p>}
            <p>{visible.length ? `${visible.length} exposures` : exposures.length
              ? 'No exposures match these filters.' : 'No exposures were listed for this patch.'}</p>
            <div className="manual-observations">
              {pageExposures.map((item) => (
                <div className="manual-observation" key={item.key} data-revealed={listJump?.key === item.key}>
                  <label>
                    <input type="checkbox" checked={selectedSources.has(item.sourceUrl)} onChange={() => toggle(item)}
                      ref={listJump?.key === item.key ? revealedExposure : undefined}
                      disabled={Boolean(status) || (selected.length >= maxManualFrames &&
                        !selected.some((selectedItem) => exposureIdentity(selectedItem) === exposureIdentity(item)))} />
                    <span>
                      <span className="manual-exposure-title">
                        <strong>{observationTime(item)} · {item.release} · D{item.detector}</strong>
                        {autoSelectedSources?.has(item.sourceUrl) && <small className="auto-selected">Auto-selected</small>}
                      </span>
                      <span>Visit {item.visit} · {bandLabel(item, bands[item.key])}</span>
                      <span>{item.observationId}</span>
                      {exposureIssues[item.key] && <strong className="manual-issue">{exposureIssueLabels[exposureIssues[item.key]]}</strong>}
                    </span>
                  </label>
                  {previewButton(item)}
                </div>
              ))}
            </div>
            {pageCount > 1 && (
              <div className="manual-actions" aria-label="Exposure pages">
                <button className="control" type="button" disabled={currentPage === 0}
                  onClick={() => changePage(currentPage - 1)}>Previous page</button>
                <span>{currentPage + 1} / {pageCount}</span>
                <button className="control" type="button" disabled={currentPage + 1 === pageCount}
                  onClick={() => changePage(currentPage + 1)}>Next page</button>
              </div>
            )}
          </>
        )}

        <section className="manual-selection" aria-label="Selected exposures">
          <h3>Selected exposures ({selected.length})</h3>
          {selected.length < 2 && <p>Choose at least two exposures.</p>}
          {selected.length === maxManualFrames && <p>Up to {maxManualFrames} exposures per comparison.</p>}
          {selected.map((item) => {
            const listedKey = inventoryKeys.get(item.sourceUrl);
            const key = listedKey ?? item.key;
            const issue = exposureIssues[key];
            return (
              <div className="manual-actions" key={item.sourceUrl}>
                <div>
                  <span className="manual-exposure-title">
                    <span>{observationTime(item)} · {item.release} · D{item.detector}</span>
                    {autoSelectedSources?.has(item.sourceUrl) && <small className="auto-selected">Auto-selected</small>}
                  </span>
                  {bandLabel(item, bands[key])}
                  {issue && <><br /><strong className="manual-issue">{exposureIssueLabels[issue]}</strong></>}
                </div>
                <div className="manual-row-buttons">
                  <button className="control" type="button" disabled={!inventoryId || listedKey === undefined}
                    onClick={() => showInList(key)}
                    aria-label={`Show in list: ${item.release} detector ${item.detector} exposure at ${observationTime(item)}`}>
                    Show in list
                  </button>
                  {previewButton(item)}
                  <button className="control" type="button" disabled={Boolean(status)} onClick={() => toggle(item)}
                    aria-label={`Remove ${item.release} detector ${item.detector} exposure at ${observationTime(item)}`}>Remove</button>
                </div>
              </div>
            );
          })}
        </section>
        {unavailable && <p role="alert">Remove exposures that are missing from this archive listing, or reopen the picker.</p>}
        {failure && <p className="manual-issue" role="alert">{failure}</p>}
        {status && <p role="status">{status}</p>}
        {exploratory && (
          <div className="manual-playback-notice" role="status">
            <strong>This selection doesn’t qualify for Matched playback.</strong>
            <p>{exploratory.message}</p>
            <p>You can still view these exposures in Exploratory blink.</p>
          </div>
        )}
        <div className="manual-actions">
          <button className={exploratory ? 'control' : 'control primary'} type="submit" disabled={!inventoryId || selected.length < 2 || Boolean(status) || Boolean(unavailable)}>Apply selection</button>
          {exploratory && <button className="control primary" type="button" onClick={() => onApply(exploratory)}>Open Exploratory blink</button>}
        </div>
      </form>
      {preview && activePreview && (
        <ExposurePreview exposure={preview} source={activePreview} onClose={() => setPreview(null)} />
      )}
    </dialog>
  );
}
