import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent } from 'react';
import { formatPosition, type SkyPosition } from './coordinates';
import { watchSpectrum, type ArchiveRelease, type Exposure, type SpectrumPoint, type SpectrumUpdate } from './exposure';

type DateRange = { startDate: string; endDate: string };
const dateOf = (timestamp: string) => timestamp.slice(0, 10);

export function spectrumDates(exposures: Exposure[]): DateRange {
  const days = exposures.flatMap(({ visitDates, observedAt }) => visitDates ?? [dateOf(observedAt)]).sort();
  return { startDate: days[0], endDate: days[days.length - 1] };
}

function numberLabel(value: number) {
  return Number(value.toPrecision(4)).toString();
}

const colors = ['#7de0ef', '#ffbd82', '#d1b0ff', '#a5e69b', '#fca8c8', '#fff296'];

function SpectrumChart({ points, active, onSelect }: {
  points: SpectrumPoint[];
  active: string;
  onSelect: (key: string) => void;
}) {
  const titleId = useId();
  const [first, last] = [Math.min(...points.map((point) => point.wavelength)),
    Math.max(...points.map((point) => point.wavelength))];
  const wavelengthSpread = Math.max(last - first, .04);
  const wavelengthMin = (last + first - wavelengthSpread * 1.08) / 2;
  const wavelengthMax = wavelengthMin + wavelengthSpread * 1.08;
  const low = Math.min(...points.map((point) => point.flux - point.uncertainty));
  const high = Math.max(...points.map((point) => point.flux + point.uncertainty));
  const fluxSpread = Math.max(high - low, .01);
  const fluxMin = (high + low - fluxSpread * 1.12) / 2;
  const fluxMax = fluxMin + fluxSpread * 1.12;
  const x = (wave: number) => 67 + 241 * (wave - wavelengthMin) / (wavelengthMax - wavelengthMin);
  const y = (flux: number) => 155 - 139 * (flux - fluxMin) / (fluxMax - fluxMin);
  return (
    <svg className="spectrum-chart" viewBox="0 0 320 190" role="img" aria-labelledby={titleId}>
      <title id={titleId}>Spectrum, with flux density by wavelength and uncertainty bars</title>
      {[fluxMin, (fluxMin + fluxMax) / 2, fluxMax].map((tick) => (
        <g key={tick}>
          <line x1="67" x2="308" y1={y(tick)} y2={y(tick)} className="spectrum-grid" />
          <text x="63" y={y(tick) + 3} textAnchor="end">{numberLabel(tick)}</text>
        </g>
      ))}
      <path d="M67 16v139h241" className="spectrum-axis" />
      {[wavelengthMin, (wavelengthMin + wavelengthMax) / 2, wavelengthMax].map((tick, index) => (
        <text key={tick} x={x(tick)} y="169" textAnchor={index === 0 ? 'start' : index === 2 ? 'end' : 'middle'}>
          {tick.toFixed(3)}
        </text>
      ))}
      <text x="67" y="11">Flux density (µJy)</text>
      <text x="188" y="185" textAnchor="middle">Wavelength (µm)</text>
      {points.map((point) => {
        const cx = x(point.wavelength);
        const cy = y(point.flux);
        const color = colors[(point.visit - 1) % colors.length];
        const selected = active === point.key;
        return (
          <g className="spectrum-chart-marker" key={point.key} opacity={selected ? 1 : .8}
            onClick={() => onSelect(point.key)}>
            <title>{`Visit ${point.visit} · ${point.wavelength.toFixed(4)} µm · ${point.observedAt}`}</title>
            <line x1={cx} x2={cx} y1={y(point.flux - point.uncertainty)}
              y2={y(point.flux + point.uncertainty)} stroke={color} />
            <circle cx={cx} cy={cy} r={selected ? 4 : 2.4} fill={color} stroke={selected ? '#fff' : 'none'} />
            <circle className="spectrum-chart-target" cx={cx} cy={cy} r="7" fill="transparent" />
          </g>
        );
      })}
    </svg>
  );
}

function SpectrumMeasurement({ points, point, onSelect }: {
  points: SpectrumPoint[];
  point: SpectrumPoint;
  onSelect: (key: string) => void;
}) {
  return (
    <>
      <label className="field spectrum-measurement">
        Measurement
        <select aria-label="Measurement" value={point.key} onChange={(event) => onSelect(event.currentTarget.value)}>
          {points.map((item) => (
            <option key={item.key} value={item.key}>Visit {item.visit} · {item.wavelength.toFixed(4)} µm · D{item.detector}</option>
          ))}
        </select>
      </label>
      <div className="spectrum-point">
        <dl>
          <dt>Flux density</dt><dd>{numberLabel(point.flux)} ± {numberLabel(point.uncertainty)} µJy</dd>
          <dt>Bandwidth</dt><dd>{point.bandwidth.toFixed(4)} µm</dd>
          <dt>Observed</dt><dd><time dateTime={point.observedAt}>{point.observedAt.replace('T', ' ').replace('Z', '')} UTC</time></dd>
          <dt>Release</dt><dd>{point.release}</dd>
          {point.flags.length > 0 && <><dt>Quality flags</dt><dd>{point.flags.join(', ')}</dd></>}
        </dl>
        <a href={point.sourceUrl} target="_blank" rel="noreferrer">Source FITS · {point.observationId}</a>
      </div>
    </>
  );
}

export function Spectrum({ position, initialDates, release, visible, onClear }: {
  position: SkyPosition;
  initialDates: DateRange;
  release: ArchiveRelease | null;
  visible: boolean;
  onClear: () => void;
}) {
  const [dates, setDates] = useState(initialDates);
  const [submitted, setSubmitted] = useState<DateRange | null>(null);
  const [result, setResult] = useState<SpectrumUpdate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const measurementDetails = useRef<HTMLDetailsElement>(null);
  const sourceDetails = useRef<HTMLDetailsElement>(null);
  const panel = useRef<HTMLElement>(null);
  const scrolledToResults = useRef(false);
  const titleId = useId();
  const positionLabel = formatPosition(position);

  useEffect(() => () => request.current?.abort(), []);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!dates.startDate || !dates.endDate || dates.endDate < dates.startDate) {
      setError('Enter a start and end date with the start on or before the end.');
      return;
    }
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError('');
    setResult(null);
    setSelectedKey(null);
    setSubmitted(dates);
    void watchSpectrum({ ...position, ...dates, release }, controller.signal, (update) => {
      setResult(update);
      const firstPoint = update.points[0];
      if (firstPoint) setSelectedKey((key) => key ?? firstPoint.key);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) {
        const message = failure instanceof Error && !(failure instanceof TypeError) ? failure.message :
          'The spectrum service could not complete the request. Try again.';
        setResult((previous) => ({ processed: 0, total: null, points: [], ...previous,
          status: previous?.points.length ? 'partial' : 'error', message, reconnecting: false }));
      }
    }).finally(() => {
      if (request.current === controller) {
        setBusy(false);
        request.current = null;
      }
    });
  }

  function cancel() {
    request.current?.abort();
    request.current = null;
    setBusy(false);
    setResult(null);
    setSubmitted(null);
    setError('');
    if (sourceDetails.current) sourceDetails.current.open = true;
  }

  const edited = submitted && (dates.startDate !== submitted.startDate || dates.endDate !== submitted.endDate);
  const points = result?.points ?? [];
  const selected = points.find((point) => point.key === selectedKey) ?? points[0];
  const hasPoints = Boolean(selected);
  const retry = !edited && (result?.status === 'error' || result?.status === 'timeout' || result?.status === 'partial');
  const scanning = busy && (!result || result.status === 'searching');
  const progress = result?.total == null ? '' : `${result.processed} / ${result.total} exposures checked.`;
  const status = result?.reconnecting ? 'Reconnecting to the spectrum request…' :
    scanning ? (hasPoints ? '' : result?.message || 'Searching the SPHEREx archive…') : (hasPoints ? '' : result?.message);
  const sparse = points.length > 0 && Math.max(...points.map((p) => p.wavelength)) -
    Math.min(...points.map((p) => p.wavelength)) < Math.min(...points.map((p) => p.bandwidth));

  useLayoutEffect(() => {
    if (!hasPoints) {
      scrolledToResults.current = false;
    } else if (visible && !scrolledToResults.current) {
      if (!sourceDetails.current?.contains(document.activeElement)) {
        if (sourceDetails.current) sourceDetails.current.open = false;
        panel.current?.scrollTo({ top: 0 });
      }
      scrolledToResults.current = true;
    }
  }, [hasPoints, visible]);

  return (
    <section className="spectrum-panel" ref={panel} aria-labelledby={titleId}>
      <div className="spectrum-heading">
        <h2 id={titleId}>Spectrum</h2>
        <div className="spectrum-actions">
          {busy && <button className="control" type="button" onClick={cancel}>Cancel spectrum</button>}
          <button className="control spectrum-clear" type="button" onClick={onClear}>Clear source</button>
        </div>
      </div>
      {!hasPoints && submitted && <p className="spectrum-window">Requested {submitted.startDate} to {submitted.endDate} UTC</p>}
      {(hasPoints || scanning || status) && (
        <p role="status" className="spectrum-caption">
          {result?.status === 'partial' && <strong>Partial · </strong>}
          {hasPoints && <span>{points.length} {points.length === 1 ? 'measurement' : 'measurements'}</span>}
          {hasPoints && scanning && (status || progress) && ' · '}
          {status}{status && scanning && progress && ' '}{scanning && progress}
        </p>
      )}
      {selected && (
        <div className="spectrum-results">
          <div className="spectrum-chart-preview">
            <button type="button" className="spectrum-chart-expand" aria-label="Expand spectrum" aria-haspopup="dialog"
              onClick={(event) => {
                event.currentTarget.focus({ preventScroll: true });
                dialog.current?.showModal();
              }} />
            <SpectrumChart points={points} active={selected.key} onSelect={(key) => {
              setSelectedKey(key);
              if (measurementDetails.current) measurementDetails.current.open = true;
            }} />
          </div>
          {!scanning && result?.message && <p className="lookup-status">{result.message}</p>}
          {sparse && <p className="lookup-status">Limited wavelength coverage.</p>}
          {submitted && <p className="spectrum-window">Requested {submitted.startDate} to {submitted.endDate} UTC</p>}
          <details className="spectrum-disclosure" ref={measurementDetails}>
            <summary>Measurement details</summary>
            <p className="spectrum-caption">Aperture photometry</p>
            <SpectrumMeasurement points={points} point={selected} onSelect={setSelectedKey} />
          </details>
          <dialog className="spectrum-dialog" ref={dialog} aria-label="Expanded spectrum"
            onKeyDown={(event) => event.stopPropagation()}>
            <div className="spectrum-heading">
              <h2>Spectrum</h2>
              <form method="dialog"><button className="control" type="submit">Close</button></form>
            </div>
            <SpectrumChart points={points} active={selected.key} onSelect={setSelectedKey} />
            <SpectrumMeasurement points={points} point={selected} onSelect={setSelectedKey} />
          </dialog>
        </div>
      )}
      <details className="spectrum-disclosure" ref={sourceDetails} open>
        <summary>Source and dates</summary>
        <p className="spectrum-position" aria-live="polite">RA {positionLabel.ra}° · Dec {positionLabel.dec}°</p>
        <form onSubmit={submit}>
          <fieldset className="spectrum-dates">
            <legend>Observation dates (UTC)</legend>
            <label className="field">
              From
              <input required type="date" value={dates.startDate}
                onChange={(event) => setDates({ ...dates, startDate: event.currentTarget.value })} />
            </label>
            <label className="field">
              Through
              <input required type="date" value={dates.endDate}
                onChange={(event) => setDates({ ...dates, endDate: event.currentTarget.value })} />
            </label>
          </fieldset>
          <button type="submit" className="control" disabled={busy && !edited}>
            {retry ? 'Retry spectrum' : 'Show spectrum'}
          </button>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
      </details>
    </section>
  );
}
